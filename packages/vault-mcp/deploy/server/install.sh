#!/bin/bash
# Provision (or update) the obsidian-vault box. Idempotent; run as root.
# `deploy.sh push` syncs deploy/ + the built wheels to /opt/vault-mcp/src and
# runs this over SSM. Settings come from /etc/vault-mcp/stack.env (UserData) and
# SSM parameters under /obsidian-vault/ (see deploy.sh).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(dirname "$HERE")
# shellcheck source=/dev/null
source /etc/vault-mcp/stack.env
: "${VAULT_BUCKET:?}" "${AWS_REGION:?}"

OBSIDIAN_VERSION=${OBSIDIAN_VERSION:-1.15.0}
VAULT_NAME=${VAULT_NAME:-Trey}
U=obsidian
H=/home/$U
VAULT=$H/vaults/$VAULT_NAME
TLS=/etc/vault-mcp
PORT=8443
AWS=/usr/local/bin/aws # AWS CLI v2, installed by deploy.sh push
log() { printf '\n== %s\n' "$*"; }
param() {
  "$AWS" ssm get-parameter --region "$AWS_REGION" --name "/obsidian-vault/$1" \
    --with-decryption --query Parameter.Value --output text 2>/dev/null || true
}

log "packages + unattended security upgrades"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  xvfb xauth x11vnc git curl jq openssl ca-certificates unattended-upgrades \
  libgtk-3-0t64 libnotify4 libnss3 libxss1 libasound2t64 libgbm1 libsecret-1-0 libdrm2 xdg-utils
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
[ -x "$AWS" ] || { echo "no AWS CLI at $AWS; run deploy.sh push" >&2; exit 1; }
# Obsidian's "today" (daily notes) must match the server's America/Chicago date,
# or every evening it would create tomorrow's daily note.
timedatectl set-timezone America/Chicago

log "swap (2 GB box: Obsidian + Xvfb take ~1 GB)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

log "user $U (systemd user services, lingering)"
id "$U" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$U"
loginctl enable-linger "$U"
RUNTIME=/run/user/$(id -u "$U")
for _ in $(seq 30); do [ -S "$RUNTIME/bus" ] && break; sleep 1; done
as_user() { sudo -u "$U" XDG_RUNTIME_DIR="$RUNTIME" DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME/bus" "$@"; }
as_user mkdir -p "$H/.local/state/vault-mcp"
# A deploy counts as activity, so the box doesn't stop mid-setup.
as_user touch "$H/.local/state/vault-mcp/last_activity"

log "Obsidian $OBSIDIAN_VERSION"
case $(dpkg --print-architecture) in
  arm64) tarball=obsidian-$OBSIDIAN_VERSION-arm64.tar.gz ;;
  amd64) tarball=obsidian-$OBSIDIAN_VERSION.tar.gz ;;
  *) echo "unsupported architecture" >&2; exit 1 ;;
esac
dest=/opt/obsidian-$OBSIDIAN_VERSION
if [ ! -x "$dest/obsidian" ]; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/obsidian.tgz" \
    "https://github.com/obsidianmd/obsidian-releases/releases/download/v$OBSIDIAN_VERSION/$tarball"
  mkdir -p "$dest"
  tar -xzf "$tmp/obsidian.tgz" -C "$dest" --strip-components=1
  rm -rf "$tmp"
