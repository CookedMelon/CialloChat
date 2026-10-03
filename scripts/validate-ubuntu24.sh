#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${1:-}" == --container ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get -o Acquire::Retries=3 update
  apt-get -o Acquire::Retries=3 install -y --no-install-recommends ca-certificates python3 python3-venv openssl
  mkdir -p /work
  tar -C /source --exclude='./.venv' --exclude='./runtime' --exclude='./dist' --exclude='__pycache__' -cf - . | tar -C /work -xf -
  cd /work
  /usr/bin/python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.lock
  VALIDATION_USER="$(getent passwd 1000 | cut -d: -f1)"
  if [[ -z "$VALIDATION_USER" ]]; then
    useradd --create-home --uid 1000 ciallochat
    VALIDATION_USER=ciallochat
  fi
  chown -R "$VALIDATION_USER:$(id -gn "$VALIDATION_USER")" /work
  # Run as an ordinary account: CLI lock, permission and concurrency checks
  # must not get accidental root privileges inside the auxiliary container.
  runuser -u "$VALIDATION_USER" -- bash -c 'cd /work; PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v'
  bash -n setup.sh streamctl scripts/*.sh
  runuser -u "$VALIDATION_USER" -- .venv/bin/python -m compileall -q src scripts/smoke_test.py
  /usr/bin/python3 - <<'PY'
import json, os, platform, sys
from pathlib import Path
release = dict(line.strip().split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
assert release['VERSION_ID'].strip('"') == '24.04'
assert sys.version_info[:2] == (3, 12)
Path('/reports/ubuntu24-container.json').write_text(json.dumps({
    'result': 'passed', 'os': release['PRETTY_NAME'].strip('"'),
    'python': platform.python_version(), 'architecture': platform.machine(),
    'checks': ['locked dependency installation', 'management and CLI tests as uid 1000',
               'Bash syntax', 'Python compilation'],
    'scope': 'Auxiliary Ubuntu 24.04 container; not native setup/systemd/host reboot acceptance',
}, ensure_ascii=False, indent=2) + '\n')
os.chown('/reports/ubuntu24-container.json', int(os.environ['CIALLOCHAT_REPORT_UID']), int(os.environ['CIALLOCHAT_REPORT_GID']))
PY
  exit 0
fi
if (($#)); then echo '用法: ./scripts/validate-ubuntu24.sh' >&2; exit 2; fi
REPORT_DIR="$ROOT/runtime/reports"
mkdir -p "$REPORT_DIR"
CONTAINER="ciallochat-ubuntu24-$$"
cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
# The auxiliary OS tag is resolved to a digest and recorded before testing.
docker pull ubuntu:24.04 > "$REPORT_DIR/ubuntu24-image-pull.txt" 2>&1
docker image inspect ubuntu:24.04 --format '{{json .RepoDigests}}' > "$REPORT_DIR/ubuntu24-image-digests.json"
IMAGE="$(docker image inspect ubuntu:24.04 --format '{{index .RepoDigests 0}}')"
if ! docker run --name "$CONTAINER" --rm \
  -e "CIALLOCHAT_REPORT_UID=$(id -u)" -e "CIALLOCHAT_REPORT_GID=$(id -g)" \
  --mount "type=bind,src=$ROOT,dst=/source,readonly" \
  --mount "type=bind,src=$REPORT_DIR,dst=/reports" \
  "$IMAGE" bash /source/scripts/validate-ubuntu24.sh --container \
  > "$REPORT_DIR/ubuntu24-container.log" 2>&1; then
  echo "Ubuntu 24.04 辅助验证失败；完整记录: $REPORT_DIR/ubuntu24-container.log" >&2
  tail -n 30 "$REPORT_DIR/ubuntu24-container.log" >&2
  exit 1
fi
echo "Ubuntu 24.04 辅助验证通过；报告: $REPORT_DIR/ubuntu24-container.json"
