"""Price-lag paper variant: bet when a fresh move in live exchange spot has
not yet been priced into the Polymarket book, and the model edge after the
taker fee still beats a safety buffer.

Resolution is Chainlink's 60-second BTC/USD (or ETH/SOL/XRP) TWAP at the
window start vs the window end. The official Chainlink Data Streams REST API
needs an API key we do not have, so:

  * strike (price to beat) = official eventMetadata.priceToBeat once it exists,
    else the previous window's official finalPrice (that is this window's
    strike, once Polymarket publishes it), else the bot's existing proxy
    (our own last-60s spot TWAP, or a Coinbase 1-minute typical price).
  * live spot = the same public USD feeds the bot already uses (Coinbase
    Exchange, Coinbase retail, Kraken, Bitstamp), plus Gemini when it is
    close to the others. Binance.com is geo-blocked (HTTP 451) from typical
    US / GitHub Actions hosts; Binance.US was stale when probed; USDT venues
    are not mixed into a USD strike.

Fake money only. No live orders, no wallet.
"""
from __future__ import annotations

import csv
import logging
import time
from typing import Callable, Optional

import config
import feeds
import net
import polymarket
import safety
import store
import strategy

log = logging.getLogger("price_lag")

TRADES_CSV = config.DATA_DIR / "price_lag_trades.csv"
TRADES_FIELDS = [
    "ts_pt", "variant", "timeframe", "window_start", "slug",
    "side", "model_p", "ask", "fee_ps", "edge",
    "spot", "ptb", "ptb_src", "secs_left",
    "fresh_move", "fresh_sec", "book_mid",
    "latency_ms", "fastest_feed", "fastest_ms", "feed_spread",
    "status", "reason", "shares", "total_cost", "trade_id",
]


def _product() -> str:
    return getattr(config, "COINBASE_PRODUCT", "BTC-USD")


def _gemini_symbol() -> str:
    return _product().replace("-", "").lower()  # BTC-USD -> btcusd


def _basis_noise(spot: float) -> float:
    if hasattr(config, "BASIS_NOISE_USD"):
        return float(config.BASIS_NOISE_USD)
    if hasattr(config, "BASIS_NOISE_PCT"):
        return float(config.BASIS_NOISE_PCT) * float(spot)
    return 0.0


def min_fresh_move(spot: float, v: dict) -> float:
    if v.get("min_fresh_usd") is not None:
        return float(v["min_fresh_usd"])
    if v.get("min_fresh_pct") is not None:
        return float(v["min_fresh_pct"]) * float(spot)
    # A sharp move, at least ~1.5x the usual proxy-vs-Chainlink noise.
    return max(_basis_noise(spot) * 1.5, 0.0008 * float(spot))


def official_strike(tf: str, ws: int, get_market_fn: Callable) -> tuple[Optional[float], str]:
    """Prefer Polymarket's published Chainlink TWAP60 strike when it is available."""
    try:
        m = get_market_fn(ws, tf)
    except TypeError:
        m = get_market_fn(ws)
    except Exception:
        m = None
    meta = (m.meta or {}) if m is not None else {}
    raw = meta.get("priceToBeat")
    if raw not in (None, ""):
        try:
            return float(raw), "official_priceToBeat"
        except (TypeError, ValueError):
            pass
    # The previous window's official final price is this window's strike.
    try:
        prev_ws = int(ws) - int(polymarket.window_seconds(tf))
        try:
            prev = get_market_fn(prev_ws, tf)
        except TypeError:
            prev = get_market_fn(prev_ws)
        fin = ((prev.meta or {}) if prev is not None else {}).get("finalPrice")
        if fin not in (None, ""):
            return float(fin), "prev_window_official_final"
    except Exception:
        pass
    return None, ""


_extra_ts = 0.0

def touch_extra_feeds(median: float) -> None:
    """Ping Gemini at most once a cycle as an extra USD print (not mixed into other variants)."""
    global _extra_ts
    now = time.time()
    if now - _extra_ts < 1.5:
        return
    _extra_ts = now
    maybe_gemini(median)

def feed_meta() -> dict:
    return getattr(feeds, "last_spot_meta", {}) or {}


