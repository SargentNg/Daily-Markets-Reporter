#!/usr/bin/env bash
# Cron wrapper for stock_news.py.
# Absolute python path + cd so it is immune to cron's minimal environment.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "run.sh: python3 not found in PATH" >&2
  exit 1
fi

exec "$PYTHON" "$DIR/stock_news.py" "$@"