#!/usr/bin/env sh
# project-memory CLI launcher for POSIX (macOS / Linux): pm [--config CONFIG] <command> ...
# 也可以始终用 "sh ./pm ..." 调用，不依赖可执行位。
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
PYTHONUTF8=1
export PYTHONUTF8
PYTHONIOENCODING=utf-8
export PYTHONIOENCODING
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" -m project_memory "$@"