def maybe_gemini(median: float) -> Optional[float]:
    """Optional extra USD print; ignored if it is far from the existing median."""
    url = f"https://api.gemini.com/v1/pubticker/{_gemini_symbol()}"
    t0 = time.time()
    try:
        data = net.hard_get_json(url, 1.5)
        px = float(data.get("last") or data["last"])
        ms = (time.time() - t0) * 1000
        meta = feed_meta()
        meta.setdefault("latencies_ms", {})["gemini"] = ms
        meta.setdefault("prices", {})["gemini"] = px
        if median > 0 and abs(px - median) / median > 0.002:
            return None  # 20 bps off — don't trust it against the strike
        return px
    except Exception as exc:  # noqa: BLE001
        meta = feed_meta()
        meta.setdefault("errors", {})["gemini"] = str(exc)[:120]
        meta.setdefault("latencies_ms", {})["gemini"] = (time.time() - t0) * 1000
        return None


def _history(state: dict) -> list:
    h = state.setdefault("spot_hist", [])
    return h


def record_spot(state: dict, now: float, spot: float, keep_sec: float = 30.0) -> None:
    h = _history(state)
    h.append((float(now), float(spot)))
    cutoff = now - keep_sec
    state["spot_hist"] = [(t, p) for t, p in h if t >= cutoff][-80:]


def fresh_move(state: dict, now: float, spot: float, need: float, lookback: float) -> tuple[float, float]:
    """Signed spot change vs the quote closest to now-lookback. Positive = spot rose."""
    target = now - float(lookback)
    best = None
    for t, p in _history(state):
        age = now - t
        if age < 0.2 or age > lookback + 1.5:
            continue
        if best is None or abs(t - target) < abs(best[0] - target):
            best = (t, p)
    if best is None:
        return 0.0, 0.0
    return float(spot) - float(best[1]), now - best[0]


def _fmt_px(x: float) -> str:
    ax = abs(float(x))
    if ax >= 1:
        return f"${x:,.2f}"
    if ax >= 0.01:
        return f"${x:.3f}"
    return f"${x:.5f}"


def _stats(state: dict, name: str) -> dict:
    return state.setdefault("price_lag_stats", {}).setdefault(name, {
        "evals": 0, "opportunities": 0, "fresh_opportunities": 0, "filled": 0,
    })


