import asyncio
import socket
import struct
import time
import unittest
import urllib.parse
from types import SimpleNamespace

from streamctl.rtspbuffer import BufferFull, ByteQueue, Message, Session, Timing, read_message
from streamctl.relayauth import reader_request
from streamctl.config import RTSP_BUFFER_DEFAULTS, buffer_limits


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
    def test_recovery_limits_are_bounded_and_validate_numeric_values(self):
        self.assertEqual(buffer_limits({}), RTSP_BUFFER_DEFAULTS)
        for overrides in ({'rtsp_max_buffer_bytes': True}, {'rtsp_max_buffer_bytes': 17 * 1024 * 1024},
                          {'rtsp_write_timeout_seconds': float('nan')},
                          {'rtsp_write_timeout_seconds': float('inf')},
                          {'rtsp_write_max_wait_seconds': 9}, {'rtsp_write_max_wait_seconds': 61},
                          {'rtsp_write_timeout_seconds': True}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                buffer_limits(overrides)

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


class BlockedWriter(Writer):
    def __init__(self):
        super().__init__()
        self.transport = self
        self.pending = 0
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = False

    def write(self, data):
        super().write(data)
        self.pending += len(data)

    def get_write_buffer_size(self):
        return self.pending

    async def drain(self):
        self.entered.set()
        try:
            await self.release.wait()
            self.pending = 0
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tcp_reader_pause_resumes_on_same_connection_with_identical_rtp(self):
        packets = [frame(0, rtp(90000, seq=i, payload=b'\x7c\x85' + b'x' * 12000))
                   for i in range(240)]
        config = configuration(max_buffer_bytes=8 * 1024 * 1024, write_timeout=10, write_max_wait=30)
        sessions, tasks, events = [], [], []
        connected = asyncio.Event()
        async def accept(reader, writer):
            writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            writer.transport.set_write_buffer_limits(high=8192, low=4096)
            session = Session(config, reader, writer, lambda **event: events.append(event))
            session.rtp_channels = {0}
            session.timing.rates = {96: 90000}
            for packet in packets:
                deadline, clock, elapsed = session.timing.rtp(0, packet[4:], time.monotonic() - 1)
                session.queue.put(Message(packet, 0, deadline, clock, elapsed))
            sessions.append(session)
            tasks.append(asyncio.create_task(session.send_media()))
            connected.set()
        server = await asyncio.start_server(accept, '127.0.0.1', 0)
        writer = None
        try:
            client_socket = socket.socket()
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
            client_socket.setblocking(False)
            await asyncio.get_running_loop().sock_connect(client_socket, server.sockets[0].getsockname())
            reader, writer = await asyncio.open_connection(sock=client_socket, limit=16384)
            writer.transport.pause_reading()
            await asyncio.wait_for(connected.wait(), 1)
            await asyncio.sleep(3.1)
            self.assertFalse(tasks[0].done())
            self.assertIsNotNone(sessions[0].write_started)
            self.assertIn('write_blocked', [event['event'] for event in events])
            writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            sessions[0].writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            sessions[0].writer.transport.set_write_buffer_limits(high=65536, low=16384)
            writer.transport.resume_reading()
            expected = b''.join(packets)
            received = await asyncio.wait_for(reader.readexactly(len(expected)), 10)
            self.assertEqual(received, expected)
            async def writes_finished():
                while sessions[0].sent_packets != len(packets):
                    if tasks[0].done():
                        tasks[0].result()
                    await asyncio.sleep(.01)
            await asyncio.wait_for(writes_finished(), 1)
            self.assertEqual(sessions[0].sent_packets, len(packets))
            self.assertGreater(sessions[0].max_drain, 2)
            self.assertEqual(sessions[0].write_timeouts, 0)
        finally:
            server.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if writer is not None:
                writer.transport.abort()
                await asyncio.wait_for(writer.wait_closed(), 1)
            for session in sessions:
                session.writer.transport.abort()
                await asyncio.wait_for(session.writer.wait_closed(), 1)
            await asyncio.wait_for(server.wait_closed(), 1)

    async def test_more_than_two_second_stall_recovers_without_packet_loss_or_affecting_other_reader(self):
        limits = buffer_limits({})
        config = configuration(max_buffer_bytes=limits['rtsp_max_buffer_bytes'],
                               write_timeout=limits['rtsp_write_timeout_seconds'],
                               write_max_wait=limits['rtsp_write_max_wait_seconds'])
        slow_writer, fast_writer = BlockedWriter(), Writer()
        events = []
        slow = Session(config, None, slow_writer, lambda **event: events.append(event))
        fast = Session(config, None, fast_writer, lambda **event: None)
        packets = [frame(0, rtp(90000, seq=i, payload=b'\x7c\x85' + b'x' * 12000))
                   for i in range(240)]
        for session in (slow, fast):
            session.rtp_channels = {0}
            session.timing.rates = {96: 90000}
            # Already paced fragments of one access unit, all with one timestamp.
            for packet in packets:
                deadline, clock, elapsed = session.timing.rtp(0, packet[4:], time.monotonic() - 1)
                session.queue.put(Message(packet, 0, deadline, clock, elapsed))
        tasks = [asyncio.create_task(session.send_media()) for session in (slow, fast)]
        try:
            await asyncio.wait_for(slow_writer.entered.wait(), 1)
            await asyncio.sleep(2.2)
            self.assertFalse(tasks[0].done())
            self.assertGreater(slow.queue.bytes, 2 * 1024 * 1024)
            self.assertEqual(fast.sent_packets, len(packets))
            self.assertEqual(fast_writer.writes, packets)
            slow_writer.release.set()
            async def recovered():
                while slow.sent_packets != len(packets):
                    await asyncio.sleep(.01)
            await asyncio.wait_for(recovered(), 2)
            self.assertEqual(slow_writer.writes, packets)
            self.assertEqual(slow.queue.bytes, 0)
            self.assertGreater(slow.max_drain, 2)
            self.assertEqual(slow.write_timeouts, 0)
            self.assertEqual(slow.write_backpressure_events, 1)
            self.assertIn('write_recovered', [event['event'] for event in events])
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_transport_progress_extends_idle_deadline(self):
        writer = BlockedWriter()
        events = []
        session = Session(configuration(write_timeout=.12, write_max_wait=.7), None, writer,
                          lambda **event: events.append(event))
        task = asyncio.create_task(session.write(b'x' * 1000))
        try:
            await writer.entered.wait()
            for _ in range(5):
                await asyncio.sleep(.05)
                writer.pending -= 100
            self.assertFalse(task.done())
            writer.release.set()
            await asyncio.wait_for(task, .5)
            self.assertEqual(writer.writes, [b'x' * 1000])
            self.assertEqual(session.write_timeouts, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_progress_closes_with_diagnostic_and_cancels_drain(self):
        writer = BlockedWriter()
        events = []
        session = Session(configuration(write_timeout=.08, write_max_wait=.5), None, writer,
                          lambda **event: events.append(event))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(session.write(b'pending'), 1)
        self.assertTrue(writer.cancelled)
        self.assertIsNone(session.write_started)
        self.assertGreaterEqual(session.max_drain, .08)
        timeout = next(event for event in events if event['event'] == 'write_timeout')
        self.assertEqual(timeout['timeout_kind'], 'no_progress')
        self.assertEqual(timeout['direction'], 'reader')
        self.assertEqual(timeout['pending_write_bytes'], 7)

    async def test_trickling_progress_still_has_total_wait_limit(self):
        writer = BlockedWriter()
        events = []
        session = Session(configuration(write_timeout=.12, write_max_wait=.3), None, writer,
                          lambda **event: events.append(event))
        async def trickle():
            await writer.entered.wait()
            while True:
                await asyncio.sleep(.03)
                writer.pending -= 1
        progress = asyncio.create_task(trickle())
        try:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(session.write(b'x' * 1000), 1)
            timeout = next(event for event in events if event['event'] == 'write_timeout')
            self.assertEqual(timeout['timeout_kind'], 'total_wait')
            self.assertLess(timeout['stalled_ms'], 120)
            self.assertTrue(writer.cancelled)
        finally:
            progress.cancel()
            await asyncio.gather(progress, return_exceptions=True)

    async def test_cancellation_and_control_writes_do_not_duplicate_or_interleave_frames(self):
        writer = BlockedWriter()
        session = Session(configuration(), None, writer, lambda **event: None)
        media, control = frame(0, rtp(90000)), response(9)
        first = asyncio.create_task(session.write(media))
        second = None
        try:
            await writer.entered.wait()
            second = asyncio.create_task(session.write(control))
            await asyncio.sleep(.02)
            self.assertEqual(writer.writes, [media])
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            writer.release.set()
            await asyncio.wait_for(second, .5)
            self.assertTrue(writer.cancelled)
            self.assertEqual(writer.writes, [media, control])
        finally:
            first.cancel()
            if second is not None:
                second.cancel()
            await asyncio.gather(*[t for t in (first, second) if t is not None], return_exceptions=True)

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
