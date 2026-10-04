#!/usr/bin/env python3
"""Deploy the complete native service on the local LAN; never touches a remote host."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from streamctl.config import Store, atomic_write, dump, render, write_watchdog_config
from streamctl.controladmin import Administration, validate_email
from streamctl.controlprotocol import load_config
from streamctl.leases import validate_mail
from streamctl.service import Service


def local_ip():
    data = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'dev', 'eth0']))
    return next(a['local'] for link in data for a in link['addr_info'] if a['scope'] == 'global')


def certificate(store, host):
    directory = store.path/'certs'
    cert, key, ca = (directory/name for name in ('server.crt', 'server.key', 'ca.crt'))
    if cert.exists() or key.exists():
        if not all(p.exists() for p in (cert, key, ca)):
            raise ValueError('本地证书不完整；请备份并清理 runtime/certs 后重试')
        subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkend', '86400'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkip', host],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    def openssl(*args):
        subprocess.run(['openssl', *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650',
            '-keyout', str(directory/'ca.key'), '-out', str(ca), '-subj', '/CN=CialloChat Local CA',
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
            '-out', str(directory/'server.csr'), '-subj', '/CN=CialloChat Local Control')
    extensions = directory/'extensions.cnf'
    atomic_write(extensions, f'subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{host}\n'
        'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
        'extendedKeyUsage=serverAuth\n')
    openssl('x509', '-req', '-in', str(directory/'server.csr'), '-CA', str(ca),
            '-CAkey', str(directory/'ca.key'), '-CAcreateserial', '-out', str(cert),
            '-days', '365', '-extfile', str(extensions))
    for name in ('server.csr', 'extensions.cnf', 'ca.srl'):
        (directory/name).unlink(missing_ok=True)
    for path in directory.iterdir():
        if path.is_file(): path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', help='内网 IPv4，默认 eth0 地址')
    parser.add_argument('--runtime', type=Path, default=ROOT/'runtime')
    parser.add_argument('--user', default='cc', help='首次创建的本地用户；已有用户不刷新')
    parser.add_argument('--email', help='默认从 config/smtp.json 的 recipients 中读取')
    parser.add_argument('--test-video', action='store_true', help='启用已生成的十分钟测试视频')
    args = parser.parse_args()
    host = args.host or local_ip()
    if not ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).version != 4:
        raise ValueError('本地部署请使用内网 IPv4')
    os.umask(0o077)
    smtp_path = ROOT/'config/smtp.json'
    if smtp_path.stat().st_mode & 0o077:
        raise ValueError('config/smtp.json 权限须为 600')
    mail = validate_mail(json.loads(smtp_path.read_text()))
    email = validate_email(args.email or mail['recipients'].get(args.user))
    control = load_config(ROOT/'config/control-server.json')
    store = Store(args.runtime)
    store.initialize()
    previous, accounts, internal = store.load()
    settings = dict(previous, service_backend='systemd', systemd_scope='user', mode='local',
        local_network=True, hostname=host, bind_address=host, native_runtime=str(store.path),
        rtmp_port=1935, rtmps_port=1936, rtsp_port=8554, rtsp_internal_port=18554,
        api_port=9997, auth_port=9000, rtsp_buffer_ms=1000, control_enabled=True,
        native_connection_guard=False, publish_limit_kbps=4000, traffic_enabled=True)
    settings['test_video_enabled'] = args.test_video or previous.get('test_video_enabled', False)
    if settings['test_video_enabled']:
        from streamctl.testchannel import prepare
        prepare(store.path)
    if previous != settings and Service(store, previous, internal).running():
        raise ValueError('已有服务正在运行；请先 ./streamctl down，再重新部署本地服务')
    if accounts['users'] and previous.get('service_backend') != 'systemd':
        raise ValueError('已有其他后端的账号，请先完成备份与迁移；本脚本不覆盖账号')
    binary = ROOT/'runtime/bin/mediamtx'
    if not binary.exists():
        cached = ROOT/'runtime/tools/mediamtx'
        if not cached.exists():
            raise ValueError('请将锁定版本 MediaMTX 原生程序放到 runtime/bin/mediamtx')
        binary.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(cached, binary); binary.chmod(0o700)
    certificate(store, host)
    atomic_write(store.path/'settings.json', dump(settings))
    atomic_write(store.path/'mediamtx/mediamtx.yml', render(settings, accounts, internal))
    atomic_write(store.path/'notifications/smtp.json', dump(mail))
    atomic_write(store.path/'control-server.json', dump(dict(control, bind=host)))
    write_watchdog_config(store, settings, internal)
    client_path = ROOT/'config/control-client.json'
    client = dict(host=host, port=control.get('port', 15347), password=control['password'],
                  ca_file=str(store.path/'certs/ca.crt'))
    if client_path.exists() and load_config(client_path) != client:
        atomic_write(store.path/'backups'/f'control-client-{time.time_ns()}.json', client_path.read_bytes())
    atomic_write(client_path, dump(client))
    Service(store, settings, internal).up()
    admin = Administration(store.path)
    if not any(u['username'] == args.user for u in accounts['users']):
        result = admin.command(['add', args.user, email])
        print('本地用户已创建；邮件状态：'+result['email_status'])
    else:
        print('保留已有用户、密码及租约。')
    subprocess.run(['bash', str(ROOT/'control-scripts/install-path.sh')], check=True)
    print(f'完整本地服务已启动：RTMP 1935，带鉴权的一秒缓冲 RTSP 8554，TLS 控制 {client["port"]}。')
    print(f'执行 cialloctl info {args.user} 获取当前 OBS 推流 URL 和播放器输入 URL。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('本地部署失败：'+str(exc), file=sys.stderr)
        sys.exit(1)
