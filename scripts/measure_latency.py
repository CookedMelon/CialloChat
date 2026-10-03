"""Measure synthetic frame generation -> encode -> server -> decode, not display latency.

Uses an isolated smoke-test deployment. A frame ID and its complement are carried
in image pixels and matched to a monotonic generation timestamp on this machine.
No OBS/Windows settings or production accounts are changed.
"""
import argparse
import json
import math
import os
from pathlib import Path
import select
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.parse

from smoke_test import Harness, wait
from streamctl.config import ROOT, VERSION, atomic_write, dump


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def measure(harness, args):
    s = harness.service.settings
    query = urllib.parse.urlencode({'user': 'alice', 'pass': harness.passwords['alice']})
    publish = f'rtmp://127.0.0.1:{s["rtmp_port"]}/live/alice?{query}'
    read = harness.read_url('alice')
    width, height, fps = args.width, args.height, args.fps
    writer_done = threading.Event()
    generated = {}
    errors = []
    samples = []
    invalid = 0
    received_ids = set()
    sender = receiver = None
    writer = None
    logs = []
    try:
        for name in ('latency-publisher.log', 'latency-reader.log'):
            logs.append(open(harness.store.path / name, 'wb'))
        sender = subprocess.Popen([
            args.ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin',
            '-f', 'rawvideo', '-pixel_format', 'gray', '-video_size', f'{width}x{height}',
            '-framerate', str(fps), '-i', 'pipe:0',
            '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
            '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-threads', str(args.encoder_threads),
            '-preset', args.preset, '-tune', 'zerolatency', '-bf', '0', '-g', str(fps),
            '-pix_fmt', 'yuv420p', '-b:v', '2M', '-c:a', 'aac', '-b:a', '128k',
            '-flush_packets', '1', '-f', 'flv', publish,
        ], stdin=subprocess.PIPE, stdout=logs[0], stderr=logs[0])

        def feed():
            start = time.monotonic()
            index = 0
            try:
                while not writer_done.is_set():
                    delay = start + index / fps - time.monotonic()
                    if delay > 0 and writer_done.wait(delay):
                        break
                    now = time.monotonic_ns()
                    generated[index] = now
                    bits = f'{index:032b}' + f'{index ^ 0xffffffff:032b}'
                    row = b''.join(bytes([235 if bit == '1' else 16]) * (width // 64) for bit in bits)
                    # Moving grayscale bars, plus the frame identifier strip.
                    body_row = bytes(((x + index * 4) % 192) + 32 for x in range(width))
                    sender.stdin.write(row * 64 + body_row * (height - 64))
                    sender.stdin.flush()
                    index += 1
            except (BrokenPipeError, OSError) as exc:
                if not writer_done.is_set():
                    errors.append(type(exc).__name__)

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        wait(lambda: harness.ready('live/alice'), seconds=15)
        reader_started = time.monotonic()
        receiver = subprocess.Popen([
            args.ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin',
            '-rtsp_transport', 'tcp', '-fflags', 'nobuffer', '-flags', 'low_delay',
            '-probesize', '32768', '-analyzeduration', '100000', '-threads', '1',
            '-i', read, '-map', '0:v:0', '-an',
            '-vf', f'crop={width}:64:0:0,scale=64:1:flags=neighbor',
            '-pix_fmt', 'gray', '-c:v', 'rawvideo', '-threads', '1',
            '-fps_mode', 'passthrough', '-flush_packets', '1', '-f', 'rawvideo', 'pipe:1',
        ], stdout=subprocess.PIPE, stderr=logs[1], bufsize=0)
        pending = bytearray()
        first_frame = None
        deadline = reader_started + args.seconds + 20
        while time.monotonic() < deadline:
            if errors or sender.poll() is not None:
                raise RuntimeError('Synthetic publisher stopped')
            readable, _, _ = select.select([receiver.stdout], [], [], 1)
            if not readable:
                continue
            chunk = os.read(receiver.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Synthetic decoder stopped')
            arrived_ns = time.monotonic_ns()
            pending.extend(chunk)
            while len(pending) >= 64:
                pixels = pending[:64]
                del pending[:64]
                number = int(''.join('1' if p > 128 else '0' for p in pixels[:32]), 2)
                inverse = int(''.join('1' if p > 128 else '0' for p in pixels[32:]), 2)
                if number ^ inverse != 0xffffffff or number not in generated:
                    invalid += 1
                    continue
                if number in received_ids:
                    raise RuntimeError('Duplicate decoded frame identifier')
                received_ids.add(number)
                arrived = arrived_ns / 1e9
                if first_frame is None:
                    first_frame = arrived
                    print('First timestamped frame decoded; warming up for 2 seconds.', flush=True)
                if arrived - first_frame >= 2:
                    samples.append({'frame': number, 'received_after_first_s': round(arrived - first_frame, 4),
                                    'latency_ms': round((arrived_ns - generated[number]) / 1e6, 3)})
            if first_frame is not None and time.monotonic() - first_frame >= args.seconds + 2:
                break
        if first_frame is None or len(samples) < args.seconds * fps * .8 or invalid:
            raise RuntimeError(f'Insufficient/corrupt samples: valid={len(samples)}, invalid={invalid}')
        values = sorted(x['latency_ms'] for x in samples)
        if values[0] < 0:
            raise RuntimeError('Invalid negative frame latency')
        measured_fps = (len(samples) - 1) / (samples[-1]['received_after_first_s'] - samples[0]['received_after_first_s'])
        return {
            'scope': 'WSL synthetic generation + FFmpeg H264/AAC encoding + RTMP + MediaMTX + RTSP/TCP + FFmpeg video decoding; excludes Windows OBS, GUI/audio playback and display',
            'settings': {'size': f'{width}x{height}', 'fps': fps, 'video': f'libx264 {args.preset} threads={args.encoder_threads} zerolatency bf=0 g={fps} target=2M',
                         'audio': 'AAC 48kHz 128k carried alongside video; audio latency not measured',
                         'decoder': 'FFmpeg nobuffer low_delay threads=1 probesize=32768 analyzeduration=100000',
                         'pattern': 'synthetic grayscale bars; not representative full-motion load'},
            'measurement': 'frame id plus complement embedded in pixels; one host monotonic clock before generation and after decoded bytes received',
            'requested_steady_seconds': args.seconds, 'warmup_seconds': 2,
            'measured_fps': round(measured_fps, 3),
            'kept_realtime': measured_fps >= fps * .95,
            'reader_first_frame_ms': round((first_frame - reader_started) * 1000, 3),
            'count': len(values), 'invalid_ids': invalid, 'min_ms': values[0],
            'median_ms': round(statistics.median(values), 3),
            'p95_ms': values[math.ceil(len(values) * .95) - 1], 'max_ms': values[-1],
            'first_third_median_ms': round(statistics.median(x['latency_ms'] for x in samples[:len(samples)//3]), 3),
            'last_third_median_ms': round(statistics.median(x['latency_ms'] for x in samples[-len(samples)//3:]), 3),
            'samples': samples,
        }
    finally:
        writer_done.set()
        stop(receiver)
        stop(sender)
        if writer:
            writer.join(timeout=5)
        if sender and sender.stdin:
            sender.stdin.close()
        if receiver and receiver.stdout:
            receiver.stdout.close()
        for file in logs:
            file.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--mediamtx', help='Optional pinned native server for comparison')
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--preset', choices=('ultrafast', 'veryfast'), default='veryfast')
    parser.add_argument('--encoder-threads', type=int, default=2)
    parser.add_argument('--report', default=str(ROOT / 'runtime/reports/latency-compose.json'))
    args = parser.parse_args()
    if not 10 <= args.seconds <= 300:
        parser.error('--seconds must be 10..300')
    if not 640 <= args.width <= 3840 or args.width % 64 or not 64 <= args.height <= 2160 or args.height % 2 or not 1 <= args.fps <= 120:
        parser.error('width must be 640..3840 and divisible by 64; height even 64..2160; fps 1..120')
    if not 1 <= args.encoder_threads <= 16:
        parser.error('--encoder-threads must be 1..16')
    args.ports = None
    os.umask(0o077)
    report = {'version': VERSION, 'backend': 'native' if args.mediamtx else 'compose',
              'date_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    with tempfile.TemporaryDirectory(prefix='ciallochat-latency-') as directory:
        harness = Harness(args, directory)
        try:
            harness.start()
            report.update(measure(harness, args))
            report['status'] = 'measured'
        except Exception as exc:
            report.update(status='failed', error=str(exc))
            # FFmpeg logs can contain the ephemeral publishing password. Do not
            # copy raw stderr to the report or console.
            raise
        finally:
            try:
                harness.cleanup()
            finally:
                atomic_write(Path(args.report), dump(report))
    print(json.dumps({k: v for k, v in report.items() if k != 'samples'}, indent=2), flush=True)


if __name__ == '__main__':
    main()
