#!/usr/bin/env bash
# Milestone 1 environment assessment. Read-only: installs nothing, changes nothing.
# Run on Ubuntu / WSL2:  bash scripts/env_check.sh
set -u
ok(){ printf "  [OK]   %s\n" "$*"; }; bad(){ printf "  [MISS] %s\n" "$*"; }

echo "== OS =="
uname -a
[ -f /etc/os-release ] && . /etc/os-release && echo "  $PRETTY_NAME"
grep -qi microsoft /proc/version 2>/dev/null && echo "  running inside WSL" || echo "  native Linux"

echo "== Tools =="
for t in python3 git docker; do
  if command -v $t >/dev/null; then ok "$t: $($t --version 2>&1 | head -1)"; else bad "$t not found"; fi
done
python3 -c 'import sys; assert sys.version_info >= (3,11)' 2>/dev/null && ok "python >= 3.11" || bad "python 3.11+ required"
python3 -m venv --help >/dev/null 2>&1 && ok "python3-venv" || bad "python3-venv (sudo apt install python3-venv)"
docker compose version >/dev/null 2>&1 && ok "docker compose" || bad "docker compose plugin"
docker info >/dev/null 2>&1 && ok "docker daemon reachable" || bad "docker daemon not reachable (start Docker Desktop / dockerd, or add user to docker group)"

echo "== Hermes Agent =="
if command -v hermes >/dev/null; then
  ok "hermes: $(hermes --version 2>&1 | head -1)"
  [ -d "$HOME/.hermes" ] && ok "~/.hermes exists" && ls "$HOME/.hermes" 2>/dev/null | sed 's/^/        /'
  [ -d "$HOME/.hermes/skills" ] && ok "skills dir: $HOME/.hermes/skills"
else
  bad "hermes CLI not on PATH"
fi

echo "== Clock (exchange timestamps need accurate time) =="
timedatectl 2>/dev/null | grep -Ei "synchronized|NTP" || date -u

echo "== Network to INDODAX (public, no key) =="
for u in https://indodax.com/api/server_time "https://api.indodax.com/api/v2/ticker/bookTicker?symbol=BTCIDR"; do
  code=$(curl -s -o /dev/null -m 10 -w "%{http_code}" "$u"); [ "$code" = 200 ] && ok "$u -> $code" || bad "$u -> $code"
done
