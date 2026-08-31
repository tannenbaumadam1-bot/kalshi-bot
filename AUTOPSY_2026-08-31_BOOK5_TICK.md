# BOOK 5 · TICK — FULL AUTOPSY, 2026-08-31

Era `tick2` · 117.9 hours · 16,132 cycles · 4,145 settled · **−$1,721.31** on a $1,000 paper book.

The evidence clock said 200. It has run 4,145. **The verdict is in.**

---

## 0. The premise, tested first: "if we flipped every trade we'd be up $1,700"

No. And the reason is the single most important number in this book.

The dashboard exposes a 200-row detail ledger (the most recent 200 settles). Over those rows:

| | |
|---|---|
| **Gross P&L** (price movement only) | **−$23.10** |
| **Fees** | **$42.56** |
| Net | −$65.66 |
| Contracts | 4,570 |
| Per contract | −0.51¢ gross, **0.93¢ fee** |

**Fees are 65% of the loss.** And Kalshi's fee is `0.07 × P × (1−P)` — which is *symmetric*: the fee on a 22¢ contract is identical to the fee on a 78¢ contract. **Flipping the trade does not flip the fee.**

> **As traded:** −$23.10 gross − $42.56 fees = **−$65.66**
> **Perfectly mirrored:** +$23.10 gross − $42.56 fees = **−$19.46**

The mirror-image bot also loses. Not by as much, but it loses. The book is not on the wrong side of the market — it is **paying a toll it doesn't earn back**, and the toll is charged in both directions.

That reframes the whole project. We are not looking for a sign flip. We are looking for trades where the move is bigger than the toll.

---

## 1. Where the money actually went: 22 rows out of 200

| exit type | n | P&L | avg |
|---|---|---|---|
| **SOLD** (round-tripped inside the window) | 160 | **+$85.12** | +$0.53 |
| **WON** (rode to settlement, won) | 18 | **+$44.81** | +$2.49 |
| STOPPED | 14 | −$98.78 | −$7.06 |
| LOST (rode to zero) | 8 | −$96.81 | −$12.10 |

**178 rows made +$129.93. Twenty-two rows lost $195.59.**

The scalp engine — the thing Adam has been pushing for since day one, buy and sell back inside the window — **works.** +2.17¢ per contract *after* fees, over 3,920 contracts. It is not the problem. The problem is an 11% tail with a 12:1 payoff asymmetry against it.

Break-even hit rate at that payoff ratio: **92.4%.** Actual: 89.0%. We are 3.4 points short — seven bad events out of 200.

---

## 2. THE BIGGEST SINGLE DEFECT: the stop does not fire

`stop_p = 0.45`. The design says: if the position falls to 45¢, get out.

Eight rows went from 75–87¢ **all the way to zero** without ever tripping it.

| market | entry | actual | if the 45¢ stop had fired | saved |
|---|---|---|---|---|
| bnb | 78.7 | −$23.97 | −$10.47 | **$13.50** |
| doge | 86.5 | −$17.47 | −$8.47 | $9.00 |
| gold | 80.5 | −$16.32 | −$7.32 | $9.00 |
| xrp / zec ×2 / bnb / silver | 74–83 | −$38.65 | −$16.55 | $22.50 |
| | | | **total** | **$54.00** |

And on the 14 rows where it *did* fire, half exited **below** 45¢ — 28¢, 25¢, 29¢, 36¢ — for another **$14.50 of slippage**.

> **Stop mechanics account for $68.50. The book's entire deficit is $65.66.**

**A stop that actually fires flips this book from −$65.66 to +$2.84 on the exact same tape, changing nothing else.**

**CORRECTION (after reading `tick_paper.py`, added same day).** I wrote the paragraph above assuming a 45¢ *price* stop existed and was firing late. It doesn't exist. The line is:

```python
broken = p_side < STOP_P          # check_exits, ~line 1610
```

