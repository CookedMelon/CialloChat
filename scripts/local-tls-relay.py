#!/usr/bin/env python3
"""Local, authenticated SOCKS relay for CialloChat TLS connections.

Only the first TLS handshake record is split. TLS, certificate verification,
credentials and all subsequent traffic remain between client and server.
The optional Mihomo integration applies two narrow rules in memory and reapplies
them after the application reloads its configuration; subscription files and
the application's database are never modified.
"""
import argparse
import asyncio
import hmac
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import socket
import ssl
import urllib.parse
import urllib.request


LOG = logging.getLogger('ciallochat-tls-relay')
NAME = 'CialloChat-TLS'


def split_record(record, size=100):
    if not 1 <= size <= 16384:
        raise ValueError('invalid fragment size')
    if (len(record) < 6 or record[0] != 22 or record[1] != 3
            or record[5] != 1 or not 1 <= int.from_bytes(record[3:5], 'big') <= 16384
            or len(record) != 5 + int.from_bytes(record[3:5], 'big')):
        raise ValueError('expected initial TLS ClientHello record')
    body = record[5:]
    return b''.join(record[:3] + len(body[k:k+size]).to_bytes(2, 'big')
                    + body[k:k+size] for k in range(0, len(body), size))


def insert_yaml_items(text, key, items):
    match = re.search(r'(?m)^' + re.escape(key) + r':\s*\n', text)
    if match is None:
        raise ValueError('missing proxy configuration section')
    tail = text[match.end():]
    first = re.match(r'([ ]*)- ', tail)
    if first is None:
        raise ValueError('unsupported proxy configuration list')
    additions = ''.join(first[1] + '- ' + json.dumps(item, ensure_ascii=False)
                        + '\n' for item in items)
    return text[:match.end()] + additions + tail


def runtime_tun(text, tun_enabled):
    section = re.search(r'(?ms)^tun:\n.*?(?=^[^ \n]|\Z)', text)
    if section is None:
        raise ValueError('missing TUN configuration')
    updated, count = re.subn(r'(?m)^( +enable:) *(?:true|false) *$',
                            r'\g<1> ' + str(tun_enabled).lower(), section[0])
    if count != 1:
        raise ValueError('invalid TUN configuration')
    return text[:section.start()] + updated + text[section.end():]


def patched_config(text, settings, tun_enabled):
    proxy = dict(name=NAME, type='socks5', server='127.0.0.1',
                 port=settings['listen_port'], username=settings['username'],
                 password=settings['password'], udp=False)
    text = insert_yaml_items(text, 'proxies', [proxy])
    domain = settings['server_name']
    rules = [f'AND,((DOMAIN,{domain}),(DST-PORT,{port})),{NAME}'
             for port in settings['allowed_ports']]
    # The relay's own upstream sockets must bypass the relay.
    rules.append(f"IP-CIDR,{settings['upstream_ip']}/32,DIRECT,no-resolve")
    text = insert_yaml_items(text, 'rules', rules)
    return runtime_tun(text, tun_enabled)


