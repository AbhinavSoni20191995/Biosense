#!/usr/bin/env bash
# The hosted image's first process: the steps that need root, and then never again.
#
#   1. Make the mounted volume writable by the app user (a fresh platform volume
#      is often root-owned).
#   2. Only when the operator has turned the agents' OS sandbox off
#      (BIOSENSE_AGENT_SANDBOX=off — Railway, which refuses the user namespaces
#      bubblewrap needs): start the model-credential proxy as its own user,
#      `keyholder`, and hand everything else a placeholder and the proxy's
#      address. An unsandboxed agent command runs as `biosense` and can read the
#      environment of `biosense` processes; it cannot read another user's, so the
#      real credential stays out of its reach. Either credential works: an API
#      key (ANTHROPIC_API_KEY) or a Claude login token (CLAUDE_CODE_OAUTH_TOKEN).
#   3. Become `biosense` and run the boot script. Nothing after this line runs as
#      root, and with the sandbox off nothing running as `biosense` holds the
#      credential.
#
# When step 2 cannot happen, BIOSENSE_MODEL_PROXY_WHY says why, and the runtime
# card and the system check repeat it with the fix.
set -uo pipefail

APP_ROOT="${BIOSENSE_APP_ROOT:-/app}"
LOGS="${BIOSENSE_LOG_DIR:-/tmp/biosense-logs}"
BOOT="$APP_ROOT/deploy/start-ai.sh"
PLACEHOLDER='held-by-the-biosense-model-proxy'
export BIOSENSE_ENTRYPOINT_DONE=1
sandbox_off=no
[ "${BIOSENSE_AGENT_SANDBOX:-on}" = 'off' ] && sandbox_off=yes

if [ "$(id -u)" != 0 ]; then
  # Started as a user already (the platform chose it): nothing can be dropped,
  # and the credential cannot be moved to another user.
  if [ "$sandbox_off" = yes ]; then
    BIOSENSE_MODEL_PROXY_WHY="not_root:$(id -u)"
    export BIOSENSE_MODEL_PROXY_WHY
    printf '[boot] WARNING: started as uid %s, not root; the model credential cannot be\n' "$(id -u)"
    printf '[boot]          separated from the agents. See the runtime card for the fix.\n'
  fi
  exec "$BOOT"
fi

mkdir -p "$LOGS" "$APP_ROOT/data/runs" "$APP_ROOT/data/private"
chown biosense:biosense "$LOGS" 2>/dev/null || true
# Everything on the volume belongs to the app user. A platform volume is often
# mounted root-owned, and anything an earlier root process created inside it
# (a workspace's projects folder, say) would otherwise refuse the app's writes —
# "Permission denied" on creating a project. Only what is not already the app
# user's is touched, so a large volume costs one walk, not one write per file.
# The volume is never followed out of: -xdev stays on it, symlinks are not
# dereferenced.
find "$APP_ROOT/data" -xdev \( ! -user biosense -o ! -group biosense \) \
  -exec chown -h biosense:biosense {} + 2>/dev/null || true

if [ "$sandbox_off" = yes ]; then
  if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    export BIOSENSE_MODEL_PROXY_WHY=no_credential
  else
    port="${BIOSENSE_MODEL_PROXY_PORT:-6790}"
    (
      cd "$APP_ROOT" || exit 0
      # Restarted if it ever exits; it holds the only copy of the credential.
      while true; do
        setpriv --reuid=keyholder --regid=keyholder --clear-groups --inh-caps=-all \
          --no-new-privs env HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 \
          "$APP_ROOT/.venv/bin/python" -m biosense.production.model_proxy --port "$port"
        sleep 2
      done
    ) 2>&1 | tee -a "$LOGS/model-proxy.log" &
    # Replace whichever credential this deployment holds with the placeholder,
    # and drop the other, so no process after this one holds either.
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
      export ANTHROPIC_API_KEY="$PLACEHOLDER"
      unset CLAUDE_CODE_OAUTH_TOKEN
    else
      export CLAUDE_CODE_OAUTH_TOKEN="$PLACEHOLDER"
      unset ANTHROPIC_API_KEY
    fi
    export ANTHROPIC_BASE_URL="http://127.0.0.1:${port}"
    # Declared running only once it answers. If it never does, runs fail with a
    # connection error and the card says why — the credential is not handed back.
    up=no
    for _ in $(seq 1 30); do
      if "$APP_ROOT/.venv/bin/python" -c "import urllib.request; urllib.request.urlopen('${ANTHROPIC_BASE_URL}/healthz', timeout=2)" 2>/dev/null; then
        up=yes
        break
      fi
      sleep 0.5
    done
    if [ "$up" = yes ]; then
      export BIOSENSE_MODEL_PROXY=on
      printf '[boot] agents sandbox OFF by operator choice; the model credential is held by a\n'
      printf '[boot] separate process (user keyholder) the agents cannot read: %s\n' "$ANTHROPIC_BASE_URL"
    else
      export BIOSENSE_MODEL_PROXY_WHY=proxy_failed
      printf '[boot] ERROR: the model-credential proxy did not start; see %s\n' "$LOGS/model-proxy.log"
      tail -n 5 "$LOGS/model-proxy.log" 2>/dev/null | sed 's/^/[model-proxy] /'
    fi
  fi
fi

exec setpriv --reuid=biosense --regid=biosense --init-groups --inh-caps=-all --no-new-privs \
  "$BOOT"
