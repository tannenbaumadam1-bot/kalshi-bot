# SPORTS PAPER BOOK — AUTOPSY & EXHIBIT (retired 8/20)

**Verdict: the funnel was never the constraint. The anchor was.** Filed
as the design brief for any future taker-side sports lane, and as the
reason the phantom book attacks the same surface from the other side.

## Life of the book
- Launched 8/12 (era `sports1`): Polymarket-anchored entries — buy a
  Kalshi team line when it sat cheap versus a de-vigged Polymarket
  price, then run the weather book's offer-side template on top
  (relist inventory at 97/99c, lift only on bid-through).
- Ran 8 days. Final state at retirement: **6 placed, 4 settled,
  2W/2L, −$3.04, 0 lifted on the offer side, 2 open**, gate 4/200.
- Day-one bug that cost the first week: gamma markets carry no
  `category` field, so the Polymarket index built empty and the book
  placed **zero** bets from launch until the 8/12 moneyline fix.

## What we learned that keeps its value

**1. Widening a funnel does not create evidence.** Across three
sessions we relaxed every gate we had: edge 3c → 1c, max spread
5c → 10c → 14c, entry band 60-94c, max open 25. Total yield: **two
extra placements.** When loosening the filter by 5x buys you 50% more
trades, the filter was not what was binding — the *anchor* was. There
simply aren't many Kalshi sports lines that Polymarket prices
differently enough to matter, and the ones that exist are the ones
where Polymarket is stale, which is adverse selection wearing an edge's
clothing.

**2. A 200-turn gate needs a book that can turn 200 times.** At 4
settled trades in 8 days, the gate would have taken **13 months** to
render a verdict. A gate slower than the thesis is not risk control,
it's a filing cabinet. Any future paper book must be sized so its
evidence clock is measured in weeks: if the surface can't produce the
sample, the surface is the problem.

**3. The offer side didn't transplant.** 0 lifts in 8 days, against
376 lifts and +$114.82 on the weather book. Weather markets converge
gradually toward a knowable number all day, so inventory bought at 60c
walks up into a 97c bid. A team line does not walk — it jumps at
events (a run scores, a starter is pulled) and otherwise sits. **The
churn engine needs a market that drifts, and sports moneylines don't.**
That is a genuine structural finding, and it points the sports effort
at in-play props (which tick continuously) rather than game winners.

**4. The wins and losses were the same trade.** 2W/2L on 5-lots at
60-67c entries, roughly ±$3 each: this is a coin flip with a fee, not a
strategy under test. Nothing in the ledger argues the anchor edge
exists or doesn't. **We spent 8 days learning we had not started
measuring.**

## Why now, and where the thesis goes
Retired the same day the **phantom book** went live because the phantom
attacks the identical surface from the opposite side: instead of
hunting the rare mispriced line as a taker, it quotes hundreds of
markets as a maker and collects a print-by-print sample. In its first
hour it recorded 9 fills and 1,236 flow observations — more evidence
than sports1 gathered in 8 days. The taker thesis isn't disproven; it's
**out-sampled**, and it can be revisited with real fair value once the
tennis Markov model exists.

## Reopening conditions (the design brief)
1. A fair-value source that isn't another market's stale quote —
   for tennis, the scoring Markov chain; for baseball, a state-based
   win-probability model.
2. A surface that can produce 200 settles in weeks, not months —
   i.e. in-play props, not one line per game.
3. Offer-side logic that only arms where prices *drift* (evidence
   required per market family, not assumed from the weather book).
4. Its own gate, and an evidence clock stated up front: if the book
   can't reach its gate in 6 weeks, it doesn't launch.

*Filed 8/20. Two open paper positions and the full ledger are preserved
on the server as `sports_paper_state.json`. Revive with
PAPER_SPORTS=1 plus the dashboard payload block and panel.*
