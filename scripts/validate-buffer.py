#!/usr/bin/env python3
"""Real media regression for authenticated fixed-delay RTSP; localhost only."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

from smoke_test import Harness, wait
from streamctl.accounts import hash_password, new_password
from streamctl.config import ROOT, atomic_write, dump
from streamctl.leases import Leases
from streamctl.service import commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mediamtx', required=True)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    args = parser.parse_args()
    args.ports, args.buffer_ms = None, 1000
    report = dict(result='failed', network='localhost only', buffer_ms=1000, tests=[])
    with tempfile.TemporaryDirectory(prefix='ciallochat-buffer-check-') as directory:
        harness = Harness(args, directory)
        try:
            harness.start()
            publisher = harness.publish(video_kbps=3200, resolution='2560x1440', fps=60, audio_kbps=160)
            wait(lambda: harness.ready('live/alice'))
            harness.first_request()
            harness.read('alice', seconds=4)
            assert publisher.poll() is None
            report['tests'].append('1440p60 H264/AAC decoded through authenticated one-second buffer')
            harness.first_request(key='incorrect-watch-key', expected=401)
            harness.first_request(username='bob', key=harness.read_keys['alice'], expected=401)
            report['tests'].append('wrong and cross-user watch keys denied')
            reader = subprocess.Popen([args.ffmpeg, '-v', 'error', '-nostdin', '-rtsp_transport', 'tcp',
                '-i', harness.read_url('alice'), '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            harness.publishers.append(reader)
            wait(lambda: len(harness.state('live/alice')['readers']) == 1)
            new_key = new_password()
            with harness.store.lock():
                accounts = harness.store.read('accounts.json')
                accounts['users'][0]['read_key_hash'] = hash_password(new_key)
                commit(harness.store, accounts, revoke=['live/alice'], revoke_states=('read',), service=harness.service)
            wait(lambda: reader.poll() is not None)
            assert publisher.poll() is None
            harness.first_request(key=harness.read_keys['alice'], expected=401)
            harness.read_keys['alice'] = new_key
            harness.first_request()
            report['tests'].append('watch refresh closed old relay reader, preserved publisher and accepted new key')
            leases = Leases(harness.store.path/'leases/leases.sqlite3')
            with leases.connection() as db:
                db.execute('UPDATE leases SET used_seconds=? WHERE username=?', (7198, 'alice'))
            wait(lambda: publisher.poll() is not None, 10)
            harness.stop_service(); harness.start()
            harness.rejected()
            report['tests'].append('expired push key remained invalid across media/relay restart')
            report['result'] = 'passed'
        finally:
            harness.cleanup()
            atomic_write(ROOT/'runtime/reports/buffer-validation.json', dump(report))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
