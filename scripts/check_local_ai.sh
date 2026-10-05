#!/usr/bin/env bash
# Is real AI ready on this machine? One answer, and the one thing to fix.
#
#   ./scripts/check_local_ai.sh
#
# Exits 0 when a real run could start right now, 1 otherwise. It starts nothing,
# changes nothing and spends nothing: it asks the same question BioSense asks
# before it accepts a run, using the same code.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$PWD"
OMNI_PORT="${BIOSENSE_OMNIGENT_PORT:-6767}"
SERVER="${BIOSENSE_OMNIGENT_SERVER:-http://127.0.0.1:${OMNI_PORT}}"

printf '\033[1mBioSense real-AI readiness\033[0m\n'

if ! command -v uv >/dev/null 2>&1; then
  printf '\033[31m✗ NOT READY\033[0m — uv is not installed.\n'
  printf '  fix: curl -LsSf https://astral.sh/uv/install.sh | sh\n'
  exit 1
fi
if ! uv run --frozen --extra omnigent python -c 'import omnigent_client' 2>/dev/null; then
  printf '\033[31m✗ NOT READY\033[0m — the Omnigent client library is not installed.\n'
  printf '  fix: uv sync --locked --extra omnigent   (or: ./scripts/start_local_ai.sh)\n'
  exit 1
fi
if ! curl -fsS --max-time 4 "$SERVER/health" >/dev/null 2>&1; then
  printf '\033[31m✗ NOT READY\033[0m — nothing is answering at %s\n' "$SERVER"
  printf '  fix: ./scripts/start_local_ai.sh\n'
  exit 1
fi

export BIOSENSE_RUNTIME_MODE="${BIOSENSE_RUNTIME_MODE:-local}"
export BIOSENSE_OMNIGENT_SERVER="$SERVER"
export BIOSENSE_OMNIGENT_WORKSPACE="${BIOSENSE_OMNIGENT_WORKSPACE:-$ROOT}"
uv run --frozen --extra omnigent python scripts/_probe_runtime.py \
  --runs "${BIOSENSE_RUNS:-$ROOT/runs}"
code=$?
if [ "$code" = 0 ]; then
  printf '  BioSense: ./scripts/start_local_ai.sh   (if it is not already running)\n'
fi
exit "$code"
