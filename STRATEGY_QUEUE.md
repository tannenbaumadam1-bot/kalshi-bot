# Strategy queue (updated 2026-07-06)

Ranked candidates. Rule: nothing goes live without (1) a historical backtest
clearing positive expectancy net of real fees, then (2) the standard paper
calibration gate. Sizing is earned, not assumed.

## 1. +EV sharp-line anchoring on prediction markets  <- QUEUED: backtest first
Use devigged sharp-sportsbook consensus (Pinnacle-style) as fair value; buy
Kalshi/Polymarket sports contracts priced > (edge threshold + fees + spread)
away from it. No forecasting model needed. Exchanges don't ban winners, so the
classic +EV death (soft books limiting accounts) doesn't apply.
- Venue preference: Polymarket first (taker fees ~0 vs Kalshi's 0.07*P*(1-P)
  ~= 1.75c/contract at 50c, ~3.5% of basis - brutal exactly where sports trade).
  VERIFY current fee schedules + Polymarket US access before build.
  Kalshi leg only via resting maker orders (zero maker fee).
- Cost: odds-feed API (~$0-100/mo). Backtest: historical sharp closes vs
  venue price history; edge must clear fees with margin.
- Capacity: best of all candidates (liquid major-sport markets).
- Subsumes the favorite-longshot bias play (that's one slice of this).

## 2. Cross-venue arb (Kalshi <-> Polymarket, sports/crypto, <48h resolution)
Locked-in spread when the same event prices differently. Mechanical edge,
highest certainty, LOW capacity (flow-limited; fine at $1k, thin by $10k+).
Legging risk is the killer: unwind-at-loss-cap rule required before trade #1.
Scanner shares matcher/client infra with #1. Pre-game + short-dated crypto
only; live in-game is a latency war - skip until infra proves out.

## 3. Weather model -> Polymarket weather markets
Reuse v6-ens if/when it passes the Kalshi calibration gate. Lowest effort,
highest return-per-effort IF gate passes; tiny capacity (thin markets).
Conditional - wait for the gate, don't pre-build.

## 4. Favorite-longshot harvesting (standalone)
Documented bias, fee-sensitive, scales well (liquid favorites). Only worth
building standalone if #1's backtest fails but shows the bias slice works.

## 5. NegRisk / multi-outcome arb scanner
Scraps behind pro bots; one-day build as an add-on to #2's scanner. Free option.

## 6. Copy-trading top Polymarket wallets
Weakest: you get their direction without their price; leaderboards are full of
lucky concentrated bettors + reward farmers. Only the flow-as-signal variant
is even testable. Park it.

## Scale reality check ($2k/week target)
These are 0.5-2%/week strategies. $2k/wk needs ~$100k+ working capital OR a
products/business route (terminal/scanner SaaS - arb-alert-only product self-
cannibalizes; broader terminal doesn't). Near-term goal: prove any edge at
1%+/week paper->small-live, compound, revisit.
