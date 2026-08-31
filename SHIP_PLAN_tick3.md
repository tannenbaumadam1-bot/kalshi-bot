# `tick3` SHIP PLAN — from −$1,721 to a book worth scaling

Written against live code at **origin/main = `3063f7b`** (the droplet is on this commit) and the 200-row ledger + 2,825 shadow observations from `/public` on 2026-08-31.

Six changes, in dependency order. **Measured value of the first three: roughly +$180 on the tape we already have.** Everything is in one file, `tick_paper.py`, which has 113 existing tests.

---

## ⚠️ PHASE 0 — DO THIS FIRST OR EVERYTHING ELSE IS DANGEROUS

**Your laptop folder is 30+ commits stale.**

```
C:\Users\tanne\Claude\Projects\Kalshi Bot   HEAD = 862faef   (Aug 24)
origin/main                                        = 3063f7b   (Aug 31, live on droplet)
```

`tick_paper.py` **does not exist in your local folder at all.** If anyone edits that folder and runs `2_push_updates.bat`, the bat sweeps uncommitted changes into a commit and pushes — which would **revert the droplet to Aug 24 code and delete the entire tick book.** This is the same trap as the 8/5 resync.

**Adam, run this in the project folder before any code work:**

```
git pull origin main
```

Then confirm `tick_paper.py` is present and `git rev-parse HEAD` starts with `3063f7b`. Nothing below is safe until that's true.

---

## PHASE 1 — STOP THE BLEEDING

Two changes. They do not require the model to be right about anything.

### 1. Add a price stop. There isn't one. *(measured: +$68.50)*

**The finding that changes the fix:** `STOP_P = 0.45` is not a 45¢ price stop. The line is

```python
broken = p_side < STOP_P     # check_exits, ~line 1610
```

— a threshold on **the model's probability**, evaluated inside `check_exits`, which is guarded by five separate `continue`s (`not m`, `yes_bid is None`, `not pa`, `sig is None`, `proxy_dead`). Any one of them silently skips the cycle. **Nothing in this book distinguishes "I can't find a new opportunity" from "I can't protect an open position."** That is why eight positions rode from 75–87¢ to zero, and it is the same failure class as the 8/24 cancel-410 and the 8/27 blind book: an instrument that fails silently.

**Build:** a new `check_stops(mkts)` that runs **before** `check_exits` in `step()`, needs nothing but a bid, and is guarded by nothing else:

```python
STOP_PX_C = float(os.environ.get("TICK_STOP_PX", "45"))

def check_stops(self, mkts):
    """PRICE stop. Deliberately independent of the model, the proxy and
    sigma - every one of which can go missing, and each of which is a
    silent skip inside check_exits. An open position must be protected
    on the ONE input that cannot disappear: what the book will pay."""
    by_tk = {m["tk"]: m for m in mkts}
    for tk, pos in list(self.pos.items()):
        m = by_tk.get(tk)
        if not m:
            self.stats["stop_blind"] = self.stats.get("stop_blind", 0) + 1
            continue
        bid = m["yes_bid"] if pos["side"] == "yes" else (
            None if m.get("yes_ask") is None else 100.0 - m["yes_ask"])
        if bid is None:
            self.stats["stop_blind"] = self.stats.get("stop_blind", 0) + 1
            continue
        if bid > STOP_PX_C:
            continue
        ...  # book the exit exactly as check_exits does, how="STOPPED"
```

**Two things that must go in with it, or we learn nothing:**

- **`stop_blind` counter.** Every cycle where an open position could not be price-checked. If that number is not ~0, the stop is decorative and we need to know *immediately* rather than in the next autopsy. This is the alarm the last three outages all lacked.
- **Record `low_px` on every position** — the worst bid seen while held. That is the only way to measure stop slippage honestly next time, and to answer "would a 50¢ or a 40¢ stop have been better" from evidence instead of argument.

**Do not** put this inside `check_exits`. The whole defect is that protection was living behind opportunity's guards.

### 2. Retire `fav_band [70, 88]`. Move the entry band to `[88, 97]`. *(measured: +$88)*

The bad-exit rate is monotone in entry price, and the mechanism is arithmetic:

| entry | n | bad exits | P&L | distance to a 45¢ stop |
|---|---|---|---|---|
| <80¢ | 28 | **28.6%** | −$54.69 | 31.5¢ |
| 80–85¢ | 43 | 16.3% | −$9.45 | 37.4¢ |
| 85–88¢ | 27 | 7.4% | −$1.05 | 40.9¢ |
| 88–91¢ | 46 | 8.7% | −$13.48 | 44.5¢ |
| **91¢+** | 56 | **1.8%** | **+$13.01** | 47.6¢ |

A 75¢ position only has to fall 30¢ to hit the disaster zone; a 91¢ position has to fall 47¢. **A 16× difference in blow-up rate**, and on top of it the round-trip fee is 2.94¢ at 70¢ versus 0.79¢ at 94¢.

