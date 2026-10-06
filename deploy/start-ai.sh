#!/usr/bin/env bash
# Boot the hosted BioSense: Omnigent server, an executor, then the web app.
#
# The order matters and the failure behaviour matters more.
#
# The web app is started LAST and in the foreground, and it is started whether or
# not the runtime came up. That is deliberate: if the runtime is broken, the
# public URL must still serve, and BioSense must say which part is missing in
# words a visitor can act on — not disappear into a restart loop, and not quietly
# answer with a deterministic demonstration while wearing a real badge. The
# substitution does not exist in the code; this script must not reintroduce it by
# making the container's health depend on the runtime.
#
# The two runtime processes are supervised: if either exits, it is restarted, up
# to a bounded number of times, and each restart is logged with the count. If the
# web app exits, this script exits, and the platform restarts the container.
set -uo pipefail

# /app in the image. Overridable so the boot sequence can be exercised from a
# checkout without building the image.
APP_ROOT="${BIOSENSE_APP_ROOT:-/app}"
cd "$APP_ROOT"

# The image starts in deploy/entrypoint-ai.sh, which does the root-only steps and
# then runs this script as the app user. A platform "start command" that names
# this script directly would skip them, so as root this hands over to it first.
if [ "$(id -u)" = 0 ] && [ -z "${BIOSENSE_ENTRYPOINT_DONE:-}" ] \
    && [ -x "$APP_ROOT/deploy/entrypoint-ai.sh" ]; then
  exec "$APP_ROOT/deploy/entrypoint-ai.sh"
fi
if [ -z "${BIOSENSE_ENTRYPOINT_DONE:-}" ] && [ "${BIOSENSE_AGENT_SANDBOX:-on}" = 'off' ]; then
  export BIOSENSE_MODEL_PROXY_WHY="${BIOSENSE_MODEL_PROXY_WHY:-entrypoint_skipped}"
fi
PORT="${PORT:-8000}"
OMNI_PORT="${BIOSENSE_OMNIGENT_PORT:-6767}"
SERVER="http://127.0.0.1:${OMNI_PORT}"
RUNS="${RUNS_DIR:-/app/runs}"
LOGS="${BIOSENSE_LOG_DIR:-/tmp/biosense-logs}"
MAX_RESTARTS="${BIOSENSE_RUNTIME_MAX_RESTARTS:-20}"
READY_WAIT_S="${BIOSENSE_RUNTIME_READY_WAIT_S:-90}"
UV=(uv run --frozen ${BIOSENSE_UV_EXTRA:---no-sync})

log() { printf '[boot] %s\n' "$*"; }

mkdir -p "$LOGS" || true
if ! mkdir -p "$RUNS" 2>/dev/null; then
  log "WARNING: ${RUNS} is not writable. Artifacts cannot be kept and real runs"
  log "         will refuse. On Railway, mount the volume at /app/runs."
fi

# Model credentials are never printed, and their absence is never fatal: the
# runtime reports `model_auth_missing` with its own remedy, which is a better
# failure than a container that will not start.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  log 'model credentials: ANTHROPIC_API_KEY present'
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  log 'model credentials: CLAUDE_CODE_OAUTH_TOKEN present'
else
  log 'model credentials: NONE. Real AI runs will refuse with model_auth_missing;'
  log '                   the deterministic demonstration path still works.'
fi

# The agents' model. Claude Code reads ANTHROPIC_MODEL, and Omnigent passes it
# through to every agent. Claude Opus 5.5's broader safety classifiers can flag
# biology-research-adjacent work and stop a run ("safeguards flagged this
# session ... [bio]"); its own message says to change the model, so the default
# here is Claude Opus 5. Set BIOSENSE_AGENT_MODEL to choose another, or an
# explicit ANTHROPIC_MODEL to override both.
if [ -n "${BIOSENSE_AGENT_MODEL:-}" ] && [ -z "${ANTHROPIC_MODEL:-}" ]; then
  export ANTHROPIC_MODEL="$BIOSENSE_AGENT_MODEL"
fi
log "agents' model: ${ANTHROPIC_MODEL:-the Claude Code default}"

# The agent bundle Omnigent registers. Normally the one in the image, untouched.
AGENT_BUNDLE="$APP_ROOT/discovery_loop"