fi
previous=$(readlink /opt/obsidian || true)
ln -sfn "$dest" /opt/obsidian
[ -x /opt/obsidian/obsidian-cli ] || echo "WARNING: /opt/obsidian/obsidian-cli missing; the CLI needs installer 1.12.7+"
install -m 755 "$HERE"/bin/* /usr/local/bin/

log "Obsidian config: CLI on, vault registered"
as_user mkdir -p "$VAULT/.obsidian" "$H/.config/obsidian" "$H/.config/vault-mcp" "$H/.config/systemd/user"
cfg=$H/.config/obsidian/obsidian.json
[ -s "$cfg" ] || echo '{}' | as_user tee "$cfg" >/dev/null
jq --arg path "$VAULT" --argjson ts "$(date +%s000)" '
  .cli = true
  | if ([.vaults // {} | .[] | select(.path == $path)] | length) == 0
    then .vaults = ((.vaults // {}) + {"vaultmcp00000001": {path: $path, ts: $ts, open: true}})
    else . end' "$cfg" | as_user tee "$cfg.new" >/dev/null
as_user mv "$cfg.new" "$cfg"
# Vault settings from the brief (§4). Only seeded before the first sync; after
# that the Mac's synced settings win, so set them there too.
seed() { [ -e "$VAULT/.obsidian/$1" ] || as_user tee "$VAULT/.obsidian/$1" >/dev/null; }
seed app.json <<'EOF'
{"newFileLocation": "folder", "newFileFolderPath": "_Inbox", "attachmentFolderPath": "_Attachments",
 "alwaysUpdateLinks": true, "newLinkFormat": "shortest", "trashOption": "local", "promptDelete": false}
EOF
seed daily-notes.json <<'EOF'
{"folder": "Personal/Daily", "format": "YYYY-MM-DD", "template": "_Templates/Daily Note"}
EOF
seed templates.json <<'EOF'
{"folder": "_Templates", "dateFormat": "YYYY-MM-DD", "timeFormat": "HH:mm"}
EOF

log "TLS: the box's own CA, published to SSM for the Lambda to pin"
# Port 8443 is open to the internet (the Lambda has no fixed IPs), so vault-mcp
# speaks HTTPS. The CA lives on the volume and survives stop/start; the Lambda
# checks the chain, not the hostname (the public DNS name changes every start).
umask 077
if [ ! -s "$TLS/ca.pem" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=obsidian-vault CA" -keyout "$TLS/ca.key" -out "$TLS/ca.pem" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash"
fi
if [ ! -s "$TLS/server.pem" ] || ! openssl x509 -checkend 2592000 -noout -in "$TLS/server.pem"; then
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj "/CN=vault-mcp" \
    -keyout "$TLS/server.key" -out "$TLS/server.csr"
  openssl x509 -req -in "$TLS/server.csr" -CA "$TLS/ca.pem" -CAkey "$TLS/ca.key" -CAcreateserial \
    -days 825 -out "$TLS/server.pem" -extfile <(printf '%s\n' \
      "subjectAltName=DNS:localhost,IP:127.0.0.1" "basicConstraints=critical,CA:FALSE" \
      "keyUsage=critical,digitalSignature" "extendedKeyUsage=serverAuth" \
      "subjectKeyIdentifier=hash" "authorityKeyIdentifier=keyid")
fi
umask 022
chmod 644 "$TLS/ca.pem" "$TLS/server.pem"
chown "root:$U" "$TLS/server.key" && chmod 640 "$TLS/server.key"
"$AWS" ssm put-parameter --region "$AWS_REGION" --name /obsidian-vault/ca-cert --type String \
  --overwrite --value "$(cat "$TLS/ca.pem")" >/dev/null

log "vault-mcp"
as_user bash -c 'test -x ~/.local/bin/uv || curl -LsSf https://astral.sh/uv/install.sh | sh -s -- -q'
# Install the wheels by path, so a same-named package on PyPI can never win.
as_user "$H/.local/bin/uv" tool install --force --reinstall --python 3.13 \
  "$(ls "$SRC"/dist/vault_mcp-*.whl)" --with "$(ls "$SRC"/dist/mcp_lambda-*.whl)"

token=$(param mcp-token)
[ -n "$token" ] || { echo "SSM /obsidian-vault/mcp-token is not set; run deploy.sh token" >&2; exit 1; }
readonly_flag=$(param readonly)
idle_minutes=$(param idle-minutes)
sync_busy=$(param sync-busy)
umask 077
cat >"$H/.config/vault-mcp/env.new" <<EOF
VAULT_ROOT=$VAULT
VAULT_NAME=$VAULT_NAME
VAULT_MCP_TOKEN=$token
VAULT_MCP_READONLY=${readonly_flag:-0}
VAULT_MCP_HOST=0.0.0.0
VAULT_MCP_PORT=$PORT
VAULT_MCP_SSL_CERT=$TLS/server.pem
VAULT_MCP_SSL_KEY=$TLS/server.key
VAULT_REQUIRE_READY=1
VAULT_SNAPSHOT_CMD=/usr/local/bin/vault-snapshot
VAULT_IDLE_MINUTES=${idle_minutes:-30}
VAULT_TZ=America/Chicago
OBSIDIAN_CLI=/usr/local/bin/obsidian-cli
EOF
# The Sync plugin's "busy" status names are a guess until seen on the box (README step 5).
[ -z "$sync_busy" ] || echo "VAULT_SYNC_BUSY=$sync_busy" >>"$H/.config/vault-mcp/env.new"
chown "$U:$U" "$H/.config/vault-mcp/env.new"
mv "$H/.config/vault-mcp/env.new" "$H/.config/vault-mcp/env"
umask 022

log "systemd units"
install -o "$U" -g "$U" -m 644 "$HERE"/systemd/* "$H/.config/systemd/user/"
as_user systemctl --user daemon-reload
as_user systemctl --user enable --now obsidian.service vault-mcp.service vault-healthcheck.timer
as_user systemctl --user restart vault-mcp.service
if [ -n "$previous" ] && [ "$previous" != "$dest" ]; then
  as_user systemctl --user restart obsidian.service
fi
install -m 644 "$HERE"/systemd-system/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vault-idle-stop.timer

cat >/etc/logrotate.d/vault-mcp <<EOF
$H/.local/state/vault-mcp/audit.log {
  su $U $U
  monthly
  rotate 24
  compress
  missingok
  notifempty
  copytruncate
}
EOF

log "status"
sleep 5
as_user systemctl --user --no-pager status obsidian.service vault-mcp.service | grep -E '●|Active:' || true
curl -fsS -m 10 --cacert "$TLS/ca.pem" -H "Authorization: Bearer $token" "https://localhost:$PORT/healthz" | jq . ||
  echo "healthz not answering yet"
