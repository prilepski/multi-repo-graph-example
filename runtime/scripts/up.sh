#!/bin/sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
# Bootstrap shim: find a Python 3.11+; ALL real logic is in up.py.
set -e
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
  echo "Python 3.11+ required — install from https://www.python.org/downloads/" >&2
  exit 1
fi
exec "$PY" "$(dirname "$0")/up.py" "$@"