# The agents' OS sandbox, decided before Omnigent starts, because the runner reads
# its environment once. biosense.production.sandbox runs the exact bwrap command
# Omnigent would build and prints the variables that make it work here — today,
# the /proc bind a container needs when a fresh procfs mount is refused. If no
# form of the sandbox can start, it says why and real runs are refused — unless
# the operator has turned the sandbox off for this deployment (below).
if [ "${BIOSENSE_AGENT_SANDBOX:-on}" = 'off' ]; then
  # The operator turned the sandbox off for this deployment (a host that refuses
  # user namespaces, e.g. Railway). The image's bundle is not edited; a copy with
  # the sandbox type set to none is registered instead, and the runtime card and
  # the system check say so. The model key is not in this process's environment:
  # deploy/entrypoint-ai.sh gave it to a proxy running as another user.
  AGENT_BUNDLE="/tmp/biosense-agents/discovery_loop"
  rm -rf /tmp/biosense-agents
  mkdir -p /tmp/biosense-agents
  cp -r "$APP_ROOT/discovery_loop" "$AGENT_BUNDLE"
  # Unsandboxed, a command cannot be kept off the network, and Omnigent refuses
  # to pretend: `type: none` with `allow_network: false` rejects every command
  # ("sandbox type 'none' cannot restrict network"). So the copy says what is
  # true — network allowed — rather than claim a restriction nothing enforces.
  find "$AGENT_BUNDLE" -name config.yaml -exec sed -i \
    -e 's/^\([[:space:]]*type:[[:space:]]*\)auto[[:space:]]*$/\1none/' \
    -e 's/^\([[:space:]]*allow_network:[[:space:]]*\)false[[:space:]]*$/\1true/' {} +
  log 'sandbox: OFF by operator choice (BIOSENSE_AGENT_SANDBOX=off). Agent commands run'
  log '         unconfined in this container; the model key is held by a separate process.'
  if [ "${BIOSENSE_MODEL_PROXY:-}" != 'on' ]; then
    log "WARNING: the model-credential proxy is not running (${BIOSENSE_MODEL_PROXY_WHY:-unknown});"
    log '         the runtime card says why and what fixes it.'
  fi
else
  # Its output is NAME=VALUE lines; only the two names below are ever exported,
  # and nothing is evaluated as shell.
  while IFS='=' read -r name value; do
    case "$name" in
      OMNIGENT_HOST_SANDBOX_BACKEND|OMNIGENT_RUNNER_ENV_PASSTHROUGH)
        export "$name=$value"
        log "sandbox: ${name}=${value}" ;;
    esac
  done < <("${UV[@]}" python -m biosense.production.sandbox --env 2>"$LOGS/sandbox.log")
  sed 's/^/[boot] /' "$LOGS/sandbox.log" 2>/dev/null
fi

# The registered bundle is always a copy, written by biosense.production.bundle:
#
#   * every agent's working directory pinned to the workspace ($APP_ROOT).
#     Omnigent starts an agent whose cwd is `.` in a per-session scratch
#     directory when the runner's workspace does not reach that session, and
#     specialists kept failing on ".venv/bin/python: No such file or directory".
#     An absolute cwd is kept as written; telling the agents to `cd` was not
#     enough, so the configuration says it instead.
#   * the operator's per-agent model choices, when set: the specialists search,
#     read and extract, and a faster model there is the biggest time lever a run
#     has; the orchestrator keeps ANTHROPIC_MODEL unless told otherwise.
#
# The image's own bundle is never edited. If the copy cannot be written, the
# bundle above is registered unchanged and the log says why.
if [ "$AGENT_BUNDLE" = "$APP_ROOT/discovery_loop" ]; then
  AGENT_BUNDLE="/tmp/biosense-agents/discovery_loop"
  rm -rf /tmp/biosense-agents
  mkdir -p /tmp/biosense-agents
  cp -r "$APP_ROOT/discovery_loop" "$AGENT_BUNDLE"
fi
if "${UV[@]}" python -m biosense.production.bundle --src "$AGENT_BUNDLE" --dst "$AGENT_BUNDLE.tmp" \
     --cwd "$APP_ROOT" \
     ${BIOSENSE_SPECIALIST_MODEL:+--specialist-model "$BIOSENSE_SPECIALIST_MODEL"} \
     ${BIOSENSE_SPECIALIST_EFFORT:+--specialist-effort "$BIOSENSE_SPECIALIST_EFFORT"} \
     ${BIOSENSE_ORCHESTRATOR_MODEL:+--orchestrator-model "$BIOSENSE_ORCHESTRATOR_MODEL"} \
     >"$LOGS/bundle.log" 2>&1; then
  rm -rf "$AGENT_BUNDLE" && mv "$AGENT_BUNDLE.tmp" "$AGENT_BUNDLE"
  sed 's/^/[boot] /' "$LOGS/bundle.log"
else
  sed 's/^/[boot] /' "$LOGS/bundle.log"
  log 'WARNING: the agent bundle copy was refused (above); the bundle is registered unchanged'
  rm -rf "$AGENT_BUNDLE.tmp"
fi

# The self-check validates the bundle Omnigent actually registers.
export BIOSENSE_AGENT_BUNDLE="$AGENT_BUNDLE"

server_up() { python -c "
import sys,urllib.request
try:
    urllib.request.urlopen('${SERVER}/health', timeout=4)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1; }

