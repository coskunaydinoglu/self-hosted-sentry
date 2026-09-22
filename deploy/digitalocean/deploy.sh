#!/usr/bin/env bash
# Deploy this repository (self-hosted Sentry + Symbol Portal + symbol server + canary)
# to a fresh Ubuntu droplet, behind Caddy with automatic HTTPS.
#
#   deploy/digitalocean/deploy.sh <droplet-ip> [options]
#
# Options:
#   --user USER        SSH user (default: root)
#   --domain NAME      public hostname pointing at the droplet
#                      (default: sentry-<ip-with-dashes>.sslip.io, no DNS needed)
#   --email ADDRESS    admin e-mail for the first Sentry user and Let's Encrypt
#   --errors-only      use the lighter errors-only compose profile (8 GB droplets)
#   --ssh-key PATH     private key to use (default: ssh defaults)
#
# Re-running is safe: it syncs the code, keeps generated secrets and existing data,
# and re-runs install.sh (which is Sentry's supported upgrade path).
set -euo pipefail

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

HOST="${1:-}"
[[ -z "$HOST" || "$HOST" == -* ]] && usage
shift
SSH_USER=root DOMAIN="" EMAIL="" PROFILE=feature-complete SSH_KEY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
  --user) SSH_USER="$2"; shift 2 ;;
  --domain) DOMAIN="$2"; shift 2 ;;
  --email) EMAIL="$2"; shift 2 ;;
  --errors-only) PROFILE=errors-only; shift ;;
  --ssh-key) SSH_KEY="$2"; shift 2 ;;
  -h | --help) usage 0 ;;
  *) echo "unknown option: $1" >&2; usage ;;
  esac
done

if [[ -z "$DOMAIN" ]]; then
  if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    DOMAIN="sentry-${HOST//./-}.sslip.io"
  else
    DOMAIN="$HOST"
  fi
fi
EMAIL="${EMAIL:-admin@${DOMAIN}}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_DIR=/opt/sentry-self-hosted
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
[[ -n "$SSH_KEY" ]] && SSH_OPTS+=(-i "$SSH_KEY")
TARGET="${SSH_USER}@${HOST}"
SUDO=""
[[ "$SSH_USER" != root ]] && SUDO="sudo"

say() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
remote() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

say "Checking SSH access to $TARGET"
remote "echo connected to \$(hostname) \$(. /etc/os-release && echo \$PRETTY_NAME)"

say "Checking droplet resources"
read -r CPUS MEM_MB DISK_GB ARCH < <(remote "echo \$(nproc) \$(free -m | awk '/Mem:/{print \$2}') \$(df -BG --output=avail / | tail -1 | tr -dc 0-9) \$(uname -m)")
echo "cpus=$CPUS memory=${MEM_MB}MB free_disk=${DISK_GB}GB arch=$ARCH"
[[ "$ARCH" == x86_64 ]] || { echo "Sentry images need x86_64, got $ARCH" >&2; exit 1; }
if [[ "$PROFILE" == feature-complete && ( "$MEM_MB" -lt 15000 || "$CPUS" -lt 4 ) ]]; then
  echo "feature-complete needs 4 vCPU / 16 GB (found ${CPUS} / ${MEM_MB} MB). Resize or pass --errors-only." >&2
  exit 1
fi
if [[ "$PROFILE" == errors-only && ( "$MEM_MB" -lt 7500 || "$CPUS" -lt 2 ) ]]; then
  echo "errors-only needs 2 vCPU / 8 GB (found ${CPUS} / ${MEM_MB} MB)." >&2
  exit 1
fi
[[ "$DISK_GB" -ge 40 ]] || { echo "need at least 40 GB free disk (found ${DISK_GB} GB)" >&2; exit 1; }

say "Preparing the host (Docker, swap, firewall, Caddy)"
remote "$SUDO bash -s" <<REMOTE
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Fresh droplets run cloud-init and unattended upgrades right after boot.
command -v cloud-init >/dev/null 2>&1 && cloud-init status --wait >/dev/null 2>&1 || true
for _ in \$(seq 1 120); do
  if fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1; then
    echo "waiting for apt/dpkg lock ..."; sleep 5
  else
    break
  fi
done
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null
if [ "$SSH_USER" != root ]; then usermod -aG docker "$SSH_USER"; fi
if ! swapon --show | grep -q /swapfile; then
  # Smaller droplets lean on swap during install (image builds, migrations).
  fallocate -l $([[ "$MEM_MB" -lt 15000 ]] && echo 8G || echo 4G) /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -qw vm.max_map_count=262144
