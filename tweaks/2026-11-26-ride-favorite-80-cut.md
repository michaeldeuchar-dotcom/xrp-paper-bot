# 2026-11-26 (12:50 AM PT) — cut ride-the-favorite (fake money)

Routine tune-up. One variant disabled (no new bets; open bets still settle; history kept in data/week2). Nothing else changed.

Run-to-date week-2 numbers at 12:45 AM PT (settled bets):
- Ride the favorite (ride_favorite on BTC, ride_favorite_80 elsewhere): BTC 285 settled -$9.78, SOL 310 -$28.40, ETH 401 -$20.88, XRP 245 -$122.77. Total 1,241 settled, -$181.83 — losing in all four fleets even though it wins 80-87% of bets (the 80c+ prices plus fees eat the wins). (Here: 245 settled, -$122.77.) Last 6 hours: ETH 27 settled -$10.90, BTC 9 -$2.93, SOL 6 -$0.48.

max_profit reads its source signals straight from the variant list (it doesn't check "enabled"), so max_profit's behavior is unchanged. auto_pick / bankroll_20 still rank only the original 5-minute hold-to-settlement strategies; the cut ones simply drop out of that pool. $10 per-bet cap unchanged.

Deploy: push main, wait for the running slice's next state save, cancel it so the chain restarts on the new commit. Paper mode only; data/week1 untouched.
