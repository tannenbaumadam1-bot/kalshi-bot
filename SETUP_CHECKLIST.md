# Kalshi Bot - Setup Checklist

Status as of the latest session. `[x]` = done.

## Done
- [x] 1. Bot built and tested (12/12 logic tests pass)
- [x] 2. Config, one-click buttons, guides created
- [x] 3. Install Python (3.14.6, added to PATH)
- [x] 4. Create free Kalshi demo account
- [x] 5. Create demo API key + key saved as kalshi-demo.key + key_id in config.yaml
- [x] 6. Install requirements
- [x] 7. Connection test - CONNECTED, sees $100.00 demo balance
- [x] 8. Updated bot for Kalshi's new "fixed-point" API format
- [x] 9. Dry run works - scans live demo markets, proposes maker orders, places nothing

## Your call next
- [ ] 10. Trade on demo (fake money) - double-click 4_trade_on_demo.bat
        (this places real orders on the DEMO sandbox; it's fake money.
         Claude won't click this for you - placing orders is your call.)
- [ ] 11. Let it run a while, then review logs/trades.csv vs fees
- [ ] 12. Decide on real money - ONLY if demo genuinely beats fees

## Notes
- Private key lives in this folder as kalshi-demo.key (never share it).
- API Key ID is in config.yaml (safe).
- environment: demo  = fake money. Leave it until demo proves itself.