grep -q '^vm.max_map_count' /etc/sysctl.conf || echo 'vm.max_map_count=262144' >> /etc/sysctl.conf
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
fi
if ! command -v caddy >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg rsync >/dev/null
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi
command -v rsync >/dev/null || apt-get install -y -qq rsync >/dev/null
mkdir -p $REMOTE_DIR
if [ "$SSH_USER" != root ]; then chown "$SSH_USER": $REMOTE_DIR; fi
REMOTE

say "Syncing the repository to $REMOTE_DIR"
# Never ship local secrets or local instance state; remote files are generated there.
rsync -az --delete \
  --rsync-path="${SUDO:+sudo }rsync" \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude .git/ --exclude .venv/ --exclude __pycache__/ --exclude .pytest_cache/ \
  --exclude /.env --exclude /.env.custom \
  --exclude /sentry/config.yml --exclude /sentry/sentry.conf.py --exclude /sentry/enhance-image.sh --exclude /sentry/requirements.txt \
  --exclude /relay/config.yml --exclude /relay/credentials.json \
  --exclude /symbolicator/config.yml \
  --exclude /geoip/GeoIP.conf --exclude '/geoip/*.mmdb' \
  --exclude /certificates/.generated/ \
  --exclude '/symbol-server/data/*' \
  --exclude 'samples/**/build/' --exclude 'samples/**/.gradle/' --exclude 'samples/**/.kotlin/' \
  --exclude 'samples/**/local.properties' --exclude 'samples/**/sentry.properties' \
  --exclude 'sentry_install_log*.txt' --exclude docker-compose.override.yml \
  --exclude /.admin-password --exclude /.portal-basic-auth --exclude '/.deploy-install.*' \
  "$REPO_ROOT"/ "$TARGET:$REMOTE_DIR/"
# The committed .env holds safe defaults only; the local one may carry tokens.
git -C "$REPO_ROOT" show HEAD:.env | remote "cat > $REMOTE_DIR/.env.committed"

say "Configuring Sentry for https://$DOMAIN"
remote "$SUDO bash -s" <<REMOTE
set -euo pipefail
cd $REMOTE_DIR
[ -f .env ] || cp .env.committed .env
rm -f .env.committed
setvar() { if grep -q "^\$1=" .env; then sed -i "s|^\$1=.*|\$1=\$2|" .env; else echo "\$1=\$2" >> .env; fi; }
setvar SENTRY_BIND 127.0.0.1:9000
# Switch the base profile but keep add-on profiles (e.g. ingest-canary, session-generator).
extras=\$(grep '^COMPOSE_PROFILES=' .env | cut -d= -f2- | tr ',' '\n' | grep -vxE 'feature-complete|errors-only|' | paste -sd, - || true)
setvar COMPOSE_PROFILES "$PROFILE\${extras:+,\$extras}"
# The portal holds a write-capable token and is reachable from the internet here:
# protect it with basic auth (credentials kept in .portal-basic-auth, root only).
# .env is the source of truth; the file is a readable copy for the operator.
current=\$(grep '^SYMBOL_PORTAL_BASIC_AUTH=' .env | cut -d= -f2- || true)
if [ -z "\$current" ]; then
  current="demo:\$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
  setvar SYMBOL_PORTAL_BASIC_AUTH "\$current"
fi
(umask 077; echo "\$current" > .portal-basic-auth)
[ -f sentry/config.yml ] || cp sentry/config.example.yml sentry/config.yml
[ -f sentry/sentry.conf.py ] || cp sentry/sentry.conf.example.py sentry/sentry.conf.py
[ -f symbolicator/config.yml ] || cp symbolicator/config.example.yml symbolicator/config.yml
if grep -q '^system.url-prefix:' sentry/config.yml; then
  sed -i "s|^system.url-prefix:.*|system.url-prefix: 'https://$DOMAIN'|" sentry/config.yml
else
  echo "system.url-prefix: 'https://$DOMAIN'" >> sentry/config.yml
fi
if ! grep -q 'deploy/digitalocean: TLS proxy' sentry/sentry.conf.py; then
  cat >> sentry/sentry.conf.py <<'PY'

# deploy/digitalocean: TLS proxy (Caddy) in front of nginx
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SOCIAL_AUTH_REDIRECT_IS_HTTPS = True
PY
  echo 'CSRF_TRUSTED_ORIGINS = ["https://$DOMAIN"]' >> sentry/sentry.conf.py
fi
cat > /etc/caddy/Caddyfile <<CADDY
{
	email $EMAIL
}

$DOMAIN {
	encode zstd gzip
	request_body {
		max_size 4GB
	}
	reverse_proxy 127.0.0.1:9000 {
		transport http {
			read_timeout 15m
			write_timeout 15m
		}
	}
}
CADDY
systemctl reload caddy || systemctl restart caddy
REMOTE

