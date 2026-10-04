#!/usr/bin/env python3
"""Isolated real-media test of the anonymous demo, IP limits and ordinary streams."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from smoke_test import Harness, wait
from streamctl.config import ROOT, atomic_write, dump
from streamctl.testquota import TestQuota


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mediamtx',required=True)
    p.add_argument('--ffmpeg',default='ffmpeg'); p.add_argument('--ffprobe',default='ffprobe')
    p.add_argument('--asset',default=str(ROOT/'runtime/test-video/test.mp4'))
    args=p.parse_args(); args.buffer_ms=1000; args.ports=None; args.test_video_asset=args.asset
    report=dict(result='failed',network='localhost only',tests=[])
    with tempfile.TemporaryDirectory(prefix='ciallochat-demo-check-') as directory:
        h=Harness(args,directory)
        try:
            h.start()
            port=h.service.settings['rtsp_port']; url=f'rtsp://127.0.0.1:{port}/test'
            quota=TestQuota(h.store.path/'test-video/quota.sqlite3')
            with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
                sock.sendall(f'OPTIONS {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n'.encode())
                assert sock.recv(4096).startswith(b'RTSP/1.0 200')
            with quota.connection() as db: assert db.execute('SELECT COUNT(*) FROM windows').fetchone()[0]==0
            report['tests'].append('OPTIONS alone created no publisher or allowance')
            progress=Path(directory)/'decoded.progress'
            def start_reader():
                progress.unlink(missing_ok=True)
                stream=subprocess.Popen([args.ffmpeg,'-v','error','-nostdin','-progress',str(progress),
                    '-rtsp_transport','tcp','-i',url,'-map','0:v:0','-map','0:a:0',
                    '-vf','setpts=N/(FRAME_RATE*TB)','-fps_mode:v','passthrough','-enc_time_base:v','1:90000',
                    '-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                h.publishers.append(stream)
                wait(lambda:any(p['name'].startswith('test/') and len(p['readers'])==1 for p in h.service.api('paths/list')['items']))
                return stream
            first=start_reader(); time.sleep(6)
            assert first.poll() is None,'test source was disconnected by ordinary two-hour watchdog'
            decoded=[int(line.split('=',1)[1]) for line in progress.read_text().splitlines() if line.startswith('frame=')]
            assert decoded and max(decoded)>=120,'test channel did not decode video and audio continuously'
            report['decoded_video_frames']=max(decoded)
            with quota.connection() as db:
                before=dict(db.execute('SELECT * FROM windows WHERE active=1').fetchone())
            with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
                sock.sendall(f'DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n'.encode()); sock.settimeout(3)
                assert sock.recv(1)==b'', 'duplicate source IP was accepted'
            first.terminate(); first.wait(timeout=5)
            wait(lambda:len(h.service.api('rtsp/sessions/list')['items'])==0)
            second=start_reader()
            with quota.connection() as db:
                after=dict(db.execute('SELECT * FROM windows WHERE active=1').fetchone())
            assert after['window_end']==before['window_end'],'reconnect reset allowance'
            assert after['token']!=before['token']
            report['tests'].append('real H264/AAC demo survived watchdog, duplicate IP denied and reconnect retained deadline')
            # Shorten only this isolated window to exercise expiry and cooldown.
            with quota.connection() as db:
                db.execute('UPDATE windows SET deadline=?,window_end=? WHERE active=1',(time.time()+2,time.time()+2))
            wait(lambda:second.poll() is not None,8)
            wait(lambda:len(h.service.api('rtsp/sessions/list')['items'])==0)
            with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
                sock.sendall(f'DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n'.encode()); sock.settimeout(3)
                assert sock.recv(1)==b'', 'cooldown was bypassed'
            report['tests'].append('deadline closed media and publisher; cooldown denied immediate reconnect')
            h.stop_service(); h.start()
            with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
                sock.sendall(f'DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n'.encode()); sock.settimeout(3)
                assert sock.recv(1)==b'', 'restart cleared cooldown'
            report['tests'].append('media/relay restart retained cooldown')
            alice=h.publish(video_kbps=3200); wait(lambda:h.ready('live/alice'))
            h.read('alice',3); assert alice.poll() is None
            report['tests'].append('ordinary authenticated stream still decoded through unchanged buffer')
            report['result']='passed'
        finally:
            h.cleanup(); atomic_write(ROOT/'runtime/reports/test-channel-validation.json',dump(report))
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
