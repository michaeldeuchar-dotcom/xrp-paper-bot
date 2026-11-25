# 2026-11-25 (4:55 AM PT) — cut ab_hedge_v2_1h and the favorite cash-out copy (fake money)

Routine tune-up. Two variants disabled (no new bets; open bets still settle; history kept in data/week2). Nothing else changed.

Run-to-date week-2 numbers at 4:50 AM PT:
- ab_hedge_v2_1h (settled A/B pairs): BTC 18 -$6.79, SOL 18 -$10.04, ETH 18 -$7.03, XRP 18 -$11.48. Total 72 pairs, -$35.34; only 3 of 72 locked a profit. Same failure pattern as the already-cut v2 5m/15m.
- Favorite cash-out copy (ride_favorite_xo on BTC, ride_favorite_80_xo elsewhere): BTC 181 -$61.96, SOL 222 -$45.03, ETH 218 -$61.10, XRP 203 -$142.33. Total 824 bets, -$310.42. The hold-to-settlement originals lost less in every fleet (-$15.69 / -$19.42 / -$1.36 / -$116.36), so selling early is making it worse.

Still running: ab_hedge_1h v1 (about 30 pairs per fleet, mixed: -$3.51 / -$13.25 / -$3.45 / +$2.55). price_lag_1h is 0-for-12 outside BTC but only 17 settled total; watching it. Other _xo variants unchanged. auto_pick / bankroll_20 unchanged (originals only). max_profit sources unchanged (none were cash-out copies). $10 per-bet cap unchanged.

Deploy: push main, wait for the running slice's next state save, cancel it so the chain restarts on the new commit. Paper mode only; data/week1 untouched.
