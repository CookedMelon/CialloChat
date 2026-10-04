"""Authenticated RTSP relay with bounded RTP pacing and per-reader isolation.

Reader credentials are supplied by the client and checked by MediaMTX.
Encoded media, RTP timestamps and sequence numbers are never changed.
"""
import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
import urllib.parse
from collections import OrderedDict
from .relayauth import load_secret, signed_query


class BufferFull(ConnectionError):
    pass


@dataclass
class Message:
    wire: bytes
    channel: int | None = None
    deadline: float | None = None
    clock: object | None = None
    elapsed: float | None = None


class ByteQueue:
    """Fail a slow reader at a hard memory limit; never drop H264 fragments."""
    def __init__(self, limit):
        self.limit = limit
        self.bytes = 0
        self.peak_bytes = 0
        self.items = deque()
        self.ready = asyncio.Event()

    def put(self, message):
        size = len(message.wire)
        if self.bytes + size > self.limit:
            raise BufferFull('reader buffer limit exceeded')
        self.items.append(message)
        self.bytes += size
        self.peak_bytes = max(self.peak_bytes, self.bytes)
        self.ready.set()

    async def get(self):
        while not self.items:
            self.ready.clear()
            await self.ready.wait()
        message = self.items.popleft()
        self.bytes -= len(message.wire)
        return message


class TrackClock:
    def __init__(self, stamp, rate, arrival):
        self.last_stamp = stamp
        self.rate = rate
        self.origin = arrival
        self.elapsed = 0.0
        self.sent_elapsed = None
        self.min_arrival_margin = float('inf')
        self.max_lateness = 0.0
        self.late_packets = 0

    def advance(self, stamp):
        delta = ((stamp - self.last_stamp + 2**31) % 2**32) - 2**31
        # A restarted source must be re-negotiated, not buffered for hours.
        if delta < -self.rate or delta > self.rate * 30:
            raise ValueError('source RTP clock discontinuity')
        self.elapsed += delta / self.rate
        self.last_stamp = stamp
        return self.elapsed


class Timing:
    def __init__(self, delay):
        self.delay = delay
        self.rates = {}
        self.track_rates = []
        self.channel_rates = {}
        self.clocks = {}
        self.lsr_maps = {}

    def sdp(self, body):
        self.track_rates = []
        section = None
        for line in body.decode().splitlines():
            if line.startswith('m='):
                section = {'control': '', 'rates': {}}
                self.track_rates.append(section)
            elif line.startswith('a=control:') and section is not None:
                section['control'] = line[len('a=control:'):]
            elif line.startswith('a=rtpmap:'):
                match = re.match(r'a=rtpmap:(\d+)\s+[^/\s]+/(\d+)', line)
                if match:
                    payload, rate = map(int, match.groups())
                    (section['rates'] if section is not None else self.rates)[payload] = rate

    def bind(self, channel, target):
        # Payload type numbers are scoped to a track/session. MediaMTX uses
        # PT=96 for both H264/90000 and AAC/48000 in this live source.
        identifier = urllib.parse.urlsplit(target).path.rstrip('/').rsplit('/', 1)[-1]
        matches = [item['rates'] for item in self.track_rates
                   if urllib.parse.urlsplit(item['control']).path.rstrip('/').rsplit('/', 1)[-1] == identifier]
        if len(matches) == 1:
            self.channel_rates[channel] = matches[0]
        elif not self.track_rates and self.rates:
            self.channel_rates[channel] = self.rates
        else:
            raise ValueError('SETUP track has no unique SDP clock')

    def rtp(self, channel, payload, arrival):
        if len(payload) < 12 or payload[0] >> 6 != 2:
            raise ValueError('invalid RTP header')
        rate = self.channel_rates.get(channel, self.rates).get(payload[1] & 127)
        if not rate:
            raise ValueError('RTP payload clock missing from SDP')
        stamp = int.from_bytes(payload[4:8], 'big')
        ssrc = int.from_bytes(payload[8:12], 'big')
        key = (channel, ssrc)
        clock = self.clocks.get(key)
        if clock is None:
            clock = self.clocks[key] = TrackClock(stamp, rate, arrival)
        elapsed = clock.advance(stamp)
        deadline = clock.origin + self.delay + elapsed
        clock.min_arrival_margin = min(clock.min_arrival_margin, deadline - arrival)
        return deadline, clock, elapsed

    def rtcp(self, payload, upstream=False):
        """Shift SR wall time; restore echoed LSR so upstream RTT stays valid."""
        data = bytearray(payload)
        offset = 0
        while offset + 4 <= len(data):
            size = (int.from_bytes(data[offset + 2:offset + 4], 'big') + 1) * 4
            if size < 4 or offset + size > len(data) or data[offset] >> 6 != 2:
                raise ValueError('invalid compound RTCP')
            typ = data[offset + 1]
            if not upstream and typ == 200 and size >= 28:
                ssrc = int.from_bytes(data[offset + 4:offset + 8], 'big')
                old_ntp = int.from_bytes(data[offset + 8:offset + 16], 'big')
                new_ntp = (old_ntp + round(self.delay * 2**32)) % 2**64
                data[offset + 8:offset + 16] = new_ntp.to_bytes(8, 'big')
                mapping = self.lsr_maps.setdefault(ssrc, {})
                mapping[(new_ntp >> 16) & 0xffffffff] = (old_ntp >> 16) & 0xffffffff
                if len(mapping) > 128:
                    del mapping[next(iter(mapping))]
            if upstream and typ in (200, 201):
                start = 28 if typ == 200 else 8
                for index in range(data[offset] & 31):
                    block = offset + start + index * 24
                    if block + 24 > offset + size:
                        raise ValueError('truncated RTCP receiver report')
                    ssrc = int.from_bytes(data[block:block + 4], 'big')
                    lsr = int.from_bytes(data[block + 16:block + 20], 'big')
                    original = self.lsr_maps.get(ssrc, {}).get(lsr)
                    if original is not None:
                        data[block + 16:block + 20] = original.to_bytes(4, 'big')
            offset += size
        if offset != len(data):
            raise ValueError('truncated RTCP compound packet')
        return bytes(data)

    def metrics(self):
        return {f'{channel}:{ssrc}': {
            'clock_hz': clock.rate,
            'buffered_media_ms': round(max(0, clock.elapsed - clock.sent_elapsed) * 1000, 2)
            if clock.sent_elapsed is not None else None,
            'min_arrival_margin_ms': round(clock.min_arrival_margin * 1000, 2),
            'max_send_lateness_ms': round(clock.max_lateness * 1000, 2),
            'late_packets_over_50ms': clock.late_packets,
        } for (channel, ssrc), clock in self.clocks.items()}


