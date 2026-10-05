#!/usr/bin/env bash
# Offline verification ladder for BioSense-AI. No model keys, no network.
#   bash scripts/check.sh
# Rungs 1-3 are what CI runs. Rung 4 is skipped unless omnigent is installed.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"   # uv and omnigent install here

echo "=== 0. environment ==="
uv --version
uv sync --locked

echo
echo "=== 1. unit tests (software correctness) ==="
uv run --frozen python -m unittest -v 2>&1 | tail -4

echo
echo "=== 2. offline production-loop smoke (synthetic fixtures) ==="
rm -rf runs/check-prod runs/check-cart
uv run --frozen python -m biosense.production.cli demo --out runs/check-prod >/dev/null
echo "demo OK -> runs/check-prod"

echo
echo "=== 3. offline agent-driven loop smoke (synthetic CAR-T) ==="
uv run --frozen python -m biosense.production.cli demo-cart --out runs/check-cart >/dev/null
echo "demo-cart OK -> runs/check-cart"
uv run --frozen python scripts/summarize_loop.py runs/check-cart

echo
echo "=== 3b. dashboard boots and serves the artifacts read-only ==="
PORT=8787
uv run --frozen python -m biosense.production.serve --runs runs --static webapp --port "$PORT" >/tmp/biosense-serve.log 2>&1 &
SERVE_PID=$!
trap 'kill "$SERVE_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break
  sleep 0.25
done
curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && echo "  /healthz OK"
echo "  loops visible: $(curl -sf "http://127.0.0.1:$PORT/api/loops" | grep -c '"loop_id"')"
echo "  tracker page:  $(curl -so /dev/null -w '%{http_code} %{size_download}B' "http://127.0.0.1:$PORT/")"
echo "  traversal:     $(curl -so /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/../AGENTS.md") (expect 404)"
kill "$SERVE_PID" 2>/dev/null || true
trap - EXIT

echo
echo "=== 3c. launcher and boot scripts parse, and the checker answers offline ==="
for f in scripts/start_local_ai.sh scripts/check_local_ai.sh deploy/start-ai.sh; do
  bash -n "$f" && echo "  $f OK"
done
# Expected to fail: nothing is running, and saying so with the one fix is the
# behaviour under test. A zero exit here would mean it answered READY with no
# runtime, which is the only wrong answer it could give.
if ./scripts/check_local_ai.sh >/tmp/biosense-check-local.log 2>&1; then
  echo "  check_local_ai.sh reported READY (a runtime is up on this machine)"
else
  echo "  check_local_ai.sh: $(head -2 /tmp/biosense-check-local.log | tail -1)"
fi

echo
echo "=== 4. Omnigent agent-bundle validation (no session, no tokens) ==="
# Prefer the project's own extra over a global install: `uv sync --extra omnigent`
# puts both the client library and the omnigent command in .venv, so this rung
# runs for anyone who has installed it, without a tool install.
if uv run --frozen --extra omnigent python -c 'import omnigent' >/dev/null 2>&1; then
  OMNI_PY=(uv run --frozen --extra omnigent python)
  uv run --frozen --extra omnigent omnigent --version
elif command -v omnigent >/dev/null 2>&1; then
  # A global `uv tool install omnigent`: run the heredoc under that tool's own
  # interpreter, which is the only one that can import it.
  OMNI_PY=("$(head -1 "$(command -v omnigent)" | sed 's|^#!||')")
  omnigent --version
else
  echo "SKIPPED: the omnigent extra is not installed (uv sync --extra omnigent)"
  exit 0
fi
"${OMNI_PY[@]}" - <<'PY'
from pathlib import Path
from omnigent.spec import parser, validator
bad = False
for root in [Path('discovery_loop')] + sorted(Path('discovery_loop/agents').iterdir()):
    if not root.is_dir():
        continue
    spec = parser.parse(root)
    result = validator.validate(spec)
    bad = bad or bool(result.errors)
    print(f'  {spec.name:24s} errors={result.errors or "none"}')
parent = parser.parse(Path('discovery_loop'))
found = sorted(a.name for a in parent.sub_agents)
declared = sorted(parent.tools.agents)
print(f'  declared sub-agents: {declared}')
print(f'  discovered under agents/: {found}')
missing = [a for a in declared if a not in found]
if missing:
    bad = True
    print(f'  MISSING: {missing}')
raise SystemExit(1 if bad else 0)
PY
echo "bundle OK"
