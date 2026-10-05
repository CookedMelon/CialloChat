#!/usr/bin/env bash
# Export source and reviewed evidence without instance credentials or tools.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec /usr/bin/python3 - "$ROOT" <<'PY'
import hashlib
import io
from pathlib import Path
import tarfile
import sys

root=Path(sys.argv[1]); output=root/'dist'
output.mkdir(exist_ok=True)
archive=output/'ciallochat-dual-key.tar.gz'
files=[]
for name in ('README.md','PROJECT_PLAN.md','setup.sh','streamctl','requirements.lock',
             'compose.yaml','compose.local.yaml','.gitignore','.dockerignore','docker','config','docs','scripts','control-scripts','src','tests','examples'):
    source=root/name
    candidates=source.rglob('*') if source.is_dir() else [source]
    files.extend(p for p in candidates if p.is_file() and not p.is_symlink() and
                 '__pycache__' not in p.parts and p.suffix not in ('.pyc','.key','.crt','.pem','.csr','.log')
                 and (p.relative_to(root).parts[0] != 'config' or p.name.endswith('.example.json')
                      or p.name in ('version.json', 'mediamtx.base.yml', 'nginx-media.conf'))
                 and not p.name.endswith('-credentials.json'))
files.sort()
manifest=''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(root))+'\n' for p in files).encode()
with tarfile.open(archive,'w:gz') as tar:
    for p in files:
        tar.add(p,arcname='ciallochat/'+str(p.relative_to(root)),recursive=False)
    entry=tarfile.TarInfo('ciallochat/SHA256SUMS'); entry.size=len(manifest); entry.mode=0o644
    tar.addfile(entry,io.BytesIO(manifest))
checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
(archive.with_suffix(archive.suffix+'.sha256')).write_text(checksum+'  '+archive.name+'\n')
print(str(archive)); print(checksum)
PY
