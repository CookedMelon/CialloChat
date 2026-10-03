#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
# Keep a stable script body during a long VM run, even if the workspace changes.
if [[ "${CIALLOCHAT_VM_SCRIPT_FROZEN:-0}" != 1 ]]; then
  CIALLOCHAT_VM_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
  export CIALLOCHAT_VM_SOURCE_ROOT
  CIALLOCHAT_VM_SCRIPT_FROZEN=1 exec bash -c "$(cat -- "${BASH_SOURCE[0]}")" "$0" "$@"
fi
ROOT="$CIALLOCHAT_VM_SOURCE_ROOT"
if [[ "${1:-}" == --container ]]; then
  mkdir -p /vm
  cd /vm
  curl -fL --retry 2 --connect-timeout 15 --max-time 300 https://cloud-images.ubuntu.com/noble/current/SHA256SUMS -o SHA256SUMS
  curl -fL --retry 2 --connect-timeout 15 --max-time 600 https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img -o noble-server-cloudimg-amd64.img
  awk '$2 == "*noble-server-cloudimg-amd64.img" || $2 == "noble-server-cloudimg-amd64.img"' SHA256SUMS > selected-checksum.txt
  [[ -s selected-checksum.txt ]]
  sha256sum -c selected-checksum.txt
  ssh-keygen -q -t ed25519 -N '' -f /vm/ssh-key
  cat > meta-data <<META
instance-id: ciallochat-native24
local-hostname: ciallochat-native24
META
  cat > user-data <<USER
#cloud-config
ssh_authorized_keys:
  - $(cat /vm/ssh-key.pub)
ssh_pwauth: false
USER
  cloud-localds seed.img user-data meta-data
  qemu-img create -f qcow2 -F qcow2 -b /vm/noble-server-cloudimg-amd64.img /vm/disk.qcow2 12G
  ACCEL=tcg
  CPU=max
  if [[ -r /dev/kvm ]]; then ACCEL=kvm; CPU=host; fi
  qemu-system-x86_64 -accel "$ACCEL" -cpu "$CPU" -m 2048 -smp 2 \
    -drive file=/vm/disk.qcow2,if=virtio,format=qcow2 \
    -drive file=/vm/seed.img,format=raw,media=cdrom,readonly=on \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=net0 \
    -nographic -serial mon:stdio > /vm/serial.log 2>&1 &
  VM_PID=$!
  cleanup_vm() { kill "$VM_PID" 2>/dev/null || true; wait "$VM_PID" 2>/dev/null || true; }
  trap cleanup_vm EXIT
  SSH=(ssh -i /vm/ssh-key -p 2222 -o BatchMode=yes -o ConnectTimeout=3 \
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/vm/known-hosts ubuntu@127.0.0.1)
  wait_ssh() {
    local deadline=$((SECONDS + 180))
    while ! "${SSH[@]}" true 2>/dev/null; do
      kill -0 "$VM_PID" || { tail -n 60 /vm/serial.log; return 1; }
      if ((SECONDS >= deadline)); then tail -n 60 /vm/serial.log; return 1; fi
      sleep 2
    done
  }
  wait_ssh
  "${SSH[@]}" 'sudo cloud-init status --wait; sudo mkdir -p /opt/ciallochat'
  tar -C /source --exclude='./.venv' --exclude='./runtime' --exclude='./dist' --exclude='__pycache__' -cf - . \
    | "${SSH[@]}" 'sudo tar -C /opt/ciallochat -xf -'
  "${SSH[@]}" 'sudo bash /opt/ciallochat/scripts/validate-ubuntu24-vm.sh --guest-install'
  BOOT_BEFORE="$("${SSH[@]}" cat /proc/sys/kernel/random/boot_id)"
  "${SSH[@]}" 'sudo reboot' || true
  # Confirm a real new kernel boot, not only a restarted Docker container.
  sleep 5
  wait_ssh
  BOOT_AFTER="$("${SSH[@]}" cat /proc/sys/kernel/random/boot_id)"
  [[ "$BOOT_AFTER" != "$BOOT_BEFORE" ]]
  "${SSH[@]}" 'sudo bash /opt/ciallochat/scripts/validate-ubuntu24-vm.sh --guest-check'
  "${SSH[@]}" 'sudo cat /opt/ciallochat/runtime/reports/vm-acceptance.json' > /reports/ubuntu24-vm.json
  "${SSH[@]}" 'sudo cat /opt/ciallochat/runtime/reports/vm-smoke.json' > /reports/ubuntu24-vm-smoke.json
  /usr/bin/python3 - "$ACCEL" "$BOOT_BEFORE" "$BOOT_AFTER" <<'PY'
