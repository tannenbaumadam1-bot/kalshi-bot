# MID-BAND BOOK — AUTOPSY & EXHIBIT (retired 8/18, panel cut 8/19)

**Verdict: the machinery failed, not the thesis.** Filed as the design
brief for the mid-band lane's return, once the weather nowcast passes
its shadow exam.

## Life of the book
- Launched 8/13 (era `midband1`): 15-55c bands, entries when the
  ensemble model disagreed with the market, exit on convergence to
  +40% or the pre-close flatten. Never held to settlement. 5 lots.
- Traded actively for ~36 hours (8/13 17:17 → 8/14 06:23), filled all
  12 `max_open` slots, and then **stopped trading entirely**.
- Final state at retirement: **4 turns, 4 wins, 0 losses, +$2.18,
  +$0.55/turn**, 16 placed, 12 frozen open positions, gate 4/200.

## What worked — keep this
All four completed round trips won, and they are textbook
buy-low/sell-mid convergence:

| Closed | Market | Entry | Exit | P&L | Why |
|---|---|---|---|---|---|
| 08-13 21:25 | washington 91° | 17c | 37c | **+$0.86** | target |
| 08-13 21:14 | philadelphia 88° | 18c | 33c | **+$0.61** | target |
| (2 more) | — | — | — | +$0.71 | target |

That is exactly the trade BlueWalker described on 8/13: *"the money is
made in buying at 20, realizing it should be priced at 50, and getting
out at 50... you don't have to be binarily right."* On our own ledger,
in our own market, it paid +55c per turn.

## What killed it — fix this before reopening
1. **No fair value that could revise.** Entries used a model edge, but
   once open, nothing ever re-priced the position. A thesis that broke
   looked identical to one that hadn't, so no exit could fire.
2. **Convergence-or-nothing exits.** The only ways out were "hit +40%"
   or the pre-close flatten. Markets that drifted sideways (or the
   flatten missing them) meant the ticket sat forever.
3. **No downside exit.** CUT did not exist yet. A mid-band position
   that broke had no floor at all.
4. **Inventory management, not pricing, was the bottleneck** — the
   exact failure Camilo names as the maker's real constraint. 12 slots
   full = a dead book, however good the entry signal was.

## The 12 frozen exhibits (state preserved on the server)
All opened 8/13-8/14, all 5 lots, model vs entry:
chicago 80° high (22% vs 15c) · phoenix 97° high (21% vs 15c) ·
oklahoma city 103° high (22% vs 16c) · seattle 77° high (20% vs 15c) ·
san francisco 68° high (21% vs 16c) · **los angeles 78° high (51% vs
40c)** · dallas 100° high (21% vs 15c) · houston 97° high (22% vs 16c)
· **new york 73° low (41% vs 17c, 22.6c edge)** · houston 92° high
(29% vs 21c) · **atlanta 78° low (36% vs 23c)** · **chicago 72° low
(42% vs 19c, 21.6c edge)**

Note the four biggest claimed edges are all LOW-temp or LA — a hint the
old ensemble was systematically overconfident in exactly the cohort
that later produced the 8/17 correlated bust. The nowcast build should
grade against these.

## Reopening conditions (the design brief)
1. Weather nowcast beats the market's Brier score in shadow (the same
   exam that convicted the old forecast model: 0.126 vs 0.083).
2. Entries priced off the nowcast, not a static ensemble.
3. **Positions re-priced every cycle**; CUT covers broken theses.
4. Inventory cap enforced by capital-hours, not slot count.
5. Its own 200-turn gate, as before.

*Filed 8/19. The thesis is alive; it just needs a brain and an exit.*
