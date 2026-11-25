# 2026-11-24 (8:50 PM PT) — cut ab_hedge_v2_5m and ab_hedge_15m (v1) (fake money)

Routine tune-up. Both variants have lost money in all four fleets over a meaningful sample, so they are disabled (no new bets; open bets still settle; history kept in data/week2). Nothing else changed.

Run-to-date week-2 numbers at 8:45 PM PT:
- ab_hedge_v2_5m: BTC 116 settled -$20.29, SOL 110 -$16.52, ETH 108 -$13.45, XRP 102 -$23.75. Total 436 settled, -$74.01. Win rate sits at ~50% because A and B are opposite sides, so fees and the soft-lock/limit-loss cost decide the result, and that cost has been negative everywhere.
- ab_hedge_15m (v1): BTC 161 settled -$4.97, SOL 156 -$43.53, ETH 156 -$28.74, XRP 151 -$27.05. Total 624 settled, -$104.29.

Kept running for more data: ab_hedge_v2_15m (also negative so far, ~320 settled, about -$69, to be reviewed next), ab_hedge_1h and ab_hedge_v2_1h (small samples, about breakeven / slightly negative). price_lag changes from 5:06 PM are still too new to judge. auto_pick / bankroll_20 unchanged (originals only). $10 per-bet cap unchanged.

Deploy: push main, wait for the running slice's next state save, cancel it so the chain restarts on the new commit. Paper mode only; data/week1 untouched.
