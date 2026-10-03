"""Disconnect publishers exceeding the measured ingress bitrate limit."""
import argparse
import base64
from collections import deque
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

WINDOW_SECONDS = 5
CONSECUTIVE_SAMPLES = 2
POLL_SECONDS = 1


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
    def __init__(self, config, api_url=None):
        self.config = Path(config)
        self.api_override = api_url
        self.limiter = RateLimiter()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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
        for item, rate in self.limiter.sample(publishers, time.monotonic(), limit):
            try:
                self.api(config, item['kind'] + '/kick/' + urllib.parse.quote(item['id'], safe=''), 'POST')
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # Publisher disconnected during this sample.
                    continue
                raise
            # No URLs, passwords or remote addresses in operational logs.
            print(f'publish bitrate limit exceeded: path={item.get("path", "")} '
                  f'protocol={item["kind"].split("/")[0]} '
                  f'average_kbps={rate:.0f} limit_kbps={limit}; disconnected', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/watchdog/config.json')
    parser.add_argument('--api-url')
    parser.add_argument('--health-file', default='/tmp/watchdog-health')
    args = parser.parse_args()
    watchdog = Watchdog(args.config, args.api_url)
    failures = 0
    print('CialloChat bitrate watchdog ready', flush=True)
    while True:
        start = time.monotonic()
        try:
            watchdog.tick()
            Path(args.health_file).touch()
            failures = 0
        except (OSError, ValueError, KeyError, TypeError):
            failures += 1
            print('bitrate watchdog check failed; credentials and responses omitted', flush=True)
            if failures >= 3:
                raise SystemExit(1)  # Compose restarts; health never reports success on failure.
        time.sleep(max(0, POLL_SECONDS - (time.monotonic() - start)))


if __name__ == '__main__':
    main()
