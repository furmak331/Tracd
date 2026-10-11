#!/usr/bin/env sh
# Tracd for macOS / Linux: installs on first run, then starts.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Tracd for the first time. This takes a minute..."
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --disable-pip-version-check --upgrade .
exec .venv/bin/python -m tracd "$@"
