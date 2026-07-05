# Market-Making & Maker-Rebate Research (2026-07-05)

**Bottom line:** Pure two-sided *spread-capture* market-making is **not viable**
for us on Kalshi **or** Polymarket — measured spreads on the liquid markets are
1–3¢, too tight to cover the round-trip maker fee, and professional MMs already
own that spread. The only genuine "fee-earner" path found is **Polymarket's
maker rebates + liquidity-rewards program** (Polymarket literally pays LPs), which
is *reward-farming*, not spread-capture — a real but separate project requiring a
new account, KYC, USDC funding, and a new API integration.

---

## Part 1 — Two-sided market-making on Kalshi (measured, live)

Pulled live order tops for **519 Kalshi markets that had real 24h volume**
(via `mm_viability.py`). What we found:

| Metric | Value |
|---|---|
| Highest-volume categories | Politics (PRESNOM), NBA, culture (Taylor Swift) |
| Most-liquid quartile — median spread | **1¢** |
| All-live median / mean spread | 3¢ / 4.0¢ |
| "Capture-net" on liquid markets (spread − 2 improve − 2 maker fees) | **−1 to −3¢ (negative)** |

Why it doesn't work:

1. **The liquid markets have 1–3¢ spreads.** You can't improve a 1–2¢ spread by a
   tick on each side and still have anything left; and the maker fee (25% of the
   7¢×p×(1−p) taker fee) rounds to ~1¢ per leg = ~2¢ round-trip. Captured spread
   < fees. Negative before you even consider risk.
2. **Professional MMs already own it.** Kalshi has ~23 active market makers; the
   top 3 provide ~70% of election liquidity and 80% of election volume trades
   within 0.5% of mid. We'd be competing against low-latency pros for a 1¢ edge.
3. **Adverse selection kills the wide-spread markets.** The markets with wide
   spreads are the *illiquid* ones (weather included). There, a REST-polling bot
   gets picked off: informed traders lift your stale resting quote right before
   settlement. Our own weather sweep already showed this.
4. **Latency.** MM profit is a latency game. A Python bot polling a public REST
   API every few seconds has no chance against co-located quoting engines.

**Adverse-selection sensitivity** (3¢ spread @ 50¢ mid): EV is already −0.5¢/round-trip
at *zero* adverse selection and only worsens — break-even requires a spread and
fill quality we don't have. **Decision: do not build a Kalshi MM module.**

---

## Part 2 — Platforms that actually pay makers (rebates)

| Platform | Maker economics | US-legal (2026) | API / bots | Verdict for us |
|---|---|---|---|---|
| **Polymarket US** | **Maker fee $0** + **20–25% of taker fees rebated daily** + separate **daily USDC liquidity rewards** for resting orders near mid | **Yes** — CFTC DCM, US exchange launched Dec 2025, waitlist removed May 2026 (blocked in 8 states: AZ, IL, MA, MD, MI, MT, NV, OH) | **Yes** — CLOB API (23 REST + 2 WS endpoints), automated trading explicitly permitted | **The fee-earner path.** Reward-farming, not spread-capture. |
| Robinhood Predictions | No user fees, but **powered by Kalshi**; retail app, no maker-rebate program, no MM API | Yes | No trading API for MM | No |
| PredictIt | Takes a cut of *profits* + withdrawal fees; $850 position cap; tiny markets | Yes (limited) | Weak | No |
| Betfair Exchange | Commission on net winnings (~2–5%), no maker rebate of the kind we want | No (not US) | Yes (intl) | No |

**Only Polymarket pays makers.** And note: its *liquid* markets (measured live,
World-Cup-heavy right now) run ~1¢ spreads too — so the income there is **not**
from capturing spread, it's from the **rewards/rebates** Polymarket pays you to
keep orders resting near mid. 32 of the top 39 markets we sampled were in an
active rewards program.

---

## Part 3 — What it would take to expand the bot to Polymarket (reward-farming)

This is a genuine project, not a config flip. Honest scope:

**You must do (I can't):**
1. **Confirm your state is allowed** (not one of the 8 blocked). *Not legal advice —
   verify current rules yourself.*
2. **Create a Polymarket US account + KYC** in the official iOS app (ID + proof of
   address). API access is gated on a verified account.
3. **Fund with USDC** (Polygon). Start tiny, like the $100 weather paper stake.
4. Generate an **API key / signing wallet** for the CLOB and hand me only what's
   safe (never a private key in GitHub — same rule as the Kalshi key).

**I would build:**
1. **`poly_client.py`** — read-only first: pull markets, order books, and the
   *rewards config* per market (min size, max spread from mid to qualify).
2. **A reward-farming paper simulator** — post two-sided quotes at the reward
   band, estimate daily rewards vs. adverse-selection/inventory losses, prove it's
   net-positive on paper *before* any real USDC (same gate discipline as weather).
3. **Inventory & risk controls** — cap exposure per market, auto-cancel/re-quote
   on moves, skip markets near resolution (adverse-selection window).
4. **Live executor** behind the same triple-lock we use for Kalshi live.

**The honest risk:** reward-farming is *not* free money. You earn rewards for
quoting near mid, but you also take on inventory that can move against you. Net
profit = rewards + rebates − adverse selection − inventory losses. It pencils
out *only* if the rewards are rich relative to volatility and you manage
inventory well. It should be proven on paper, on real reward-config data, before
funding — exactly the gate philosophy we already use.

---

## Recommendation

- **Do not** build Kalshi two-sided MM (measured negative; `mm_viability.py`
  reproduces the evidence any time).
- **Keep** the maker-only weather entries on Kalshi — that already makes us
  effectively fee-*free* at probe size (maker fee rounds to ~$0).
- **If** you want to pursue becoming a fee-*earner*, **Polymarket reward-farming**
  is the only real avenue. It's a multi-step expansion (account/KYC/funding +
  new integration). Say the word and I'll start with the read-only `poly_client.py`
  + reward-farming paper simulator so we can measure whether it's actually
  net-positive before you fund anything.