**This contradicts the 8/28 retune, and it should.** The comment at `FAV_MIN_C` argues the cheap favourite is better because "at 75¢ you must win 75% and you win ~88%." That was fitted on 35–50 **window-level, hold-to-settlement** samples. **We do not hold to settlement** — 160 of 200 rows are round-trips and 22 are stopped or dead. A backtest that measures a strategy we don't run cannot referee one we do. 200 live settles beat 50 backtested windows.

**Change:** `FAV_MIN_C 70 → 88`, `FAV_MAX_C 88 → 97`. The `early` lane band (`FAV2 88–95`) is already right and stays.

---

## PHASE 2 — TURN THE EDGE ON

### 3. One function: `cal()`. *(the whole thesis)*

```python
CAL_B = float(os.environ.get("TICK_CAL_B", "1.95"))

def cal(p):
    """Calibrate the model's probability. Fitted 8/31 by weighted MLE on
    2,825 graded shadow observations: true_logit = 0.00 + 1.95*model_logit.
    Intercept zero - the model has NO directional bias. Slope two - it is
    exactly half as confident as it should be. Equivalently:

        true_odds = model_odds ** 1.95

    Brier on those same rows: 0.0877 raw -> 0.0769 calibrated, against
    the market's 0.0827. Uncalibrated, the model looked worthless
    (0.1256 vs 0.0827) and we read that as 'the market is efficient'.
    It was a broken link function, not an absent signal."""
    p = min(1 - 1e-6, max(1e-6, p))
    o = (p / (1 - p)) ** CAL_B
    return o / (1 + o)
```

**Exactly two call sites**, both immediately before the `CONF_CAP` clamp:

- `decide()` ~line 1285: `p_yes = min(CONF_CAP, max(1-CONF_CAP, cal(p_raw)))`
- `check_exits()` ~line 1606: same line, same change.

### ☠️ THE TRAP — read this before writing the code

**`observe_shadow()` must keep logging the RAW `model_p`.** It computes `p` independently (~line 1680) and that value feeds `shadow_calib`, which is what we re-fit `CAL_B` from. If calibration leaks into the shadow observer, next week's fit sees already-corrected numbers, returns `b ≈ 1.0`, and we conclude the correction was never needed — silently un-shipping the single most valuable line in the project.

**Rule: `cal()` is applied where we ACT. Never where we OBSERVE.** Put that in a test.

### What falls out for free: the short side

Every one of the 200 trades in the ledger is a buy at 70–97¢. **The book has never sold a longshot.** We do not need to build that lane — `decide()` already prices both sides independently and takes the better edge. It never fired because the raw arithmetic said no:

| | raw | calibrated |
|---|---|---|
| model says YES 25% | NO side = 75%, priced 80¢ → **−5¢, refused** | NO side = **89.5%**, priced 80¢ → **+9.5¢, taken** |

`no_edge` refusals: **118,010.** That counter has been read all month as "the market was efficient, which is itself the finding." **It was the inventory.** Half the edge surface opens the moment `cal()` lands, in the same cheap-fee zone, with no new lane.

### 4. Expect the trade count to jump, and gate it

Calibration adds ~14.5 points of edge at `model_p ≈ 0.25` and `≈ 0.75`. Volume will rise sharply. Raise `EDGE_C` **2 → 4** on the same commit — the edge is now measured on a sharper number, so the bar should be higher, and we want the *best* of the newly-visible trades, not all of them. Re-tune from the tape after 200 settles, not before.

**One honest caveat, stated up front:** `SHADOW_AT_S = 120`, so `b = 1.95` is fitted at **T-minus 2 minutes**. That is exactly the `fav` lane's horizon, so the fit is best-validated where it matters most — but applying it at the `early` lane's 300–600s is an extrapolation. Fixed by change 6.

---

## PHASE 3 — HYGIENE

### 5. Feed-granularity gate, and drop four instruments *(measured: BNB alone is −$31.80)*

`MIN_LIVE_BP` measures the proxy's *range* and BNB passes it (2.9bp > 1.0bp). It needs a second test — **distinct values**:

| market | distinct prints / 8 | range |
|---|---|---|
| BTC, ETH | 8 | 0.083% |
| SOL, XRP, ZEC, NEAR, HYPE | 6–7 | 0.11–0.16% |
| **BNB** | **4** | **0.029%** |

**An instrument whose proxy prints fewer distinct values than the market it predicts will always look certain and always be wrong.** Add `MIN_DISTINCT_N` alongside `MIN_LIVE_BP` in `liveness_bp`/`proxy_dead`.

Immediately, via env so no code change is needed to test it: **drop BNB, NEAR, gold, silver, WTI.** Metals are gross **−$30.17** (crypto is gross **+$7.07**), WTI has never placed a trade in 117 hours, and the paid Pyth trial is being spent entirely on the losing half. Dropping the four and adding the price stop is **+$48.27 on the existing tape**.

### 6. Two small measurement fixes

- **`fee_c(px, 1, ...)` ceilings to a whole penny**, so the fee term in `decide()` is effectively a flat 2¢ across the entire band. The edge test therefore *cannot see the fee curve* that the strategy's whole design rests on. Compute the fee for `SIZE` contracts and divide.
- **Observe shadow at three horizons** (600s / 300s / 120s), not one. Then re-fit `CAL_B` per bucket. If `b` is ~1.0 late and ~2.5 early, that re-times the whole strategy on evidence rather than on the §6 correlation — and it's the difference between a lucky constant and a model.

