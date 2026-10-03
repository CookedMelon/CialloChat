#!/usr/bin/env bash
# Certbot deploy hook. Validates new PEM files before replacing runtime files.
set -Eeuo pipefail
umask 077
ROOT="${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
: "${RENEWED_LINEAGE:?This script must receive RENEWED_LINEAGE from Certbot}"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$ROOT/.venv/bin/python" - <<'PY'
import os
from pathlib import Path
import tempfile
import time
from streamctl.config import Store, atomic_write, check_tls
from streamctl.service import Service, recover

store=Store()
lineage=Path(os.environ['RENEWED_LINEAGE'])
with store.lock():
    recover(store)
    settings,accounts,control=store.load()
    if settings['mode']!='production':
        raise SystemExit('证书部署钩子仅用于 production 实例。')
    cert=(lineage/'fullchain.pem').read_bytes()
    key=(lineage/'privkey.pem').read_bytes()
    with tempfile.TemporaryDirectory(prefix='ciallochat-cert-') as directory:
        staging=Store(directory)
        atomic_write(staging.path/settings['certificate'],cert)
        atomic_write(staging.path/settings['private_key'],key)
        check_tls(staging,settings)
    service=Service(store,settings,control)
    running=service.running()
    certificate=store.path/settings['certificate']
    private_key=store.path/settings['private_key']
    old_cert=certificate.read_bytes()
    old_key=private_key.read_bytes()
    prefix=store.path/'backups'/('certificate-'+str(time.time_ns()))
    atomic_write(prefix.with_suffix('.crt'),old_cert)
    atomic_write(prefix.with_suffix('.key'),old_key)
    try:
        atomic_write(certificate,cert)
        atomic_write(private_key,key)
        if running:
            service.reload_certificate()
    except Exception:
        atomic_write(certificate,old_cert)
        atomic_write(private_key,old_key)
        if running:
            try:
                service.reload_certificate()
            except Exception as failure:
                print('旧证书文件已恢复；服务恢复需人工检查：'+type(failure).__name__)
        raise
print('证书链、主机名和私钥校验通过；' + ('已部署并重载活动服务。' if running else '已部署，停止的服务将在下次启动时使用新证书。'))
PY
