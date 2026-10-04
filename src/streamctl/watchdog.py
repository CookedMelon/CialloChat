"""Disconnect publishers exceeding the measured ingress bitrate limit."""
import argparse
import base64
from collections import deque
import json
import re
from pathlib import Path
import sqlite3
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import yaml

try:
    from .leases import Leases, publisher_policies, validate_mail, send_notice
except ImportError:
    from leases import Leases, publisher_policies, validate_mail, send_notice

WINDOW_SECONDS = 5
CONSECUTIVE_SAMPLES = 2
POLL_SECONDS = 1

try:
    from .traffic import TrafficSampler, TrafficStore
except ImportError:
    from traffic import TrafficSampler, TrafficStore


class RateLimiter:
    def __init__(self):
        self.history = {}

    def sample(self, publishers, now, limit):
        """Return (publisher, Kbps); readers and disconnected IDs are forgotten."""
        active = set()
        exceeded = []
        for item in publishers:
            if item.get('state') != 'publish':
                continue
            count = item.get('inboundBytes')
            if type(count) is not int or count < 0:
                raise ValueError('missing publisher ingress counter')
            key = (item['kind'], item['id'])
            active.add(key)
            history, strikes = self.history.get(key, (deque(), 0))
            if history and (now <= history[-1][0] or count < history[-1][1]):
                history, strikes = deque(), 0
            history.append((now, count))
            while len(history) > 1 and now - history[1][0] >= WINDOW_SECONDS:
                history.popleft()
            elapsed = now - history[0][0]
            if elapsed >= WINDOW_SECONDS:
                kbps = (count - history[0][1]) * 8 / elapsed / 1000
                strikes = strikes + 1 if kbps > limit else 0
                if strikes >= CONSECUTIVE_SAMPLES:
                    exceeded.append((item, kbps))
            self.history[key] = (history, strikes)
        self.history = {k: v for k, v in self.history.items() if k in active}
        return exceeded


class Watchdog:
    def __init__(self, config, api_url=None, leases=None, policy=None, mail=None, traffic=None):
        self.config = Path(config)
        self.api_override = api_url
        self.limiter = RateLimiter()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        root = self.config.parent.parent
        self.leases = Leases(leases or root/'leases/leases.sqlite3')
        self.policy = Path(policy or root/'mediamtx/mediamtx.yml')
        self.mail = Path(mail or root/'notifications/smtp.json')
        self.traffic_path = Path(traffic or root/'traffic/traffic.sqlite3')
        self.traffic = None
        self.traffic_failed = False

    def notifications(self):
        if not self.mail.exists():
            return
        config = validate_mail(json.loads(self.mail.read_text()))
        policies = publisher_policies(yaml.safe_load(self.policy.read_text()))
        settings = json.loads(self.config.read_text())
        publish_base = settings['publish_base']
        with self.leases.connection() as db:
            vault = {r['username']: dict(r) for r in db.execute('SELECT * FROM credential_vault')}
        for username, hashed in policies.items():
            record = vault.get(username)
            if record and record['policy_hash'] == hashed:
                config['recipients'][username] = record['email']
            if username not in config['recipients']:
                continue
            notice = self.leases.prepare_notice(username, hashed)
            if notice:
                if record and record['policy_hash'] == hashed:
                    notice.update(read_key=record['read_key'], email=record['email'])
                send_notice(config, notice, publish_base, settings.get('read_base'),
                            test_url=settings.get('test_url'))
                self.leases.delivered(notice)
                print('publisher renewal email sent; credentials omitted', flush=True)

    def notification_worker(self):
        while True:
            try:
                self.notifications()
            except Exception:
                # SMTP faults must never delay or disable disconnect enforcement.
                print('publisher renewal email failed; retry scheduled; details omitted', flush=True)
            time.sleep(5)

    def api(self, config, endpoint, method='GET'):
        token = base64.b64encode((config['username'] + ':' + config['password']).encode()).decode()
        address = (self.api_override or config['api_url']).rstrip('/') + '/'
        request = urllib.request.Request(address + endpoint, method=method,
                                        headers={'Authorization': 'Basic ' + token})
        with self.opener.open(request, timeout=2) as response:
            body = response.read()
            return json.loads(body) if body else None

    def tick(self):
        config = json.loads(self.config.read_text())
        limit = config['publish_limit_kbps']
        if type(limit) is not int or not 100 <= limit <= 1000000:
            raise ValueError('invalid ingress limit')
        publishers = []
        for kind in ('rtmp/conns', 'rtmps/conns', 'rtsp/sessions'):
            try:
                items = self.api(config, kind + '/list?itemsPerPage=10000')['items']
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # Disabled listener, not an authentication failure.
                    continue
                raise
            publishers.extend(dict(item, kind=kind) for item in items)
        policies = publisher_policies(yaml.safe_load(self.policy.read_text()))
        def test_source(item):
            return (config.get('test_video_enabled', False) and item['kind']=='rtsp/sessions'
                    and re.fullmatch(r'test/[a-f0-9]{32}', item.get('path','')))
        if config.get('traffic_enabled', False):
            try:
                if self.traffic is None:
                    self.traffic = TrafficSampler(TrafficStore(self.traffic_path))
                counted = [dict(item, path='live/__test__') if test_source(item) else item for item in publishers]
                users = set(policies) | ({'__test__'} if config.get('test_video_enabled') else set())
                self.traffic.sample(counted, users)
                self.traffic_failed = False
            except Exception:
                # Accounting must not disable stream expiry or bitrate checks.
                if not self.traffic_failed:
                    print('traffic checkpoint failed; accounting may be incomplete; details omitted', flush=True)
                self.traffic_failed = True
        invalid = self.leases.invalid_sessions([item for item in publishers if not test_source(item)], policies)
        rates = self.limiter.sample(publishers, time.monotonic(), limit)
        reasons = {item['id']: (item, 'publisher credential expired or revoked') for item in invalid}
        reasons.update({item['id']: (item, f'publish bitrate limit exceeded: '
                        f'average_kbps={rate:.0f} limit_kbps={limit}') for item, rate in rates})
        for item, reason in reasons.values():
            try:
                self.api(config, item['kind'] + '/kick/' + urllib.parse.quote(item['id'], safe=''), 'POST')
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # Publisher disconnected during this sample.
                    continue
                raise
            # No URLs, passwords or remote addresses in operational logs.
            print(f'{reason}: path={item.get("path", "")} '
                  f'protocol={item["kind"].split("/")[0]} '
                  '; disconnected', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/watchdog/config.json')
    parser.add_argument('--api-url')
    parser.add_argument('--leases')
    parser.add_argument('--policy')
    parser.add_argument('--mail')
    parser.add_argument('--traffic')
    parser.add_argument('--health-file', default='/tmp/watchdog-health')
    args = parser.parse_args()
    watchdog = Watchdog(args.config, args.api_url, args.leases, args.policy, args.mail, args.traffic)
    threading.Thread(target=watchdog.notification_worker, daemon=True).start()
    failures = 0
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    print('CialloChat bitrate watchdog ready', flush=True)
    while not stopped.is_set():
        start = time.monotonic()
        try:
            watchdog.tick()
            Path(args.health_file).touch()
            failures = 0
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, yaml.YAMLError):
            failures += 1
            print('bitrate watchdog check failed; credentials and responses omitted', flush=True)
            if failures >= 3:
                raise SystemExit(1)  # Compose restarts; health never reports success on failure.
        stopped.wait(max(0, POLL_SECONDS - (time.monotonic() - start)))
    if watchdog.traffic is not None:
        watchdog.traffic.flush()


if __name__ == '__main__':
    main()