import json, os, sys
from pathlib import Path
p=Path('/reports/ubuntu24-vm.json')
r=json.loads(p.read_text())
r.update(acceleration=sys.argv[1], boot_id_before=sys.argv[2], boot_id_after=sys.argv[3],
         cloud_image_sha256=Path('/vm/selected-checksum.txt').read_text().split()[0])
p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
for name in ('ubuntu24-vm.json','ubuntu24-vm-smoke.json'):
    os.chown('/reports/'+name,int(os.environ['CIALLOCHAT_REPORT_UID']),int(os.environ['CIALLOCHAT_REPORT_GID']))
PY
  exit 0
fi
if [[ "${1:-}" == --guest-install ]]; then
  cd /opt/ciallochat
  ./setup.sh --mode local --with-validation
  ./streamctl user add vmprobe > runtime/reports/vmprobe-credentials.json
  ./scripts/make-test-cert.sh runtime/certs
  sha256sum runtime/settings.json runtime/accounts.json runtime/control.json runtime/certs/* > runtime/reports/initial.sha256
  ./setup.sh --mode local --with-validation
  sha256sum -c runtime/reports/initial.sha256
  ./setup.sh --check --with-validation > runtime/reports/setup-check.json
  PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
  ./streamctl up --mode local
  ./streamctl status
  systemctl is-active docker
  systemctl is-enabled docker
  cat /proc/1/comm
  exit 0
fi
if [[ "${1:-}" == --guest-check ]]; then
  cd /opt/ciallochat
  deadline=$((SECONDS + 60))
  until ./streamctl status > runtime/reports/post-reboot-status.json; do
    if ((SECONDS >= deadline)); then exit 1; fi
    sleep 2
  done
  sha256sum -c runtime/reports/initial.sha256
  systemctl is-active docker
  systemctl is-enabled docker
  ./scripts/smoke-test.sh --report runtime/reports/vm-smoke.json
  .venv/bin/python - <<'PY'
import json, platform, subprocess
from pathlib import Path
r=json.loads(Path('runtime/reports/vm-smoke.json').read_text())
assert r['result']=='passed'
assert Path('/proc/1/comm').read_text().strip()=='systemd'
Path('runtime/reports/vm-acceptance.json').write_text(json.dumps({
 'result':'passed','os':'Ubuntu 24.04 VM','python':platform.python_version(),'init':'systemd',
 'docker':subprocess.check_output(['docker','info','--format','{{.ServerVersion}}'],text=True).strip(),
 'checks':['native setup installs Docker official noble repository and validation dependencies',
           'repeat setup preserves accounts/settings/control credentials/certificates',
           'setup --check passes','management tests pass','Docker daemon active and enabled',
           'guest reboot restores managed container and API with persisted accounts',
           'isolated RTMP RTSP RTMPS media and permission smoke passes'],
 'media_tests':len(r['tests']),
},ensure_ascii=False,indent=2)+'\n')
PY
  ./streamctl down
  exit 0
fi
if (($#)); then echo '用法: ./scripts/validate-ubuntu24-vm.sh' >&2; exit 2; fi
REPORT_DIR="$ROOT/runtime/reports"
mkdir -p "$REPORT_DIR"
CONTAINER="ciallochat-native24-$$"
cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker pull ubuntu:24.04 > "$REPORT_DIR/ubuntu24-vm-image-pull.txt" 2>&1
BASE_IMAGE="$(docker image inspect ubuntu:24.04 --format '{{index .RepoDigests 0}}')"
docker build --build-arg "BASE_IMAGE=$BASE_IMAGE" -t ciallochat-validation-vm:ubuntu24 \
  -f "$ROOT/scripts/ubuntu24-vm.Dockerfile" "$ROOT/scripts" > "$REPORT_DIR/ubuntu24-vm-build.log" 2>&1
if ! docker run --name "$CONTAINER" --rm --device /dev/kvm \
  -e "CIALLOCHAT_REPORT_UID=$(id -u)" -e "CIALLOCHAT_REPORT_GID=$(id -g)" \
  --mount "type=bind,src=$ROOT,dst=/source,readonly" \
  --mount "type=bind,src=$REPORT_DIR,dst=/reports" \
  ciallochat-validation-vm:ubuntu24 bash /source/scripts/validate-ubuntu24-vm.sh --container \
  > "$REPORT_DIR/ubuntu24-vm.log" 2>&1; then
  echo "Ubuntu 24.04 VM 验证失败；完整记录: $REPORT_DIR/ubuntu24-vm.log" >&2
  tail -n 40 "$REPORT_DIR/ubuntu24-vm.log" >&2
  exit 1
fi
echo "Ubuntu 24.04 VM 验证通过；报告: $REPORT_DIR/ubuntu24-vm.json"
