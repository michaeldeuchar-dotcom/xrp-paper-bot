# Cash-out variants and 15-minute variants (week 2)

Added **new** paper-only variants to the week-2 two-week test (ends Monday, Dec 7, 2026, 11:30 AM PT). Existing variants, their week-2 ledgers, and the frozen week-1 data are unchanged.

Fake money only. $10 max per bet including the fee. No real trades, no wallet key.

## Cash-out (early sell) variants

Until now every paper bet was held to the official settlement. These new variants use the same entry rules as a strong week-1 strategy, then may sell early.

**How a sale is simulated:** we sell the full position into the live best BID, only if that bid's size is at least our share count (we do not walk the book). The taker fee on the sale is `shares × 0.07 × p × (1 − p)` with p = the bid. Proceeds = shares × bid − fee. P&L is proceeds minus what we paid to get in. If the bid is too thin, we keep holding (and may settle officially later).

**Exit rules (plain English):**

- **Take profit:** sell if the bid reaches 95 cents, or if selling now would lock in at least **+20%** after the sell fee (**+10%** on the 80-cent favorite cash-out, because those bets win small).
- **Stop loss:** sell if the position is down about **45%** (sale proceeds are 55% or less of what we paid).
- **Lead flip, little time left:** sell if the coin's price is on the other side of the price-to-beat from our bet, we are already in the back half of the window, and there is about a minute or less left.
- **Hold at least 20 seconds** after entry, so we don't flip out on the same print we bought.
- **Last 8 seconds:** we leave those for the official result instead of a last-second sale.

Every exit is logged (time, bid, size, fee, proceeds, reason) in `data/week2/cash_out_exits.csv`, and the trade ledger marks the outcome as `sold_early` with the correct P&L.

## 15-minute variants

Polymarket 15-minute Up/Down markets (`{coin}-updown-15m-{unix}`) exist for BTC, ETH, SOL, and XRP. BTC and ETH books were comfortably liquid when checked (Nov 23, 2026, afternoon PT). SOL and XRP 15-minute books exist but were thinner at the touch (sometimes only a handful of shares). We still added 15-minute variants for all four so the test can measure them; thin books may mean fewer fills or skipped bets when the ask is too small.

Timing gates are scaled ×3 (a 60–120s 5-minute window becomes 180–360s on 15 minutes). The "skip the first minute" toggle skips the first **3 minutes** of a 15-minute window. Trend reads for 15-minute variants use the last 3 hours of 1-minute candles. These 15-minute copies hold to official settlement (they are not cash-out variants).

## auto_pick and bankroll_20

Unchanged in behavior. They still rank and follow only the original **5-minute, hold-to-settlement** strategies. Cash-out variants and 15-minute variants are in the test and show up in results, but they are **not** ranking candidates for auto_pick (and therefore not what bankroll_20 follows).
