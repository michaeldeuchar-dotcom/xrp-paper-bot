# xrp-paper-bot — PAPER-TRADING EXPERIMENT ONLY (fake money)

A fake-money experiment on Polymarket's **XRP (Ripple) Up or Down** markets: the 5-minute family
(`xrp-updown-5m-<unix>`) and the 1-hour family (`xrp-updown-1h-<unix>`). Both resolve on Chainlink's XRP/USD
60-second TWAP stream. **It never places real orders**: the code has no wallet, no order-signing, and uses no
secrets. It reads public prices (Coinbase, Kraken, Bitstamp, Polymarket Gamma + CLOB order books), pretends to buy
at the real best ask (including Polymarket's taker fee, `shares x 0.07 x p x (1-p)`, 5-share minimum), and holds each
pretend bet until the official result. Per-bet cost is hard-capped at $10 (fee included). Nothing here is financial advice.

- Built from the same code as the Bitcoin paper bot (`btc5m-paper-bot`), adapted for XRP (price-scaled basis noise,
  higher volatility limits, a minimum-lead gate, down-only variants, and an hourly market family).
- 9 strategy variants run side by side, each with its own fake account (`variants.json`).
- Week-2 also runs cash-out, 15-minute, crowd, max-profit, and A/B-hedge (`ab_hedge_5m` / `_15m` / `_1h`, plus tuned `ab_hedge_v2_5m` / `_15m` / `_1h`) paper variants. Those are scored separately and are not used by auto_pick or bankroll_20.
- Results: the `data` branch — current run in `data/week2/` (`trades_<variant>.csv`, `state.json`, `heartbeat.json`, log tails); the first trial is kept untouched in `data/week1/`.
- Summary: copy `data/` from the `data` branch, then `python3 summarize.py`.
- Runs on GitHub Actions in ~5h40m slices (`.github/workflows/paper-bot.yml`) until 2026-12-07 11:30 AM PT
  (extendable: change `END_AT` in the workflow and `RUN_UNTIL` in `config.py`).
  Each slice starts the next one; an hourly cron (:17) restarts the chain if it ever breaks.
- Stop it: commit a file named `STOP` to `main`, or disable the workflow.
