# Weather go-live runbook (v2 executor, 2026-07-18)

The executor (`weather_live.py`) runs the SAME v7/v8 brain as the paper book:
maker-only entries, multi-strike, probe stakes (<=60c/bet) until the live book
passes its own 30-bet gate, forecast-based exits, hard caps from
`config_live.yaml` ($2/bet, $15 open, $3 daily-loss halt).

It is deployed but SAFE by default: without the arm conditions it runs in
DRY mode (logs would-be orders, sends nothing).

## Pre-flight (already done / no money involved)
- [x] Executor deployed, running DRY on the droplet, state on the dashboard
- [ ] Watch DRY mode for a few days: order intents should mirror the paper book
- [ ] Weather v7-obs gate formally passed at ~90 settled (check dashboard)

## Adam's part (~30 min, when ready)
1. Kalshi account: KYC complete, deposit **$100**.
2. Create a LIVE API key on kalshi.com -> note the Key ID, download the
   private key file.
3. In the DO web console (cloud.digitalocean.com -> droplet -> terminal),
   never via GitHub:
   - save the private key as `/opt/kalshibot/kalshi-live.key`
     (type it or use the console upload; `chmod 600` it)
   - put the Key ID into `/opt/kalshibot/config_live.yaml` under
     `api.key_id` (replacing the PASTE placeholder)

## Claude's part (one action, together with Adam)
```
cp /opt/kalshibot/deploy/kalshi-weather-live.service /etc/systemd/system/
systemctl daemon-reload
touch /opt/kalshibot/logs/LIVE_ARMED
systemctl enable --now kalshi-weather-live
```
Verify within one cycle (~10 min): dashboard LIVE badge shows mode LIVE,
balance ~$100, first maker orders resting.

## Kill switch (either of these stops all new orders immediately)
```
systemctl stop kalshi-weather-live
rm /opt/kalshibot/logs/LIVE_ARMED
```
Built-in halts: $3 daily loss -> no new bets until midnight; balance reserve
$2; resting orders auto-cancel after 4h unfilled.

## Judgment period (2 weeks)
Run live + paper side by side. Compare on the dashboard:
- fill rate: live maker orders vs paper's optimistic instant fills
- expectancy/bet on era `live1` vs `v7-obs` over the same dates
Auto-revert rule: if the daily halt trips twice in a week or live expectancy
diverges clearly negative vs paper, stop the service and go back to paper.

## Optional dress rehearsal (demo exchange, fake money)
```
KALSHI_ENV=demo KALSHI_DEMO_KEY_ID=<id> KALSHI_DEMO_KEY_PATH=kalshi-demo.key \
  python3 weather_live.py --once
```
