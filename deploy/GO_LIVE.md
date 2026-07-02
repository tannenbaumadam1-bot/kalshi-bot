# Switching to REAL money (when you're ready)

The transition is seamless because real money runs the SAME strategy you
proved on paper - only the account + key change. Do this ONLY after the
paper results justify it, and start small.

## Steps
1. On kalshi.com (your REAL, verified account), create an API key.
   Download the private key file and save it in this folder as:
       kalshi-live.key
2. Open config_live.yaml and replace PASTE_YOUR_LIVE_KEY_ID_HERE with the
   Key ID Kalshi shows you. Save the file.
3. (Recommended) Fund the account with a SMALL amount first (e.g. $20-50).
4. Double-click  13_go_live_REAL_MONEY.bat  and type LIVE to confirm.

## Built-in safety
- It refuses to start until a real key is in place (no accidental launches).
- It requires typing LIVE to confirm, plus internal --i-understand-live gate.
- Conservative caps in config_live.yaml: max $2/position, $15 deployed,
  auto-halt after $3 daily loss. Tune these once you trust it.
- Watch it the same way: 11_dashboard.bat and 10_paper_report.bat read the
  same logs. (Real fills now appear in logs/trades.csv.)

## Recommended order
paper test (weeks) -> small real money ($20-50) -> scale only if it works.
