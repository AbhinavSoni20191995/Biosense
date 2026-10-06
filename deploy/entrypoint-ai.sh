#!/usr/bin/env bash
# The hosted image's first process: the steps that need root, and then never again.
#
#   1. Make the mounted volume writable by the app user (a fresh platform volume
#      is often root-owned).
#   2. Only when the operator has turned the agents' OS sandbox off
#      (BIOSENSE_AGENT_SANDBOX=off — Railway, which refuses the user namespaces
#      bubblewrap needs): start the model-key proxy as its own user, `keyholder`,
#      and hand everything else a placeholder key and the proxy's address. An
#      unsandboxed agent command runs as `biosense` and can read the environment
#      of `biosense` processes; it cannot read another user's, so the real key
#      stays out of its reach.
#   3. Become `biosense` and run the boot script. Nothing after this line runs as
#      root, and with the sandbox off nothing running as `biosense` holds the key.
set -uo pipefail

APP_ROOT="${BIOSENSE_APP_ROOT:-/app}"
LOGS="${BIOSENSE_LOG_DIR:-/tmp/biosense-logs}"
BOOT="$APP_ROOT/deploy/start-ai.sh"

if [ "$(id -u)" != 0 ]; then
  # Started as a user already (e.g. `docker run --user`): nothing to drop.
  exec "$BOOT"
fi

mkdir -p "$LOGS" "$APP_ROOT/data/runs" "$APP_ROOT/data/private"
chown biosense:biosense "$LOGS" "$APP_ROOT/data" "$APP_ROOT/data/runs" \
  "$APP_ROOT/data/private" 2>/dev/null || true

if [ "${BIOSENSE_AGENT_SANDBOX:-on}" = 'off' ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  port="${BIOSENSE_MODEL_PROXY_PORT:-6790}"
  (
    cd "$APP_ROOT" || exit 0
    # Restarted if it ever exits; it holds the only copy of the key.
    while true; do
      setpriv --reuid=keyholder --regid=keyholder --clear-groups --inh-caps=-all \
        --no-new-privs env HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 \
        "$APP_ROOT/.venv/bin/python" -m biosense.production.model_proxy --port "$port"
      sleep 2
    done
  ) >>"$LOGS/model-proxy.log" 2>&1 &
  export ANTHROPIC_BASE_URL="http://127.0.0.1:${port}"
  export ANTHROPIC_API_KEY='held-by-the-biosense-model-proxy'
  export BIOSENSE_MODEL_PROXY=on
  printf '[boot] agents sandbox OFF by operator choice; the model key is held by a separate\n'
  printf '[boot] process (user keyholder) the agents cannot read; they reach it at %s\n' \
    "$ANTHROPIC_BASE_URL"
fi

exec setpriv --reuid=biosense --regid=biosense --init-groups --inh-caps=-all --no-new-privs \
  "$BOOT"
