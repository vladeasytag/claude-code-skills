#!/bin/bash
# Launch the hybrid realtime voice server behind the PERMANENT Tailscale Funnel
# (https://dst-box.<tailnet>.ts.net -> 127.0.0.1:8478). The funnel config lives in
# tailscaled (systemd, survives reboots); this script only (re)starts server.py.
# The secret path persists so the URL stays stable; pass --rotate for a new secret.
# Fallback: --quick uses an ephemeral Cloudflare quick tunnel (no Tailscale needed).
cd "$(dirname "$0")"
exec 9>.lock
# Wait out a concurrent starter (lock releases when it exits). The server itself
# must NOT inherit fd 9 (see 9>&- below) or every later start.sh would give up here.
flock -w 30 9 || { echo "start.sh: lock held too long — is another starter stuck?"; exit 1; }

[ "$1" = "--rotate" ] && rm -f .secret
[ -s .secret ] || head -c16 /dev/urandom | xxd -p | tr -d ' \n' > .secret
SECRET=$(cat .secret)

pkill -f "realtime/server.py" 2>/dev/null
fuser -k 8478/tcp 2>/dev/null   # server may have been started as plain "python3 server.py"
sleep 1
nohup python3 server.py 9>&- >> server.log 2>&1 &

if [ "$1" = "--quick" ]; then
  pkill -f "cloudflared tunnel --url http://127.0.0.1:8478" 2>/dev/null
  : > tunnel.log
  nohup ./cloudflared tunnel --url http://127.0.0.1:8478 >> tunnel.log 2>&1 &
  for i in $(seq 1 30); do
    HOST=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1)
    [ -n "$HOST" ] && break
    sleep 1
  done
else
  HOST=$(tailscale funnel status 2>/dev/null | grep -o 'https://[a-z0-9.-]*\.ts\.net' | head -1)
fi
[ -z "$HOST" ] && { echo "no tunnel host — is tailscaled/funnel up? (try --quick)"; exit 1; }

echo "$HOST/$SECRET/" > url.txt
echo "$HOST/$SECRET/"
