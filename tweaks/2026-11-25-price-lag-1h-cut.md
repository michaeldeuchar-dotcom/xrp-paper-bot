# 2026-11-25 (9:00 AM PT) — cut price_lag_1h (fake money)

Routine tune-up. One variant disabled (no new bets; open bets still settle; history kept in data/week2). Nothing else changed.

Run-to-date week-2 numbers at 8:55 AM PT (settled bets):
- price_lag_1h: BTC 9 settled -$11.76, SOL 6 +$2.22, ETH 7 -$14.37, XRP 7 -$22.78. Total 29 settled, 10 won, -$46.69 (losing in 3 of 4 fleets). It was flagged "watching" at the 4:55 AM tune-up; it kept losing.

Still running: price_lag_5m and price_lag_15m (mixed to positive; price_lag_15m is up in all four fleets). ab_hedge_1h v1 unchanged. auto_pick / bankroll_20 unchanged (originals only). $10 per-bet cap unchanged.

Deploy: push main, wait for the running slice's next state save, cancel it so the chain restarts on the new commit. Paper mode only; data/week1 untouched.
