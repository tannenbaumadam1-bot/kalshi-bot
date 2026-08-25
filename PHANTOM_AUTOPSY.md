# PHANTOM AUTOPSY — Book 4, 2026-08-20 → 2026-08-25

**Retired 8/25 on Adam's instruction** ("please totally get rid of this book from the bot and the tracker"). Module, tests and ledger preserved; `PAPER_PHANTOM=1` revives it. Zero real dollars were ever at risk.

## The thesis it was built to test

Adam, 8/20: *"I want to be the book for retail."* Quote both sides of thousands of low-attention sports props, manufacture our own overround, and earn the spread on volume instead of being right about anything — Susquehanna's trade, in the long tail where nobody competes.

Three numbers were named upfront as the ones that would decide it. All three came back negative. That is the cleanest possible outcome for an experiment: it answered its own question.

## The verdict, on the book's own KPIs

| KPI | Result | What it means |
|---|---|---|
| **Match rate (contract-weighted)** | **9%** — 82 paired of 1,772 filled | **1,608 contracts never paired.** This was not market-making. It was naked directional risk wearing a market-maker costume. |
| **Spread per pair, after fees** | **−2.45¢** (gross +3.24¢, fees −5.68¢) | **THE thesis, inverted.** At widths retail will actually trade against, Kalshi's fee is bigger than the spread. No tuning fixes a sign. |
| **Adverse selection (5m)** | **−5.1¢** (n=293) | We were filled mainly when we were wrong. By the book's own 8/20 definition: *"a maker who is filled only when he's wrong is not the house, he's the fish."* |
| Total P&L | **−$44.09** on a $1,000 paper book (−$1.06 banked over 483 settles, −$43.03 open) | The loss is 93% directional mark on unpaired inventory — a coin flip we never chose to take. |

Capital turnover 0.04x/hr against a book that saturated its collateral (82,841 refusals at cap). It was simultaneously over-committed and under-recycling.

## Why it failed — the structural reading

**The spread we could capture was always smaller than the fee we had to pay.** 88% of prints on this surface sit in 1–3¢ books. To get filled we had to quote inside a spread narrower than the round-trip fee at those prices. The fee curve (`0.07 × P × (1−P)`, peaking at 1.75¢/contract at 50¢) is hostile to exactly the mid-priced two-sided quoting this book did. Meanwhile the only fills we got were the ones informed flow *wanted* to give us.

**And the pairing never came, because retail flow is one-sided by nature.** The whole premise of the overround requires both sides to arrive. On a prop nobody is watching, only the one person with an opinion shows up — and they hit the side they like. 9% pairing is that fact, measured.

## What this cost, and what it bought

Cost: five days of compute and one deleted paper ledger. Nothing else.

Bought — three transferable findings:

1. **Unhedged two-sided quoting is a losing trade for us, proven on 1,772 contracts.** This is now settled, and it did real work on 8/25: it is the reason we did *not* attempt delta-hedged market-making on Kalshi perps. The pro version of that trade only works because the hedge ships the directional risk to GC/CL futures; strip the hedge and you get this book. We had the receipt before we spent the money.
2. **The fee curve dictates strategy, not preference.** Everything sustainable at our size lives at the extremes where `P × (1−P)` collapses. Book 5 (Tick) was designed around this from line one, and the live weather book's flatten leak is the same lesson (mid-price exits pay peak fee).
3. **Fill realism has to be conservative or the experiment lies.** STRICT (a real print traded *through* us) vs LOOSE (traded *at* us) was the single most valuable piece of machinery here — 721 vs 609. A book grading itself on LOOSE fills would have reported a business that does not exist. That machinery is inherited by Book 5.

## Process notes worth keeping

- **The evidence clock worked exactly as designed.** It was set at 100 settles on 8/24 with a pre-agreed verdict condition (adverse selection staying negative). It rang at 434/100 and the answer never wavered. Pre-committing the kill condition is what made this a decision instead of an argument.
- **One caution for next time:** phantom7 raised the book's capital $100 → $1,000 on 8/24 — *after* the verdict was already due. Scaling a lane whose evidence clock has matured but not been read is how a small negative becomes a large one; the open mark went from −$32 to −$43 across that change. Read the clock before turning the dial.
- Four ledger resets in five days (phantom1→7), each because a constraint changed mid-experiment. The rule held every time: **a ledger built under a different constraint cannot be compared to one built after it.** But the frequency is itself a signal — freeze the constraints before starting the clock, not during.

## The durable lesson

The book was *correctly built* and still lost — the code did what it was told, the instrumentation was honest, and the honesty is what killed it on schedule. That is the system working. An experiment that cannot return "no" is not an experiment, and this shop's edge is that ours can.
