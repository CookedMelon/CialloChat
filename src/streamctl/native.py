"""Native systemd deployment: media, admission, bounded buffering and control."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
import urllib.request

from .config import ROOT, VERSION, atomic_write

UNITS = ('ciallochat-auth', 'ciallochat-mediamtx', 'ciallochat-watchdog')


def manager(settings=None):
    return ['systemctl', '--user'] if (settings or {}).get('systemd_scope') == 'user' else ['systemctl']


def units(settings):
    result = list(UNITS)
    if settings.get('rtsp_buffer_ms'):
        result.append('ciallochat-buffer')
    if settings.get('control_enabled', False):
        result.append('ciallochat-control')
    return result


def run(args, capture=True):
    result = subprocess.run(args, capture_output=capture, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('systemd 操作失败，请检查服务日志')
    return result.stdout if capture else ''


def running(settings=None):
    return subprocess.run(manager(settings)+['is-active', '--quiet', UNITS[1]],
                          capture_output=True, timeout=10).returncode == 0


def binary():
    path = ROOT/'runtime/bin/mediamtx'
    if not path.is_file():
        raise ValueError('缺少 runtime/bin/mediamtx；请安装锁定版本的原生可执行文件')
    version = subprocess.check_output([str(path), '--version'], text=True, timeout=5).strip().lstrip('v')
    if version != VERSION['mediamtx']:
        raise ValueError('原生 MediaMTX 版本与版本锁不一致')
    return path


def compose(service, args, capture=True):
    settings = service.settings
    if args[:1] == ['ps']:
        return 'systemd\n' if running(settings) else ''
    if args == ['config', '--quiet']:
        return run([str(binary()), '--validate-conf='+str(service.store.path/'mediamtx/mediamtx.yml')])
    if args == ['restart', 'mediamtx']:
        return run(manager(settings)+['restart', UNITS[1]])
    if args[:1] == ['logs']:
        cmd = ['journalctl', '--no-pager']
        if settings.get('systemd_scope') == 'user':
            cmd.append('--user')
        for unit in units(settings):
            cmd += ['-u', unit]
        if '--tail' in args:
            cmd += ['-n', args[args.index('--tail')+1]]
        if '--follow' in args:
            if subprocess.call(cmd+['-f']):
                raise RuntimeError('无法读取 systemd 日志')
            return ''
        return run(cmd, capture)
    raise ValueError('systemd 后端不支持此 Compose 操作')


def wait_for(check, description):
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except OSError:
            pass
        time.sleep(.2)
    raise RuntimeError(description+'启动失败，请检查服务日志')


def unit_text(root, runtime, command, unit, settings):
    scope = settings.get('systemd_scope', 'system')
    # Authentication fails closed on HTTP failure. Wants allows admission to
    # restart after a crash without permanently stopping the other services.
    dependency = f'After={UNITS[0]}.service\nWants={UNITS[0]}.service\n' if unit != UNITS[0] else ''
    if unit in ('ciallochat-buffer', 'ciallochat-watchdog'):
        # No Requires on MediaMTX: a media restart must reconnect clients, not
        # leave the relay stopped after certificate renewal.
        dependency += f'After={UNITS[1]}.service\n'
    hardening = ('ProtectSystem=strict\nProtectHome=true\n'
                 f'ReadWritePaths={runtime}\nPrivateTmp=true\nPrivateDevices=true\n'
                 'CapabilityBoundingSet=\n') if scope == 'system' else ''
    return (f'[Unit]\nDescription=CialloChat {unit}\nAfter=network-online.target\n'
            f'Wants=network-online.target\n{dependency}\n[Service]\nType=simple\n'
            f'WorkingDirectory={root}\nEnvironment=PYTHONPATH={root}/src\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1\nEnvironment=PYTHONUNBUFFERED=1\n'
            f'ExecStart={command}\nRestart=always\nRestartSec=1\nUMask=0077\n'
            f'NoNewPrivileges=true\n{hardening}LimitCORE=0\n'
            f'\n[Install]\nWantedBy={"default" if scope == "user" else "multi-user"}.target\n')


def up(service):
    root, runtime, settings = ROOT, service.store.path, service.settings
    user = settings.get('systemd_scope') == 'user'
    if not user and os.geteuid() != 0:
        raise ValueError('系统级 systemd 部署需使用 root；本地可用 systemd_scope=user')
    if user and settings.get('native_connection_guard'):
        raise ValueError('连接防护规则需系统级部署')
    if any(any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_.-' for c in str(p))
           for p in (root, runtime)):
        raise ValueError('systemd 项目路径只支持 ASCII 字母、数字、/、_、.、-')
    media = binary()
    run([str(media), '--validate-conf='+str(runtime/'mediamtx/mediamtx.yml')])
    python = root/'.venv/bin/python'
    if settings.get('test_video_enabled'):
        from .testchannel import prepare
        if not prepare(runtime, allow_pending=True):
            print('测试视频尚未生成；普通直播先启动，测试频道等待素材。')
    secret_argument = ''
    if settings.get('rtsp_buffer_ms'):
        directory = runtime/'rtspbuffer'
        directory.mkdir(mode=0o700, exist_ok=True)
        secret = directory/'proxy-secret'
        if not secret.exists():
            atomic_write(secret, secrets.token_bytes(32))
        from .relayauth import load_secret
        load_secret(secret)
        secret_argument = f' --proxy-secret {secret}'
    commands = {
        UNITS[0]: f'{python} -m streamctl.authserver --config {runtime}/mediamtx/mediamtx.yml '
                  f'--leases {runtime}/leases/leases.sqlite3 --host 127.0.0.1 '
                  f'--port {settings.get("auth_port", 9000)}{secret_argument}',
        UNITS[1]: f'{media} {runtime}/mediamtx/mediamtx.yml',
        UNITS[2]: f'{python} -m streamctl.watchdog --config {runtime}/watchdog/config.json '
                  f'--health-file {runtime}/leases/watchdog-health',
    }
    if settings.get('rtsp_buffer_ms'):
        commands['ciallochat-buffer'] = f'{python} -m streamctl.rtspbuffer --runtime {runtime}'
    if settings.get('control_enabled'):
        from .controlprotocol import load_config
        config = load_config(runtime/'control-server.json')
        occupied = [settings[k] for k in ('rtmp_port', 'rtmps_port', 'rtsp_port', 'api_port')]
        occupied += [settings.get('auth_port', 9000)]
        if settings.get('rtsp_buffer_ms'):
            occupied.append(settings.get('rtsp_internal_port', 18554))
        if config.get('port', 15347) in occupied:
            raise ValueError('控制端口不能与其他端口重复')
        commands['ciallochat-control'] = (f'{python} -m streamctl.controlserver '
            f'--config {runtime}/control-server.json --runtime {runtime}')
    directory = Path.home()/'.config/systemd/user' if user else Path('/etc/systemd/system')
    directory.mkdir(parents=True, exist_ok=True)
    for unit, command in commands.items():
        atomic_write(directory/(unit+'.service'), unit_text(root, runtime, command, unit, settings), mode=0o644)
    if settings.get('native_connection_guard', False):
        guard = ('[Unit]\nDescription=CialloChat media connection rate limit\n'
                 'Before=ciallochat-mediamtx.service\n\n[Service]\nType=oneshot\n'
                 f'ExecStart=/bin/bash {root}/scripts/native-connection-guard.sh '
                 f'{settings["rtmps_port"]} {settings["rtsp_port"]}\nRemainAfterExit=true\n'
                 '\n[Install]\nWantedBy=multi-user.target\n')
        atomic_write(directory/'ciallochat-connection-guard.service', guard, mode=0o644)
    cmd = manager(settings)
    run(cmd+['daemon-reload'])
    try:
        if settings.get('native_connection_guard'):
            run(cmd+['enable', '--now', 'ciallochat-connection-guard'])
        # Restart on up so changed units/configurations are actually loaded.
        run(cmd+['enable', *commands])
        run(cmd+['restart', UNITS[0]])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def auth_ready():
            with opener.open(f'http://127.0.0.1:{settings.get("auth_port", 9000)}/health', timeout=1) as response:
                return response.status == 200
        wait_for(auth_ready, '鉴权服务')
        health = runtime/'leases/watchdog-health'
        health.unlink(missing_ok=True)
        run(cmd+['restart', *list(commands)[1:]])
        wait_for(lambda: health.exists() and time.time()-health.stat().st_mtime < 10, '监控服务')
        for name in commands:
            if subprocess.run(cmd+['is-active', '--quiet', name], capture_output=True).returncode:
                raise RuntimeError(name+'未运行，请检查服务日志')
        for port in ([settings['rtsp_port']] if settings.get('rtsp_buffer_ms') else []) + (
                [config.get('port', 15347)] if settings.get('control_enabled') else []):
            host = ('127.0.0.1' if settings['bind_address'] in ('0.0.0.0', '::')
                    or settings['mode'] == 'local' and not settings.get('local_network') else settings['bind_address'])
            def listening():
                with socket.create_connection((host, port), timeout=1):
                    return True
            wait_for(listening, '端口 '+str(port))
    except Exception:
        subprocess.run(cmd+['stop', *reversed(commands)], capture_output=True)
        raise


def down(settings=None):
    settings = settings or {}
    targets = units(settings)
    if settings.get('native_connection_guard'):
        targets.append('ciallochat-connection-guard')
    run(manager(settings)+['disable', '--now', *reversed(targets)])
