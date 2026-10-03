#!/usr/bin/env bash
# User-invoked reference player; does not change OBS/VLC or system settings.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
EXTRACTED="$ROOT/runtime/tools/ffmpeg-root"
if [[ -x "$EXTRACTED/usr/bin/ffplay" ]]; then
  PLAYER="$EXTRACTED/usr/bin/ffplay"
  export LD_LIBRARY_PATH="$EXTRACTED/usr/lib/x86_64-linux-gnu:$EXTRACTED/usr/lib/x86_64-linux-gnu/pulseaudio:$EXTRACTED/usr/lib/x86_64-linux-gnu/samba${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
elif command -v ffplay >/dev/null 2>&1; then
  PLAYER="$(command -v ffplay)"
else
  echo '找不到 FFplay；此脚本不自动安装系统软件。' >&2
  exit 1
fi
if [[ "${1:-}" == --check ]]; then
  "$PLAYER" -version >/dev/null
  echo 'FFplay 可执行文件与动态依赖可用；尚未启动图形窗口。'
  exit 0
fi
if [[ $# != 1 || "$1" != rtsp://* ]]; then
  echo '用法：./scripts/play-low-latency.sh rtsp://127.0.0.1:18554/live/obscheck' >&2
  exit 2
fi
exec "$PLAYER" -hide_banner -loglevel warning \
  -rtsp_transport tcp -fflags nobuffer -flags low_delay \
  -probesize 32768 -analyzeduration 100000 -threads 1 -framedrop \
  -window_title 'CialloChat 低缓冲对照（按 Q 退出）' "$1"
