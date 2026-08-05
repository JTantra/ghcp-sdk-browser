#!/usr/bin/env bash
# Local dev runner. Creates the venv on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

[ -f .env ] && set -a && . ./.env && set +a
exec .venv/bin/python -m app.main