---

## PHASE 4 — VERIFY, THEN SCALE

Ship 1–6, then **do nothing for 200 fresh settles.** Specifically:

| gate | pass condition |
|---|---|
| `stop_blind` | ~0. If not, the stop is decorative — fix before anything else. |
| bad-exit rate | <5% (currently 11%) |
| fees ÷ \|gross\| | <0.5 (currently **1.84** — fees are 65% of the loss) |
| `CAL_B` re-fit | still 1.7–2.2 on fresh out-of-sample rows |
| net P&L | positive over 200 settles |

**Do not touch `SIZE`, `MAX_POS` or `BOOK_CAPITAL_C`.** Standing rule: no risk-limit change without Adam saying so. The whole lesson of this book is that a 69% win rate with a fat left tail looks exactly like an edge right up until it doesn't.

---

## Effort and sequencing

| # | change | edit size | tests | measured value |
|---|---|---|---|---|
| 1 | `check_stops` + `stop_blind` + `low_px` | ~60 lines, new method | 6–8 new | **+$68.50** |
| 2 | `FAV_MIN_C`/`FAV_MAX_C` → 88/97 | 2 constants | 2 | **+$88** |
| 3 | `cal()` + 2 call sites | ~15 lines | 4 (incl. the observe-shadow trap) | the thesis |
| 4 | `EDGE_C` 2 → 4 | 1 constant | 1 | throttles #3 |
| 5 | `MIN_DISTINCT_N` + drop 4 series | ~10 lines + env | 3 | **+$88** |
| 6 | fee precision + multi-horizon shadow | ~25 lines | 4 | measurement |

**1, 2 and 5 need no new model and are independently verifiable.** 3 is the one with real upside and real risk, which is why it ships behind a stop that works, not before one.

---

---

## DECIDED (Adam, 8/31): all six in one commit, shipped from an on-computer session

### The problem that creates, and the fix

Six changes in one commit means that if P&L moves, **we can't tell which change moved it** — and this project has been burned four times by a number nobody could attribute.

**It solves itself, because every constant below is already an env var.** So we ship one commit and keep six independent switches. Each can be disabled by a single line in the systemd unit plus a restart — no code change, no deploy, no waiting for a pull:

| change | switch | value that turns it OFF |
|---|---|---|
| 1 · price stop | `TICK_STOP_PX` | `0` (never triggers) |
| 2 · entry band | `TICK_FAV_MIN` / `TICK_FAV_MAX` | `70` / `88` (the old band) |
| 3 · calibration | `TICK_CAL_B` | **`1.0` — the identity function, exactly the current behaviour** |
| 4 · edge bar | `TICK_EDGE` | `2` |
| 5 · instrument set | `TICK_SERIES` / `TICK_MIN_DISTINCT` | the full list / `0` |
| 6 · fee precision, multi-horizon shadow | `TICK_FEE_EXACT`, `TICK_SHADOW_AT` | `0`, `120` |

`CAL_B = 1.0` collapsing to the identity is the important one: it means **the riskiest change in the commit is also the most cleanly reversible**, in about fifteen seconds, without touching the repo.

**Non-negotiable condition on shipping all six together: every change carries its own counter in `/public`.** `stop_blind`, `stop_px_n`, `cal_shift_c` (mean cents the calibration moved the decision), `band_skip_lo` (refusals caused by the new 88¢ floor), `distinct_dead`. Without those, one commit is one uninterpretable number. With them, it's six measurements.

### The one thing that must NOT be batched

Ship the **era bump to `tick3`** in the same commit. A ledger cannot be compared across a constraint change — the project rule, sixth application. Six simultaneous constraint changes make the existing 4,145 rows meaningless as a comparison set, and pretending otherwise is how we'd end up "proving" the fix worked on a mixed ledger.

### The on-computer session's running order

Do not write code into the current folder first — it's stale (Phase 0) and `2_push_updates.bat` sweeps uncommitted files into a commit.

1. `git pull origin main`; confirm `tick_paper.py` exists and HEAD is `3063f7b`.
2. Write all six changes against **that** tree.
3. `pytest tests/test_tick_paper.py` — 113 existing tests must stay green, plus the new ones. **The `cal()`-must-not-touch-`observe_shadow` test is mandatory**; it is the only guard against silently un-shipping the thesis in a week.
4. `ast.parse` the committed copy and verify the commit tree actually contains `tick_paper.py` (git on the mounted folder has produced commits with missing files before).
5. Push. Droplet pulls and restarts within 3 minutes.
6. Watch `/public`: `era` reads `tick3`, `stop_blind` ~0, `stop_px_n` rising, trade count up but not exploding. If `stop_blind` is anything but ~0, stop and fix that before letting the book run — a stop that can't see is the defect we just spent a day finding.

**This plan and the autopsy are both in the repo folder, and the findings are in memory — the on-computer session will pick both up without re-deriving anything.**
