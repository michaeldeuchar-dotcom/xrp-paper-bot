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
