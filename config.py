"""Configuration for the PAPER run of the XRP (Ripple) Up/Down bot (5-minute and 1-hour markets).

PAPER ONLY. This build has no live-order path. Per-trade stake can never exceed
STAKE_HARD_MAX_USD ($10); that ceiling is enforced in code at fill time, no matter
what variants.json says.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_NAME = "week1"
DATA_DIR = ROOT / "data" / RUN_NAME          # week run lives here; today's 2-hour run stays in data/
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "bot.log"
DECISIONS_CSV = DATA_DIR / "decisions.csv"
HEARTBEAT_PATH = DATA_DIR / "heartbeat.json"
PID_PATH = DATA_DIR / "bot.pid"
FAULT_PATH = DATA_DIR / "stack_dumps.log"
VARIANTS_PATH = ROOT / "variants.json"
TWEAKS_LOG = ROOT / "data" / "TWEAKS_LOG.md"
KILL_SWITCH_PATH = ROOT / "STOP"             # touch this file to stop bot + supervisor

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
ASSET = "XRP"
# Polymarket's XRP Up/Down market families we trade (verified on the public Gamma API). Each variant picks one via "timeframe".
TIMEFRAMES = {
    "5m": {"seconds": 300, "slug": "xrp-updown-5m-{start}", "candle_sec": 60, "candle_lookback": 60},
    "1h": {"seconds": 3600, "slug": "xrp-updown-1h-{start}", "candle_sec": 300, "candle_lookback": 48},   # hourly trend read = last 4h of 5-min candles
}
WINDOW_SECONDS = 300          # kept for the 5-minute family (first-minute skip etc.)
COINBASE_PRODUCT = "XRP-USD"

RUN_UNTIL = "2026-12-07T11:30:00-08:00"      # hard end of this paper run (PT); new bets stop here
RESOLVE_GRACE_SEC = 900                      # after RUN_UNTIL keep settling open paper bets

STAKE_HARD_MAX_USD = 10.00                   # ABSOLUTE ceiling per trade. Never raise.
MIN_ORDER_SHARES = 5
TAKER_FEE_RATE = 0.07                        # fee = shares * 0.07 * p * (1-p)

# Model. Basis noise = how far our Coinbase price-to-beat proxy is from Chainlink's official one.
# Measured on 67 resolved XRP 5-minute markets on Nov 19 (Coinbase 1m typical price vs Polymarket's official priceToBeat): sd = 0.024-0.030% of price. Rounded up to 0.03% to stay conservative.
BASIS_NOISE_PCT = 0.0003
VOL_MULT = 1.0
DEFAULT_ANNUAL_VOL = 0.8                     # XRP realized vol was ~0.64-0.77/yr over the last 14 days (median 2-hour reading 0.64); the bot re-measures every 2 min
VOL_MIN, VOL_MAX = 0.4, 3.0
VOL_LOOKBACK_MINUTES = 120

# Loop / network
POLL_INTERVAL_SEC = 2.0
HTTP_CONNECT_TIMEOUT = 4
HTTP_READ_TIMEOUT = 6
HTTP_HARD_DEADLINE = 8                       # wall-clock cap per request (covers DNS too)
CYCLE_STALL_DUMP_SEC = 90                    # dump a stack trace if one loop pass takes longer
HEARTBEAT_EVERY_SEC = 60
DECISION_LOG_EVERY_SEC = 20                  # per variant, plus every buy
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 5
DECISIONS_MAX_BYTES = 20_000_000
DECISIONS_BACKUPS = 3
USER_AGENT = "xrp-paper-bot/1.0 (paper; research)"

# ---------------------------------------------------------------------------
# The three labeled tweaks (global on/off switches; each also applies per variant)
# ---------------------------------------------------------------------------
# Tweak 1: SKIP_FIRST_MINUTE — never trade in the first 60 seconds of a 5-minute window.
SKIP_FIRST_MINUTE = True
# Tweak 2: HOUR_FILTER — skip an hour of the day (PT) once a variant has at least
# HOUR_FILTER_MIN_TRADES settled paper trades in that hour-of-day this run AND is net
# negative there. (CLOB /trades needs auth / 401s, so this uses the bot's own history.)
HOUR_FILTER = True
HOUR_FILTER_MIN_TRADES = 8
# Tweak 3: LOG_BET_REASONS — write a plain-English reason for buys and skips to the log.
LOG_BET_REASONS = True

# Decimal places used when printing dollar gaps/levels in plain-English messages (XRP trades around $0.97).
PRICE_DP = 4
