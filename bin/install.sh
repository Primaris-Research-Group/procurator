#!/usr/bin/env bash
# PROCURATOR box installer: a fresh Ubuntu 24.04 server -> running gateway + brokers, in one command (distributable plan Phase 2, first cut).
#
#   curl -fsSL https://<release host>/install.sh | sudo bash -s -- --repo https://github.com/<org>/procurator.git [--domain box.example.com]
#   or, from a checkout:   sudo bash bin/install.sh --local
#
# What it does, idempotently (a second run upgrades):
#   1. checks the OS, memory and disk; installs Docker Engine + compose plugin, git, python3, curl (apt);
#   2. puts the code in /opt/procurator (clone or pull; --local uses the checkout it runs from);
#   3. runs bin/initialize.py (private .env, state dirs, gateway token) if there is no .env yet;
#   4. writes /etc/procurator/instance.env (DOMAIN, EDITION, TIMEZONE) for the brokers, the Setup pages and the TLS proxy;
#      --edition standard (default, PROCURATOR) or x (PROCURATOR-X: adds the coding orchestrator and builders);
#   5. installs the broker systemd units found under integrations/*/procurator-*.service and enables them;
#   6. pulls the pinned OpenClaw image and starts Docker Compose (upstream file + private overlays present in the repo);
#   7. puts a TLS proxy (Caddy, automatic HTTPS) in front of the dashboard — on your domain, or on a free
#      <your-ip>.sslip.io name when you have none — and waits for the gateway health check;
#   8. prints a ONE-TIME SETUP LINK. Open it in any browser: model key, Telegram bot, and one tap on your phone to pair.
#      (Prefer the terminal? Answer T when asked, or run: sudo python3 bin/connect-accounts.py)
#
#   sudo bash bin/install.sh --setup-link [--reconfigure]   print a fresh setup link (the old one stops working)
#
# The installer never asks for or prints a secret. Keys and the bot token are typed into the setup page (or hidden, on this console)
# and go straight to .env (0600) on this box.
set -euo pipefail

REPO=""; BRANCH="main"; DOMAIN=""; LOCAL=0; TZ_NAME="${TZ:-America/New_York}"; DEST=/opt/procurator; EDITION="standard"
LINK_ONLY=0; RECONFIGURE=0; NO_PROXY=0; WEB_STATE=/var/lib/procurator-web
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --branch) BRANCH="$2"; shift 2;;
    --domain) DOMAIN="$2"; shift 2;;
    --timezone) TZ_NAME="$2"; shift 2;;
    --edition) EDITION="$2"; shift 2;;
    --setup-link) LINK_ONLY=1; shift;;
    --reconfigure) RECONFIGURE=1; shift;;
    --no-proxy) NO_PROXY=1; shift;;
    --dest) DEST="$2"; shift 2;;
    --local) LOCAL=1; shift;;
    -h|--help) sed -n 2,24p "$0"; exit 0;;
    *) echo "Unknown option: $1"; exit 2;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mSTOP:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$EDITION" == "standard" || "$EDITION" == "x" ]] || fail "--edition is standard (PROCURATOR) or x (PROCURATOR-X, adds the coding orchestrator)."
[[ $EUID -eq 0 ]] || fail "Run as root (sudo)."

mint_link() {   # prints the one-time setup URL (integrations/web/setup_wizard.py); needs DOMAIN
  local args=(mint --base "https://${DOMAIN}" --state-dir "$WEB_STATE")
  [[ $RECONFIGURE -eq 1 ]] && args+=(--reconfigure)
  python3 "$DEST/integrations/web/setup_wizard.py" "${args[@]}"
}
if [[ $LINK_ONLY -eq 1 ]]; then
  [[ -f /etc/procurator/instance.env ]] || fail "PROCURATOR is not installed on this box yet. Run the installer without --setup-link first."
  DOMAIN=$(grep '^DOMAIN=' /etc/procurator/instance.env | cut -d= -f2-); DEST=$(grep '^INSTALL_DIR=' /etc/procurator/instance.env | cut -d= -f2-)
  [[ -n "$DOMAIN" ]] || fail "No domain is configured, so there is no setup page. Use the terminal instead: sudo python3 $DEST/bin/connect-accounts.py"
  echo; echo "Open this link in your browser to set up PROCURATOR (valid 24 hours, works once):"; echo; echo "    $(mint_link)"; echo
  exit 0
