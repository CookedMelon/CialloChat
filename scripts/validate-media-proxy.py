#!/usr/bin/env python3
"""Isolated Nginx + RTMPS + authenticated buffered RTSP regression."""
import argparse
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile

from smoke_test import Harness, free_ports, wait
from streamctl.config import ROOT, atomic_write, dump, render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mediamtx', required=True)
    parser.add_argument('--nginx', required=True)
    parser.add_argument('--stream-module', required=True)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--asset', default=str(ROOT/'runtime/test-video/test.mp4'))
    args = parser.parse_args()
    args.ports, args.buffer_ms, args.test_video_asset = None, 1000, args.asset
    report = dict(result='failed', network='localhost only', tests=[])
    with tempfile.TemporaryDirectory(prefix='ciallochat-proxy-check-') as directory:
        h = Harness(args, directory)
        nginx = None
        try:
            subprocess.run([str(ROOT/'scripts/make-test-cert.sh'), str(h.store.path/'certs')],
                           check=True, stdout=subprocess.DEVNULL)
            public_tls, public_rtsp = free_ports()[:2]
            s, a, c = h.store.load()
            s.update(mode='production', hostname='localhost', read_hostname='localhost',
                     reverse_proxy_enabled=True, public_rtmps_port=public_tls, public_rtsp_port=public_rtsp)
            atomic_write(h.store.path/'settings.json', dump(s))
            atomic_write(h.store.path/'mediamtx/mediamtx.yml', render(s, a, c))
            h.start()
            media = (ROOT/'config/nginx-media.conf').read_text().replace('chat.v50to.cc', 'localhost')
            media = media.replace('127.0.0.1:1936', f'127.0.0.1:{s["rtmps_port"]}')
            media = media.replace('127.0.0.1:8554', f'127.0.0.1:{s["rtsp_port"]}')
            media = media.replace('listen 443;', f'listen 127.0.0.1:{public_tls};')
            media = media.replace('listen 554;', f'listen 127.0.0.1:{public_rtsp};')
            media = media.replace('/var/log/nginx/ciallochat-media.log', f'{directory}/proxy-access.log')
            conf = Path(directory)/'nginx.conf'
            conf.write_text(f'load_module {Path(args.stream_module).resolve()};\n'
                            f'daemon off; master_process off; pid {directory}/nginx.pid;\n'
                            f'error_log {directory}/proxy-error.log;\nevents {{}}\nstream {{\n{media}\n}}\n')
            cmd = [str(Path(args.nginx).resolve()), '-p', directory, '-c', str(conf)]
            subprocess.run(cmd + ['-t'], check=True, capture_output=True)
            nginx = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def proxy_ready():
                try:
                    with socket.create_connection(('127.0.0.1', public_tls), timeout=.3):
                        return True
                except OSError:
                    return False
            wait(proxy_ready)
            context = ssl.create_default_context(cafile=str(h.store.path/'certs/ca.crt'))
            with socket.create_connection(('127.0.0.1', public_tls), timeout=3) as sock:
                with context.wrap_socket(sock, server_hostname='localhost'):
                    pass
            try:
                with socket.create_connection(('127.0.0.1', public_tls), timeout=3) as sock:
                    with context.wrap_socket(sock, server_hostname='unknown.invalid'):
                        raise AssertionError('unknown SNI was forwarded')
            except (OSError, ssl.SSLError):
                pass
            report['tests'].append('trusted TLS passthrough succeeded; unknown SNI rejected')
            h.service.reload_certificate()
            report['tests'].append('certificate reload verified backend TLS through trusted PROXY header')

            original = h.service.settings
            h.service.settings = dict(original, rtmps_port=public_tls)
            publisher = h.publish(tls=True, video_kbps=1000, resolution='640x360')
            h.service.settings = original
            wait(lambda: h.ready('live/alice'))
            h.read('alice', seconds=3)
            assert publisher.poll() is None
            report['tests'].append('H264/AAC published via Nginx and decoded through authenticated one-second buffer')

            def describe(url):
                with socket.create_connection(('127.0.0.1', public_rtsp), timeout=5) as sock:
                    sock.sendall(f'DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n'.encode())
                    return sock.recv(4096)
            from streamctl.accounts import credentials
            wrong = credentials(s, 'alice', read_key='wrong-reader-key-123')['read_url']
            cross = credentials(s, 'bob', read_key=h.read_keys['alice'])['read_url']
            for url in (wrong, cross):
                assert describe(url).startswith(b'RTSP/1.0 401'), 'invalid watch key was admitted'
            old_url = h.read_url('alice')
            h.change('reset-read')
            assert describe(old_url).startswith(b'RTSP/1.0 401')
            assert describe(h.read_url('alice')).startswith(b'RTSP/1.0 200')
            assert publisher.poll() is None
            report['tests'].append('wrong/cross-user/revoked watch keys rejected; refresh retained publisher')

            demo = f'rtsp://localhost:{public_rtsp}/test'
            result = subprocess.run([args.ffmpeg, '-v', 'error', '-nostdin', '-rtsp_transport', 'tcp',
                                     '-i', demo, '-frames:v', '120', '-map', '0:v:0', '-map', '0:a:0',
                                     '-vf', 'setpts=N/(FRAME_RATE*TB)', '-fps_mode:v', 'passthrough',
                                     '-enc_time_base:v', '1:90000', '-f', 'null', '-'],
                                    capture_output=True, timeout=20)
            assert result.returncode == 0 and not result.stderr, 'proxied public demo did not decode'
            events = [json.loads(line) for line in (h.store.path/'reports/rtsp-buffer.jsonl').read_text().splitlines()]
            assert any(event.get('event') == 'open' and event.get('peer') == '127.0.0.1' for event in events)
            assert any(event.get('event') == 'buffer_metrics' and event['buffer_ms'] == 1000 for event in events)
            report['tests'].append('public demo decoded 120 video frames plus audio; peer and buffer metrics retained')
            report['result'] = 'passed'
        finally:
            h.cleanup()
            if nginx is not None:
                nginx.terminate()
                nginx.wait(timeout=5)
            atomic_write(ROOT/'runtime/reports/media-proxy-validation.json', dump(report))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
