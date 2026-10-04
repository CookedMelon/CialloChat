import asyncio
import struct
import unittest
import urllib.parse
from types import SimpleNamespace

from streamctl.rtspbuffer import BufferFull, ByteQueue, Message, Session, Timing, read_message
from streamctl.relayauth import reader_request


def configuration(**overrides):
    return SimpleNamespace(**dict(dict(buffer_ms=150, max_buffer_bytes=10000, write_timeout=.5,
        public_hosts={'127.0.0.1'}, listen_port=8558, upstream_host='127.0.0.1',
        upstream_port=18554, proxy_secret=b's'*32), **overrides))


def rtp(stamp, seq=1, ssrc=123, payload=b'\x65encoded-video', pt=96):
    return struct.pack('!BBHII', 0x80, 0x80 | pt, seq, stamp, ssrc) + payload


def frame(channel, payload):
    return b'$' + bytes([channel]) + len(payload).to_bytes(2, 'big') + payload


def response(cseq, headers='', body=b''):
    return (f'RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n' + headers +
            f'Content-Length: {len(body)}\r\n\r\n').encode() + body


class ClockTests(unittest.TestCase):
    def test_tracks_with_same_payload_number_keep_their_own_clock_rates(self):
        timing = Timing(1)
        timing.sdp(b'v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n'
                   b'a=control:trackID=0\r\nm=audio 0 RTP/AVP 96\r\n'
                   b'a=rtpmap:96 MPEG4-GENERIC/48000/2\r\na=control:trackID=1\r\n')
        timing.bind(4, 'rtsp://server/live/lan/trackID=0')
        timing.bind(6, 'rtsp://server/live/lan/trackID=1')
        video_start, video, _ = timing.rtp(4, rtp(90000), 10)
        audio_start, audio, _ = timing.rtp(6, rtp(48000, ssrc=456), 10)
        video_next, _, _ = timing.rtp(4, rtp(91500, seq=2), 10.001)
        audio_next, _, _ = timing.rtp(6, rtp(49024, seq=2, ssrc=456), 10.001)
        self.assertEqual(video.rate, 90000)
        self.assertEqual(audio.rate, 48000)
        self.assertAlmostEqual(video_next - video_start, 1 / 60)
        self.assertAlmostEqual(audio_next - audio_start, 1024 / 48000)

    def test_buffer_margin_survives_bursts_and_detects_underflow(self):
        timing = Timing(1)
        timing.sdp(b'a=rtpmap:96 H264/90000\r\na=rtpmap:97 MPEG4-GENERIC/48000\r\n')
        first, clock, _ = timing.rtp(4, rtp(90000), 100)
        second, _, _ = timing.rtp(4, rtp(91500, seq=2), 100.001)
        third, _, _ = timing.rtp(4, rtp(93000, seq=3), 101.2)
        self.assertAlmostEqual(first, 101)
        self.assertAlmostEqual(second - first, 1 / 60)
        self.assertAlmostEqual(third - first, 2 / 60)
        self.assertLess(clock.min_arrival_margin, 0)
        # Pacing never changes source RTP bytes, sequence or timestamp.
        payload = rtp(93000, seq=3)
        timing.rtp(4, payload, 101.2)
        self.assertEqual(payload, rtp(93000, seq=3))

    def test_timestamp_wrap_and_each_ssrc_have_distinct_clocks(self):
        timing = Timing(.75)
        timing.sdp(b'a=rtpmap:96 H264/90000\r\n')
        first, _, _ = timing.rtp(0, rtp(2**32 - 750), 10)
        next_deadline, _, _ = timing.rtp(0, rtp(750, seq=2), 10.001)
        self.assertAlmostEqual(next_deadline - first, 1 / 60)
        replacement, _, _ = timing.rtp(0, rtp(42, ssrc=456), 11)
        self.assertEqual(replacement, 11.75)

    def test_source_restart_cannot_create_hours_of_delay(self):
        timing = Timing(1)
        timing.sdp(b'a=rtpmap:96 H264/90000\r\n')
        timing.rtp(0, rtp(900000), 10)
        with self.assertRaises(ValueError):
            timing.rtp(0, rtp(0), 11)

    def test_rtcp_fraction_carry_and_feedback_echo_are_reversible(self):
        for delay in (1, .3, .75):
            timing = Timing(delay)
            old_ntp = (100 << 32) + int(.9 * 2**32)
            sr = struct.pack('!BBHIQIII', 0x80, 200, 6, 123, old_ntp, 90000, 12, 345)
            shifted = timing.rtcp(sr)
            new_ntp = int.from_bytes(shifted[8:16], 'big')
            self.assertEqual(new_ntp, old_ntp + round(delay * 2**32))
            self.assertEqual(shifted[:8], sr[:8])
            self.assertEqual(shifted[16:], sr[16:])
            block = struct.pack('!IIIIII', 123, 0, 12, 9, (new_ntp >> 16) & 0xffffffff, 1000)
            rr = struct.pack('!BBHI', 0x81, 201, 7, 999) + block
            restored = timing.rtcp(rr, upstream=True)
            self.assertEqual(int.from_bytes(restored[24:28], 'big'), (old_ntp >> 16) & 0xffffffff)
            self.assertEqual(restored[:24], rr[:24])
            self.assertEqual(restored[28:], rr[28:])