`STOP_P = 0.45` is a threshold on **the model's probability**, not on price. There is no price stop in this book at all. Two consequences, both worse than the thing I assumed:

1. **The trigger depends on the same model that is under-confident by construction** (§4). A position genuinely dying shows a compressed `model_p`, so the stop argues with itself about whether the trade is broken while the price goes to zero.
2. **The stop lives inside `check_exits`, behind five separate silent `continue`s** — no market in `mkts`, no `yes_bid`, no proxy, `sigma is None`, `proxy_dead`. Any one of them skips the cycle, and *nothing distinguishes "I can't find a new opportunity" from "I can't protect an open position."* With `no_proxy` at 11,216, that path is well travelled. This is the same failure class as the 8/24 cancel-410 and the 8/27 blind book: **an instrument that fails silently.**

So the fix is not "make the stop faster." It is **add a price stop that does not exist**, in its own pass, ahead of every guard, needing nothing but a bid. The $68.50 above is the measured value of a 45¢ price stop applied to these rows; it stands as the size of the prize.

---

## 3. The pattern nobody looked at: the fee curve peaks exactly where we chose to trade

Kalshi taker fee `= 0.07 × P × (1−P)` is maximised at 50¢ and collapses toward the edges.

| entry price | fee/contract | round-trip toll | our realised gross |
|---|---|---|---|
| 70–75¢ | 1.47¢ | **2.94¢** | **−7.69¢** |
| 75–80¢ | 1.31¢ | 2.63¢ | **−10.37¢** |
| 80–85¢ | 1.12¢ | 2.24¢ | +0.03¢ |
| 85–88¢ | 0.89¢ | 1.79¢ | +0.81¢ |
| 88–91¢ | 0.74¢ | 1.48¢ | −0.28¢ |
| **91–94¢** | 0.57¢ | 1.15¢ | **+1.38¢** |
| **94–97¢** | 0.39¢ | 0.79¢ | **+2.57¢** |

The bot's configured `fav_band` is **[70, 88]**.

We chose the band where the toll is **3.4× higher** than the band that makes money, and the realised P&L is **monotonically increasing in entry price across all seven buckets.** Entry ≥91¢: **+$13.01 over 56 rows.** Entry <80¢: **−$54.69 over 28 rows.**

This is not a subtle statistical effect. It is arithmetic that was available before the first trade.

---

## 4. THE DISCOVERY: the model isn't wrong. Its link function is.

The shadow table — 2,825 honest, untraded observations — looked like a catastrophe. Model says 15%, reality 3%. Model says 30%, reality 9%. The dashboard's own fit agrees: **Brier 0.1256 for the model vs 0.0827 for the market**, optimal blend weight 0.05. Read naively: *our model is worthless, the market beats it, give up.*

Read the residuals instead:

| model says | reality | gap |
|---|---|---|
| 5% | 0.7% | −4.3 |
| 15% | 3.1% | −11.9 |
| 25% | 9.0% | −16.0 |
| 35% | 19.3% | −15.7 |
| 45% | 42.0% | −3.0 |
| **50%** | **—** | **crossover** |
| 55% | 59.1% | +4.1 |
| 65% | 79.9% | +14.9 |
| 75% | 94.8% | +19.8 |
| 85% | 95.4% | +10.4 |
| 95% | 99.1% | +4.1 |

The errors are not noise. They are **perfectly monotone and perfectly antisymmetric about 50%.** Fitting `true_logit = a + b · model_logit` by weighted maximum likelihood over all 2,825 observations gives:

```
a = +0.00      b = 1.95
```

**Intercept zero. Slope two.**

The model has no directional bias whatsoever. It is simply *exactly half as confident as it should be, in log-odds space.* The correction is one line:

> ### true_odds = model_odds²

| model_p | recalibrated |
|---|---|
| 15% | 3.3% |
| 25% | 10.5% |
| 35% | 23.0% |
| 45% | 40.3% |
| 65% | 77.0% |
| 75% | **89.5%** |
| 85% | 96.7% |