fi
. /etc/os-release 2>/dev/null || fail "Cannot read /etc/os-release."
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" =~ ^(22\.04|24\.04)$ ]] || fail "PROCURATOR boxes run Ubuntu 22.04 or 24.04 (this is ${PRETTY_NAME:-unknown})."
MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo); DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
[[ $MEM_MB -ge 1800 ]] || fail "At least 2 GB of memory is needed (found ${MEM_MB} MB). 4 GB is recommended."
[[ $DISK_GB -ge 15 ]] || fail "At least 15 GB of free disk is needed (found ${DISK_GB} GB)."
[[ $MEM_MB -ge 3500 ]] || say "Memory is ${MEM_MB} MB: the gateway will run with the low-memory limits from bin/initialize.py."

# ---------- 1. packages ----------
export DEBIAN_FRONTEND=noninteractive
say "Installing packages (git, python3, curl, Docker)"
apt-get update -qq
apt-get install -y -qq ca-certificates curl git python3 gnupg >/dev/null
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
fi
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is missing after install."
systemctl enable --now docker >/dev/null
if [[ $MEM_MB -lt 3500 && ! -f /swapfile ]]; then
  say "Adding 2 GB of swap (small box)"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------- 1b. address ----------
PUBLIC_IP=$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)
[[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || PUBLIC_IP=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -Ev '^(10\.|127\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)' | head -1 || true)
if [[ -z "$DOMAIN" && $NO_PROXY -eq 0 && -n "$PUBLIC_IP" ]]; then
  DOMAIN="${PUBLIC_IP//./-}.sslip.io"
  say "No --domain given: using the free name $DOMAIN (it always points at this server). You can switch to your own domain later."
fi
if [[ -n "$DOMAIN" && -n "$PUBLIC_IP" && "$DOMAIN" != *.sslip.io ]]; then
  RESOLVED=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}')
  [[ "$RESOLVED" == "$PUBLIC_IP" ]] || say "Heads-up: $DOMAIN points to '${RESOLVED:-nothing}', not to this server ($PUBLIC_IP). Add an A record for it at your DNS provider; HTTPS starts working once it resolves."
fi

# ---------- 2. code ----------
if [[ $LOCAL -eq 1 ]]; then
  SRC="$(cd "$(dirname "$0")/.." && pwd)"
  if [[ "$SRC" != "$DEST" ]]; then
    say "Copying the checkout to $DEST"
    mkdir -p "$DEST"; cp -a "$SRC"/. "$DEST"/
  fi
else
  [[ -n "$REPO" ]] || fail "Pass --repo <git url> (or --local from a checkout)."
  if [[ -d "$DEST/.git" ]]; then
    say "Updating the code in $DEST"
    git -C "$DEST" fetch -q origin "$BRANCH" && git -C "$DEST" checkout -q "$BRANCH" && git -C "$DEST" pull -q --ff-only origin "$BRANCH"
  else
    say "Cloning $REPO ($BRANCH) into $DEST"
    git clone -q --branch "$BRANCH" "$REPO" "$DEST"
  fi
fi
cd "$DEST"
chmod 700 "$DEST"
# The gateway container is not root: plugin and broker code must be root-owned and world-readable (OpenClaw also refuses
# plugin folders it considers unsafe). Same normalisation the deploy scripts apply.
chown -R root:root integrations bin
find integrations -type d -exec chmod 755 {} +
find integrations -type f -exec chmod 644 {} +
find bin -type f -name '*.sh' -exec chmod 755 {} +

# ---------- 3. private state ----------
if [[ ! -f .env ]]; then
  say "Creating private state (.env, state/, auth-profiles/)"
  python3 bin/initialize.py
else
  say "Keeping the existing .env"
fi
grep -q '^OPENCLAW_TZ=' .env || echo "OPENCLAW_TZ=$TZ_NAME" >> .env
# Edition for the gateway container (the brokers read /etc/procurator/instance.env): PROCURATOR = standard, PROCURATOR-X = x.
if grep -q '^PROCURATOR_EDITION=' .env; then sed -i "s/^PROCURATOR_EDITION=.*/PROCURATOR_EDITION=$EDITION/" .env; else echo "PROCURATOR_EDITION=$EDITION" >> .env; fi
if [[ -n "$DOMAIN" ]]; then
  if grep -q '^PROCURATOR_PUBLIC_BASE=' .env; then sed -i "s#^PROCURATOR_PUBLIC_BASE=.*#PROCURATOR_PUBLIC_BASE=https://$DOMAIN#" .env; else echo "PROCURATOR_PUBLIC_BASE=https://$DOMAIN" >> .env; fi
