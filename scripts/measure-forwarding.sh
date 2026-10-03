#!/usr/bin/env bash
# Optional development benchmark. Go is not a deployment dependency.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if ! command -v go >/dev/null 2>&1; then
  echo '协议层验证需要已有 Go 1.26+；本脚本不安装系统依赖，生产服务不需要 Go。' >&2
  exit 1
fi
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo '缺少项目 Python 虚拟环境，请先完成项目 setup。' >&2
  exit 1
fi
mkdir -p "$ROOT/runtime/tools" "$ROOT/runtime/protocol-cache"
export GOTOOLCHAIN=local
export GOCACHE="$ROOT/runtime/protocol-cache/go-cache"
export GOMODCACHE="$ROOT/runtime/protocol-cache/go-mod-cache"
(
  cd "$ROOT/scripts/protocol_latency"
  go build -mod=readonly -o "$ROOT/runtime/tools/protocol-latency" .
)
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/measure_forwarding.py" "$@"
