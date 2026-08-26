# BOOK 5 (TICK) AUTOPSY — 2026-08-26

**Adam's question: "is this P&L legitimate?"**

## No. The +$304.07 was fabricated.

The arithmetic refutes it before any code is read. The book has **$100** of paper capital and trades **5 contracts** per entry. A binary contract pays at most 100¢, so the theoretical maximum profit is **~$5 per window**. Six settled windows cannot produce $304 — the number was ~10x beyond the ceiling of the strategy as designed.

### What actually happened

Position sizes in the settled ledger, against a designed `SIZE=5` and `MAX_POS=20`:

| window | contracts | should have been |
|---|---|---|
| silver 18:15 | 70.38 | ≤ 5 |
| gold 18:30 | 71.35 | ≤ 5 |
| wti 06:30 | 219.86 | ≤ 5 |
| wti 08:45 | 168.66 | ≤ 5 |
| gold 09:30 | 275.91 | ≤ 5 |
| **wti 10:15** | **677.55** | ≤ 5 |

That last position was 677 contracts at 92¢ = **$623 of collateral on a $100 book**. Six wins, zero losses — a 6-0 record that looks like edge and is actually just a size bug compounding a lane that buys ≥90% favourites.

### Root cause

`check_fills` called `_fill` **once per print** for up to `SIZE` contracts **each**:

```
for t in prints:
    ...
    n = min(float(t["ct"]), float(SIZE))
    self._fill(q, n, our)          # <- every print, forever
```

A resting order for 5 contracts can fill **5 contracts in total, ever**. This filled 5 *per print*, and there were 361 strict fills. In a book printing every second, one quote became hundreds of contracts within a single window.

### Contributing cause

`MAX_POS` and the capital cap were enforced **only when posting a quote**. Quoting is an intention; a *fill* is what actually consumes the book. Nothing checked the caps at the moment inventory was created, so both limits were decorative.

### A third defect found while auditing

The exit lane credited itself an **automatic calibration win**, on the reasoning that "the market agreeing with us IS the model being right." That is the **8/17 `sold_net` winner-selection bias wearing a new hat**: we exit the trades that are *working*, so scoring every exit as a win guarantees a flattering calibration table however bad the model is — and the calibration table is the entire deliverable of this phase.

## Fixes shipped (commit 257c235, 582 tests green)

1. **A resting order fills at most its own size.** Per-quote fill progress is tracked and carries across cycles, so rebuilding quotes every 20s cannot re-open the tap.
2. **Caps enforced inside `_fill`** — trimmed to the room genuinely left, returning the size actually taken so the caller stops when a cap bites. Position cap and capital cap both.
3. **Exits are parked and graded against Kalshi's real result**, not self-credited. P&L unaffected; the calibration table now covers every prediction made, not only the ones we chose to sit through.
4. **Era `tick1` → `tick2`.** A ledger built over a broken constraint cannot be repaired in place — the project rule, fifth application. The $304 is gone, as it should be.

## The honest status of Book 5

**Zero evidence either way.** Every settled row in the old ledger is void. The clock restarts at 0/200. What we know so far is only that the plumbing works: real strikes, real books, live Pyth prices, basis correction applied, sane model output (gold 52.2% vs book 55/56; silver 61.1% vs 73/74).

That is genuinely worth something — three days ago none of it existed — but it is not edge, and the six "wins" prove nothing.

## Free real-time price sources — the answer is thin

The 13-day trial matters, so this was tested rather than assumed:

| source | verdict |
|---|---|
| **Pyth Hermes public** | **Dead.** Auth mandatory since 2026-07-31; 401 without a key. |
| **Yahoo `GC=F` (gold futures)** | **Unusable — 606s stale.** Ten minutes of lag against a 15-minute window. |
| Yahoo `XAUUSD=X` spot | 404, symbol not served |
| Stooq `xauusd` | 404, endpoint gone |
| Pyth demo (current) | Works for XAU + XAG; **WTI not in plan**; `Metal.Index.GOLD/USD` — the actual settlement feed — **not in plan** |

**Kalshi's own data is the one free, authoritative source**, and it deserves emphasis: each window's **strike IS the settlement feed's value at that window's open**. That gives a perfectly-calibrated reading every 15 minutes, for free, forever — it is what makes the basis correction possible. What it cannot give is *intra-window* movement, which is what the model needs.

So the realistic options are: pay Pyth after the trial, find a broker/exchange API with real-time metals (most require an account and many are paid), or **restrict the strategy to what Kalshi's own book can support** — the pair tracker and arb scanner already run without any external feed.

## Recommendation

Do not spend a dollar on data yet. The trial covers roughly the evidence window the book needs. **Let the calibration table fill up on honest numbers and decide on evidence**, exactly as we did with phantom. If the model proves calibrated, a data subscription is a rounding error against the opportunity. If it doesn't, we saved the subscription.

## The durable lesson

This is the **fourth** time this project has produced a spectacular P&L that was an accounting artefact: the 8/17 `$137` ATH (stale marks), the 8/20 phantom `+$49` (look-ahead fills), the 8/24 bucket ledger (first-exit grading), and now this. The pattern is identical every time — **the number that makes you excited is the number to audit first**, and the fastest audit is not reading code but asking whether the strategy's own ceiling permits the result. Five seconds of arithmetic beat an hour of debugging here.

Adam asked the right question. That instinct — "is this legitimate?" — has now caught three of the four.
