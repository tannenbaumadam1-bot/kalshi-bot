#!/usr/bin/env bash
# One-shot setup: installs deps and runs the paper bot + dashboard 24/7
# via systemd. Run as root from the extracted app folder:
#     sudo bash deploy/setup_paper_server.sh
set -e

APP="$(cd "$(dirname "$0")/.." && pwd)"
echo ">> App directory: $APP"

echo ">> Installing Python + tools..."
apt-get update -y
apt-get install -y python3 python3-pip ufw curl

echo ">> Installing Python packages..."
pip3 install requests pyyaml --break-system-packages 2>/dev/null \
  || pip3 install requests pyyaml

# random read-only dashboard token
TOKEN="$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 14)"

echo ">> Writing services..."
cat > /etc/systemd/system/kalshi-paper.service <<UNIT
[Unit]
Description=Kalshi paper-trading bot (live data, no money)
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=$APP
ExecStart=/usr/bin/python3 $APP/paper.py --config=config_cloud.yaml --start=100
StartLimitIntervalSec=0
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/kalshi-dashboard.service <<UNIT
[Unit]
Description=Kalshi paper dashboard (web)
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=$APP
Environment=DASH_HOST=0.0.0.0
Environment=DASH_PORT=8765
Environment=DASH_TOKEN=$TOKEN
ExecStart=/usr/bin/python3 $APP/dashboard.py
StartLimitIntervalSec=0
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT

echo ">> Enabling + starting services..."
systemctl daemon-reload
systemctl enable --now kalshi-paper.service kalshi-dashboard.service

echo ">> Opening dashboard port in the firewall..."
ufw allow 22/tcp >/dev/null 2>&1 || true
ufw allow 8765/tcp >/dev/null 2>&1 || true
yes | ufw enable >/dev/null 2>&1 || true

IP="$(curl -s --max-time 5 ifconfig.me || echo YOUR_SERVER_IP)"
echo "http://$IP:8765/?token=$TOKEN" > /root/DASHBOARD_LINK.txt
echo ""
echo "=================================================================="
echo "  DONE - the bot is now running 24/7."
echo ""
echo "  Open your dashboard from ANY device:"
echo "    http://$IP:8765/?token=$TOKEN"
echo ""
echo "  (Keep this link private - the token is your password.)"
echo ""
echo "  Useful commands:"
echo "    systemctl status kalshi-paper       # is the bot running?"
echo "    journalctl -u kalshi-paper -f       # watch the bot live"
echo "    systemctl restart kalshi-paper      # restart it"
echo "=================================================================="
