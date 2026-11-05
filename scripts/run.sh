#!/usr/bin/env bash
# Web arayüzünü başlatır (gerekirse önce kurulum yapar).  Ek argümanlar `localasistan ui`ya iletilir:
#   ./scripts/run.sh --port 9000 --no-browser
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  ./scripts/setup.sh
fi
exec .venv/bin/python -m localasistan ui "$@"
