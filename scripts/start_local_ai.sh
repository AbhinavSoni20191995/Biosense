#!/usr/bin/env bash
# One command to a working real-AI BioSense on this machine.
#
# Everything the long way round in docs/RUN_ON_YOUR_PC.md section B — install the
# client extra, start an Omnigent server, register the agent, register this
# machine as a host, point BioSense at it with the right workspace — happens here
# in the right order, and each step says what it found rather than assuming.
#
#   ./scripts/start_local_ai.sh          start everything, then BioSense
#   ./scripts/start_local_ai.sh stop     stop the runtime this script started
#   ./scripts/check_local_ai.sh          is it ready? one line, and the one fix
#
# Three rules it keeps:
#
#   Loopback only. The Omnigent server is bound to 127.0.0.1 and BioSense is
#   started in `local` mode, which refuses any server that is not this machine.
#   This script can therefore never attach your laptop to a production runtime.
#
#   Reuse what is healthy. A server or host already up and answering is used as
#   it is. Nothing is restarted to make the script's own bookkeeping tidier.
#
#   No silent synthetic. If the runtime cannot be made ready, this exits with the
#   reason and the fix. It never starts BioSense in a state where pressing the
#   button quietly produces a deterministic demonstration instead.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$PWD"
STATE="$ROOT/.local-ai"
OMNI_PORT="${BIOSENSE_OMNIGENT_PORT:-6767}"
SERVER="http://127.0.0.1:${OMNI_PORT}"
APP_PORT="${BIOSENSE_PORT:-8000}"
RUNS="${BIOSENSE_RUNS:-$ROOT/runs}"
UV_RUN=(uv run --frozen --extra omnigent)

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$1" >&2; shift; for l in "$@"; do printf '  %s\n' "$l" >&2; done; exit 1; }

http_ok() { curl -fsS --max-time 4 "$1" >/dev/null 2>&1; }
pid_live() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

stop_all() {
  bold 'Stopping the local Omnigent runtime'
  for name in host server; do
    p="$STATE/$name.pid"
    if pid_live "$p"; then
      kill "$(cat "$p")" 2>/dev/null || true
      ok "$name stopped (pid $(cat "$p"))"
    else
      say "$name was not running from this script"
    fi
    rm -f "$p"
  done
  say "BioSense itself runs in the foreground; Ctrl-C stops it."
  exit 0
}
[ "${1:-}" = "stop" ] && stop_all

mkdir -p "$STATE" "$RUNS"

bold 'BioSense — real AI on this machine'
printf '\n'

# ── 1. the tools ────────────────────────────────────────────────────────────
bold '1 · dependencies'
command -v uv >/dev/null 2>&1 || die 'uv is not installed.' \
  'curl -LsSf https://astral.sh/uv/install.sh | sh' \
  'then reopen the terminal, or: export PATH="$HOME/.local/bin:$PATH"'
command -v curl >/dev/null 2>&1 || die 'curl is not installed.' 'Install curl and run this again.'
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# The extra carries both the client library BioSense imports and the `omnigent`
# command itself, so nothing has to be installed by hand and nothing global is
# touched: it all lands in this project's .venv.
say 'syncing the omnigent extra into .venv (first run downloads it)…'
uv sync --locked --extra omnigent >/dev/null 2>&1 || die \
  'the omnigent extra could not be installed.' \
  'Run it directly to see why: uv sync --locked --extra omnigent'
"${UV_RUN[@]}" python -c 'import omnigent_client' 2>/dev/null \
  || die 'the omnigent client library still does not import.' \
         'uv sync --locked --extra omnigent'
ok "omnigent $("${UV_RUN[@]}" omnigent --version 2>/dev/null | awk '{print $2}') and the client library"

# ── 2. model credentials ────────────────────────────────────────────────────
bold '2 · model credentials'
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  ok 'ANTHROPIC_API_KEY is set in this shell (never printed, never logged)'
elif command -v claude >/dev/null 2>&1; then
  warn 'no ANTHROPIC_API_KEY; the Claude CLI is installed, so a subscription login may serve'
  say  'if the runtime reports no model credentials: claude auth login --claudeai'
else
  warn 'no model credentials found in this shell'
  say  'export ANTHROPIC_API_KEY=sk-...   (or install the Claude CLI and: claude auth login)'
  say  'the runtime will start either way, and will say so if the key is what is missing'
fi

# The agents' model. Claude Code reads ANTHROPIC_MODEL, and Omnigent passes it
# through to every agent. Claude Opus 5.5's broader safety classifiers can flag
# biology-research-adjacent work and stop a run ("safeguards flagged this
# session ... [bio]"); its own message says to change the model, so the default
# here is Claude Opus 5. Set BIOSENSE_AGENT_MODEL to choose another, or an
# explicit ANTHROPIC_MODEL to override both.
BIOSENSE_AGENT_MODEL="${BIOSENSE_AGENT_MODEL-claude-opus-5}"
if [ -n "${BIOSENSE_AGENT_MODEL:-}" ] && [ -z "${ANTHROPIC_MODEL:-}" ]; then
  export ANTHROPIC_MODEL="$BIOSENSE_AGENT_MODEL"
