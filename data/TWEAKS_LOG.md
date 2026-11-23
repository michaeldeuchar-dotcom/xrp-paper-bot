
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