def decide(name: str, v: dict, *, state: dict, books: dict, secs_left: float, spot: float,
           ptb: float, ptb_src: str, ws: int, now: float, tf: str, m, p_up: float,
           official_ptb: Optional[float] = None, official_src: str = "",
           observed_twap: Optional[float] = None) -> strategy.Decision:
    D = lambda action, reason, english, **kw: strategy.Decision(  # noqa: E731
        name, action, reason, english, model_p_up=p_up, **kw)
    st = _stats(state, name)
    st["evals"] = int(st.get("evals") or 0) + 1
    record_spot(state, now, spot)

    use_ptb, use_src = ptb, ptb_src or "proxy"
    if official_ptb:
        use_ptb, use_src = float(official_ptb), official_src or "official"
        # Recompute P(Up) against the official Chainlink strike when we have it.
        vol = feeds.realized_annual_vol()
        obs = None
        # last-minute observed TWAP is handled by the caller-provided p_up when
        # official_ptb is None; when we swap the strike, recompute.
        p_up = strategy.p_up_model(spot, use_ptb, secs_left, vol, observed_twap)

    meta = feed_meta()
    lats = meta.get("latencies_ms") or {}
    fastest_feed, fastest_ms = "", None
    if lats:
        fastest_feed, fastest_ms = min(lats.items(), key=lambda kv: kv[1])
    latency_ms = max(lats.values()) if lats else None
    prices = [p for p in (meta.get("prices") or {}).values() if isinstance(p, (int, float)) and p > 0]
    # readings from the last current_spot call may only be the median's inputs
    if not prices and isinstance(meta.get("readings"), dict):
        prices = [p for p in meta["readings"].values() if isinstance(p, (int, float)) and p > 0]
    spread = (max(prices) - min(prices)) if len(prices) >= 2 else 0.0

    up, down = books.get("Up"), books.get("Down")
    mid_up = strategy._mid(up)
    if mid_up is None and down is not None and strategy._mid(down) is not None:
        mid_up = 1.0 - strategy._mid(down)

    min_edge = float(v.get("min_edge", 0.05))
    lookback = float(v.get("fresh_lookback_sec", 8))
    need = min_fresh_move(spot, v)
    signed, age = fresh_move(state, now, spot, need, lookback)
    move = abs(signed)

    # Edge on each side vs the best ask, after the taker fee.
    cands = []
    for side, book, est in (("Up", up, p_up), ("Down", down, 1.0 - p_up)):
        if book is None or book.best_ask is None:
            continue
        ask = float(book.best_ask)
        fps = strategy.fee_per_share(ask)
        edge = float(est) - ask - fps
        cands.append((edge, side, book, ask, fps, float(est)))
    cands.sort(reverse=True, key=lambda x: x[0])

    def log_row(status, reason, side="", ask=None, fps=None, edge=None, est=None,
                shares="", total="", tid="", force=False):
        _maybe_log(
            name, tf, ws, m, now, status, reason, side, est, ask, fps, edge,
            spot, use_ptb, use_src, secs_left, move, age, mid_up,
            latency_ms, fastest_feed, fastest_ms, spread, shares, total, tid,
            force=force, state=state)

    if not cands:
        return D("skip", "no_book", "No best ask on either side.")

    # Prefer a side whose fresh move matches the bet (Up only after a rise, Down only after a fall).
    edged = [c for c in cands if c[0] >= min_edge - 1e-12]
    if not edged:
        edge, side, book, ask, fps, est = cands[0]
        kw = dict(side=side, est_prob=est, ask=ask, ask_size=book.best_ask_size,
                  fee_ps=fps, edge=edge, market_p_up=mid_up)
        return D("skip", "edge_small",
                 f"{side} at {ask*100:.0f}c: model {est:.0%} minus price+fee leaves {edge*100:.1f}c "
                 f"(need {min_edge*100:.0f}c after the fee and a safety buffer).", **kw)

    def directed(side):
        return signed if side == "Up" else -signed

    fresh_cands = [c for c in edged if directed(c[1]) >= need - 1e-12]
    st["opportunities"] = int(st.get("opportunities") or 0) + 1
    if not fresh_cands:
        edge, side, book, ask, fps, est = edged[0]
        kw = dict(side=side, est_prob=est, ask=ask, ask_size=book.best_ask_size,
                  fee_ps=fps, edge=edge, market_p_up=mid_up)
        log_row("stale_edge", "edge_but_move_not_fresh", side, ask, fps, edge, est)
        return D("skip", "not_fresh",
                 f"{side} is {edge*100:.1f}c cheap vs the model ({est:.0%} vs {ask*100:.0f}c + fee), "
                 f"but spot has only moved {_fmt_px(signed)} in the last {lookback:.0f}s "
                 f"(need a { _fmt_px(need)} jump the same way as this bet). Logged as a stale edge, not a trade.", **kw)

    edge, side, book, ask, fps, est = fresh_cands[0]
    kw = dict(side=side, est_prob=est, ask=ask, ask_size=book.best_ask_size,
              fee_ps=fps, edge=edge, market_p_up=mid_up)

    st["fresh_opportunities"] = int(st.get("fresh_opportunities") or 0) + 1

    if not (float(v.get("min_ask", 0)) <= ask <= float(v.get("max_ask", 1))):
        log_row("ask_band", "edge_fresh_but_ask_band", side, ask, fps, edge, est)
        return D("skip", "ask_band",
                 f"Fresh lag on {side} at {ask*100:.0f}c, outside {v.get('min_ask')}-{v.get('max_ask')}.", **kw)

    stake, shares, note = strategy.size_bet(v, edge, ask)
    if shares + 1e-9 < config.MIN_ORDER_SHARES:
        log_row("min_shares", "edge_fresh_but_min_shares", side, ask, fps, edge, est)
        return D("skip", "min_shares",
                 f"${stake:.2f} only buys {shares:.2f} shares of {side} at {ask*100:.0f}c; need 5.",
                 stake=stake, shares=shares, **kw)
    if float(book.best_ask_size or 0) + 1e-9 < shares:
        log_row("thin_book", "edge_fresh_but_thin", side, ask, fps, edge, est, shares=f"{shares:.2f}")
        return D("skip", "thin_book",
                 f"Fresh lag on {side} at {ask*100:.0f}c, but only {book.best_ask_size:.1f} sh offered; need {shares:.2f}.",
                 stake=stake, shares=shares, **kw)
    if stake > config.STAKE_HARD_MAX_USD + 1e-9:
        log_row("hard_max", "edge_fresh_but_cap", side, ask, fps, edge, est)
        return D("skip", "hard_max", "Stake would exceed the $10 hard max.", **kw)

    paused, why = safety.paused(state, name, v, now)
    if paused:
        log_row("kill_switch", "edge_fresh_but_daily_loss", side, ask, fps, edge, est, shares=f"{shares:.2f}")
        return D("skip", "daily_loss", why, stake=stake, shares=shares, **kw)

    fee = shares * fps
    total = shares * ask + fee
    eng = (f"Price lag: spot jumped {_fmt_px(signed)} in {age:.1f}s and the book hasn't caught up. "
           f"I put {side} at {est:.0%} (strike {use_src} {use_ptb:.2f}, spot {spot:.2f}, {secs_left:.0f}s left); "
           f"{side} is still offered at {ask*100:.0f}c + {fps*100:.1f}c fee, edge {edge*100:.1f}c. "
           f"Buying {shares:.2f} sh for ${total:.2f}. Fastest feed {fastest_feed or 'n/a'} "
           f"in {fastest_ms:.0f} ms." if fastest_ms is not None else
           f"Price lag: spot jumped {_fmt_px(signed)} in {age:.1f}s; {side} at {est:.0%} vs {ask*100:.0f}c, "
           f"edge {edge*100:.1f}c. Buying {shares:.2f} sh for ${total:.2f}.")
    if note:
        eng += f" ({note})"
    log_row("filling", "buy", side, ask, fps, edge, est, shares=f"{shares:.2f}", total=f"{total:.4f}", force=True)
    st["filled"] = int(st.get("filled") or 0) + 1
    return D("buy", "ok", eng, stake=stake, shares=shares, notional=shares * ask, fee=fee,
             total_cost=total, extra={"leg": "price_lag", "ptb_src": use_src, "fresh_move": move,
                                      "latency_ms": latency_ms, "fastest_feed": fastest_feed}, **kw)


