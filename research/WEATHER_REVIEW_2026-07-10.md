# Weather strategy full review — 2026-07-10

> **STATUS (same day): all 7 recommendations implemented as era v7-obs, plus an
> 8th, bigger fix discovered during implementation — the scanner derived the
> forecast date from close_time, which is the NEXT UTC day for these markets,
> so v2–v6 priced every bet off the WRONG day's forecast (+1). v7 parses the
> settlement day from the ticker. See commit "weather v7-obs".

Data: live /public @ 16:06 UTC. v6-ens era: 44 settled with outcomes, 16 exits, 18 open.
All stats verified programmatically (binomial tests + Brier scores).

## Headline verdict

**The v6 ensemble has no demonstrated edge vs the market.** Actual win rate 22.7% vs
market-implied entry 24.7% (p=0.46 — indistinguishable from "market is right").
Blended-model Brier 0.187 vs market-mid Brier 0.165: the market forecasts our own
bets better than our model does, even after 65% shrinkage toward the market.
Still 14pts overconfident (pred 36.8% vs act 22.7%, p=0.034). The gate is correctly
holding probe mode. Compounding is sign-multiplication: until expectancy > 0,
scaling/volume/Kelly only compound losses faster.

## What is going right

1. **Risk plumbing works.** v6 lost only $4.00 over 44 bets (probe ≤60c) vs v2's
   −$37.10 over 14. Exposure 13% of NAV (was 74–90%). Maker entries cut fees to
   ~1c/bet (fee drag was 55% of all losses pre-v6).
2. **The gate is doing its job** — refused to scale into a negative-expectancy model
   twice. Sizing is earned, never assumed. This is why the account still has $72.
3. **Honest instrumentation**: era tagging, calibration buckets, shadow logging
   (~40 markets/scan raw-model data), forecast-based exits (16 exits cost only
   −$1.18 total vs riding losers to zero), watchdog uptime fixes.
4. **v5-cal footnote**: n=12, act 33.3% vs pred 34.1%, +$4.51 — the best-calibrated
   era was the simpler GFS-only ensemble. The multi-model KDE (v6) has not
   demonstrably improved calibration.

## What is going wrong

1. **No edge at 24–48h lead.** At that horizon our inputs are the same public
   models the market prices in. Conditional on disagreement, the market wins —
   adverse selection, exactly like the earlier sharp-EV finding on MLB lines.
2. **Low-market skew: 40/44 bets are LOW-temp markets** and that's where we lose
   (act 20% vs pred 36.5%; highs: 4 bets, act 50%). The model's daily-min
   distribution systematically disagrees with the market and is wrong. Suspects:
   climate-day windowing of hourly mins, sunrise-min sharpness, station
   microclimate. Highs look fine but n=4.
3. **Exit→re-enter churn loop.** Phoenix 92-lo exited 5×, Philly 74-lo 5×
   (median hold 2.4h). Root cause: entry values bets with the BLEND
   (0.35·model+0.65·market) but exit_check values them with RAW p_new — and the
   raw model swung 0.92→0.29 within hours. Inconsistent valuation + no cooldown
   = pay the spread repeatedly on model noise.
4. **Cheap entries are the loss center.** 15–30c entries: act ~15%. 30c+ entries:
   n=10, act 50% vs 38.5c avg entry — the only +EV bucket. Consistent with every
   prior era ("cheap = tail = worst calibration").

## Improvements (ranked by expected impact)

1. **Move to observation-anchored same-day bets (nowcasting).** After ~2pm local
   the day's high is mostly realized; ingest live METAR obs (api.weather.gov)
   and bet P(max ≥ strike | running max). This is where real information edge
   exists; 24–48h forecast bets fight an efficient market with public data.
   Biggest yield lever on the board.
2. **Fix the churn loop**: (a) 12h re-entry cooldown per ticker after exit;
   (b) exit_check must value holds with the same blend as entry;
   (c) require the exit signal to persist 2 consecutive scans.
3. **Raise MIN_PRICE 15→~30c** (or lo-markets only). Would have flipped v6 P&L
   positive. Sample is small (n=10 winners) — do it, but keep shadow-logging the
   15–30c band to confirm.
4. **Cap lo-market concentration** (≤50% of open book) until the shadow report
   proves lo-calibration; separately, extend weather_backtest to measure daily-min
   MEAN error (bias), not just MAE.
5. **Fit MODEL_WEIGHT empirically** from shadow data (weight minimizing Brier of
   blend). If optimal weight ≲0.1, the forecast edge is dead — retire forecast
   bets and keep only nowcast bets. Stop guessing weights per era.
6. **Expose `weather_shadow.py --report` on /public** so calibration can be judged
   without DO-console access; it accrues ~10× faster than settled bets and is the
   decisive dataset for #5.
7. **When the gate ever passes**: size Kelly on calibration-corrected probabilities
   (fit an isotonic/beta map from pside→actual on settled data), not raw pside —
   overconfident probs inflate f* and oversize exactly the worst bets.

## Portfolio context

Weather probe burn is ~$0.5/day — cheap data collection, fine to continue.
Volume + compounding near-term should come from the other books (sharp-EV probe,
poly rewards ~34% APY paper, funding carry ~27% APY paper), not from scaling an
unproven weather model.