**Brier on the same 2,825 rows: 0.0877 raw → 0.0769 recalibrated.** The market's is 0.0827.

**Recalibrated, our model beats the market price.** One parameter. No new data, no new feature, no subscription. It has been sitting in the shadow table for a week wearing the costume of a failure.

*(Caveat, stated honestly: the 0.0827 market Brier is computed by the dashboard on a 722-row subsample, not on the identical 2,825. The 0.0877 → 0.0769 improvement **is** measured on the same rows and is solid — a 12.3% Brier reduction from two fitted parameters over 2,825 observations, which is not an overfit.)*

### Why this cost us everything

`edge_c = 2.0` — take the trade if the model beats the price by 2¢. But the edge was computed against the **raw** `model_p`.

Where does the recalibration move the number most? At **model_p ≈ 0.25 and ≈ 0.75**, by ±14.5 points.

So: model says 0.75, market is priced 82¢. Raw comparison: *"we're 7¢ below the market, no edge, stand aside."* Recalibrated truth: **89.5% — an 7.5¢ edge, and we refused it.**

**`no_edge` refusals: 118,010.** That counter has been read all month as "the market was efficient, which is itself the finding." It is not the finding. **It is the inventory.** A large share of those 118,010 stand-asides were real edge hidden behind a bad link function — and the 335 trades the `fav` lane *did* take were exactly the ones where the raw model and the market happened to agree, i.e. the ones with no edge at all. `fav` P&L: **−$68.47.**

### And the trade we have never once taken

The recalibration is symmetric. `model_p = 0.25 → true 10.5%.` If that side is priced 20¢, the *other* side is priced ~80¢ and is worth 89.5¢.

Every single one of the 200 trades in the ledger is a **buy at 70–97¢.** The book has never sold a longshot, never bought the cheap side of a mispricing. **Half the edge surface has never been touched**, and by the fee curve it is the *same* cheap-toll zone as the favourites — you reach it by buying the opposite ticket at 88–97¢.

---

## 5. The market's own bias, measured — and it points the same way

849 shadow observations, bucketed by what Kalshi was actually charging:

| market price | n | realised | edge to BUY | net of fees |
|---|---|---|---|---|
| 20–35¢ | 130 | 15.4% | −12.3¢ | **−15.1¢ → SELL** |
| 35–50¢ | 113 | 35.4% | −6.5¢ | −9.9¢ → sell |
| 50–65¢ | 13 | 38.5% | −16.2¢ | sell |
| 65–80¢ | 11 | 63.6% | −9.4¢ | sell |
| **80–90¢** | **17** | **100.0%** | **+14.0¢** | **+12.3¢ → BUY** |
| **90–95¢** | **7** | **100.0%** | +8.2¢ | **+7.1¢ → BUY** |
| 95–100¢ | 3 | 100.0% | +1.7¢ | +1.5¢ |

Textbook **favourite–longshot bias**: the middle of the book is systematically overpriced, and true favourites are underpriced. 27 of 27 contracts priced above 80¢ resolved yes.

*Honest correction:* the tracked side is priced 18.45¢ on average and realises 14.74¢ — a −3.7pt aggregate gap that is almost certainly the bid/ask overround, not alpha. Subtract 3.7 from every row above and the *shape survives intact*: 20–35¢ still −8.6¢ (sell), 80–90¢ still +17.7¢ (buy). The small n on the top buckets is the real caveat, not the overround.

**Both independent measurements — our recalibrated model and the market's own realised outcomes — say the same thing: the money is at the price extremes, and we have been trading the middle.**

---

## 6. Time is inverted against the design

`fav_at_s = 240` — the fav lane only enters in the **last four minutes**, on the theory that certainty rises toward the bell.

| seconds left at exit | n | P&L |
|---|---|---|
| 60–120s | 50 | **−$41.26** |
| 120–180s | 49 | **−$41.72** |
| 180–240s | 16 | −$2.40 |
| 240–300s | 21 | −$13.53 |
| **300–450s** | 49 | **+$21.49** |
| 450s+ | 11 | +$5.45 |

