"""Public XRP spot feeds + candles (Binance.com is geo-blocked from many hosts). All calls have hard deadlines."""
from __future__ import annotations

import logging
import math
import statistics
import time
from datetime import datetime, timezone

import config
import net

log = logging.getLogger("xrpbot")


def _cb_exchange():
    return float(net.hard_get_json("https://api.exchange.coinbase.com/products/XRP-USD/ticker", 5)["price"])


def _cb_retail():
    return float(net.hard_get_json("https://api.coinbase.com/v2/prices/XRP-USD/spot", 5)["data"]["amount"])


def _kraken():
    d = net.hard_get_json("https://api.kraken.com/0/public/Ticker?pair=XRPUSD", 5)
    if d.get("error"):
        raise RuntimeError(d["error"])
    return float(next(iter(d["result"].values()))["c"][0])


def _bitstamp():
    return float(net.hard_get_json("https://www.bitstamp.net/api/v2/ticker/xrpusd", 5)["last"])


FEEDS = {"coinbase_exchange": _cb_exchange, "coinbase": _cb_retail, "kraken": _kraken, "bitstamp": _bitstamp}


def current_spot() -> tuple[float, dict]:
    ok, err = net.parallel(FEEDS, deadline=6)
    if not ok:
        raise RuntimeError(f"all spot feeds failed: {err}")
    return float(statistics.median(ok.values())), ok


def coinbase_candles(start_ts: int, end_ts: int, granularity: int = 60) -> list:
    f = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    url = (f"https://api.exchange.coinbase.com/products/{config.COINBASE_PRODUCT}/candles?granularity={granularity}"
           f"&start={f(start_ts)}&end={f(end_ts)}")
    data = net.hard_get_json(url, 8)
    if not isinstance(data, list):
        raise RuntimeError(f"bad candles payload {str(data)[:100]}")
    return sorted(data, key=lambda r: r[0])


def candle_ptb(window_start: int) -> tuple[float, str]:
    rows = coinbase_candles(window_start - 60, window_start + 1)  # 1-minute candle ending at the window start
    for r in rows:
        if int(r[0]) == window_start - 60:
            return (float(r[1]) + float(r[2]) + float(r[4])) / 3.0, f"coinbase_1m_typical t={int(r[0])}"
    raise RuntimeError("no candle for price-to-beat")


_vol_cache = {"ts": 0.0, "v": config.DEFAULT_ANNUAL_VOL}


def realized_annual_vol() -> float:
    now = time.time()
    if now - _vol_cache["ts"] < 120:
        return _vol_cache["v"]
    _vol_cache["ts"] = now  # even on failure, don't retry every cycle
    try:
        rows = coinbase_candles(int(now) - config.VOL_LOOKBACK_MINUTES * 60 - 60, int(now))  # <=300 candles
        closes = [float(r[4]) for r in rows if float(r[4]) > 0]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        if len(rets) >= 30:
            v = statistics.pstdev(rets) * math.sqrt(365.25 * 24 * 60)
            _vol_cache["v"] = min(max(v, config.VOL_MIN), config.VOL_MAX)
    except Exception as exc:  # noqa: BLE001
        log.warning("vol candles failed (%s); keeping %.2f", exc, _vol_cache["v"])
    return _vol_cache["v"]


# ---------------------------------------------------------------- trend / structure / volume (trend_volume variant)
_trend_cache: dict = {}


def _chunk_hl(rows, n_chunks):
    size = len(rows) // n_chunks
    out = []
    for i in range(n_chunks):
        part = rows[len(rows) - (n_chunks - i) * size: len(rows) - (n_chunks - i - 1) * size]
        out.append((max(float(r[2]) for r in part), min(float(r[1]) for r in part)))
    return out


