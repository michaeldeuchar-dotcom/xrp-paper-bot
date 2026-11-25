# 2026-11-25 (12:55 AM PT) — cut ab_hedge_v2_15m (fake money)

Routine tune-up. ab_hedge_v2_15m has lost money in all four fleets over a meaningful sample, so it is disabled (no new bets; open bets still settle; history kept in data/week2). Nothing else changed.

Run-to-date week-2 numbers at 12:50 AM PT (settled A/B pairs):
- BTC 57 pairs -$19.30, SOL 58 -$30.69, ETH 57 -$26.63, XRP 53 -$25.62. Total 225 pairs, -$102.24.
- Only 5 of 225 pairs locked a profit; most ended as small soft-lock losses and the limit-loss caps did the real damage.

Still running: ab_hedge_1h and ab_hedge_v2_1h (only ~14 settled pairs each per fleet, slightly negative; review when the sample is bigger). price_lag variants are mixed by fleet and still small. auto_pick / bankroll_20 unchanged (originals only). $10 per-bet cap unchanged.

Deploy: push main, wait for the running slice's next state save, cancel it so the chain restarts on the new commit. Paper mode only; data/week1 untouched.
