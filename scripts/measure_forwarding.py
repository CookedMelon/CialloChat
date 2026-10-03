"""Isolated RTMP -> MediaMTX -> RTSP/TCP protocol forwarding benchmark.

Requires the optional Go probe built from scripts/protocol_latency. FFmpeg only
prepares synthetic encoded input before timing starts. No GUI player is used.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse

import yaml
from smoke_test import Harness
from streamctl.config import ROOT, VERSION, atomic_write, dump


def fixture(path):
    data = path.read_bytes()
    if data[:3] != b'FLV':
        raise ValueError('Invalid encoded fixture')
    offset = int.from_bytes(data[5:9], 'big') + 4
    out = {'Packets': []}
    b64 = lambda value: base64.b64encode(value).decode()
    while offset + 11 <= len(data):
        kind = data[offset]
        size = int.from_bytes(data[offset + 1:offset + 4], 'big')
        dts = int.from_bytes(data[offset + 4:offset + 7], 'big') | (data[offset + 7] << 24)
        body = data[offset + 11:offset + 11 + size]
        if len(body) != size:
            raise ValueError('Truncated FLV tag')
        offset += 11 + size + 4
        if kind == 9 and len(body) >= 5 and body[0] & 15 == 7:
            if body[1] == 0:
                avcc = body[5:]
                if (avcc[4] & 3) != 3:
                    raise ValueError('Expected four-byte AVCC NAL lengths')
                cursor = 6
                for _ in range(avcc[5] & 31):
                    n = int.from_bytes(avcc[cursor:cursor + 2], 'big'); cursor += 2
                    out['SPS'] = b64(avcc[cursor:cursor + n]); cursor += n
                count = avcc[cursor]; cursor += 1
                for _ in range(count):
                    n = int.from_bytes(avcc[cursor:cursor + 2], 'big'); cursor += 2
                    out['PPS'] = b64(avcc[cursor:cursor + n]); cursor += n
            elif body[1] == 1:
                cts = int.from_bytes(body[2:5], 'big', signed=True)
                cursor, nalus = 5, []
                while cursor < len(body):
                    n = int.from_bytes(body[cursor:cursor + 4], 'big'); cursor += 4
                    if not n or cursor + n > len(body):
                        raise ValueError('Invalid AVC fixture packet')
                    nalus.append(b64(body[cursor:cursor + n])); cursor += n
                out['Packets'].append({'Video': True, 'DTSMS': dts, 'PTSMS': dts + cts, 'NALUs': nalus})
        elif kind == 8 and len(body) >= 2 and body[0] >> 4 == 10:
            if body[1] == 0:
                out['AudioConfig'] = b64(body[2:])
            elif body[1] == 1:
                out['Packets'].append({'Video': False, 'DTSMS': dts, 'PTSMS': dts, 'Data': b64(body[2:])})
    out['Packets'].sort(key=lambda p: p['DTSMS'])
    out['LoopMS'] = max(p['DTSMS'] for p in out['Packets']) + 20
    if not all(k in out for k in ('SPS', 'PPS', 'AudioConfig')):
        raise ValueError('Missing fixture tracks')
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ffmpeg', default='ffmpeg')
    p.add_argument('--probe', default=str(ROOT / 'runtime/tools/protocol-latency'))
    p.add_argument('--seconds', type=int, default=60)
    p.add_argument('--readers', type=int, default=1)
    p.add_argument('--slow-reader', action='store_true')
    p.add_argument('--queue-size', type=int, default=512, help='isolated test override only')
    p.add_argument('--report', default=str(ROOT / 'runtime/reports/forwarding.json'))
    args = p.parse_args()
    if not 10 <= args.seconds <= 300 or not 1 <= args.readers <= 8:
        p.error('seconds must be 10..300 and readers 1..8')
    if args.queue_size < 32 or args.queue_size > 4096 or args.queue_size & (args.queue_size - 1):
        p.error('queue-size must be a power of two from 32 to 4096')
    if not Path(args.probe).is_file():
        p.error('Build the optional protocol probe first; see docs/latency.md')
    args.ports = args.mediamtx = None
    os.umask(0o077)
    report = {'version': VERSION, 'date_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'fixture': 'pre-encoded synthetic testsrc2 2560x1440 60fps H264 ultrafast bf=0 keyint=60 target 6Mbps + AAC 48kHz 128kbps',
              'write_queue_size': args.queue_size, 'requested_seconds': args.seconds,
              'account_schema': 2, 'authenticated_read': True}
    with tempfile.TemporaryDirectory(prefix='ciallochat-forwarding-') as directory:
        root = Path(directory)
        subprocess.run([args.ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc2=size=2560x1440:rate=60', '-f', 'lavfi', '-i',
                        'sine=frequency=440:sample_rate=48000', '-t', '3',
                        '-c:v', 'libx264', '-threads', '4', '-preset', 'ultrafast',
                        '-tune', 'zerolatency', '-bf', '0', '-g', '60', '-pix_fmt', 'yuv420p',
                        '-b:v', '6M', '-maxrate', '6M', '-bufsize', '6M',
                        '-c:a', 'aac', '-b:a', '128k', '-f', 'flv', str(root / 'fixture.flv')],
                       check=True, timeout=60)
        encoded = fixture(root / 'fixture.flv')
        report['fixture_loop_ms'] = encoded['LoopMS']
        report['fixture_average_mbps_including_flv'] = round((root / 'fixture.flv').stat().st_size * 8 / (encoded['LoopMS'] / 1000) / 1e6, 3)
        atomic_write(root / 'fixture.json', dump(encoded))
        harness = Harness(args, root / 'service')
        config_file = harness.store.path / 'mediamtx/mediamtx.yml'
        config = yaml.safe_load(config_file.read_text())
        config['writeQueueSize'] = args.queue_size
        atomic_write(config_file, yaml.safe_dump(config, sort_keys=False))
        try:
            harness.start()
            # wait_loaded already confirms accounts and paths; also verify the
            # experimental queue setting actually loaded.
            actual = harness.service.api('config/global/get')
            if actual['writeQueueSize'] != args.queue_size:
                raise RuntimeError('Queue override did not load')
            s = harness.service.settings
            query = urllib.parse.urlencode({'user': 'alice', 'pass': harness.passwords['alice']})
            settings = {'PublishURL': f'rtmp://127.0.0.1:{s["rtmp_port"]}/live/alice?{query}',
                        'ReadURL': harness.read_url('alice'),
                        'Fixture': str(root / 'fixture.json'), 'Seconds': args.seconds,
                        'Readers': args.readers, 'SlowReader': args.slow_reader}
            print('Protocol forwarding measurement running (no encoder/decoder in timed path).', flush=True)
            result = subprocess.run([args.probe], input=json.dumps(settings), text=True,
                                    capture_output=True, timeout=args.seconds + 40)
            if result.stdout.strip():
                report.update(json.loads(result.stdout))
            if result.returncode:
                raise RuntimeError('Protocol probe failed: ' + result.stderr.replace(settings['PublishURL'], '<publish-url>').replace(settings['ReadURL'], '<read-url>'))
            logs = harness.service.compose(['logs', '--tail', '80', 'mediamtx'])
            report['server_queue_warning_count_last_80_log_lines'] = logs.lower().count('queue')
            report['status'] = 'measured'
        except Exception as exc:
            report.update(status='failed', error=str(exc))
            raise
        finally:
            try:
                harness.cleanup()
            finally:
                atomic_write(Path(args.report), dump(report))
    summary = dict(report)
    summary['readers'] = [{k: v for k, v in r.items() if k != 'samples'} for r in report['readers']]
    print(dump(summary))


if __name__ == '__main__':
    main()
