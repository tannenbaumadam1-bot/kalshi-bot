# Strategy rankings (updated 2026-07-06)

ALL strategies - running paper books AND candidates - ranked for viability,
return, and scalability toward Adam's $2k/week income target. Rule: nothing
goes live (and nothing running scales up) without a backtest/calibration gate
clearing positive expectancy net of real fees. Sizing is earned, not assumed.

## 1. +EV sharp-line anchoring  [CANDIDATE - QUEUED: backtest next]
Devigged sharp-sportsbook consensus as fair value; buy Kalshi/Poly sports
contracts priced away from it beyond fees+spread. No forecasting model needed;
exchanges don't ban winners (kills classic +EV death at soft books).
- Venue: Polymarket first (maker $0 + rebates; Kalshi taker 1.75c@50c ~3.5% of
  basis is brutal where sports trade; Kalshi legs = resting maker only).
- VERIFY at build: both fee schedules; Polymarket state-block list (8 states).
- Cost: odds-feed API ~$0-100/mo. Capacity: best on the board (liquid sports).
- Subsumes favorite-longshot bias as a special case.

## 2. Polymarket reward farming  [RUNNING - paper, day 1]
Structural subsidy (~$5M/mo pool), the most credible income stream currently
running. Modeled 20-70% APY but CAPTURE_EFF=0.08 is a guess that could be off
20x either way - the gate here is calibrating vs a FIRST REAL PAYOUT on tiny
live stake before scaling. Capacity: good (dilutes gently with size).

## 3. Cross-venue arb, sports/crypto <48h  [CANDIDATE]
Locked-in spread, mechanical, highest certainty per trade, LOW capacity
(flow-limited; fine at $1k, thin by $10k+). Legging risk needs an
unwind-at-loss-cap rule before trade #1. Shares matcher/client infra with #1
- build as its sidecar, not standalone. Pre-game + short-dated crypto only;
live in-game is a latency war - skip.

## 4. Weather edge on Kalshi (v6-ens)  [RUNNING - paper, gate 0/30]
The proving ground. Legacy record is negative (fees + overconfidence);
v6 ensemble unproven until the 30-bet calibration gate answers (~1-2 wks).
Capacity tiny (~$400 ceiling) - its real value is testing whether our
model-building pipeline can produce calibrated edge AT ALL. Don't scale; let
the gate decide. If gate fails: park weather, keep the ensemble tooling.

## 5. Weather model -> Polymarket weather markets  [CANDIDATE - conditional]
Reuse v6-ens iff #4's gate passes. Lowest effort, tiny capacity. Wait.

## 6. Crypto funding carry  [RUNNING - paper, day 1]
Math is real and uncorrelated, but the live US path is only ~11% APY majors
carry (Coinbase Derivatives/Kraken/CME) - Hyperliquid alt rates are
DATA-ONLY for US persons. Scalable but low %. Keep as paper research +
diversifier; not a path to the income target.

## 7. Favorite-longshot harvesting  [CANDIDATE - subsumed by #1]
Only revisit standalone if #1's backtest fails overall but this slice works.

## 8. NegRisk / multi-outcome arb scanner  [CANDIDATE - add-on]
One-day build on #3's scanner. Scraps behind pro bots. Free option.

## 9. Copy-trading top Polymarket wallets  [PARKED]
Direction without price; leaderboards = lucky bettors + MMs + reward farmers.
Only the flow-as-signal variant is even testable. Not worth build time now.

## Scale reality check ($2k/week target)
These are 0.5-2%/week strategies; $2k/wk needs ~$100k+ working capital OR the
product route (broad "prediction-markets terminal" SaaS; arb-alert-only
self-cannibalizes). Near-term: prove ANY edge at 1%+/wk paper->small-live
(#1 backtest + #2 payout calibration + #4 gate are the three live questions),
compound, then revisit scale.
