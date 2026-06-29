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
systemctl enable --now cron >/dev/null 2>&1 || systemctl enable --now crond >/dev/null 2>&1 || true
(crontab -l 2>/dev/null | grep -v kalshi-update; echo "*/3 * * * * /usr/local/bin/kalshi-update.sh") | crontab -
systemctl start kalshi-paper kalshi-dashboard
echo "=== AUTO-UPDATE ENABLED - server pulls from GitHub every 3 minutes ==="
