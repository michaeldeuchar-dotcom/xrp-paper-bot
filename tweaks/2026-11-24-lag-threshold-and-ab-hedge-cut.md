# 2026-11-24 — price_lag fresh-jump fix (alts) and ab_hedge_5m cut (fake money)

Paper trading only. No real orders, no wallet key. Week-1 data untouched; week-2 keeps running through Dec 7, 2026, 11:30 AM PT.

## 1. price_lag: smaller fresh-jump bar ($0.0015 -> $0.0003)

**What:** `price_lag_5m`, `price_lag_15m`, and `price_lag_1h` now need a $0.0003 spot jump in the last 8 seconds (`min_fresh_usd`), down from $0.0015. Nothing else in price_lag changed: still a 5¢ edge after the fee, about $5 a bet, $10 cap, daily −$20 kill switch, and the jump must be in the bet's direction.

**Why:** the old bar was badly set. BTC's $20 is about 0.034% of spot; the XRP bar was about 0.16–0.17% of spot. Across all four alt fleets (about 600–700 logged edges each; 699 here) the biggest fresh move ever seen was about 0.089% of spot, so **zero** edges ever counted as fresh and these variants never traded. The new bar is the same %-of-spot as BTC's $20, and is also the level that gives about the same hit rate BTC has (about 5% of logged edges fresh).

## 2. ab_hedge_5m (the original v1) switched off

**What:** `ab_hedge_5m` now has `"enabled": false` in `variants.json` — the same switch already used for `contrarian`, `breakout_scale`, etc. The bot stops opening new ones; any bet already open still settles normally; its history stays in `summarize.py` / `pull_results.sh`.

**Why:** 257 settled bets in this fleet, net **−$46.99** fake. It lost in every fleet: BTC 244 bets −$56.13, SOL 211 −$69.12, ETH 263 −$47.98, XRP 257 −$46.99. The cheap-hedge "lock profit" wins are small, and the few times A rides alone and loses wipe them out.

Still running: `ab_hedge_15m`, `ab_hedge_1h` (v1) and all `ab_hedge_v2_*`. auto_pick / bankroll_20 ranking (originals only) and the $10 per-bet cap are unchanged.

This deploy also picks up the price_lag direction-rule commit that was already on `main` but not in the slice running since 2:05 PM PT.

## Deploy

Checks before push: `python -m py_compile *.py`, a dry `load_variants()` (ab_hedge_5m dropped, other ab_hedge variants kept), and `summarize.py --run week2` on the latest data branch (ab_hedge_5m history still shown).

Same safe method as before: push `main`, wait for the running slice's next state save to `data`, cancel that slice so the chain restarts on the new commit, confirm a fresh heartbeat.

(Run IDs and restart times are added right after the restart.)
