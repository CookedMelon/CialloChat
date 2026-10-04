#!/usr/bin/env python3
"""Exercise real TLS control and MediaMTX reloads in an isolated runtime.

Only short JSON messages and localhost connections; notification delivery is
captured, never sent. Existing production accounts and systemd units are untouched.
"""
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from streamctl.config import Store, atomic_write, dump, render, write_watchdog_config
from streamctl.controladmin import Administration
from streamctl.controlclient import call
from streamctl.controlprotocol import encode, sign_request
from streamctl.controlserver import ControlServer
from streamctl.accounts import new_password
from streamctl.service import Service


def main():
    source = Store()
    original_settings, _, _ = source.load()
    media = ROOT/'runtime/bin/mediamtx'
    if not media.exists(): media = ROOT/'runtime/tools/mediamtx'
    processes, holders = [], []
    server = None
    report = dict(result='failed', real_tls=True, real_mediamtx_reload=True,
                  network='localhost only', actual_emails_sent=0, tests=[])
    with tempfile.TemporaryDirectory(prefix='ciallochat-control-check-') as directory:
        store = Store(directory); store.initialize()
        settings, accounts, control = store.load()
        settings.update(service_backend='systemd', mode='production', bind_address='127.0.0.1',
                        hostname=original_settings['hostname'], native_runtime=directory)
        for key in ('rtmp_port', 'rtmps_port', 'rtsp_port', 'api_port', 'auth_port'):
            sock = socket.socket(); sock.bind(('127.0.0.1', 0))
            settings[key] = sock.getsockname()[1]; holders.append(sock)
        for key in ('certificate', 'private_key'):
            atomic_write(store.path/settings[key], (source.path/original_settings[key]).read_bytes())
        atomic_write(store.path/'settings.json', dump(settings))
        atomic_write(store.path/'mediamtx/mediamtx.yml', render(settings, accounts, control))
        write_watchdog_config(store, settings, control)
        atomic_write(store.path/'notifications/smtp.json', dump(dict(
            host='smtp.invalid', port=465, security='ssl',
            **{'from': 'capture@example.com', 'recipients': {'check': 'capture@example.com'}})))
        password = new_password()+new_password()
        config = store.path/'control-server.json'
        atomic_write(config, dump(dict(password=password)))
        try:
            for sock in holders: sock.close()
            env = dict(__import__('os').environ, PYTHONPATH=str(ROOT/'src'), PYTHONDONTWRITEBYTECODE='1')
            with open(store.path/'process.log', 'wb') as log:
                processes.append(subprocess.Popen([sys.executable, '-m', 'streamctl.authserver',
                    '--config', str(store.path/'mediamtx/mediamtx.yml'), '--leases', str(store.path/'leases/leases.sqlite3'),
                    '--host', '127.0.0.1', '--port', str(settings['auth_port'])], env=env, stdout=log, stderr=log))
                processes.append(subprocess.Popen([str(media), str(store.path/'mediamtx/mediamtx.yml')],
                                                  stdout=log, stderr=log))
            service = Service(store, settings, control)
            deadline = time.monotonic()+10
            while True:
                try:
                    service.api('config/global/get'); break
                except OSError:
                    if time.monotonic()>deadline: raise RuntimeError('isolated MediaMTX startup failed')
                    time.sleep(.1)
            admin = Administration(directory)
            server = ControlServer(('127.0.0.1', 0), config, admin)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            client = dict(host='127.0.0.1', server_name=settings['hostname'],
                          port=server.server_address[1], password=password)
            ca = source.path/'certs/ca.crt'
            if ca.exists(): client['ca_file'] = str(ca)
            # Runtime health is supplied by the actual subprocess above, not
            # by the host's production systemd unit.
            with patch('streamctl.service.Service.running', return_value=True), \
                    patch('streamctl.controladmin.send_account_notice') as sender:
                created = call(client, ['add', 'controlcheck', 'capture@example.com'])
                assert created['email_status']=='sent', 'notification capture failed'
                before = call(client, ['list'])['users'][0]
                assert before['push_password'] and before['pull_password'], 'missing keys'
                assert service.api('config/paths/list')['items'][0]['name']=='live/controlcheck', 'path not loaded'
                report['tests'].append('create and list loaded by real MediaMTX')
                call(client, ['refresh', 'controlcheck', 'push'])
                pushed = call(client, ['list'])['users'][0]
                assert pushed['push_password']!=before['push_password'], 'push key not changed'
                assert pushed['pull_password']==before['pull_password'], 'pull key changed'
                assert pushed['push_seconds_remaining']>=7195, 'usage allowance not reset'
                report['tests'].append('push refresh and two-hour reset')
                usage_before = admin.leases.statuses()[0]['used_seconds']
                call(client, ['refresh', 'controlcheck', 'pull'])
                pulled = call(client, ['list'])['users'][0]
                assert pulled['push_password']==pushed['push_password'], 'push key changed'
                assert pulled['pull_password']!=pushed['pull_password'], 'pull key not changed'
                assert admin.leases.statuses()[0]['used_seconds']==usage_before, 'push usage changed'
                report['tests'].append('pull refresh preserved push usage')
                call(client, ['refresh', 'controlcheck', 'all'])
                final = call(client, ['list'])['users'][0]
                assert final['push_password']!=pulled['push_password'] and final['pull_password']!=pulled['pull_password'], 'all refresh failed'
                assert sender.call_count==4, 'creation/refresh notifications missing'
                assert all(c.args[5].startswith('rtsp://') for c in sender.call_args_list), 'wrong notification scheme'
                report['tests'].append('all refresh and four notification captures')
                call(client, ['del', 'controlcheck'])
                assert call(client, ['list'])['users']==[], 'deletion failed'
                assert service.api('config/paths/list')['items']==[], 'path remained active'
                report['tests'].append('delete revoked live configuration')
                import ssl
                context = ssl.create_default_context(cafile=client.get('ca_file'))
                with socket.create_connection((client['host'], client['port']), timeout=3) as sock:
                    with context.wrap_socket(sock, server_hostname=client['server_name']) as tls:
                        tls.settimeout(3); tls.sendall(encode(sign_request('invalid', ['list']))+b'\n')
                        assert tls.recv(1)==b'', 'invalid authentication received a response'
                report['tests'].append('wrong password received zero application bytes')
                report['result']='passed'
        finally:
            if server:
                server.shutdown(); server.server_close()
            for process in processes:
                if process.poll() is None: process.terminate()
            for process in processes:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
    destination = ROOT/'runtime/reports/control-native-validation.json'
    atomic_write(destination, dump(report))
    print(json.dumps(report, ensure_ascii=False))


if __name__=='__main__':
    main()
