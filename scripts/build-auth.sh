#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
image="$("$ROOT/.venv/bin/python" "$ROOT/src/streamctl/images.py")"
if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker build -f "$ROOT/docker/auth.Dockerfile" -t "$image" "$ROOT"
fi