async def read_message(reader, max_body=65536, max_frame=65535, allow_media=True):
    first = await reader.readexactly(1)
    if first == b'$':
        if not allow_media:
            raise ValueError('reader media before authentication')
        header = await reader.readexactly(3)
        length = int.from_bytes(header[1:], 'big')
        if length > max_frame:
            raise ValueError('reader media frame too large')
        payload = await reader.readexactly(length)
        return first + header + payload, header[0], None, None
    header = first + await reader.readuntil(b'\r\n\r\n')
    if len(header) > 16384:
        raise ValueError('RTSP header too large')
    lines = header.decode('utf-8').split('\r\n')
    lengths = [int(line.split(':', 1)[1]) for line in lines[1:]
               if line.lower().startswith('content-length:')]
    if len(lengths) > 1 or (lengths and not 0 <= lengths[0] <= max_body):
        raise ValueError('invalid RTSP body length')
    body = await reader.readexactly(lengths[0]) if lengths else b''
    return header + body, None, lines, body


class Session:
    def __init__(self, config, reader, writer, logger):
        self.config, self.reader, self.writer, self.log = config, reader, writer, logger
        self.id = str(time.time_ns())
        self.queue = ByteQueue(config.max_buffer_bytes)
        self.timing = Timing(config.buffer_ms / 1000)
        self.write_lock = asyncio.Lock()
        self.rtp_channels = set()
        self.rtcp_channels = set()
        self.play_at = None
        self.pending_play = set()
        self.pending_setup = {}
        self.sent_packets = 0
        self.max_drain = 0.0
        self.tasks = []
        self.upstream = None
        self.route = None
        self.reader_query = {}
        self.public_origin = None
        self.admitted = False
        self.admitted_event = asyncio.Event()
        self.test_grant = None
        self.test_event = asyncio.Event()

    def test_target(self, value):
        u = urllib.parse.urlsplit(value)
        return (u.scheme == 'rtsp' and u.hostname in self.config.public_hosts
                and u.port == self.config.listen_port and not u.username and not u.password
                and re.fullmatch(r'/test(?:/trackID=[01]|/)?', u.path))

    async def prepare_test(self, target, method):
        if self.test_target(target):
            channel = getattr(self.config, 'test_channel', None)
            if channel is None:
                raise ValueError('test channel is disabled')
            if self.route is not None and not self.route.startswith('/test/'):
                raise ValueError('reader cannot switch into the test channel')
            if method == 'DESCRIBE' and self.test_grant is None:
                self.test_grant = await channel.acquire(self.writer.get_extra_info('peername')[0], self.id)
                self.route = '/test/' + self.test_grant.token
                self.test_event.set()

    def map_url(self, value, inbound):
        if value == '*' and inbound:
            return value
        u = urllib.parse.urlsplit(value)
        if inbound:
            if self.test_target(value):
                if getattr(self.config, 'test_channel', None) is None:
                    raise ValueError('test channel is disabled')
                self.public_origin = u.netloc
                if self.test_grant is None:
                    return '*'  # OPTIONS alone creates neither media nor a lease.
                route = '/test/' + self.test_grant.token
                suffix = u.path[len('/test'):]
                query = signed_query({}, self.test_grant.ip, route.lstrip('/'), self.config.proxy_secret)
                return urllib.parse.urlunsplit(u._replace(netloc=f'127.0.0.1:{self.config.upstream_port}',
                    path=route+suffix, query=urllib.parse.urlencode(query, doseq=True)))
            match = re.fullmatch(r'/live/([a-zA-Z0-9][a-zA-Z0-9_-]{0,47})(?:/trackID=[01]|/)?', u.path)
            if (u.scheme != 'rtsp' or u.hostname not in self.config.public_hosts
                    or u.port != self.config.listen_port or not match or u.username or u.password):
                raise ValueError('unexpected request target')
            route = '/live/' + match[1]
            if self.route is not None and route != self.route:
                raise ValueError('reader cannot change account on an existing connection')
            self.route = route
            self.public_origin = u.netloc
            query = urllib.parse.parse_qs(u.query)
            for key in ('read_key', 'user', 'pass'):
                if key in query:
                    if key in self.reader_query and query[key] != self.reader_query[key]:
                        raise ValueError('reader credentials changed within a connection')
                    self.reader_query[key] = query[key]
                elif key in self.reader_query:
                    query[key] = self.reader_query[key]
            query = signed_query(query, self.writer.get_extra_info('peername')[0], route.lstrip('/'),
                                 self.config.proxy_secret)
            host = self.config.upstream_host
            host = '[' + host + ']' if ':' in host else host
            return urllib.parse.urlunsplit(u._replace(netloc=f'{host}:{self.config.upstream_port}',
                query=urllib.parse.urlencode(query, doseq=True)))
        path = u.path
        if self.test_grant and path.startswith(self.route):
            path = '/test' + path[len(self.route):]
        return urllib.parse.urlunsplit(u._replace(netloc=self.public_origin, path=path, query=''))

    async def write(self, wire):
        async with self.write_lock:
            self.writer.write(wire)
            before = time.monotonic()
            await asyncio.wait_for(self.writer.drain(), self.config.write_timeout)
            self.max_drain = max(self.max_drain, time.monotonic() - before)

    async def client_control(self):
        while True:
            wire, channel, lines, body = await read_message(self.reader, max_body=1024,
                max_frame=4096, allow_media=self.admitted)
            if channel is not None:
                if not self.admitted or channel not in self.rtcp_channels:
                    raise ValueError('unexpected reader media channel')
                wire = wire[:4] + self.timing.rtcp(wire[4:], upstream=True)
            else:
                method, target, version = lines[0].split(' ', 2)
                if method not in {'OPTIONS', 'DESCRIBE', 'SETUP', 'PLAY', 'PAUSE', 'TEARDOWN', 'GET_PARAMETER', 'SET_PARAMETER'}:
                    raise ValueError('unsupported reader method')
                if body and method not in {'GET_PARAMETER', 'SET_PARAMETER'}:
                    raise ValueError('unexpected reader body')
                if method not in {'OPTIONS', 'DESCRIBE'} and not self.admitted:
                    raise ValueError('reader must authenticate with DESCRIBE first')
                await self.prepare_test(target, method)
                cseq = next((line.split(':', 1)[1].strip() for line in lines if line.lower().startswith('cseq:')), '')
                if method == 'PLAY':
                    self.pending_play.add(cseq)
                if method == 'SETUP':
                    self.pending_setup[cseq] = target
                    transport = next((line.split(':',1)[1].strip() for line in lines if line.lower().startswith('transport:')), '')
                    self.log(connection=self.id, event='setup_request',
                        transport='tcp' if 'interleaved=' in transport.lower() else 'udp',
                        target_track=urllib.parse.urlsplit(target).path.rsplit('/',1)[-1] if self.test_grant else '<business>')
                lines[0] = f'{method} {self.map_url(target, True)} {version}'
                self.log(connection=self.id, method=method)
                wire = '\r\n'.join(lines).encode() + body
            self.upstream.write(wire)
            await asyncio.wait_for(self.upstream.drain(), self.config.write_timeout)

    async def ingest(self, reader):
        while True:
            wire, channel, lines, body = await read_message(reader)
            arrival = time.monotonic()
            if channel is not None:
                if self.play_at is None:
                    raise ValueError('media before successful PLAY')
                deadline = None
                if channel in self.rtp_channels:
                    deadline, clock, elapsed = self.timing.rtp(channel, wire[4:], arrival)
                    message = Message(wire, channel, deadline, clock, elapsed)
                elif channel in self.rtcp_channels:
                    message = Message(wire, channel)
                else:
                    raise ValueError('unnegotiated interleaved channel')
                self.queue.put(message)
                continue
            status = lines[0].split(' ', 2)[1]
            if not re.fullmatch(r'\d{3}',status): raise ValueError('invalid RTSP status')
            cseq = next((line.split(':', 1)[1].strip() for line in lines if line.lower().startswith('cseq:')), '')
            transport_fallback = cseq in self.pending_setup and status == '461'
            if status == '200' and any(line.lower().startswith('content-type:') and 'application/sdp' in line.lower() for line in lines):
                self.timing.sdp(body)
                self.admitted = True
                self.admitted_event.set()
            if cseq in self.pending_setup:
                setup_target = self.pending_setup.pop(cseq)
                if status == '200':
                    transport = next((line for line in lines if line.lower().startswith('transport:')), '')
                    channels = re.search(r'interleaved=(\d+)-(\d+)', transport)
                    if not channels:
                        self.log(connection=self.id,event='setup_rejected',reason='missing_tcp_channels',status=status)
                        raise ValueError('only RTSP interleaved TCP is supported')
                    rtp, rtcp = map(int, channels.groups())
                    if not 0 <= rtp <= 255 or not 0 <= rtcp <= 255 or rtp == rtcp:
                        raise ValueError('invalid interleaved channels')
                    self.rtp_channels.add(rtp)
                    self.rtcp_channels.add(rtcp)
                    try:
                        self.timing.bind(rtp, setup_target)
                    except ValueError:
                        self.log(connection=self.id,event='setup_rejected',reason='unmatched_sdp_track',status=status)
                        raise
                else:
                    self.log(connection=self.id,event='setup_rejected',reason='upstream_status',status=status)
            for index, line in enumerate(lines):
                if line.lower().startswith(('content-base:', 'content-location:', 'rtp-info:')):
                    lines[index] = re.sub(r'rtsp://[^\s;,]+', lambda m: self.map_url(m.group(0), False), line)
                    if line.lower().startswith('content-base:'):
                        prefix, value = lines[index].split(':', 1)
                        base = urllib.parse.urlsplit(value.strip())
                        lines[index] = prefix + ': ' + urllib.parse.urlunsplit(base._replace(path=base.path.rstrip('/') + '/'))
            wire = '\r\n'.join(lines).encode() + body
            if cseq in self.pending_play:
                self.pending_play.discard(cseq)
                if status == '200':
                    if self.play_at is not None:
                        raise ValueError('repeated PLAY requires a fresh stream connection')
                    self.play_at = arrival
                    self.queue.put(Message(wire, deadline=arrival + self.timing.delay))
                    continue
            # Keepalive replies bypass media pacing and its startup wait.
            await self.write(wire)
            # VLC/AVPro starts with UDP and retries TCP on 461 in this session.
            # Forward the negotiation response without prematurely closing it.
            if status not in {'200', '401'} and not transport_fallback:
                raise ValueError('upstream rejected reader request')
            if status == '401' and self.reader_query:
                raise ValueError('upstream rejected reader credential')

    async def send_media(self):
        while True:
            message = await self.queue.get()
            if message.deadline is not None:
                wait = message.deadline - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            wire = message.wire
            if message.channel in self.rtcp_channels:
                wire = wire[:4] + self.timing.rtcp(wire[4:])
            if message.channel in self.rtp_channels:
                clock = message.clock
                lateness = max(0, time.monotonic() - message.deadline)
                clock.max_lateness = max(clock.max_lateness, lateness)
                clock.late_packets += lateness > 0.05
            await self.write(wire)
            if message.channel in self.rtp_channels:
                message.clock.sent_elapsed = message.elapsed
                self.sent_packets += 1

    async def report(self):
        while True:
            await asyncio.sleep(2)
            self.log(connection=self.id, event='buffer_metrics', buffer_ms=self.config.buffer_ms,
                queued_bytes=self.queue.bytes, peak_queued_bytes=self.queue.peak_bytes,
                max_drain_ms=round(self.max_drain * 1000, 2), sent_rtp_packets=self.sent_packets,
                tracks=self.timing.metrics())

    async def admission_timeout(self):
        await asyncio.wait_for(self.admitted_event.wait(), 8)
        await asyncio.Future()  # Remain pending until the connection is closed.

    async def test_timeout(self):
        await self.test_event.wait()
        while True:
            if self.test_grant is not None:
                remaining = await self.config.test_channel.remaining(self.test_grant)
                if remaining <= 0:
                    raise ConnectionError('test allowance exhausted')
                await asyncio.sleep(min(1, remaining))

    async def run(self):
        try:
            upstream_reader, self.upstream = await asyncio.wait_for(
                asyncio.open_connection(self.config.upstream_host, self.config.upstream_port, limit=32768), 3)
            peer = self.writer.get_extra_info('peername')
            self.log(connection=self.id, event='open', peer=peer[0], peer_port=peer[1])
            self.tasks = [asyncio.create_task(self.client_control()), asyncio.create_task(self.ingest(upstream_reader)),
                          asyncio.create_task(self.send_media()), asyncio.create_task(self.report()),
                          asyncio.create_task(self.admission_timeout()), asyncio.create_task(self.test_timeout())]
            done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError) as exc:
            self.log(connection=self.id, event='closed', reason=type(exc).__name__)
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            for writer in (self.writer, self.upstream):
                if writer:
                    writer.close()
            if self.test_grant is not None:
                await self.config.test_channel.release(self.test_grant)


