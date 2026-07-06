#!/bin/bash
# One-time server hardening: stops the dashboard from going "always down".
# Fixes the two real causes on a 1GB droplet: (1) out-of-memory kills,
# (2) systemd giving up after a few rapid restarts. Safe to re-run.
set -e
echo ">> 1/3  Adding 1GB swap (prevents out-of-memory kills)..."
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "   swap added."
else
  echo "   swap already present."
fi

echo ">> 2/3  Making the services NEVER give up restarting..."
for s in kalshi-paper kalshi-dashboard; do
  mkdir -p /etc/systemd/system/$s.service.d
  cat > /etc/systemd/system/$s.service.d/override.conf <<OVR
[Unit]
StartLimitIntervalSec=0
[Service]
Restart=always
RestartSec=10
OVR
done
systemctl daemon-reload
systemctl reset-failed kalshi-paper kalshi-dashboard 2>/dev/null || true

echo ">> 3/3  Restarting both services..."
systemctl restart kalshi-paper kalshi-dashboard
sleep 2
echo "-------------------------------------------"
systemctl --no-pager --lines=0 status kalshi-dashboard | head -4
echo "-------------------------------------------"
echo "DONE. Your dashboard link should work now and stay up."
cat /root/DASHBOARD_LINK.txt 2>/dev/null || true
