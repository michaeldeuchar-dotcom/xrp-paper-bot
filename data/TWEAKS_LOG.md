
## 2026-11-19 — initial XRP fleet (fake money)
- Copied from `sol-paper-bot` (main at da9ecdc, 9 variants: control, model_lead, ride_favorite_80, trend_volume, trend_down, breakdown, down_ride (5m), hourly_ride_down (1h), plus `auto_pick`). `auto_pick` had already landed on SOL's main, so it is included: it follows whichever other 5m variant has the best net P&L over the trailing 6h (needs 5+ settled bets and positive P&L), flat ~$3 stake. See SOL's `tweaks/2026-11-19-auto-pick.md` for the full rules (copied below).
- Swapped the coin: Polymarket slugs `xrp-updown-5m-<unix>` / `xrp-updown-1h-<unix>` (both verified live on the Gamma API; resolution source Chainlink XRP/USD 60s TWAP), Coinbase `XRP-USD`, Kraken `XRPUSD`, Bitstamp `xrpusd` spot feeds.
- Calibration (14 days of Coinbase 1m candles, 2026-11-05..19): XRP realized vol was ~0.64-0.77/yr over the last 14 days (median 2-hour reading 0.64). 5-minute move sd and median |move| are 0.229% / 0.122% (SOL: 0.223% / 0.108%), so the lead gates are 0.055% (5m) and 0.1% (1h) instead of SOL's 0.05% / 0.10%. Trend / momentum / volume thresholds are z-scores or ratios and stay the same.
- Basis noise (our Coinbase price-to-beat proxy vs Polymarket's official priceToBeat) measured on 67 resolved XRP 5m markets: sd 0.024-0.030% of price; set to 0.03%.
- Fill logic unchanged from SOL: paper fills only at the real best ask, only if the best ask level shows enough shares (else `thin_book` skip), 5-share minimum, fee = shares x 0.07 x p x (1-p), $10 hard cap per bet incl. fee.
- Plain-English gap messages now print 4 decimals (XRP price scale).

### (copied unchanged from sol-paper-bot, where it was written; 'SOL' below means the Solana bot)
## 2026-11-19 (about 5:15 PM PT) — add `auto_pick` variant (fake money only)
- Why: the owner asked for a SOL bot that picks the best-performing strategy on its own instead of a fixed approach.
- What: new variant `auto_pick` (5m), its own separate fake account (`trades_auto_pick.csv`). The 8 existing variants are UNCHANGED and keep supplying the scores.
- Rules:
  - Each round it ranks the other 5-minute variants by net fake P&L on bets that settled in the last 6 hours; if nobody qualifies in that window it falls back to since-start.
  - A variant qualifies only with 5+ settled bets and positive P&L; if nobody qualifies, auto_pick sits out.
  - It follows the leader's bets (same side, same moment). If the leader isn't betting it follows the next-ranked qualifier, so it never takes both sides of one window.
  - First pick is taken at once; switching between two qualifiers needs the new one to be best at 2 straight round checks (no flapping). If the leader stops qualifying, it moves to the best qualifier at once.
  - Stake: flat $3 target including fee; when $3 can't buy Polymarket's 5-share minimum (asks of ~60c+) it rounds up to 5 shares, never above $5 total. Hard max $10 unchanged.
  - Every check and every leader switch is logged (`AUTO_PICK CHECK` / `AUTO_PICK SWITCH`) and each switch is appended to `data/week1/auto_pick_switches.csv`. The current pick is in `state.json` (`auto_pick`) and in `heartbeat.json`.
  - `hourly_ride_down` (1h) is not a candidate: auto_pick trades 5m windows only.
- Avoiding BTC `meta` flaws: flat stake (meta scaled stake up after hot streaks, so its biggest losses came right after a leader's best run); a switch needs confirmation; ptb is passed to the copied variant's rules so lead gates really apply (the SOL port of meta_decide forgot it).
- Also: `summarize.py` shows auto_pick's current pick and leaves it out of the "all variants together" total (it only copies others' bets); `scripts/ci_run.sh` saves `auto_pick_switches.csv`.
- Unchanged: end time Nov 23 11:30 AM PT, $10 hard max, no wallet, no real trades.

## 2026-11-19 (about 5:45 PM PT) — auto_pick follows ONLY its current pick (fake money only)
- Change: auto_pick no longer falls back to the next-ranked qualifying variant. If its current pick doesn't bet in a window, auto_pick sits out that window.
- Why: the fallback meant most of its bets copied lower-ranked variants (mostly `control`) instead of the pick it reports.
- Unchanged: 6h net-P&L ranking (since-start fallback), 5+ bets and positive P&L to qualify, two-check rule to switch leaders, flat $3 stake with round-up to the 5-share minimum capped at $5 total, $10 hard max, end time.

## 2026-11-22 (evening, PT) — bankroll_20: $20 fake-money start, $1,000 goal
- Change: added one new variant, `bankroll_20`. It gets its own $20 fake-money balance and tries to turn that into $1,000. It follows ONLY the group's current auto_pick (same 6-hour net-P&L ranking, 5+ settled bets and positive P&L to qualify, two-check rule to switch). Stake is about 20% of cash on hand, rounded up to the 5-share minimum, never more than $10 including the taker fee, and never more than the cash it has left. Balance updates when a bet settles. If the cash can't cover a minimum order it stops (busted) and logs it. If the balance hits $1,000 it records the goal and stops new bets. Balance history is written to `data/week1/bankroll_history.csv`. The results summary shows a `Bankroll: $X of $20 start, goal $1,000, N bets` line.
- Why: give this group a single funded fake-money account with a clear $1,000 target, without changing how the other variants bet or how auto_pick ranks them.
- Unchanged: every other variant, auto_pick's own $3 stake, $10 hard max per bet, fake money only, end time (still Nov 23, 11:30 AM PT).

## 2026-11-22 (evening, PT) — extend the paper run through Monday, Dec 7, 11:30 AM PT
- Change: the hard end time is now Monday, December 7, 2026 at 11:30 AM PT (was November 23, 11:30 AM PT). Every variant, including `bankroll_20`, can keep placing new fake-money bets until then. After that the bots stop new bets and settle anything still open. The hourly backup cron and the self-restarting job chain use the same new end time. Results still live in `data/week1/` (that folder name is just the run label, not a calendar week).
- Why: the owner approved two more weeks so the fleets, including the $20-to-$1,000 bankroll bots, have a longer stretch of fake-money results.
- Unchanged: fake money only, no wallet, no real trades, $10 hard max per bet including the taker fee, existing state and ledgers, `bankroll_20` starting balance and $1,000 goal.

## 2026-11-22 (evening, PT) — end the week1 trial; start a fresh two-week run in data/week2 through Dec 7
- Change: the first trial (`data/week1`) is frozen as-is on the `data` branch — no reset, no delete, no overwrite. A new paper run starts in `data/week2` from zero for every variant, including a fresh `bankroll_20` at $20 fake money with a $1,000 goal, and auto_pick ranking that uses only week2 results. New bets in this run stop Monday, December 7, 2026 at 11:30 AM PT. The results tool defaults to week2; pass `--run week1` to read the first trial.
- Why: the owner asked to close the first trial and start a clean two-week test, while keeping the week1 ledgers for the final breakdown.
- Unchanged: fake money only, no wallet, no real trades, $10 hard max per bet including the taker fee, week1 files on the data branch.

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

# Crowd and max-profit paper variants (week 2)

Added two **new** paper-only variants to the week-2 test (ends Monday, Dec 7, 2026, 11:30 AM PT). Existing variants, their week-2 ledgers, and the frozen week-1 data are unchanged.

Fake money only. No real trades, no wallet key.

These two are **not** ranking candidates for auto_pick (and therefore not what bankroll_20 follows). auto_pick still ranks only the original 5-minute hold-to-settlement strategies.

## `crowd` — follow the most popular bet

Bets with the crowd on the 5-minute Up/Down market, mid-window (about 60–180 seconds left), at a normal stake (about $3–5, 5-share minimum, usual $10 cap including the fee).

It uses only official public APIs (no website scraping, no login):

1. **Chat / comments** from Gamma `GET /comments?parent_entity_type=Event&parent_entity_id=…`. Comments in the current window are counted as leaning Up or Down with a simple keyword list (up/pump/moon/long/green/bull vs down/dump/short/red/rekt/bear, and similar). A comment that hits both sides is ignored. Chat only counts as a signal when there are at least three classified comments and about two-thirds lean one way.
2. **Trade-flow popularity** from Data API v2 `GET /v2/trades?condition=…` (public, no key). It sums **buy** notional on Up vs Down in the current window (sells are not treated as a vote for the other side). One side needs about 62% of that buy flow, with at least about $15 traded, to count as a signal.
3. **Order-book imbalance** (bid size on Up vs Down) as a check. If flow and the book strongly disagree, it sits out.

If chat and flow agree (or chat is silent and flow is clear), it buys that side, as long as the ask is in a sensible range (about 25–90¢). If the comment feed is empty or the comments call fails, it falls back to trade flow and records that. These short 5-minute and 15-minute markets often have **zero comments** (`commentCount` was 0 when checked on Nov 23, 2026, PT); the API itself is reachable without auth. Results and the bot log will show how often comments were actually present.

## `max_profit` — grow the fake bankroll as fast as possible, without going to zero

Its **own** fake-money account, starting at **$100**. It does not share the $20 bankroll_20 account.

It may use any signal from the other paper strategies in the same window (the strong week-1 names, their 15-minute copies, and crowd when that has a clear read), on **both the 5-minute and 15-minute** markets, either side. It can sell early with the same cash-out rules as the cash-out variants (take profit at 95¢ or about +20% after the sell fee; stop if down about 45%; exit if the lead flips against it late in the window).

**Sizing:** half-Kelly from its estimated chance of winning and the ask + fee, then capped so any **single** bet is at most **25%** of current equity, never more than cash on hand, and never so large that a loss would break the **$12 floor**. If cash falls below $12, it pauses until a settlement brings it back. It can have a 5-minute and a 15-minute bet open at the same time, but each is still capped at 25%.

**The $10 cap:** the usual $10-per-bet ceiling is a real-money safety rule. Because this is **fake money**, `max_profit` is allowed to exceed $10 on a single paper bet (still bounded by the 25% / floor rules above). **Do not use this variant with real money as-is.**

**Maker-style entries:** when the spread is at least 2¢ and there is time, it rests a paper limit at the best bid and only fills if the ask crosses that price (no taker fee on that fill). If it has not filled with about 40 seconds left, it takes the ask if the trade still fits the caps.

Bankroll history is written to `data/week2/max_profit_history.csv`. `pull_results.sh` / summarize prints the current max-profit balance.

# A/B hedge paper variants (week 2)

Added three **new** paper-only variants to the week-2 test (ends Monday, Dec 7, 2026, 11:30 AM PT): `ab_hedge_5m`, `ab_hedge_15m`, and `ab_hedge_1h`. Existing variants, their week-2 ledgers, and the frozen week-1 data are unchanged.

Fake money only. $10 max per bet including the fee. No real trades, no wallet key.

These three are **not** ranking candidates for auto_pick (and therefore not what bankroll_20 follows). auto_pick still ranks only the original 5-minute hold-to-settlement strategies.

## The idea

An A bot and a B bot play opposite sides, trying to pull a win off a loss. Buying both sides at the same moment always loses a little, because Up plus Down together cost a bit over $1 once you add the fee. So A bets first, and B only buys the other side later if that side gets cheap enough that the two together cost under $1 by a margin. Then one of them pays out $1 at settlement and the pair locks a small win. If the other side never gets cheap, A just rides to the official result alone.

Hourly markets have more time for the other side to get cheap, which is why we run the same idea on 5-minute, 15-minute, and 1-hour Up/Down markets. 1-hour slugs are `{coin}-updown-1h-{unix}` (verified on the public Gamma API on Nov 23, 2026, PT, for BTC, ETH, SOL, and XRP). Bitcoin's bot did not previously watch the 1-hour family; that slug is now in its timeframe list so the 1-hour hedge can run.

## How a pair is placed

- **Leg A:** early-ish in the window, on the side the coin's price already leads (spot vs the price to beat). About $5 including the fee, at least 5 shares, usual $10 cap. The lead has to be bigger than the usual price-to-beat noise, so A does not bet when the coin is basically flat. Asks outside about 20–80¢ are skipped. The best ask must be large enough to fill the whole order (we do not walk into worse prices). Thin books, especially on SOL and XRP, will often mean a skipped bet.
- **Leg B (lock_profit):** during the rest of the window (leaving the last seconds alone), watch the opposite side's best ask. Buy the **same number of shares as A** only if A's entry cost per share + B's ask + both taker fees (`shares × 0.07 × p × (1 − p)`) is at least **2 cents under $1/share**. That locks a guaranteed profit at settlement.
- **Leg B (limit_loss), optional:** if a cheap lock never appears, but we are in the back half of the window, A has been held for a short minimum time, the price lead has flipped against A, and A is down about 40% (or the other side is a heavy favorite), B may still buy if the locked loss is at most about **10 cents/share**. This is a damage cap, not a guaranteed win, and it is logged separately as `limit_loss`.
- **Otherwise** A is held to official settlement alone (`a_only`).

Each pair is written to `data/week2/ab_hedge_pairs.csv` (window, A side/price, B price or none, type, P&L) once both legs have settled (or A settles with no B). `pull_results.sh` / summarize prints a short pair breakdown alongside the usual per-variant ledgers (`trades_ab_hedge_5m.csv` and the 15m/1h twins).

## Other fix in this same change (Bitcoin bot errors)

On the evening of Nov 23, 2026, PT, the Bitcoin 5-minute family was logging the same error over and over: `KeyError: 'min_edge'` inside `strategy.decide`, called from the meta copier. Meta was allowed to consider the $20 bankroll account (and any other variant missing a `min_edge` setting) as a leader to copy. Because that account was doing well, meta crashed every cycle and skipped later 5-minute variants in the same pass (including crowd, max_profit, and bankroll_20).

The fix: `min_edge` is now read with a default of 0 so a missing setting cannot crash a cycle; meta no longer ranks or copies copiers, cash-out variants, or the excluded experimental ones (crowd, max_profit, ab_hedge, bankroll_20); and one variant throwing an error can no longer abort the rest of that timeframe's pass. No bots were cut.

The start-next-slice workflow now looks for `data/week2/FINISHED` (the current test) instead of `data/week1/FINISHED`.