start_server() {
  # Reuse a server that is already answering rather than starting a second one
  # that cannot bind the port. Without this, a supervisor restart after the app
  # process is replaced turns into a loop of failed binds.
  if server_up; then
    log 'omnigent server already answering /health; reusing it'
    SERVER_PID=''
    return
  fi
  # --agent registers the discovery_loop bundle from source at boot, so the agent
  # BioSense asks for exists before the first request. Bound to loopback: nothing
  # outside the container can reach the runtime, which is why it needs no login.
  "${UV[@]}" omnigent server \
    --host 127.0.0.1 --port "$OMNI_PORT" --no-open \
    --agent "$AGENT_BUNDLE" \
    >>"$LOGS/omnigent-server.log" 2>&1 &
  SERVER_PID=$!
  log "omnigent server starting (pid $SERVER_PID) on $SERVER"
}

start_host() {
  # A server is not an executor. This registers the container as a host; the
  # server launches a runner inside it when a session is created, with /app as
  # the workspace — which is what makes /app/runs the directory BioSense reads.
  "${UV[@]}" omnigent host --server "$SERVER" --no-open --non-interactive \
    >>"$LOGS/omnigent-host.log" 2>&1 &
  HOST_PID=$!
  log "omnigent host starting (pid $HOST_PID)"
}

stop_all() {
  trap - TERM INT
  log 'shutting down'
  for pid in "${HOST_PID:-}" "${SERVER_PID:-}" "${APP_PID:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  sleep 1
  for pid in "${HOST_PID:-}" "${SERVER_PID:-}"; do
    [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
  done
  wait 2>/dev/null
  exit 0
}
trap stop_all TERM INT

start_server
for _ in $(seq 1 60); do server_up && break; sleep 1; done
if server_up; then
  log 'omnigent server is answering /health'
else
  log 'WARNING: the omnigent server did not answer in 60s; see omnigent-server.log'
  tail -n 5 "$LOGS/omnigent-server.log" 2>/dev/null | sed 's/^/[omnigent] /'
fi
start_host

# BioSense's own verdict, logged once at boot so a deploy's logs say plainly
# whether the service can do what it offers. It is advisory here: /readyz answers
# the same question live, and the app serves either way.
(
  deadline=$(( $(date +%s) + READY_WAIT_S ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if out="$("${UV[@]}" python scripts/_probe_runtime.py --runs "$RUNS" 2>&1)"; then
      log "runtime READY — real AI can run"
      exit 0
    fi
    sleep 5
  done
  log 'runtime NOT READY after boot wait. The app is serving; /readyz says why:'
  printf '%s\n' "$out" | sed 's/^/[boot] /'
) &

# The offline system check, once per boot, in the log: does a prompt have every
# command, tool, builder and file it needs to become a protocol? It uses synthetic
# fixtures and no model. The live variant is on the Runs page, for operators.
(
  sleep 20
  "${UV[@]}" python -m biosense.production.selfcheck --runs "$RUNS" 2>&1 | sed 's/^/[selfcheck] /'
) &

# The supervisor. It restarts the runtime processes only; the web app is the
# container's own lifetime.
(
  restarts=0
  give_up() {
    log 'runtime restart limit reached. The app keeps serving and /readyz reports why;'
    log 'the deterministic demonstration path is unaffected.'
    exit 0
  }
  while true; do
    sleep 10
    # The server is judged by its health endpoint rather than by its pid, so a
    # hung process counts as down and a reused one counts as up.
    if ! server_up; then
      restarts=$((restarts + 1))
      [ "$restarts" -gt "$MAX_RESTARTS" ] && give_up
      log "omnigent server is not answering; restarting (${restarts}/${MAX_RESTARTS})"
      tail -n 5 "$LOGS/omnigent-server.log" 2>/dev/null | sed 's/^/[omnigent] /'
      [ -n "${SERVER_PID:-}" ] && kill -9 "$SERVER_PID" 2>/dev/null
      [ -n "${HOST_PID:-}" ] && kill "$HOST_PID" 2>/dev/null
      HOST_PID=''
      start_server
      for _ in $(seq 1 60); do server_up && break; sleep 1; done
      start_host
      continue
    fi
    if [ -n "${HOST_PID:-}" ] && ! kill -0 "$HOST_PID" 2>/dev/null; then
      restarts=$((restarts + 1))
      [ "$restarts" -gt "$MAX_RESTARTS" ] && give_up
      log "omnigent host exited; restarting (${restarts}/${MAX_RESTARTS})"
      tail -n 5 "$LOGS/omnigent-host.log" 2>/dev/null | sed 's/^/[omnigent] /'
      start_host
    fi
  done
) &

log "BioSense app on 0.0.0.0:${PORT} (runs: ${RUNS})"
"${UV[@]}" python -m biosense.production.app \
  --runs "$RUNS" --static webapp --host 0.0.0.0 --port "$PORT" &
APP_PID=$!
wait "$APP_PID"
code=$?
log "the BioSense app exited with ${code}; stopping the runtime so the platform can restart cleanly"
stop_all