async def serve(config):
    report = config.runtime / 'reports/rtsp-buffer.jsonl'
    def log(**event):
        event['utc_seconds'] = time.time()
        if report.exists() and report.stat().st_size > 1024 * 1024:
            os.replace(report, report.with_suffix('.previous.jsonl'))
        with report.open('a') as file:
            file.write(json.dumps(event) + '\n')
    active = set()
    rates = OrderedDict()
    async def connect(reader, writer):
        peer = writer.get_extra_info('peername')
        now = time.monotonic()
        while rates and next(iter(rates.values()))[-1] <= now - 60:
            rates.popitem(last=False)
        attempts = [t for t in rates.get(peer[0] if peer else '', []) if t > now - 60]
        if not peer or len(active) >= 8 or len(attempts) >= 30 or (peer[0] not in rates and len(rates) >= 4096):
            writer.close()
            return
        rates[peer[0]] = attempts + [now]
        rates.move_to_end(peer[0])
        task = asyncio.current_task()
        active.add(task)
        try:
            await Session(config, reader, writer, log).run()
        finally:
            active.discard(task)
    server = await asyncio.start_server(connect, config.listen_host, config.listen_port, limit=32768)
    log(event='listening', listen_port=config.listen_port, buffer_ms=config.buffer_ms,
        max_buffer_bytes=config.max_buffer_bytes)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--listen-port', type=int)
    parser.add_argument('--buffer-ms', type=int)
    parser.add_argument('--max-buffer-bytes', type=int, default=2 * 1024 * 1024)
    parser.add_argument('--write-timeout', type=float, default=2)
    args = parser.parse_args()
    settings = json.loads((args.runtime / 'settings.json').read_text())
    args.listen_port = args.listen_port or settings['rtsp_port']
    args.buffer_ms = args.buffer_ms or settings.get('rtsp_buffer_ms', 1000)
    if not 1 <= args.listen_port <= 65535 or not 100 <= args.buffer_ms <= 3000:
        parser.error('port or buffer outside allowed range')
    if not 65536 <= args.max_buffer_bytes <= 16 * 1024 * 1024 or not 0.1 <= args.write_timeout <= 10:
        parser.error('queue limit or write timeout outside allowed range')
    args.listen_host = (settings['bind_address'] if settings['mode'] == 'production' or settings.get('local_network')
                        else '127.0.0.1')
    args.upstream_host = '127.0.0.1'
    args.upstream_port = settings.get('rtsp_internal_port', 18554)
    args.public_hosts = {settings['hostname'], args.listen_host}
    args.proxy_secret = load_secret(args.runtime / 'rtspbuffer/proxy-secret')
    args.test_channel = None
    if settings.get('test_video_enabled'):
        from .testchannel import TestChannel
        args.test_channel = TestChannel(args.runtime, settings)
    if settings['mode'] == 'local':
        args.public_hosts.update({'localhost', '127.0.0.1'})
    if args.listen_port == args.upstream_port:
        parser.error('listener cannot be its own upstream')
    os.umask(0o077)
    asyncio.run(serve(args))


if __name__ == '__main__':
    main()
