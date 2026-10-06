#!/usr/bin/env bash
# Put BioSense with real AI on a fresh Linux VM (Ubuntu 24.04 or Debian 12).
#
#   curl -fsSL https://raw.githubusercontent.com/AbhinavSoni20191995/Biosense/main/deploy/vm/setup.sh -o setup.sh
#   sudo bash setup.sh
#
# It asks for four things (address, model key, and optionally the accounts server
# and admin ids), then: installs Docker, lets the agents' sandbox create user
# namespaces, checks out the code, starts BioSense behind HTTPS, and installs a
# timer that redeploys within five minutes of every push to main. Safe to run
# again: it keeps the settings file and only updates what changed.
#
# Nothing typed here is ever run as a command. Answers go into deploy/vm/.env
# (root-only) and are read by Docker as plain values.
set -euo pipefail

REPO_URL="${BIOSENSE_REPO_URL:-https://github.com/AbhinavSoni20191995/Biosense.git}"
BRANCH="${BIOSENSE_BRANCH:-main}"
DIR="${BIOSENSE_DIR:-/opt/biosense}"
COMPOSE=(docker compose -f "$DIR/deploy/vm/docker-compose.yml")

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mstopped: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die 'run it as root: sudo bash setup.sh'
command -v apt-get >/dev/null || die 'this script expects Ubuntu or Debian (apt-get)'

say '1/7  packages and Docker'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates openssl >/dev/null
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version

say "2/7  let the agents' sandbox create user namespaces"
# Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor; some
# kernels gate them with a separate switch. Each line is written only if this
# kernel has that switch, so the file means the same thing everywhere.
conf=/etc/sysctl.d/60-biosense-agent-sandbox.conf
: > "$conf"
if [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
  echo 'kernel.apparmor_restrict_unprivileged_userns = 0' >> "$conf"
fi
if [ -e /proc/sys/kernel/unprivileged_userns_clone ]; then
  echo 'kernel.unprivileged_userns_clone = 1' >> "$conf"
fi
if [ "$(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo 1)" = 0 ]; then
  echo 'user.max_user_namespaces = 15000' >> "$conf"
fi
sysctl -q --system >/dev/null || true
cat "$conf"

say "3/7  code ($BRANCH)"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO_URL" "$DIR"
fi
git -C "$DIR" log --oneline -1

say '4/7  settings'
envf="$DIR/deploy/vm/.env"
if [ -f "$envf" ]; then
  echo "keeping $envf (delete it and run again to start over)"
else
  ip="$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
  suggested="$(printf '%s' "$ip" | tr '.' '-').sslip.io"
  read -rp "Address for the site [$suggested]: " domain </dev/tty
  domain="${domain:-$suggested}"
  key=''
  while [ -z "$key" ]; do
    read -rsp 'ANTHROPIC_API_KEY (typing is hidden): ' key </dev/tty; echo
  done
  read -rp 'BIOSENSE_AUTH_SERVER, from Railway (Enter to skip): ' auth </dev/tty
  read -rp 'BIOSENSE_ADMIN_USER_IDS, from Railway (Enter to skip): ' admins </dev/tty
  (
    umask 077
    {
      printf '%s=%s\n' BIOSENSE_DOMAIN "$domain"
      [ -n "$auth" ] && printf '%s=%s\n' BIOSENSE_AUTH_SERVER "$auth"
      [ -n "$admins" ] && printf '%s=%s\n' BIOSENSE_ADMIN_USER_IDS "$admins"
      printf '%s=%s\n' ANTHROPIC_API_KEY "$key"
    } > "$envf"
  )
  unset key
  echo "written: $envf (root-only)"
fi
domain="$(sed -n 's/^BIOSENSE_DOMAIN=//p' "$envf" | head -n1)"

say '5/7  firewall'
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
  echo 'ufw: opened 80 and 443'
else
  echo 'no active ufw; if your provider has a cloud firewall, allow ports 80 and 443 there'
fi

say '6/7  build and start (the first build takes a few minutes)'
"${COMPOSE[@]}" up -d --build --remove-orphans

say '7/7  redeploy automatically after every push to main'
install -m 0755 "$DIR/deploy/vm/update.sh" /usr/local/bin/biosense-update
cat > /etc/systemd/system/biosense-update.service <<UNIT
[Unit]
Description=Redeploy BioSense when main changes
After=network-online.target docker.service

[Service]
Type=oneshot
Environment=BIOSENSE_DIR=$DIR
Environment=BIOSENSE_BRANCH=$BRANCH
ExecStart=/usr/local/bin/biosense-update
UNIT
cat > /etc/systemd/system/biosense-update.timer <<'UNIT'
[Unit]
Description=Check for a new BioSense version every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now biosense-update.timer >/dev/null
echo 'timer installed: biosense-update.timer'

say 'waiting for the site to answer'
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "https://$domain/healthz" >/dev/null 2>&1; then
    echo "up: https://$domain"
    break
  fi
  sleep 5
done

say "the agents' sandbox and the system check (from the boot log)"
sleep 30
"${COMPOSE[@]}" logs biosense 2>/dev/null | grep -E '\[sandbox\]|\[selfcheck\]|sandbox:' | tail -n 20 || true

cat <<DONE

Done. Open https://$domain
  - Real AI should show as available on the Discovery page.
  - Runs page -> System check -> "Run live check" proves the agents talk.
  - Logs:      sudo docker compose -f $DIR/deploy/vm/docker-compose.yml logs -f biosense
  - Redeploy:  sudo biosense-update --force
DONE
