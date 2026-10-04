#!/usr/bin/env bash
# StitchBitch starten. Beim ersten Aufruf wird die venv angelegt und die
# Abhaengigkeiten werden installiert; danach wird direkt gestartet.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Richte venv ein ..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python stitchbitch.py "$@"
