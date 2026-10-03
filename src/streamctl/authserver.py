"""Private MediaMTX admission callback; media never passes through this server."""
import argparse
from collections import OrderedDict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import threading
import time
import urllib.parse

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
import yaml


class Policy:
    def __init__(self, config):
        self.config = Path(config)
        self.lock = threading.RLock()
        self.signature = None
        self.users = []
        self.cache = OrderedDict()
        self.hasher = PasswordHasher()
        self.verifiers = threading.BoundedSemaphore(2)

    def _load(self):
        # Atomic replacement of the generated policy invalidates all cached
        # successes, including on key reset, disable, delete and rollback.
        stat = self.config.stat()
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if signature != self.signature:
            self.cache.clear()
            self.users = []
            self.signature = None
            if stat.st_size > 16 * 1024 * 1024:
                raise ValueError('policy too large')
            data = yaml.safe_load(self.config.read_text())
            users = data['authInternalUsers']
            if not isinstance(users, list):
                raise ValueError('invalid policy')
            self.users = users
            self.signature = signature
        return signature

    def healthy(self):
        try:
            with self.lock:
                self._load()
            return True
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            return False

    def _matches(self, hashed, value, signature):
        if not isinstance(hashed, str) or not hashed.startswith('argon2:$argon2id$'):
            return False
        # Cache contains fingerprints of valid credentials, never plaintext.
        fingerprint = hashlib.sha256((hashed + '\0' + value).encode()).digest()
        now = time.monotonic()
        with self.lock:
            if signature != self.signature:
                return False
            if self.cache.get(fingerprint, 0) > now:
                self.cache.move_to_end(fingerprint)
                return True
        if not self.verifiers.acquire(timeout=2):
            return False
        try:
            try:
                valid = self.hasher.verify(hashed[7:], value)
            except (VerificationError, InvalidHashError):
                return False
        finally:
            self.verifiers.release()
        if valid:
            with self.lock:
                if signature != self.signature:
                    return False
                self.cache[fingerprint] = now + 30
                self.cache.move_to_end(fingerprint)
                while len(self.cache) > 512:
                    self.cache.popitem(last=False)
        return valid

    def authorize(self, request):
        try:
            if not isinstance(request, dict):
                return False
            fields = ('action', 'path', 'protocol', 'user', 'password', 'query', 'ip')
            if any(not isinstance(request.get(k, ''), str) for k in fields):
                return False
            action, path = request.get('action', ''), request.get('path', '')
            query = urllib.parse.parse_qs(request.get('query', ''), keep_blank_values=True,
                                          strict_parsing=True, max_num_fields=16, errors='strict')
            if 'read_key' in query:
                # URL keys grant only RTSP read access to this exact account.
                if action != 'read' or request.get('protocol') != 'rtsp':
                    return False
                if not re.fullmatch(r'live/[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', path):
                    return False
                values = query['read_key']
                if len(values) != 1:
                    return False
                username = 'ciallochat-read-' + path[5:]
                value = values[0]
            else:
                # Preserve existing publish, control and legacy reader clients.
                username, value = request.get('user', ''), request.get('password', '')
            if not username or not 12 <= len(value) <= 256:
                return False
            with self.lock:
                signature = self._load()
                users = list(self.users)
            for user in users:
                if user.get('user') != username:
                    continue
                if not any(p.get('action') == action and
                           (p.get('path', '') == path if action in ('read', 'publish', 'playback')
                            else not p.get('path')) for p in user.get('permissions', [])):
                    return False
                if user.get('ips') and not any(ipaddress.ip_address(request.get('ip', '')) in
                        ipaddress.ip_network(network, strict=False) for network in user['ips']):
                    return False
                return self._matches(user.get('pass'), value, signature)
            return False
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, yaml.YAMLError):
            return False


class AdmissionServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, policy):
        self.policy = policy
        self.workers = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()

    def handle_error(self, request, client_address):
        # Do not print request URLs, credentials or exception tracebacks.
        pass


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *args):
        pass

    def reply(self, status):
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def do_GET(self):
        self.reply(200 if self.path == '/health' and self.server.policy.healthy() else 503)

    def do_POST(self):
        if self.path != '/auth':
            self.reply(404)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Transfer-Encoding') or not 0 < size <= 16384:
                self.reply(400)
                return
            request = json.loads(self.rfile.read(size))
        except (ValueError, OSError):
            self.reply(400)
            return
        self.reply(200 if self.server.policy.authorize(request) else 403)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/config/mediamtx.yml')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9000)
    args = parser.parse_args()
    server = AdmissionServer((args.host, args.port), Policy(args.config))
    print('CialloChat admission service ready; request logging disabled', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