fi
ok "agents' model: ${ANTHROPIC_MODEL:-the Claude Code default}"
# Per-agent choices: a faster model for the specialists (the biggest time lever
# a run has), the orchestrator on ANTHROPIC_MODEL unless told otherwise. They
# are written into a copy of the bundle under the state dir; the source is
# never edited, and the self-check validates the copy that is registered.
AGENT_BUNDLE="$ROOT/discovery_loop"
if [ -n "${BIOSENSE_SPECIALIST_MODEL:-}${BIOSENSE_SPECIALIST_EFFORT:-}${BIOSENSE_ORCHESTRATOR_MODEL:-}" ]; then
  if "${UV_RUN[@]}" python -m biosense.production.bundle --src "$ROOT/discovery_loop" --dst "$STATE/agents" \
       ${BIOSENSE_SPECIALIST_MODEL:+--specialist-model "$BIOSENSE_SPECIALIST_MODEL"} \
       ${BIOSENSE_SPECIALIST_EFFORT:+--specialist-effort "$BIOSENSE_SPECIALIST_EFFORT"} \
       ${BIOSENSE_ORCHESTRATOR_MODEL:+--orchestrator-model "$BIOSENSE_ORCHESTRATOR_MODEL"}; then
    AGENT_BUNDLE="$STATE/agents"
    ok "specialists on ${BIOSENSE_SPECIALIST_MODEL:-$ANTHROPIC_MODEL}${BIOSENSE_SPECIALIST_EFFORT:+ (effort $BIOSENSE_SPECIALIST_EFFORT)}; orchestrator on ${BIOSENSE_ORCHESTRATOR_MODEL:-$ANTHROPIC_MODEL}"
  else
    die 'the per-agent model choice was refused (above); fix the variable or unset it'
  fi
fi
export BIOSENSE_AGENT_BUNDLE="$AGENT_BUNDLE"

# ── 3. the Omnigent server ──────────────────────────────────────────────────
bold "3 · omnigent server on ${SERVER}"
if http_ok "$SERVER/health"; then
  ok 'already up and answering; reusing it'
else
  if lsof -nP -iTCP:"$OMNI_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port ${OMNI_PORT} is in use by something that is not answering /health." \
        "Stop it, or choose another port: BIOSENSE_OMNIGENT_PORT=6868 $0"
  fi
  say "starting it, with the discovery_loop agent registered at boot…"
  # Registered from source with --agent, so the agent BioSense asks for exists
  # before the first request rather than being typed in by hand afterwards.
  OMNIGENT_LOCAL_SINGLE_USER=1 nohup "${UV_RUN[@]}" omnigent server \
    --host 127.0.0.1 --port "$OMNI_PORT" --no-open \
    --agent "$AGENT_BUNDLE" \
    >"$STATE/server.log" 2>&1 &
  echo $! >"$STATE/server.pid"
  for _ in $(seq 1 90); do http_ok "$SERVER/health" && break; sleep 1; done
  http_ok "$SERVER/health" || die 'the Omnigent server did not come up.' \
    "Its log is $STATE/server.log" "$(tail -n 3 "$STATE/server.log" 2>/dev/null || true)"
  ok "started (pid $(cat "$STATE/server.pid")), log: .local-ai/server.log"
fi

# ── 4. this machine as a host ───────────────────────────────────────────────
bold '4 · this machine as an executor'
# A server is not a runner. `omnigent host` registers this machine; the server
# launches a runner when a session needs one, with this directory as the
# workspace — which is what makes runs/ the directory BioSense reads.
if pid_live "$STATE/host.pid"; then
  ok "host daemon already running from this script (pid $(cat "$STATE/host.pid"))"
else
  say 'registering as a host…'
  nohup "${UV_RUN[@]}" omnigent host --server "$SERVER" --no-open --non-interactive \
    >"$STATE/host.log" 2>&1 &
  echo $! >"$STATE/host.pid"
  sleep 2
  pid_live "$STATE/host.pid" || die 'the host daemon exited immediately.' \
    "Its log is $STATE/host.log" "$(tail -n 5 "$STATE/host.log" 2>/dev/null || true)"
  ok "started (pid $(cat "$STATE/host.pid")), log: .local-ai/host.log"
fi

# ── 5. BioSense's own verdict ───────────────────────────────────────────────
bold '5 · readiness, as BioSense itself judges it'
# The acceptance condition is not "the processes are up" but "BioSense can start
# a session", so the check is BioSense's own probe rather than a second opinion.
export BIOSENSE_RUNTIME_MODE=local
export BIOSENSE_ALLOWED_RUNTIMES="${BIOSENSE_ALLOWED_RUNTIMES:-synthetic,local}"
export BIOSENSE_OMNIGENT_SERVER="$SERVER"
export BIOSENSE_OMNIGENT_WORKSPACE="$ROOT"
READY=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if out="$("${UV_RUN[@]}" python scripts/_probe_runtime.py --runs "$RUNS" 2>&1)"; then
    READY=1; break
  fi
  [ "$attempt" = 1 ] && say 'waiting for the host to come online…'
  sleep 3
done
if [ "$READY" != 1 ]; then
  printf '\n'
  printf '%s\n' "$out" >&2
  die 'the real-AI runtime is not ready, so BioSense is not being started.' \
      'Nothing was downgraded: a synthetic run is a different claim, and this' \
      'script will not start one while you asked for real AI.' \
      "Logs: $STATE/server.log and $STATE/host.log"
fi
printf '%s\n' "$out"

# ── 6. BioSense ─────────────────────────────────────────────────────────────
bold "6 · BioSense on http://127.0.0.1:${APP_PORT}"
say 'runtime: REAL AI — LOCAL     runs: '"${RUNS#$ROOT/}"
say 'Ctrl-C stops BioSense; ./scripts/start_local_ai.sh stop stops the runtime.'
printf '\n'
exec "${UV_RUN[@]}" python -m biosense.production.app \
  --runs "$RUNS" --static webapp --host 127.0.0.1 --port "$APP_PORT"
