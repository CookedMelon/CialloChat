"""TLS-only control listener. Invalid authentication receives no JSON reply."""
import argparse
import hashlib
import re
import ipaddress
from pathlib import Path
import socket
import socketserver
import ssl
import threading
import time

from .controladmin import Administration
from .controlprotocol import encode, load_config, MAX_REQUEST, receive, verify_request


class ControlServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, config, administration):
        self.config = Path(config)
        self.administration = administration
        self.slots = threading.BoundedSemaphore(4)
        self.rates = {}
        self.rates_lock = threading.Lock()
        if ':' in address[0]: self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        now = time.monotonic()
        with self.rates_lock:
            self.rates = {ip: times for ip, times in self.rates.items() if times[-1] > now-60}
            times = [t for t in self.rates.get(client_address[0], []) if t > now-60]
            allowed = len(times) < 30 and (client_address[0] in self.rates or len(self.rates) < 4096)
            if allowed:
                self.rates[client_address[0]] = times + [now]
        if not allowed or not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Never log request bodies, password hashes, credentials or traceback.
        print('control connection failed; details omitted', flush=True)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            config = load_config(self.server.config)
            settings = self.server.administration.store.read('settings.json')
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            runtime = self.server.administration.store.path
            # Reload the cert for every connection so renewals need no restart.
            context.load_cert_chain(runtime/settings['certificate'], runtime/settings['private_key'])
            self.request.settimeout(4)
            with context.wrap_socket(self.request, server_side=True) as tls:
                request = receive(tls, MAX_REQUEST)
                if not verify_request(config['password'], request): return
                with self.server.administration.leases.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    now = time.time()
                    db.execute('DELETE FROM control_nonces WHERE expires<?', (now,))
                    if db.execute('SELECT 1 FROM control_nonces WHERE nonce=?', (request['nonce'],)).fetchone():
                        return
                    db.execute('INSERT INTO control_nonces VALUES (?,?)',
                               (request['nonce'], request['timestamp']+61))
                tls.settimeout(90)
                try:
                    command = request['command']
                    if command and command[0] == 'traffic-mail':
                        if len(command) != 3 or not re.fullmatch(r'[0-9a-f]{32}', command[1]):
                            raise ValueError('非法报告上传')
                        match = re.fullmatch(r'([0-9]{1,6}):([0-9a-f]{64})', command[2])
                        if not match or not 33 <= int(match[1]) <= 256*1024:
                            raise ValueError('报告图表超出上限')
                        # Do not accept a large body until authentication and
                        # replay checks pass. Existing 8 KiB frame bound stays.
                        tls.sendall(encode(dict(ok=True, upload=True))+b'\n')
                        tls.settimeout(5)
                        body = bytearray()
                        deadline = time.monotonic()+5
                        size = int(match[1])
                        while len(body) < size:
                            tls.settimeout(max(0.01, deadline-time.monotonic()))
                            part = tls.recv(min(16384, size-len(body)))
                            if not part or time.monotonic() > deadline:
                                raise ConnectionError('报告上传中断')
                            body.extend(part)
                        if hashlib.sha256(body).hexdigest() != match[2]:
                            raise ValueError('报告图表校验失败')
                        tls.settimeout(90)
                        response = self.server.administration.traffic_reports.upload(command[1], bytes(body))
                    else:
                        response = self.server.administration.command(command)
                except ValueError as exc:
                    response = dict(ok=False, error=str(exc))
                except Exception:
                    response = dict(ok=False, error='操作未完成，请检查服务器状态；不要盲目重复变更命令')
                    print('control operation failed; details omitted', flush=True)
                tls.sendall(encode(response) + b'\n')
        except (OSError, ValueError, KeyError, ConnectionError):
            return  # Includes TLS errors, oversized frames and malformed JSON.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--runtime', required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    bind = config.get('bind', '0.0.0.0')
    ipaddress.ip_address(bind)
    admin = Administration(args.runtime)
    settings = admin.store.read('settings.json')
    ports = [settings[k] for k in ('rtmp_port', 'rtmps_port', 'rtsp_port', 'api_port')] + [settings.get('auth_port', 9000)]
    if settings.get('rtsp_buffer_ms'):
        ports.append(settings.get('rtsp_internal_port', 18554))
    if config.get('port', 15347) in ports:
        raise ValueError('控制端口不能与已有服务端口重复')
    def worker():
        while True:
            for retry in (admin.retry_notifications, admin.traffic_reports.retry):
                try:
                    retry()
                except Exception:
                    print('control outbox retry failed; details omitted', flush=True)
            time.sleep(5)
    with ControlServer((bind, config.get('port', 15347)), args.config, admin) as server:
        threading.Thread(target=worker, daemon=True).start()
        print('CialloChat TLS control listener ready', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
