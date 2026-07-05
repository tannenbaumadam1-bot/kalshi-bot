# Polymarket Reward-Farming vs Kalshi Weather Edge (2026-07-05)

Honest side-by-side. Nothing here is proven for us yet; both must clear a paper
gate before real money. Models are transparent assumptions, not promises.

## 1. Sustainability & risk — the core difference

| | **Kalshi weather edge** (directional) | **Polymarket reward farming** (liquidity) |
|---|---|---|
| Where the money comes from | Being *more accurate* than the market (informational edge) | Polymarket *paying* LPs to quote (structural subsidy) |
| Is it proven? | **No** — currently negative / at the calibration gate | Program is real & measurable, but *our* net is unproven |
| Main risk | The edge may not exist / may be arbitraged away; high binary variance | Adverse selection + inventory; reward **dilution** as more LPs join; program can change |
| Capacity | **Tiny** — thin markets absorb only a few hundred $ before edges vanish | **Large** — deploy $1k–10k+, but net % falls with size |
| Sustainability | Only if we truly out-forecast — fragile | Sustainable while the subsidy exists & we manage inventory — sturdier |
| Effort | Model upkeep | Uptime, re-quoting, inventory management |

**Adverse selection cuts both ways but differently.** In weather we take a
directional view and hold to settlement — variance is high but we chose the bet.
In reward farming we quote *both sides* to earn the subsidy; informed traders
pick off our stale quotes (that's the adverse-selection cost, ~35% haircut in
the model). The rewards have to out-earn that. On big markets our share is tiny
(measured: **$500 = 0.15% of the qualifying pool** on the top market), so it's a
grind-out-small-yield game, not a jackpot.

## 2. Compound model — Polymarket (net APY after adverse selection, dilutes with size)

1-year value (base 35% APY scenario), from `compound_model.py`:

| Capital | 1 month | 1 year | Return |
|---|---|---|---|
| $100 | $102 | $134 | +34% |
| $500 | $512 | $662 | +32% |
| $1,000 | $1,022 | $1,301 | +30% |
| $5,000 | $5,077 | $5,982 | +20% |

Pessimistic 10% APY and optimistic 80% APY brackets are in the tool. Note the
return **falls as capital grows** (dilution). Empirical sources cite 40–120% APY
for disciplined makers — treat the top end with skepticism.

## 3. Compound model — Kalshi weather edge (IF real; ~$400 capacity ceiling)

1-year value (base 3%/bet edge, 30% of bank deployed/day):

| Capital | 1 year | Return |
|---|---|---|
| $100 | $1,157 | **+1,057%** |
| $500 | $1,814 | +263% |
| $1,000 | $2,314 | +131% |
| $5,000 | $6,314 | +26% |

The eye-popping $100 number is real *math* but rests on two shaky legs: the edge
is **unproven/currently negative**, and the smooth curve ignores brutal binary
variance. Crucially, watch the columns: by $5k the two strategies **converge**
(~$6k) because weather **caps out** — most capital sits idle in thin markets.

## 4. The honest read

- **Weather = a high-%, low-capacity engine.** If the edge is real it can turn
  $100 into a few hundred fast — then it hits a wall. It cannot absorb real size.
- **Reward farming = a lower-%, high-capacity, more *sustainable* engine.** It
  scales with capital and pays a structural subsidy, but it's a grind with real
  adverse-selection risk and dilution.
- They are **complementary, not competing**: different capacity, different risk
  source, low correlation.

## 5. Recommendation — run BOTH, paper-gated, capital by capacity

1. **Keep the Kalshi weather experiment running** (tiny stake). It's cheap, and
   it answers a real question: do we out-forecast? If v6-ens calibration passes
   the gate, it's a genuine edge for small capital.
2. **Start Polymarket reward farming on PAPER now** (built — see below). Let it
   accrue modeled net for a week, then calibrate `CAPTURE_EFF` to reality by
   deploying a *tiny* real stake (~$20–50) and comparing actual daily payouts to
   the model. Only scale after the paper/real numbers agree and are net-positive.
3. **Allocation as capital grows:** weather caps ~$300–500 no matter what, so
   send only that much there; route the rest to Polymarket reward farming once it
   proves out. At $100 total: split ~$50/$50 to learn both. At $1k+: weather stays
   ~$400, Polymarket takes the balance.
4. **Do not switch entirely to either.** Weather alone can't scale; Polymarket
   alone forgoes a possible real forecast edge and concentrates in one program's
   subsidy.

## What's built (this session)
- `compound_model.py` — the projections above (run it to re-model).
- `poly_client.py` — read-only Polymarket data (rewarded markets, books, reward
  configs, competing-liquidity estimate). Live-verified.
- `poly_paper.py` — reward-farming **paper** simulator with risk controls (max
  markets, ≤25%/market, min-size affordability, cash reserve, daily-net cap) and
  a conservative capture model calibrated to the empirical APY range.
- `mm_viability.py` — proves pure spread-capture MM is negative on both venues.

## To go LIVE on Polymarket (your steps — I can't)
Confirm your state allows it; you already have an account (complete KYC/funding if
not). Generate CLOB API credentials and share only what's safe (never a private
key via GitHub). Then I wire the live executor behind the same triple-lock we use
for Kalshi. **Not before** the paper sim shows net-positive against real payouts.