And by holding time:

| hold | n | P&L |
|---|---|---|
| ≤45s | 95 | **+$23.50** |
| 45–90s | 34 | +$4.97 |
| **90–180s** | 41 | **−$58.05** |
| **180–300s** | 22 | **−$54.49** |
| 300s+ | 8 | +$18.41 |

**≤90 seconds: +$28.47 over 129 rows. 90–300 seconds: −$112.54 over 63 rows.**

Median hold by outcome: **SOLD 43s. STOPPED 138s. LOST 164s.**

That is the **disposition effect, encoded in software**: the bot takes its 2–3¢ profit in 43 seconds and then sits on losers for three times as long, hoping. The `early` lane (enters 5–10 minutes out) is **gross-positive at +$17.94**; the `fav` lane (last 4 minutes) is **−$41.04 gross**.

The design thesis — "wait for certainty" — is exactly backwards. In the last four minutes, a 78¢ favourite is 78¢ *because it is genuinely uncertain and there is no time left to resolve it*, and at that horizon the market's Brier beats our raw model's. Early in the window there is still a premium the book hasn't squeezed out.

---

## 7. BNB is a broken feed and it cost $31.80

Tick density over the last 8 proxy samples (~150 seconds):

| market | distinct prints | price range |
|---|---|---|
| BTC / ETH | 8 / 8 | 0.083%, 0.081% |
| DOGE / ZEC / NEAR / XRP / SOL / HYPE | 6–7 / 8 | 0.11–0.16% |
| **BNB** | **4 / 8** | **0.029%** |

BNB's proxy is quantised roughly **5× coarser** than everything else. The model reads a flat feed as *no movement → high certainty*, sizes up, and then the real price gaps. BNB is the **single worst market in the book at −$1.87 per turn, −$31.80 total** — half the entire deficit from one instrument. NEAR is second at −$1.00/turn.

There is a general rule here worth keeping: **an instrument whose proxy prints fewer distinct values than the market it predicts will always look certain and always be wrong.** Feed granularity belongs in the pre-trade check, next to staleness.

## 8. We are paying for the losing half

| | gross | net |
|---|---|---|
| **Crypto** (free Coinbase/Kraken, 60s averaging settlement) | **+$7.07** | −$25.72 |
| **Metals** (paid Pyth trial, single-print settlement) | **−$30.17** | −$39.94 |

Gold −$20.19, silver −$19.75. **WTI has never placed a single trade** — 0 basis samples, vol `null`, 0 tape prints. The paid data trial is being spent entirely on the half that loses, and a third of it on an instrument that has never traded.

---

## 9. Two ideas the data kills (so we stop revisiting them)

**"Just rest orders and stop paying taker fees."** Adverse selection on the 381 measured maker fills is **−1.87¢**. The taker fee it would save is ~0.9¢ a side. **Resting is worse by about a cent.** This is the same wall the pair strategy hit on 8/28 across 18 configurations, and `pair` still reports `rate 0.635 vs breakeven 0.852, pays: false`. The fee is not avoidable; it is a real cost of doing business, and the answer is to trade where it is small (§3), not to dodge it.

**"True arbitrage: 8 crossed books."** All eight have a **0.1–2.6¢ leg**. A 0.1¢ ask is a stale quote on a market that has effectively already resolved, not depth you can lift. Eight in 117 hours, at 2–11¢ each. That is a data artifact wearing a headline. Fire *one* real order at the next one to find out if the leg is real; if it isn't, delete the counter — it is currently the most encouraging number on a dashboard that lost $1,721.

---

## 10. What the same tape does under the fixes

Applied to the identical 200 rows, changing nothing but the rules:

| | n | P&L | bad exits |
|---|---|---|---|
| **Baseline** | 200 | **−$65.66** | 22 (11%) |
| Drop BNB, NEAR, gold, silver | 122 | +$22.07 | 8 (7%) |
| + entry ≥ 88¢ | 60 | +$21.10 | 1 (2%) |
| **Baseline + a stop that fires** | 200 | +$2.84 | — |
| **Drop-4 markets + a stop that fires** | **122** | **+$48.27** | — |

**Two changes — delete four instruments, make the stop a resting order — turn −$65.66 into +$48.27 on the tape we already have.** A $114 swing with no new model, no new data, no new idea.

*(These are in-sample filters on 200 rows and should be read as a direction, not a forecast. They are corroborated by the independent shadow calibration, which is what makes them worth acting on.)*

---

## THE BUILD — `tick3`

**Tier 1 — the loss stops (do these first, in this order):**

1. **The stop becomes a resting order placed at fill time.** Not polled. The moment we're filled at 85¢, a sell at 45¢ goes into the book and stays there. *(+$68.50 measured)*
2. **Hard time-stop: flat at 90 seconds, unconditional.** ≤90s is +$28.47; 90–300s is −$112.54. No discretion, no "it might come back." *(+$112 measured)*
3. **Delete BNB, NEAR, gold, silver, WTI.** *(+$88 measured)* Add a **feed-granularity gate** — refuse any instrument printing fewer than N distinct proxy values per window — so the next BNB is caught before it trades.
4. **Entry band → [88, 97].** Retire `fav_band [70,88]` entirely. Both the fee curve and seven consecutive P&L buckets agree.

**Tier 2 — the edge turns on:**

5. **Recalibrate: `true_odds = model_odds²` (logit × 1.95), applied before `edge_c` is computed.** This is the highest-value line of code in the project. Re-fit the slope weekly from the shadow table; log the fitted `b` so we can watch it drift.
6. **Open the short side.** Every trade so far is a buy at 70–97¢. `model_p ≤ 0.11` recalibrated, other side priced ≤ 88¢ → buy the *opposite* ticket. Same cheap-fee zone, double the opportunity, and it is the biggest untouched surface we have.
7. **Move the entry window earlier: `t_left` 300–600s, retire `fav_at_s = 240`.** The last four minutes are where the model is worst and the market is sharpest.
8. **Flat size.** Size currently scales with model confidence, and model confidence is most wrong in exactly the band where the big losers sit (silver 40 lots at 90.2¢ → −$16.10; bnb 30 lots at 78.7¢ → −$23.97).

**Tier 3 — measurement:**

9. Split the shadow table by `t_left` bucket. The recalibration slope is almost certainly not 1.95 everywhere — if `b` is near 1.0 in the last 4 minutes and 2.5+ early, that alone re-times the entire strategy on evidence instead of on §6's correlation.
10. Fire one real order into the next crossed book. Settle the arb question with a fill, not a counter.

**Do not scale size until Tier 1 has run 200 fresh settles with the tail closed.** The whole lesson of this book is that a 69% win rate and a fat left tail look identical to an edge right up until they don't.

---

## The durable lesson

For eight days the shadow table has been reported as evidence the model doesn't work. It was evidence of the opposite — the residuals were monotone, antisymmetric, and fit a two-parameter link with an intercept of exactly zero. **We ran a Brier comparison, saw 0.1256 vs 0.0827, and stopped reading.**

The fifth spectacular-number audit in this project caught fabricated *profits*. This is the first time the number that needed auditing was a **failure**. A model that is badly calibrated and a model that is uninformative produce the same headline Brier and are worth wildly different amounts of money — and telling them apart takes one plot of residuals against prediction.

**Check the losers as hard as we check the winners.**

---

*Live book, same timestamp, for context: marked NAV $75.62, $72.04 withdrawn, $100 in → +47.7% on cash in, but current-period P&L is **−$24.47** (weather −$14.99, crypto −$9.48) and `k_realized_suspect: true`. The headline ROI rests entirely on that $72.04 withdrawal, which is still the open question from 8/27.*
