#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -n "${CODEX_TRACE_PYTHON:-}" ]; then
  exec "$CODEX_TRACE_PYTHON" -B "$HERE/install.py" "$@"
fi
for PYTHON in python3.14 python3 python; do
  if command -v "$PYTHON" >/dev/null 2>&1 && "$PYTHON" -I -c 'import sys; from compression import zstd; assert sys.version_info >= (3,14)' >/dev/null 2>&1; then
    exec "$PYTHON" -B "$HERE/install.py" "$@"
  fi
done
printf '%s\n' '需要 Python 3.14+。请从 https://www.python.org/downloads/ 安装，或设置 CODEX_TRACE_PYTHON 为解释器路径。' >&2
exit 1
