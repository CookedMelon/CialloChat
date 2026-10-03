#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODE=local
CHECK=0
VALIDATION=0
STAGE=arguments
trap 'echo "CialloChat setup 失败阶段: $STAGE；修复后重复同一命令，不会重置账号或证书。" >&2' ERR
while (($#)); do
  case "$1" in
    --mode) MODE="${2:?missing mode}"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --with-validation) VALIDATION=1; shift ;;
    --help) echo './setup.sh [--mode local|production] [--check] [--with-validation]'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done
[[ "$MODE" == local || "$MODE" == production ]]
STAGE=environment
source /etc/os-release
[[ "$ID" == ubuntu && ( "$VERSION_ID" == 24.04 || "$VERSION_ID" == 26.04 ) ]] || { echo '支持 Ubuntu 24.04/26.04' >&2; exit 1; }
[[ "$(uname -m)" == x86_64 || "$(uname -m)" == aarch64 ]] || { echo '支持 amd64/arm64' >&2; exit 1; }
if ((CHECK)); then
  if [[ ! -x "$ROOT/.venv/bin/python" ]]; then echo '缺少 .venv，请先初始化；--check 不安装或修改文件。' >&2; exit 1; fi
  args=(doctor)
  if ((VALIDATION)); then args+=(--with-validation); fi
  exec "$ROOT/streamctl" "${args[@]}"
fi
[[ -w "$ROOT" ]] || { echo "项目目录不可写: $ROOT；请由管理员配置目录权限。" >&2; exit 1; }
run_root() {
  if ((EUID == 0)); then "$@"; else sudo "$@"; fi
}
STAGE=system-dependencies
packages=()
for item in ca-certificates curl openssl python3 python3-venv; do
  if ! dpkg-query -W -f='${Status}' "$item" 2>/dev/null | grep -q '^install ok installed$'; then packages+=("$item"); fi
done
if ((VALIDATION)) && ! command -v ffmpeg >/dev/null; then packages+=(ffmpeg); fi
if ((${#packages[@]})); then
  run_root apt-get update
  run_root apt-get install -y "${packages[@]}"
fi
STAGE=docker
if ! docker_message="$(timeout 15 docker info 2>&1)"; then
  if [[ "$docker_message" == *"permission denied"* ]]; then
    echo 'Docker socket 访问被拒绝。请由管理员确认 docker 组权限；已入组但旧会话未生效时，在自己的终端执行 newgrp docker 或重新登录。setup 不修改用户组或 socket 权限。' >&2
    exit 1
  fi
  if [[ "$(uname -r)" == *microsoft* || "$(uname -r)" == *Microsoft* ]]; then
    echo 'WSL 的 Docker 不可用：请在 Docker Desktop Settings → Resources → WSL Integration 启用本发行版后重试。' >&2
    exit 1
  fi
  if command -v docker >/dev/null || dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -q '^install ok installed$'; then
    echo '已有 Docker，但 daemon 或权限不可用；请检查 systemctl status docker / docker info，不自动替换安装。' >&2
    exit 1
  fi
  conflicts=()
  for item in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$item" 2>/dev/null | grep -q '^install ok installed$'; then conflicts+=("$item"); fi
  done
  if ((${#conflicts[@]})); then echo "Docker 冲突包: ${conflicts[*]}；请管理员处理，不自动卸载。" >&2; exit 1; fi
  run_root install -m 0755 -d /etc/apt/keyrings
  run_root /usr/bin/curl -fsSL --connect-timeout 10 --max-time 60 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  run_root chmod 0644 /etc/apt/keyrings/docker.asc
  tmp_source="$(mktemp)"
  cat > "$tmp_source" <<SOURCE
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
SOURCE
  run_root install -m 0644 "$tmp_source" /etc/apt/sources.list.d/docker.sources
  rm -f "$tmp_source"
  run_root apt-get update
  run_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  run_root systemctl enable --now docker
  # Do not add the current user to docker group implicitly.
  docker info >/dev/null || { echo 'Docker 已安装；请用同一管理员身份重跑 setup（例如 sudo ./setup.sh）。' >&2; exit 1; }
fi
docker compose version >/dev/null
# !override requires Compose >= 2.24.4.
compose_version="$(docker compose version --short | sed 's/^v//')"
/usr/bin/python3 - "$compose_version" <<'PY'
import re,sys
v=tuple(map(int,re.match(r'(\d+)\.(\d+)\.(\d+)',sys.argv[1]).groups()))
if v < (2,24,4): raise SystemExit('Docker Compose 需要 >= 2.24.4')
PY
STAGE=python-venv
cd "$ROOT"
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3,12)'
if [[ ! -x .venv/bin/python ]]; then /usr/bin/python3 -m venv .venv; fi
.venv/bin/python -c 'import sys; assert sys.version_info >= (3,12), "虚拟环境需要 Python >= 3.12"; assert sys.prefix != sys.base_prefix; assert "conda" not in sys.base_prefix.lower(), "请使用系统 Python 重建 .venv"'
.venv/bin/python -m pip install -r requirements.lock
STAGE=initialization
./streamctl init --mode "$MODE"
STAGE=image
image="$(.venv/bin/python -c 'import json; print(json.load(open("config/version.json"))["image"])')"
if docker image inspect "$image" >/dev/null 2>&1; then
  printf '已存在锁定 digest 的媒体镜像，复用: %s\n' "$image"
else
  docker pull "$image"
fi
STAGE=configuration
./streamctl apply
./streamctl config-check
./streamctl doctor
printf '%s\n' '准备完成。创建账号: ./streamctl user add alice；启动: ./streamctl up；生产模式须先配置 hostname、证书与私钥。'
