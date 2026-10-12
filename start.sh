#!/usr/bin/env sh
# Tracd for macOS / Linux: installs on first run, then starts.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Tracd for the first time. This takes a minute..."
  "$PY" -m venv .venv
fi
# Install only on the first run or after an update changed what Tracd needs,
# so a normal start works offline.
want=$(.venv/bin/python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest())")
if [ "$(cat .venv/tracd-installed 2>/dev/null)" != "$want" ]; then
  echo "Installing what Tracd needs..."
  .venv/bin/python -m pip install --quiet --disable-pip-version-check -e .
  echo "$want" > .venv/tracd-installed
fi
exec .venv/bin/python -m tracd "$@"
