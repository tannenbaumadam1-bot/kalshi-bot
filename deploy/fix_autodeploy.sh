#!/bin/bash
# Permanent auto-deploy via a systemd TIMER (reliable; cron-free).
# Pulls latest code every 3 min and restarts the bot only when it changed.
# Survives reboots and bot crashes. Run once; never touch the console again.
set -e
echo "Installing robust auto-deploy (systemd timer)..."
cd /opt/kalshibot
git fetch origin main
git reset --hard origin/main
pip3 install requests pyyaml --break-system-packages >/dev/null 2>&1 || true

cat > /usr/local/bin/kalshi-update.sh <<'UPD'
#!/bin/bash
cd /opt/kalshibot || exit 0
git fetch -q origin main || exit 0
L=$(git rev-parse HEAD 2>/dev/null); R=$(git rev-parse origin/main 2>/dev/null)
if [ -n "$R" ] && [ "$L" != "$R" ]; then
    git reset --hard -q origin/main
    systemctl restart kalshi-paper kalshi-dashboard
fi
exit 0
UPD
chmod +x /usr/local/bin/kalshi-update.sh

cat > /etc/systemd/system/kalshi-update.service <<'SVC'
[Unit]
Description=Kalshi auto-update (git pull + restart on change)
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/kalshi-update.sh
SVC

cat > /etc/systemd/system/kalshi-update.timer <<'TMR'
[Unit]
Description=Run Kalshi auto-update every 3 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
Persistent=true
[Install]
WantedBy=timers.target
TMR

# retire the unreliable cron entry if it exists
( crontab -l 2>/dev/null | grep -v kalshi-update ) | crontab - 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now kalshi-update.timer
systemctl restart kalshi-paper kalshi-dashboard
echo "=== AUTO-DEPLOY FIXED (systemd timer). Now running: $(git log -1 --oneline) ==="
systemctl list-timers kalshi-update.timer --no-pager | head -3
