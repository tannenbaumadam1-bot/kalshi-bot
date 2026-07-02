#!/bin/bash
# Switches the server to pull code from GitHub automatically every 3 minutes.
# Preserves the running portfolio (logs) and the dashboard token.
set -e
echo "Enabling GitHub auto-update on this server..."
systemctl stop kalshi-paper kalshi-dashboard 2>/dev/null || true
apt-get install -y git >/dev/null 2>&1 || true
mv /opt/kalshibot/logs /tmp/kalshi-logs 2>/dev/null || true
rm -rf /opt/kalshibot
git clone -q https://github.com/tannenbaumadam1-bot/kalshi-bot.git /opt/kalshibot
mkdir -p /opt/kalshibot/logs
mv /tmp/kalshi-logs/* /opt/kalshibot/logs/ 2>/dev/null || true
pip3 install requests pyyaml --break-system-packages >/dev/null 2>&1 || true
cat > /usr/local/bin/kalshi-update.sh <<'UPD'
#!/bin/bash
cd /opt/kalshibot || exit 0
old=$(git rev-parse HEAD 2>/dev/null)
git fetch -q origin main
git reset --hard -q origin/main
new=$(git rev-parse HEAD 2>/dev/null)
[ "$old" != "$new" ] && systemctl restart kalshi-paper kalshi-dashboard
UPD
chmod +x /usr/local/bin/kalshi-update.sh
cat > /etc/systemd/system/kalshi-update.service <<'SVC'
[Unit]
Description=Kalshi auto-update
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
systemctl daemon-reload
systemctl enable --now kalshi-update.timer
systemctl start kalshi-paper kalshi-dashboard
echo "=== AUTO-UPDATE ENABLED - server pulls from GitHub every 3 minutes ==="