say "Running install.sh on the droplet (first run takes 20-40 minutes)"
# Detached from this SSH session so a dropped connection cannot kill the install.
# Re-running deploy.sh while it is still running just waits for it.
remote "$SUDO bash -s" <<REMOTE
set -euo pipefail
cd $REMOTE_DIR
if [ -f .deploy-install.pid ] && kill -0 "\$(cat .deploy-install.pid)" 2>/dev/null; then
  echo "install.sh already running (pid \$(cat .deploy-install.pid)), waiting for it"
else
  rm -f .deploy-install.exit
  setsid nohup bash -c './install.sh --skip-user-creation --no-report-self-hosted-issues --skip-commit-check --apply-automatic-config-updates > .deploy-install.log 2>&1; echo \$? > .deploy-install.exit' </dev/null >/dev/null 2>&1 &
  echo \$! > .deploy-install.pid
  echo "install.sh started (pid \$!)"
fi
REMOTE
last=""
while :; do
  status=$(remote "cd $REMOTE_DIR && if [ -f .deploy-install.exit ]; then echo EXIT:\$(cat .deploy-install.exit); else grep -E '^(▶ |[A-Z][a-z]+ing )' .deploy-install.log 2>/dev/null | tail -1; fi" || echo "ssh-retry")
  if [[ "$status" == EXIT:* ]]; then
    code="${status#EXIT:}"
    echo "install.sh finished with exit code $code"
    if [[ "$code" != 0 ]]; then
      remote "tail -40 $REMOTE_DIR/.deploy-install.log" >&2
      exit 1
    fi
    break
  fi
  if [[ -n "$status" && "$status" != "$last" && "$status" != ssh-retry ]]; then
    echo "  $status"
    last="$status"
  fi
  sleep 30
done

say "Starting services"
remote "cd $REMOTE_DIR && $SUDO docker compose up -d --wait --wait-timeout 900 2>&1 | tail -3" || true
for i in $(seq 1 60); do
  code=$(remote "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9000/_health/" || true)
  [[ "$code" == 200 ]] && break
  sleep 10
done
echo "sentry health: ${code:-none}"

say "Creating the admin user (skipped if it exists)"
PASSWORD_FILE="$REMOTE_DIR/.admin-password"
remote "$SUDO bash -s" <<REMOTE
set -euo pipefail
cd $REMOTE_DIR
if [ ! -f $PASSWORD_FILE ]; then
  umask 077
  pw=\$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
  # Only record the password if this run really created the user; an existing
  # admin keeps the password its owner set.
  if docker compose run --rm -T web createuser --email "$EMAIL" --password "\$pw" --superuser --no-input >/dev/null 2>&1; then
    echo "\$pw" > $PASSWORD_FILE
    echo "admin user created"
  else
    echo "admin user already exists (password unchanged)"
  fi
fi
REMOTE

say "Checking HTTPS"
resolved=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)
if [[ "$HOST" =~ ^[0-9.]+$ && "$resolved" != "$HOST" ]]; then
  echo "WARNING: $DOMAIN resolves to '${resolved:-nothing}', not $HOST. Create the A record;"
  echo "         Caddy keeps retrying the certificate, or re-run this script once DNS is live."
fi
restarted=""
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$DOMAIN/auth/login/sentry/" || true)
  [[ "$code" == 200 ]] && break
  # Caddy backs off after a failed ACME attempt (e.g. it tried before DNS existed).
  # Once the name points here, a restart makes it request the certificate right away.
  if [[ -z "$restarted" && "$resolved" == "$HOST" && "$i" -ge 3 ]]; then
    echo "restarting Caddy to retry the certificate now that DNS resolves"
    remote "$SUDO systemctl restart caddy"
    restarted=1
  fi
  sleep 5
done
echo "https://$DOMAIN -> HTTP ${code:-none}"

cat <<DONE

Sentry:        https://$DOMAIN
Symbols:       https://$DOMAIN/organizations/sentry/symbols/
Admin e-mail:  $EMAIL
Password:      ssh $TARGET ${SUDO:+sudo }cat $PASSWORD_FILE
Portal login:  ssh $TARGET ${SUDO:+sudo }cat $REMOTE_DIR/.portal-basic-auth   (user:password)

Next: create a Personal Token in Sentry and put it on the droplet:
  ssh $TARGET
  cd $REMOTE_DIR && ${SUDO:+sudo }nano .env     # SYMBOL_PORTAL_SENTRY_TOKEN=...
  ${SUDO:+sudo }docker compose up -d --force-recreate symbol-portal
Then run scripts/bootstrap-mobile-projects.py with SENTRY_URL=https://$DOMAIN
DONE