def api(settings, path, method='GET', body=None):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = urllib.request.Request(settings['controller'].rstrip('/') + path,
                                     data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        data = response.read(2 * 1024 * 1024)
        return json.loads(data) if data else None


def routes_installed(settings, rules):
    expected = settings['allowed_ports']
    return len(rules) >= len(expected) and all(
            rules[i].get('proxy') == NAME
            and settings['server_name'] in rules[i].get('payload', '')
            and str(port) in rules[i].get('payload', '')
            for i, port in enumerate(expected))


def ensure_routes(settings, restore=False, force=False):
    rules = api(settings, '/rules')['rules']
    if not restore and not force and routes_installed(settings, rules):
        return False
    live = api(settings, '/configs')
    selectors = {name: value['now'] for name, value in
                 api(settings, '/proxies')['proxies'].items()
                 if value.get('type') == 'Selector' and value.get('now')}
    text = Path(settings['mihomo_config']).read_text(encoding='utf-8')
    payload = (runtime_tun(text, live['tun']['enable']) if restore else
               patched_config(text, settings, live['tun']['enable']))
    api(settings, '/configs', 'PUT', {'payload': payload})
    for name, selected in selectors.items():
        api(settings, '/proxies/' + urllib.parse.quote(name, safe=''),
            'PUT', {'name': selected})
    return True


def receive_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        data = sock.recv(count - len(result))
        if not data:
            raise ConnectionError('relay closed')
        result.extend(data)
    return bytes(result)


def check_connection(settings):
    checks = []
    def add(name, ok, detail):
        checks.append(dict(name=name, ok=ok, detail=detail))
    try:
        live = api(settings, '/configs')
        add('TUN', bool(live['tun']['enable']), '已开启' if live['tun']['enable'] else '请在 FLYCLOUD 中开启 TUN')
        add('routing_mode', live['mode'] == 'rule', '规则模式' if live['mode'] == 'rule' else '请在 FLYCLOUD 中切换为规则模式')
        installed = routes_installed(settings, api(settings, '/rules')['rules'])
        add('routes', installed, '服务路由已安装' if installed else '服务路由缺失；运行修复入口')
    except Exception:
        add('controller', False, '无法连接本机代理控制接口；请启动 FLYCLOUD')
    try:
        with socket.create_connection(('127.0.0.1', settings['listen_port']), timeout=3) as sock:
            sock.sendall(b'\x05\x01\x02')
            if receive_exact(sock, 2) != b'\x05\x02':
                raise ConnectionError('invalid relay greeting')
            user, password = settings['username'].encode(), settings['password'].encode()
            sock.sendall(b'\x01' + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if receive_exact(sock, 2) != b'\x01\x00':
                raise ConnectionError('relay authentication failed')
        add('relay', True, '本机转发运行正常')
    except Exception:
        add('relay', False, '本机转发未启动或配置不匹配；运行修复入口')
    # Avoid sending a known failing ClientHello if routing isn't ready.
    if all(check['ok'] for check in checks):
        for port in settings['allowed_ports']:
            try:
                with socket.create_connection((settings['server_name'], port), timeout=6) as sock:
                    with ssl.create_default_context().wrap_socket(sock, server_hostname=settings['server_name']):
                        pass
                add(f'TLS_{port}', True, '入口可连接，证书校验通过')
            except ssl.SSLCertVerificationError:
                add(f'TLS_{port}', False, '证书校验失败；检查系统时间、域名及服务器证书')
            except Exception:
                add(f'TLS_{port}', False, '本机转发已就绪，但入口连接失败；需要检查网络或服务器')
    return dict(ok=all(check['ok'] for check in checks), checks=checks)


async def watch_routes(settings):
    failed = False
    while True:
        try:
            if await asyncio.to_thread(ensure_routes, settings):
                LOG.info('Applied CialloChat TLS routes; original proxy selections retained')
            failed = False
        except Exception as error:
            if not failed:
                LOG.warning('Proxy controller unavailable: %s', type(error).__name__)
            failed = True
        await asyncio.sleep(5)


class Relay:
    def __init__(self, settings):
        self.settings = settings
        self.active = 0

    async def pipe(self, reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def connect(self, reader, writer):
        version, count = await reader.readexactly(2)
        if version != 5 or not 1 <= count <= 16:
            raise ValueError('invalid SOCKS greeting')
        methods = await reader.readexactly(count)
        if 2 not in methods:
            writer.write(b'\x05\xff'); await writer.drain()
            raise ValueError('authentication required')
        writer.write(b'\x05\x02'); await writer.drain()
        version, length = await reader.readexactly(2)
        username = await reader.readexactly(length)
        length = (await reader.readexactly(1))[0]
        password = await reader.readexactly(length)
        authenticated = (hmac.compare_digest(username, self.settings['username'].encode())
                         & hmac.compare_digest(password, self.settings['password'].encode()))
        if version != 1 or not authenticated:
            writer.write(b'\x01\x01'); await writer.drain()
            raise ValueError('authentication failed')
        writer.write(b'\x01\x00'); await writer.drain()
        version, command, reserved, kind = await reader.readexactly(4)
        if kind == 3:
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode('ascii').rstrip('.').lower()
        elif kind == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        else:
            raise ValueError('unsupported address')
        port = int.from_bytes(await reader.readexactly(2), 'big')
        if (version != 5 or command != 1 or reserved != 0
                or host not in [self.settings['server_name'], self.settings['upstream_ip']]
                or port not in self.settings['allowed_ports']):
            writer.write(b'\x05\x02\x00\x01' + b'\x00' * 6)
            await writer.drain()
            raise ValueError('destination denied')
        remote_reader, remote_writer = await asyncio.open_connection(
            self.settings['upstream_ip'], port)
        try:
            writer.write(b'\x05\x00\x00\x01' + b'\x00' * 6)
            await writer.drain()
            header = await reader.readexactly(5)
            length = int.from_bytes(header[3:5], 'big')
            if header[0] != 22 or header[1] != 3 or not 1 <= length <= 16384:
                raise ValueError('invalid TLS record')
            body = await reader.readexactly(length)
            remote_writer.write(split_record(header + body))
            await remote_writer.drain()
            return remote_reader, remote_writer, port
        except BaseException:
            remote_writer.close()
            await remote_writer.wait_closed()
            raise

    async def handle(self, reader, writer):
        if self.active >= 32:
            writer.close(); await writer.wait_closed(); return
        self.active += 1
        remote_writer = None
        tasks = []
        try:
            remote_reader, remote_writer, port = await asyncio.wait_for(
                self.connect(reader, writer), timeout=15)
            LOG.info('TLS connection forwarded on port %d', port)
            tasks = [asyncio.create_task(self.pipe(reader, remote_writer)),
                     asyncio.create_task(self.pipe(remote_reader, writer))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ValueError, asyncio.IncompleteReadError, TimeoutError) as error:
            LOG.info('Connection ended: %s', type(error).__name__)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for target in (remote_writer, writer):
                if target is not None:
                    target.close()
                    try:
                        await target.wait_closed()
                    except OSError:
                        pass
            self.active -= 1


async def run(settings):
    relay = Relay(settings)
    server = await asyncio.start_server(relay.handle, '127.0.0.1', settings['listen_port'])
    Path(settings['pid_file']).write_text(str(os.getpid()), encoding='ascii')
    LOG.info('Local TLS compatibility relay started on loopback port %d', settings['listen_port'])
    watcher = asyncio.create_task(watch_routes(settings)) if settings.get('controller') else None
    try:
        async with server:
            await server.serve_forever()
    finally:
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--restore-routes', action='store_true')
    action.add_argument('--repair-routes', action='store_true')
    action.add_argument('--check', action='store_true')
    args = parser.parse_args()
    settings = json.loads(args.config.read_text(encoding='utf-8'))
    handler = RotatingFileHandler(args.config.parent / 'relay.log',
                                  maxBytes=1024 * 1024, backupCount=1, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler); LOG.setLevel(logging.INFO)
    if args.restore_routes:
        ensure_routes(settings, restore=True)
        return
    if args.repair_routes:
        ensure_routes(settings, force=True)
        return
    if args.check:
        result = check_connection(settings)
        print(json.dumps(result))
        raise SystemExit(0 if result['ok'] else 1)
    asyncio.run(run(settings))


if __name__ == '__main__':
    main()
