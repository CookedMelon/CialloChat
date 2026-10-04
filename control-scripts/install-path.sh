#!/usr/bin/env bash
# Keep existing InstallSpace scripts; link this folder and its command alongside them.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 - "$ROOT" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
destination = Path.home()/'InstallSpace/scripts'
destination.mkdir(parents=True, exist_ok=True)
for name, target in [('ciallochat', root/'control-scripts'),
                     ('cialloctl', root/'control-scripts/cialloctl')]:
    link = destination/name
    if link.is_symlink():
        if link.resolve() != target.resolve():
            raise SystemExit(f'已有其他软链接，请检查：{link}')
    elif link.exists():
        raise SystemExit(f'已有文件，请检查：{link}')
    else:
        link.symlink_to(target, target_is_directory=target.is_dir())
rc = Path.home()/'.bashrc'
line = 'export PATH="$HOME/InstallSpace/scripts:$PATH"'
content = rc.read_text() if rc.exists() else ''
if line not in content.splitlines():
    # Keep SDKMAN's required final block at the end, if present.
    marker = '#THIS MUST BE AT THE END OF THE FILE FOR SDKMAN TO WORK!!!'
    addition = '# CialloChat remote control\n' + line + '\n\n'
    index = content.find(marker)
    content = content[:index]+addition+content[index:] if index >= 0 else content.rstrip()+'\n\n'+addition
    rc.write_text(content)
print('已创建控制脚本软链接，并将 ~/InstallSpace/scripts 加入 ~/.bashrc。')
PY