fi

# ---------- 4. instance file ----------
mkdir -p /etc/procurator; chmod 700 /etc/procurator
{
  echo "# Written by bin/install.sh on $(date -u +%FT%TZ). No secrets here."
  echo "DOMAIN=${DOMAIN}"
  [[ -n "$DOMAIN" ]] && echo "PROCURATOR_PUBLIC_BASE=https://${DOMAIN}"
  echo "EDITION=${EDITION}"
  echo "TIMEZONE=${TZ_NAME}"
  echo "INSTALL_DIR=${DEST}"
  echo "RELEASE=$(python3 -c 'import json,sys;print(json.load(open("integrations/web-plugin/package.json"))["version"])' 2>/dev/null || echo unknown)"
} > /etc/procurator/instance.env
chmod 600 /etc/procurator/instance.env

# ---------- 5. broker services ----------
say "Installing broker services"
for unit in integrations/*/procurator-*.service integrations/*/procurator-*.timer integrations/*/procurator-*.path; do
  [[ -f "$unit" ]] || continue
  # Units that run as their own system user (e.g. the voice bridge) get that user created first.
  user=$(awk -F= '/^User=/{print $2; exit}' "$unit")
  if [[ -n "$user" ]] && ! id "$user" >/dev/null 2>&1; then useradd --system --no-create-home --shell /usr/sbin/nologin "$user"; fi
  cp "$unit" /etc/systemd/system/
done
systemctl daemon-reload
for unit in /etc/systemd/system/procurator-*.service; do
  name=$(basename "$unit")
  # The setup applier is a one-shot started only by its .path unit when the setup page asks; never run it here.
  [[ "$name" == *-setup.service ]] && continue
  systemctl enable "$name" >/dev/null 2>&1 || true
  systemctl restart "$name" || say "$name did not start yet (it may wait for its configuration); check: journalctl -u $name -n 20"
done
for timer in /etc/systemd/system/procurator-*.timer /etc/systemd/system/procurator-*.path; do
  [[ -f "$timer" ]] && systemctl enable --now "$(basename "$timer")" >/dev/null 2>&1 || true
done

# ---------- 6. gateway ----------
say "Pulling the pinned OpenClaw image and starting the gateway"
IMAGE=$(grep '^OPENCLAW_IMAGE=' .env | cut -d= -f2-)
docker pull -q "$IMAGE" >/dev/null
say "Configuring the gateway for PROCURATOR (plugins, tools, defaults)"
python3 bin/configure-gateway.py --timezone "$TZ_NAME"
COMPOSE_FILES=(-f compose.yaml)
for f in compose.private.yaml compose.web.yaml compose.mail.yaml compose.voice.yaml compose.google.yaml; do
  [[ -f "$f" ]] && COMPOSE_FILES+=(-f "$f")
done
docker compose "${COMPOSE_FILES[@]}" up -d --no-build --remove-orphans

# ---------- 6b. HTTPS proxy ----------
if [[ -n "$DOMAIN" && $NO_PROXY -eq 0 ]]; then
  if ss -ltnp 2>/dev/null | grep -E ':(80|443) ' | grep -vq caddy; then
    say "Ports 80/443 are already served by another program (an existing nginx?); leaving it alone. Route https://$DOMAIN/assistant/dashboard/ to 127.0.0.1:3343 yourself."
  else
    if ! command -v caddy >/dev/null 2>&1; then
      say "Installing Caddy (automatic HTTPS)"
      if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
        apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
        curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
        apt-get update -qq && apt-get install -y -qq caddy >/dev/null
      fi
    fi
    # Only the private pages the brokers serve are reachable; everything else is 404. Every route below exists in this repo.
    cat > /etc/caddy/Caddyfile <<CADDY
# Written by bin/install.sh — PROCURATOR. Re-running the installer rewrites this file.
${DOMAIN} {
	header {
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		-Server
	}
	@dashboard path_regexp ^/assistant/dashboard(/.*)?\$
	handle @dashboard {
		reverse_proxy 127.0.0.1:3343
	}
	@signin path_regexp ^/assistant/signin/[A-Za-z0-9_-]{40,64}(/keys|/done|/login|/code|/shot|/tap|/scroll)?\$
	handle @signin {
		reverse_proxy 127.0.0.1:3340
	}
	@missions path_regexp ^/assistant/missions/[A-Za-z0-9_-]{40,64}(/save)?\$
	handle @missions {
		reverse_proxy 127.0.0.1:3341
	}
	@activity path_regexp ^/assistant/activity/[A-Za-z0-9_-]{40,64}\$
	handle @activity {
		reverse_proxy 127.0.0.1:3342
	}
	handle /assistant/voice/webhook {
		reverse_proxy 127.0.0.1:3335
	}
	@realtime path_regexp ^/assistant/voice/realtime/[A-Za-z0-9_-]{16,128}\$
	handle @realtime {
		reverse_proxy 127.0.0.1:3334
	}
	handle {
		respond "Not found" 404
	}
}
CADDY
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || fail "The generated Caddyfile did not validate (domain: $DOMAIN)."
    systemctl enable caddy >/dev/null 2>&1 || true
    systemctl reload caddy 2>/dev/null || systemctl restart caddy
    say "HTTPS proxy ready for https://$DOMAIN (the certificate is issued on the first visit; allow a minute)"
  fi
fi

# ---------- 7. health ----------
say "Waiting for the gateway health check"
for i in $(seq 1 40); do
  if docker compose "${COMPOSE_FILES[@]}" ps --format json 2>/dev/null | grep -q '"Health":"healthy"'; then HEALTHY=1; break; fi
  sleep 3
done
[[ "${HEALTHY:-0}" -eq 1 ]] || say "The gateway is still starting; check in a minute with: docker compose ps"
# The PROCURATOR plugins must have loaded; a permission or path problem shows up only in the gateway log.
if docker compose "${COMPOSE_FILES[@]}" logs --tail 300 openclaw-gateway 2>&1 | grep -Eq 'unsafe plugin manifest path|plugin not found: (owner|procurator)-'; then
  say "WARNING: the gateway did not load the PROCURATOR plugins. See: docker compose logs --tail 80 openclaw-gateway"
fi

# ---------- 8. the one thing left: setup ----------
OWNER_SET=0
python3 - "$DEST" 2>/dev/null <<'PYOWNER' && OWNER_SET=1 || true
import sys, json, pathlib
try:
    d = json.loads((pathlib.Path(sys.argv[1]) / 'config' / 'owner.json').read_text())
except (OSError, ValueError):
    sys.exit(1)                     # no owner yet: a fresh install
sys.exit(0 if any(c.get('type') == 'telegram' and str(c.get('id', '')).isdigit() for c in d.get('channels') or []) else 1)
PYOWNER

echo
if [[ $OWNER_SET -eq 1 && $RECONFIGURE -eq 0 ]]; then
  cat <<EOF
PROCURATOR is up to date in $DEST and already paired to its owner.
  Dashboard: send /panel to your bot.   New setup link (e.g. a new phone):  sudo bash $DEST/bin/install.sh --setup-link --reconfigure
EOF
  exit 0
fi

if [[ -n "$DOMAIN" && $NO_PROXY -eq 0 ]]; then
  LINK=$(mint_link)
  cat <<EOF
==========================================================================================
  PROCURATOR is installed. One step left — open this link in your browser:

      $LINK

  It walks you through your AI key, your Telegram bot, and one tap on your phone.
  The link works once and expires in 24 hours (new one: sudo bash $DEST/bin/install.sh --setup-link).
==========================================================================================
EOF
else
  echo "PROCURATOR is installed. Finish setup in this terminal:  sudo python3 $DEST/bin/connect-accounts.py"
fi

# Prefer the terminal? Offer it when someone is at the keyboard (works with curl | bash: we read from the terminal, not the pipe).
if [[ -r /dev/tty && -t 1 ]]; then
  printf '\nPress Enter to finish here and use the link, or type T then Enter to set up in this terminal instead: '
  CHOICE=""
  read -r -t 600 CHOICE </dev/tty || true
  if [[ "${CHOICE,,}" == "t" ]]; then
    python3 "$DEST/bin/connect-accounts.py" </dev/tty || say "Setup did not finish. Run it again any time: sudo python3 $DEST/bin/connect-accounts.py"
  fi
fi
echo
echo "Optional later: phone calls (bin/connect-twilio.py), cloud browser (bin/connect-browserbase.py), Google sign-in (bin/set-oauth-client.sh google)."
echo "Upgrade any time by running this installer again; nothing in .env, config/ or state/ is replaced."
