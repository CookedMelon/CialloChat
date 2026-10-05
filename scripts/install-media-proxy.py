#!/usr/bin/env python3
"""Install media-only Nginx routing on the existing native production instance.

Install distro nginx and libnginx-mod-stream first. This script never downloads
dependencies, uploads media, changes credentials, or provisions website content.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from streamctl.config import ROOT, Store, atomic_write, check_tls, dump, render, validate_settings
from streamctl.native import binary
from streamctl.service import Service, recover


def main():
    if os.geteuid() != 0:
        raise SystemExit('请以 root 执行')
    if not shutil.which('nginx'):
        raise SystemExit('请先安装 nginx 和 libnginx-mod-stream')
    store = Store()
    with store.lock():
        recover(store)
        old, accounts, control = store.load()
        if old['mode'] != 'production' or old.get('service_backend') != 'systemd':
            raise SystemExit('仅支持现有 production/systemd 实例')
        settings = dict(old, reverse_proxy_enabled=True, hostname='chat.v50to.cc',
                        read_hostname='watch.v50to.cc', public_rtmps_port=443, public_rtsp_port=554)
        validate_settings(settings)
        check_tls(store, settings)
        nginx_conf = Path('/etc/nginx/nginx.conf')
        routes = Path('/etc/nginx/stream-conf.d/ciallochat.conf')
        default_site = Path('/etc/nginx/sites-enabled/default')
        original_nginx = nginx_conf.read_text()
        marker = '# CialloChat media stream routes'
        if re.search(r'^\s*stream\s*\{', original_nginx, re.M) and marker not in original_nginx:
            raise SystemExit('检测到已有 stream 配置，请先合并媒体入口，避免覆盖现有代理')
        if default_site.exists() and (not default_site.is_symlink()
                or default_site.resolve() != Path('/etc/nginx/sites-available/default')):
            raise SystemExit('检测到自定义 default 网站，请先确认其 80 端口与证书续期的配置')
        backup = ROOT.parent/'ciallochat-backups'/f'media-proxy-{time.time_ns()}'
        backup.mkdir(parents=True, mode=0o700)
        paths = [store.path/'settings.json', store.path/'mediamtx/mediamtx.yml',
                 store.path/'watchdog/config.json', nginx_conf, routes]
        originals = {path: path.read_bytes() if path.exists() else None for path in paths}
        for index, (path, data) in enumerate(originals.items()):
            if data is not None:
                atomic_write(backup/f'{index}-{path.name}', data)
        original_link = os.readlink(default_site) if default_site.is_symlink() else None
        atomic_write(backup/'manifest.json', dump(dict(paths=[str(path) for path in paths],
                                                      default_site=original_link)))
        generated = render(settings, accounts, control)
        staged = backup/'staged-mediamtx.yml'
        atomic_write(staged, generated)
        subprocess.run([str(binary()), '--validate-conf=' + str(staged)], check=True, capture_output=True)
        active = subprocess.run(['systemctl', 'is-active', '--quiet', 'nginx']).returncode == 0
        was_running = Service(store, old, control).running()
        stopped = False
        try:
            atomic_write(routes, (ROOT/'config/nginx-media.conf').read_bytes(), mode=0o644)
            if marker not in original_nginx:
                original_nginx += '\n' + marker + '\nstream {\n    include /etc/nginx/stream-conf.d/*.conf;\n}\n'
            atomic_write(nginx_conf, original_nginx, mode=0o644)
            if original_link is not None:
                default_site.unlink()
            subprocess.run(['nginx', '-t'], check=True, capture_output=True)
            if was_running:
                Service(store, old, control).down()
                stopped = True
            atomic_write(store.path/'settings.json', dump(settings))
            atomic_write(store.path/'mediamtx/mediamtx.yml', generated)
            Service(store, settings, control).up()
            subprocess.run(['systemctl', 'enable', 'nginx'], check=True, capture_output=True)
            subprocess.run(['systemctl', 'reload-or-restart', 'nginx'], check=True, capture_output=True)
        except Exception:
            if stopped:
                Service(store, settings, control).down()
            for path, data in originals.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data, mode=0o644 if path == nginx_conf or path == routes else 0o600)
            if original_link is not None and not default_site.is_symlink():
                default_site.symlink_to(original_link)
            if was_running and stopped:
                Service(store, old, control).up()
            subprocess.run(['systemctl', 'reload-or-restart' if active else 'stop', 'nginx'], check=True)
            raise
        print('已部署：rtmps://chat.v50to.cc 与 rtsp://watch.v50to.cc；后端 1936/8554 仅监听本机。')
        print('配置备份：' + str(backup))


if __name__ == '__main__':
    main()