def compute_trend(rows: list, lookback: int = 60, n_chunks: int = 4, candle_sec: int = 60) -> dict:
    """Pure function on Coinbase 1m candles [t, low, high, open, close, volume] (oldest first).

    Returns momentum, market structure (higher highs/lows vs lower highs/lows) and
    volume confirmation over the last `lookback` minutes. No network calls here.
    """
    rows = [r for r in rows if float(r[4]) > 0][-(lookback + 1):]
    if len(rows) < lookback * 0.8:
        return {"ok": False, "why": f"only {len(rows)} candles"}
    closes = [float(r[4]) for r in rows]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    sd1 = statistics.pstdev(rets) or 1e-9
    body = rows[1:]
    ret60 = math.log(closes[-1] / closes[0])
    ret15 = math.log(closes[-1] / closes[-16]) if len(closes) > 16 else 0.0
    mom_z = ret60 / (sd1 * math.sqrt(len(rets)))
    mom15_z = ret15 / (sd1 * math.sqrt(15))
    path = sum(abs(x) for x in rets) or 1e-9
    efficiency = abs(ret60) / path                    # 1 = straight line, ~0.1 = random chop
    ann_vol = sd1 * math.sqrt(365.25 * 24 * 3600 / candle_sec)
    hl = _chunk_hl(body, n_chunks)
    hh = sum(1 for i in range(1, n_chunks) if hl[i][0] > hl[i - 1][0])
    hlow = sum(1 for i in range(1, n_chunks) if hl[i][1] > hl[i - 1][1])
    lh = sum(1 for i in range(1, n_chunks) if hl[i][0] < hl[i - 1][0])
    ll = sum(1 for i in range(1, n_chunks) if hl[i][1] < hl[i - 1][1])
    need = n_chunks - 2  # e.g. 2 of 3 transitions
    if hh >= need and hlow >= need and hl[-1][0] > hl[0][0] and hl[-1][1] > hl[0][1]:
        structure = "up"
    elif lh >= need and ll >= need and hl[-1][0] < hl[0][0] and hl[-1][1] < hl[0][1]:
        structure = "down"
    else:
        structure = "mixed"
    up_vol = [float(r[5]) for r in body if float(r[4]) > float(r[3])]
    dn_vol = [float(r[5]) for r in body if float(r[4]) < float(r[3])]
    avg_all = statistics.mean(float(r[5]) for r in body)
    avg_up = statistics.mean(up_vol) if up_vol else 0.0
    avg_dn = statistics.mean(dn_vol) if dn_vol else 0.0
    # spike read (trend_spike): last 5 finished minutes vs the hour's average 5-minute volume
    last5 = body[-5:]
    vol5 = sum(float(r[5]) for r in last5)
    spike_ratio = vol5 / (avg_all * 5) if avg_all > 0 else 0.0
    up5 = sum(1 for r in last5 if float(r[4]) > float(r[3]))
    dn5 = sum(1 for r in last5 if float(r[4]) < float(r[3]))
    ret5 = math.log(closes[-1] / closes[-6]) if len(closes) > 6 else 0.0
    # breakout read: did one of the last 2 finished minutes close beyond the prior 30-min range on heavy volume?
    prior, last2 = body[-32:-2], body[-2:]
    range_hi = max(float(r[2]) for r in prior)
    range_lo = min(float(r[1]) for r in prior)
    avg_vol_prior = statistics.mean(float(r[5]) for r in prior) or 1e-9
    bo_up = [float(r[5]) / avg_vol_prior for r in last2 if float(r[4]) > range_hi]
    bo_dn = [float(r[5]) / avg_vol_prior for r in last2 if float(r[4]) < range_lo]
    span_min = lookback * candle_sec / 60.0
    lbl = lambda m: (f"{m / 60:g}h" if m >= 120 else f"{m:g}m")  # noqa: E731
    return {"ok": True, "span_label": lbl(span_min), "mid_label": lbl(15 * candle_sec / 60.0), "short_label": lbl(5 * candle_sec / 60.0),
            "spike_ratio": spike_ratio, "up5": up5, "dn5": dn5, "ret5_pct": ret5 * 100,
            "struct_up_steps": min(hh, hlow), "struct_dn_steps": min(lh, ll), "range_hi": range_hi, "range_lo": range_lo, "bo_up_vol": max(bo_up, default=0.0),
            "bo_dn_vol": max(bo_dn, default=0.0), "ret60_pct": ret60 * 100, "ret15_pct": ret15 * 100, "mom_z": mom_z, "mom15_z": mom15_z,
            "efficiency": efficiency, "ann_vol": ann_vol, "structure": structure,
            "hh": hh, "hl": hlow, "lh": lh, "ll": ll, "avg_vol": avg_all,
            "up_vol_ratio": (avg_up / avg_dn) if avg_dn > 0 else 9.99,
            "dn_vol_ratio": (avg_dn / avg_up) if avg_up > 0 else 9.99,
            "n": len(rows), "last_close": closes[-1]}


def trend_signals(tf: str = "5m", max_age: float = 30.0) -> dict:
    """Cached (30s) trend read for a timeframe family. "5m": last hour of 1-minute candles. "1h": last 4 hours of
    5-minute candles. Never raises."""
    now = time.time()
    c = _trend_cache.get(tf)
    if c is not None and now - c["ts"] < max_age:
        return c["sig"]
    cfg = config.TIMEFRAMES[tf]
    csec, look = int(cfg["candle_sec"]), int(cfg["candle_lookback"])
    try:
        rows = coinbase_candles(int(now) - (look + 6) * csec, int(now), csec)
        rows = [r for r in rows if int(r[0]) + csec <= now]   # drop the still-forming candle
        sig = compute_trend(rows, lookback=look, candle_sec=csec)
    except Exception as exc:  # noqa: BLE001
        log.warning("trend candles failed (%s)", exc)
        sig = {"ok": False, "why": f"candles failed: {str(exc)[:80]}"}
    _trend_cache[tf] = {"ts": now, "sig": sig}
    return sig


def trend_summary(sig: dict) -> str:
    if not sig or not sig.get("ok"):
        return f"trend n/a ({(sig or {}).get('why', 'none')})"
    sp, mid, sh = sig["span_label"], sig["mid_label"], sig["short_label"]
    return (f"{sp} {sig['ret60_pct']:+.2f}% (z {sig['mom_z']:+.1f}), {mid} {sig['ret15_pct']:+.2f}% (z {sig['mom15_z']:+.1f}), "
            f"structure {sig['structure']} (HH{sig['hh']} HL{sig['hl']} LH{sig['lh']} LL{sig['ll']}), "
            f"{sh} {sig['ret5_pct']:+.2f}%, spike {sig['spike_ratio']:.1f}x ({sig['up5']}up/{sig['dn5']}dn of last 5 candles), "
            f"vol up/down {sig['up_vol_ratio']:.2f}x, chop-eff {sig['efficiency']:.2f}, ann vol {sig['ann_vol']:.0%}, "
            f"range {sig['range_lo']:.{config.PRICE_DP}f}-{sig['range_hi']:.{config.PRICE_DP}f} breakout vol up {sig['bo_up_vol']:.1f}x / down {sig['bo_dn_vol']:.1f}x")