class Writer:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        pass

    def get_extra_info(self, key):
        return ('192.168.1.2', 12345)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_udp_rejection_allows_tcp_retry_on_same_connection(self):
        writer = Writer()
        session = Session(configuration(), asyncio.StreamReader(), writer, lambda **event: None)
        session.map_url('rtsp://127.0.0.1:8558/live/lan?read_key=private', True)
        session.pending_setup['2'] = 'rtsp://127.0.0.1:8558/live/lan/trackID=0'
        reader = asyncio.StreamReader()
        sdp = b'v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\n'
        reader.feed_data(response(1, 'Content-Type: application/sdp\r\n', sdp))
        reader.feed_data(b'RTSP/1.0 461 Unsupported Transport\r\nCSeq: 2\r\n\r\n')
        task = asyncio.create_task(session.ingest(reader))
        try:
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.assertIn(b'461 Unsupported Transport', b''.join(writer.writes))
            session.pending_setup['3'] = 'rtsp://127.0.0.1:8558/live/lan/trackID=0'
            reader.feed_data(response(3, 'Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n'))
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.assertEqual(session.rtp_channels, {0})
            self.assertEqual(session.timing.channel_rates[0], {96:90000})
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_queue_limit_is_enforced_without_partial_packet_loss(self):
        queue = ByteQueue(8)
        queue.put(Message(b'1234'))
        queue.put(Message(b'5678'))
        with self.assertRaises(BufferFull):
            queue.put(Message(b'9'))
        self.assertEqual((await queue.get()).wire, b'1234')
        self.assertEqual((await queue.get()).wire, b'5678')
        self.assertEqual(queue.bytes, 0)

    async def test_fragmented_interleaved_frame_is_not_a_control_message(self):
        reader = asyncio.StreamReader()
        payload = rtp(90000)
        task = asyncio.create_task(read_message(reader))
        data = frame(4, payload)
        reader.feed_data(data[:3])
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        reader.feed_data(data[3:])
        wire, channel, lines, body = await task
        self.assertEqual(wire, data)
        self.assertEqual(channel, 4)
        self.assertIsNone(lines)

    async def test_ingest_prefills_and_keepalive_bypasses_media_wait(self):
        config = configuration()
        writer = Writer()
        session = Session(config, asyncio.StreamReader(), writer, lambda **event: None)
        session.map_url('rtsp://127.0.0.1:8558/live/lan?read_key=private', True)
        session.pending_setup['2'] = 'rtsp://127.0.0.1:8558/live/lan/trackID=0'
        session.pending_play.add('3')
        reader = asyncio.StreamReader()
        sdp = b'v=0\r\na=rtpmap:96 H264/90000\r\n'
        reader.feed_data(response(1, 'Content-Type: application/sdp\r\nContent-Base: rtsp://127.0.0.1:8554/live/lan?read_key=private/\r\n', sdp))
        reader.feed_data(response(2, 'Transport: RTP/AVP/TCP;unicast;interleaved=4-5\r\n'))
        reader.feed_data(response(3, 'RTP-Info: url=rtsp://127.0.0.1:8554/live/lan/trackID=0\r\n'))
        original = [frame(4, rtp(90000 + i * 1500, seq=i + 1)) for i in range(3)]
        for packet in original:
            reader.feed_data(packet)
        reader.feed_data(response(4))
        tasks = [asyncio.create_task(session.ingest(reader)), asyncio.create_task(session.send_media())]
        try:
            await asyncio.sleep(.04)
            output = b''.join(writer.writes)
            self.assertIn(b'CSeq: 4\r\n', output)
            self.assertNotIn(b'CSeq: 3\r\n', output)
            self.assertGreater(session.queue.bytes, 0)
            self.assertIn(b'Content-Base: rtsp://127.0.0.1:8558/live/lan/\r\n', output)
            self.assertNotIn(b'private', output)
            await asyncio.sleep(.22)
            self.assertEqual([wire for wire in writer.writes if wire.startswith(b'$')], original)
            self.assertEqual(session.sent_packets, 3)
            self.assertEqual(session.queue.bytes, 0)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_untrusted_bodies_are_rejected_before_reading_the_payload(self):
        for wire, options in [(b'$\x00\xff\xff', dict(allow_media=False)),
                              (b'$\x01\x20\x00', dict(max_frame=4096)),
                              (b'DESCRIBE rtsp://host/live/user RTSP/1.0\r\nContent-Length: 999999\r\n\r\n',
                               dict(max_body=1024))]:
            reader = asyncio.StreamReader(); reader.feed_data(wire)
            with self.assertRaises(ValueError):
                await asyncio.wait_for(read_message(reader, **options), .1)

    async def test_reader_key_is_retained_without_injecting_a_server_key(self):
        session = Session(configuration(), asyncio.StreamReader(), Writer(), lambda **event: None)
        describe = session.map_url('rtsp://127.0.0.1:8558/live/alice?read_key=alice-key&_cc_peer=evil', True)
        setup = session.map_url('rtsp://127.0.0.1:8558/live/alice/trackID=0', True)
        for url in (describe, setup):
            verified = reader_request(dict(protocol='rtsp', action='read', ip='127.0.0.1',
                path='live/alice', query=urllib.parse.urlsplit(url).query), b's'*32)
            self.assertEqual(verified['ip'], '192.168.1.2')
            self.assertEqual(verified['query'], 'read_key=alice-key')
        with self.assertRaises(ValueError):
            session.map_url('rtsp://127.0.0.1:8558/live/bob', True)
        with self.assertRaises(ValueError):
            session.map_url('rtsp://outside:8558/live/alice', True)
        other = Session(configuration(), asyncio.StreamReader(), Writer(), lambda **event: None)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(other.map_url(
            'rtsp://127.0.0.1:8558/live/bob', True)).query)
        self.assertNotIn('read_key', query)


if __name__ == '__main__':
    unittest.main()
