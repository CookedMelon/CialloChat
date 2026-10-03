import os
import platform
import re
import shutil
import socket
import subprocess
from .config import check_tls


def doctor(store, validation=False):
    results = []
    def add(name, ok, detail):
        results.append(dict(check=name, passed=ok, detail=detail))
    release = {}
    for line in open('/etc/os-release'):
        if '=' in line:
            k, v = line.strip().split('=', 1)
            release[k] = v.strip('"')
    add('OS', release.get('ID') == 'ubuntu' and release.get('VERSION_ID') in ('24.04', '26.04'), release.get('PRETTY_NAME', 'unknown'))
    add('architecture', platform.machine() in ('x86_64', 'aarch64'), platform.machine())
    add('environment', True, 'WSL' if 'microsoft' in platform.release().lower() else 'native Linux')
    add('system Python', __import__('sys').version_info >= (3,12), __import__('sys').executable)
    for cmd in (['docker', 'info'], ['docker', 'compose', 'version']):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            ok = p.returncode == 0
            if ok and cmd[1:] == ['compose','version']:
                match = re.search(r'v?(\d+)\.(\d+)\.(\d+)',p.stdout)
                ok = bool(match) and tuple(map(int,match.groups())) >= (2,24,4)
            add(' '.join(cmd), ok, p.stdout.strip()[:100] if ok else (p.stderr or p.stdout).strip()[:500])
        except (OSError, subprocess.TimeoutExpired) as exc:
            add(' '.join(cmd), False, str(exc))
    if validation:
        for command in ('ffmpeg', 'ffprobe'):
            add(command, bool(shutil.which(command)), shutil.which(command) or 'missing; install with --with-validation')
    try:
        settings, accounts, control = store.load()
        add('accounts/settings', True, f'{len(accounts["users"])} users; mode={settings["mode"]}')
        for name in ('accounts.json', 'settings.json', 'control.json', 'mediamtx/mediamtx.yml'):
            path = store.path / name
            add('permissions ' + name, path.is_file() and not path.stat().st_mode & 0o077, 'require 600')
        add('runtime permissions', not store.path.stat().st_mode & 0o077, 'require 700')
        from .service import Service
        service = Service(store, settings, control)
        if service.running():
            add('ports', True, 'service running; media availability requires smoke test')
        else:
            check_ports(settings)
            add('ports', True, 'configured ports available for startup')
        if settings['mode'] == 'production':
            check_tls(store, settings)
            add('TLS', True, 'certificate/key/hostname/validity/chain valid')
    except Exception as exc:
        add('configuration', False, str(exc))
    return results


def check_ports(settings):
    host = '127.0.0.1' if settings['mode'] == 'local' else settings['bind_address']
    keys = ['rtsp_port', 'api_port', 'rtmp_port' if settings['mode'] == 'local' else 'rtmps_port']
    for key in keys:
        address = '127.0.0.1' if key == 'api_port' else host
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            try:
                # Match server restart semantics: TIME_WAIT is not a listener.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((address, settings[key]))
                sock.listen(1)
            except OSError as exc:
                raise ValueError(f'端口不可绑定 {address}:{settings[key]}: {exc}') from exc
