#!/usr/bin/env bash
# Redeploy BioSense when main has moved. Run by biosense-update.timer every five
# minutes; run it by hand with --force to rebuild now.
#
# The checkout under /opt/biosense is a deploy copy: it is reset to origin/main,
# so edits made there are discarded. The settings file (deploy/vm/.env) is
# untracked and is never touched. A redeploy restarts the container, which ends
# any real run in flight — exactly as a Railway deploy does.
set -euo pipefail

DIR="${BIOSENSE_DIR:-/opt/biosense}"
BRANCH="${BIOSENSE_BRANCH:-main}"

# One at a time: a slow build must not be started again on top of itself.
exec 9>/var/lock/biosense-update.lock
flock -n 9 || exit 0

cd "$DIR"
git fetch -q origin "$BRANCH"
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ] && [ "${1:-}" != '--force' ]; then
  exit 0
fi
git reset -q --hard "origin/$BRANCH"
docker compose -f deploy/vm/docker-compose.yml up -d --build --remove-orphans
docker image prune -f >/dev/null 2>&1 || true
echo "deployed $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"
