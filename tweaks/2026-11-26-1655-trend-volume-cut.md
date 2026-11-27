# 2026-11-26 (4:55 PM PT) — cut trend_volume (fake money)

Routine tune-up. Variants disabled (no new bets; open bets still settle; history kept in data/week2). Nothing else changed.

Run-to-date week-2 numbers at 4:55 PM PT (settled bets):
- trend_volume (XRP): 101 settled, 50% win, -$17.31.
- trend_volume across fleets: BTC 73 settled -$25.15 (already cut), ETH 69 -$15.67, XRP 101 -$17.31, SOL 89 -$3.68 (kept for now, near flat). Win rate ~50% at the prices it buys isn't enough to cover fees.

max_profit reads its signals from the variant list regardless of "enabled", so it is unchanged. auto_pick / bankroll_20 still rank only the original 5-minute hold-to-settlement strategies; cut ones drop out of that pool. $10 per-bet cap unchanged. Paper mode only; data/week1 untouched.
