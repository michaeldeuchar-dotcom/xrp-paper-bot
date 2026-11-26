# 2026-11-25 (8:50 PM PT) — cut price_lag_5m (fake money)

Routine tune-up. One variant disabled (no new bets; open bets still settle; history kept in data/week2). Nothing else changed.

Run-to-date week-2 numbers at 8:45 PM PT (settled bets):
- price_lag_5m: BTC 27 settled -$43.44, SOL 63 -$32.06, ETH 129 -$10.76, XRP 25 +$14.34. Total 244 settled, -$71.92 — losing in 3 of 4 fleets. (Here: 25 settled, +$14.34.) ETH alone lost -$18.70 on 22 bets in the last 6 hours.
- The slower price_lag_15m stays on (up in 3 of 4 fleets: ETH +$51.47, XRP +$18.71, SOL +$4.39, BTC -$11.24).

auto_pick / bankroll_20 unchanged (originals only; price_lag was never in their pool). $10 per-bet cap unchanged.

Deploy: push main, wait for the running slice's next state save, cancel it so the chain restarts on the new commit. Paper mode only; data/week1 untouched.
