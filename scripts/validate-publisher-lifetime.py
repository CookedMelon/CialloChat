"""Isolated localhost media test; shortens only its temporary test lease."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

from smoke_test import Harness, wait
from streamctl.config import ROOT, atomic_write, dump
from streamctl.leases import Leases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mediamtx', required=True)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--report', default=str(ROOT/'runtime/reports/publisher-lifetime.json'))
    args = parser.parse_args()
    args.ports = None
    report = {'backend': 'native', 'network': 'localhost only', 'tests': []}
    with tempfile.TemporaryDirectory(prefix='ciallochat-lease-test-') as directory:
        harness = Harness(args, directory)
        try:
            harness.start()
            harness.rejected(value='incorrect-publisher-secret')
            report['tests'].append('wrong password closed before live media')
            publisher = harness.publish(video_kbps=3200, resolution='2560x1440', fps=30)
            wait(lambda: harness.ready('live/alice'))
            leases = Leases(harness.store.path/'leases/leases.sqlite3')
            publisher.terminate(); publisher.wait(timeout=5)
            wait(lambda: not harness.ready('live/alice'))
            wait(lambda: not leases.statuses()[0]['is_streaming'])
            remaining = leases.statuses()[0]['remaining_seconds']
            time.sleep(2)
            assert leases.statuses()[0]['remaining_seconds'] == remaining
            report['tests'].append('stopped publisher paused cumulative usage')
            publisher = harness.publish(video_kbps=3200, resolution='2560x1440', fps=30)
            wait(lambda: harness.ready('live/alice'))
            wait(lambda: leases.statuses()[0]['is_streaming'])
            with leases.connection() as db:
                db.execute('UPDATE leases SET used_seconds=6600 WHERE username=?', ('alice',))
            hashed = harness.store.read('accounts.json')['users'][0]['publish_key_hash']
            notice = leases.prepare_notice('alice', hashed)
            assert notice is not None
            leases.delivered(notice)  # Simulate SMTP acceptance; no actual email.
            reader = subprocess.Popen([args.ffmpeg, '-v', 'error', '-nostdin',
                '-rtsp_transport', 'tcp', '-i', harness.read_url('alice'), '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            harness.publishers.append(reader)
            wait(lambda: len(harness.state('live/alice')['readers']) == 1)
            with leases.connection() as db:
                db.execute('UPDATE leases SET used_seconds=? WHERE username=?', (7197, 'alice'))
            wait(lambda: publisher.poll() is not None, 10)
            wait(lambda: reader.poll() is not None, 10)
            wait(lambda: not harness.ready('live/alice'))
            report['tests'].append('cumulative usage exhaustion actively closed publisher and attached reader')
            harness.rejected()
            report['tests'].append('old password permanently denied on reconnect')
            harness.stop_service(); harness.start()
            harness.rejected()
            report['tests'].append('restart retained expired password rejection')
            renewed = harness.publish(value=notice['key'], video_kbps=3200)
            wait(lambda: harness.ready('live/alice'))
            assert leases.statuses()[0]['remaining_seconds'] > 7190
            offset = harness.log.stat().st_size
            old_attempt = harness.publish()
            old_attempt.wait(timeout=9)
            assert old_attempt.returncode != 0
            reason = harness.log.read_text()[offset:].lower()
            if 'authentication' not in reason and '403' not in reason:
                import re
                safe = re.sub(r'(?:rtmps?|rtsp)://\S+', '[URL]', reason)
                for secret in (*harness.passwords.values(), notice['key']):
                    safe = safe.replace(secret.lower(), '[KEY]')
                raise AssertionError('old key rejection reason: '+safe)
            assert renewed.poll() is None
            report['tests'].append('manually applied next password started a fresh lease; old key stayed rejected')
            renewed.terminate(); renewed.wait(timeout=5)
            wait(lambda: not harness.ready('live/alice'))
            harness.first_request(expected=404)
            assert not harness.state('live/alice')['readers']
            report['tests'].append('stopped source had no readers; idle playback received only RTSP 404')
            report['result'] = 'passed'
        except Exception as exc:
            report['result'] = 'failed'
            report['error'] = type(exc).__name__
            raise
        finally:
            harness.cleanup()
            atomic_write(Path(args.report), dump(report))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