def on_fill(name: str, tid: str, d: strategy.Decision) -> None:
    """Stamp the last filling row with the paper trade id (best-effort)."""
    # The filling row was already written; a short follow-up row is enough.
    pass


def _maybe_log(name, tf, ws, m, now, status, reason, side, est, ask, fps, edge,
               spot, ptb, ptb_src, secs_left, move, age, mid_up,
               latency_ms, fastest_feed, fastest_ms, spread, shares, total, tid,
               force, state):
    key = f"{name}:{tf}:{ws}:{status}:{side}"
    book = state.setdefault("price_lag_logged", {})
    last = book.get(key) or 0
    if not force and now - last < 15:
        return
    book[key] = now
    if len(book) > 2000:
        # drop old
        cutoff = now - 3600
        state["price_lag_logged"] = {k: t for k, t in book.items() if t >= cutoff}
    row = {
        "ts_pt": store.iso_pt(now),
        "variant": name,
        "timeframe": tf,
        "window_start": ws,
        "slug": getattr(m, "slug", "") if m is not None else "",
        "side": side or "",
        "model_p": f"{est:.4f}" if est is not None else "",
        "ask": f"{ask:.4f}" if ask is not None else "",
        "fee_ps": f"{fps:.6f}" if fps is not None else "",
        "edge": f"{edge:.6f}" if edge is not None else "",
        "spot": f"{spot:.6f}",
        "ptb": f"{ptb:.6f}" if ptb is not None else "",
        "ptb_src": ptb_src or "",
        "secs_left": f"{secs_left:.1f}",
        "fresh_move": f"{move:.6f}",
        "fresh_sec": f"{age:.2f}",
        "book_mid": f"{mid_up:.4f}" if mid_up is not None else "",
        "latency_ms": f"{latency_ms:.0f}" if latency_ms is not None else "",
        "fastest_feed": fastest_feed or "",
        "fastest_ms": f"{fastest_ms:.0f}" if fastest_ms is not None else "",
        "feed_spread": f"{spread:.4f}" if spread else "",
        "status": status,
        "reason": reason,
        "shares": shares or "",
        "total_cost": total or "",
        "trade_id": tid or "",
    }
    _append(row)
    log.info("PRICE_LAG %s [%s] %s %s model=%s ask=%s edge=%s move=$%.2f in %.1fs ptb=%s lat=%sms — %s",
             status, name, tf, side, row["model_p"], row["ask"], row["edge"], move, age, ptb_src,
             row["latency_ms"], reason)


def _append(row: dict) -> None:
    TRADES_CSV.parent.mkdir(parents=True, exist_ok=True)
    fresh = not TRADES_CSV.exists() or TRADES_CSV.stat().st_size == 0
    try:
        with TRADES_CSV.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADES_FIELDS)
            if fresh:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in TRADES_FIELDS})
    except Exception:  # noqa: BLE001
        log.exception("could not write price_lag_trades.csv")
