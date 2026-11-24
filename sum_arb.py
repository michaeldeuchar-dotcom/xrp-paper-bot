"""Sum-of-asks arbitrage paper variant: buy Up and Down when both best asks
plus taker fees cost less than $1/share, locking a guaranteed profit.

Fake money only. No live orders, no wallet.
"""
from __future__ import annotations

import csv
import logging
from typing import Optional

import config
import safety
import store
import strategy

log = logging.getLogger("sum_arb")

OPS_CSV = config.DATA_DIR / "sum_arb_ops.csv"
OPS_FIELDS = [
    "ts_pt", "variant", "timeframe", "window_start", "slug",
    "ask_up", "ask_down", "fee_up", "fee_down", "combined", "buffer",
    "up_size", "down_size", "shares", "locked_ps", "locked_usd",
    "fillable", "status", "reason", "latency_ms", "note",
]


def combined_cost(ask_up: float, ask_down: float) -> float:
    return strategy.cost_per_share(ask_up) + strategy.cost_per_share(ask_down) if hasattr(strategy, "cost_per_share") else (
        float(ask_up) + strategy.fee_per_share(ask_up) + float(ask_down) + strategy.fee_per_share(ask_down)
    )


def locked_ps(ask_up: float, ask_down: float) -> float:
    return 1.0 - combined_cost(ask_up, ask_down)


def _can_fill(book, shares: float) -> bool:
    if book is None or book.best_ask is None:
        return False
    return float(book.best_ask_size or 0) + 1e-9 >= float(shares)


def _size_pair(v: dict, ask_u: float, ask_d: float) -> tuple[float, float, float, str]:
    """Most shares we can buy of both sides under the $10 total cap (and $10 per leg)."""
    cu = float(ask_u) + strategy.fee_per_share(ask_u)
    cd = float(ask_d) + strategy.fee_per_share(ask_d)
    cps = cu + cd
    if cps <= 0:
        return 0.0, 0.0, 0.0, "bad_cost"
    cap = min(float(v.get("max_pair_usd") or config.STAKE_HARD_MAX_USD), config.STAKE_HARD_MAX_USD)
    # each leg also has to stay under the per-bet hard max
    max_by_leg = min(config.STAKE_HARD_MAX_USD / cu, config.STAKE_HARD_MAX_USD / cd)
    shares = min(cap / cps, max_by_leg)
    shares = math_floor_2(shares)
    if shares + 1e-9 < config.MIN_ORDER_SHARES:
        # try the 5-share minimum if it still fits
        need = config.MIN_ORDER_SHARES * cps
        if need <= cap + 1e-9 and config.MIN_ORDER_SHARES * cu <= config.STAKE_HARD_MAX_USD + 1e-9 and config.MIN_ORDER_SHARES * cd <= config.STAKE_HARD_MAX_USD + 1e-9:
            shares = float(config.MIN_ORDER_SHARES)
        else:
            return 0.0, 0.0, 0.0, "below_min_shares"
    total = shares * cps
    return shares, total, cps, ""


def math_floor_2(x: float) -> float:
    return int(x * 100) / 100.0


def decide(name: str, v: dict, *, state: dict, books: dict, secs_left: float, ws: int,
           now: float, tf: str, m, p_up: float, latency_ms: Optional[float] = None) -> strategy.Decision:
    D = lambda action, reason, english, **kw: strategy.Decision(  # noqa: E731
        name, action, reason, english, model_p_up=p_up, **kw)
    up, down = books.get("Up"), books.get("Down")
    ask_u = getattr(up, "best_ask", None) if up is not None else None
    ask_d = getattr(down, "best_ask", None) if down is not None else None
    buf = float(v.get("arb_buffer", 0.02))
    if ask_u is None or ask_d is None:
        return D("skip", "no_book", "Need a best ask on both Up and Down to check a sum-of-asks lock.")
    ask_u, ask_d = float(ask_u), float(ask_d)
    comb = combined_cost(ask_u, ask_d)
    locked = 1.0 - comb
    fee_u, fee_d = strategy.fee_per_share(ask_u), strategy.fee_per_share(ask_d)
    size_u = float(getattr(up, "best_ask_size", 0) or 0)
    size_d = float(getattr(down, "best_ask_size", 0) or 0)
    shares, total, cps, why = _size_pair(v, ask_u, ask_d)
    fillable = bool(shares >= config.MIN_ORDER_SHARES and _can_fill(up, shares) and _can_fill(down, shares))
    # Log any window where the raw combined cost is under $1, even if too tight to trade or too thin.
    if comb < 1.0 - 1e-12:
        status = "opportunity"
        reason = "combined_under_1"
        if comb > 1.0 - buf + 1e-12:
            status, reason = "too_tight", "under_1_but_inside_buffer"
        elif not fillable:
            status, reason = "not_fillable", why or "thin_book"
        _log_op(name, v, tf, ws, m, ask_u, ask_d, fee_u, fee_d, comb, buf,
                size_u, size_d, shares, locked, fillable, status, reason, latency_ms, now)
    if comb > 1.0 - buf + 1e-12:
        return D("skip", "no_arb",
                 f"Up {ask_u*100:.1f}c + Down {ask_d*100:.1f}c + fees = {comb*100:.2f}c/share "
                 f"(need ≤ {(1-buf)*100:.1f}c for a locked profit of at least {buf*100:.0f}c).",
                 ask=ask_u)
    if shares + 1e-9 < config.MIN_ORDER_SHARES:
        return D("skip", "min_shares",
                 f"A both-sides lock is on the book at {comb*100:.2f}c/share, but even 5 shares "
                 f"would cost ${config.MIN_ORDER_SHARES * cps:.2f}, over the cap.",
                 ask=ask_u)
    if not _can_fill(up, shares) or not _can_fill(down, shares):
        return D("skip", "thin_book",
                 f"Lock is {locked*100:.2f}c/share, but the best asks are only {size_u:.1f} Up / {size_d:.1f} Down; "
                 f"need {shares:.2f} of each. Logged as a missed fill.",
                 ask=ask_u, shares=shares)
    paused, why = safety.paused(state, name, v, now)
    if paused:
        return D("skip", "daily_loss", why, ask=ask_u, shares=shares)
    fee = shares * (fee_u + fee_d)
    locked_usd = shares * locked
    eng = (f"Sum-of-asks lock: buying {shares:.2f} sh of Up at {ask_u*100:.1f}c and Down at {ask_d*100:.1f}c "
           f"(combined {comb*100:.2f}c/share including fees, total ${total:.2f}). "
           f"This locks about {locked*100:.2f}c/share (${locked_usd:+.2f}) at settlement, no matter which side wins.")
    leg_up = strategy.Decision(
        name, "buy", "ok", f"Sum-arb Up leg: {shares:.2f} sh @ {ask_u*100:.1f}c.",
        side="Up", ask=ask_u, ask_size=size_u, fee_ps=fee_u, stake=shares * ask_u,
        shares=shares, notional=shares * ask_u, fee=shares * fee_u, total_cost=shares * (ask_u + fee_u),
        model_p_up=p_up, extra={"leg": "Up", "pair": "sum_arb"})
    leg_dn = strategy.Decision(
        name, "buy", "ok", f"Sum-arb Down leg: {shares:.2f} sh @ {ask_d*100:.1f}c.",
        side="Down", ask=ask_d, ask_size=size_d, fee_ps=fee_d, stake=shares * ask_d,
        shares=shares, notional=shares * ask_d, fee=shares * fee_d, total_cost=shares * (ask_d + fee_d),
        model_p_up=p_up, extra={"leg": "Down", "pair": "sum_arb"})
    _log_op(name, v, tf, ws, m, ask_u, ask_d, fee_u, fee_d, comb, buf,
            size_u, size_d, shares, locked, True, "filling", "buy_pair", latency_ms, now)
    return D("buy_pair", "ok", eng, side="Up", ask=ask_u, fee_ps=fee_u + fee_d,
             stake=total, shares=shares, notional=shares * (ask_u + ask_d), fee=fee,
             total_cost=total, edge=locked, extra={"legs": [leg_up, leg_dn], "locked_ps": locked, "combined": comb})


def _log_op(name, v, tf, ws, m, ask_u, ask_d, fee_u, fee_d, comb, buf,
            size_u, size_d, shares, locked, fillable, status, reason, latency_ms, now):
    # Debounce: one row per window+status unless filling.
    key = f"{tf}:{ws}:{status}"
    seen = _log_op._seen  # type: ignore[attr-defined]
    if status != "filling" and key in seen:
        return
    seen.add(key)
    if len(seen) > 4000:
        seen.clear()
    row = {
        "ts_pt": store.iso_pt(now),
        "variant": name,
        "timeframe": tf,
        "window_start": ws,
        "slug": getattr(m, "slug", "") if m is not None else "",
        "ask_up": f"{ask_u:.4f}",
        "ask_down": f"{ask_d:.4f}",
        "fee_up": f"{fee_u:.6f}",
        "fee_down": f"{fee_d:.6f}",
        "combined": f"{comb:.6f}",
        "buffer": f"{buf:.4f}",
        "up_size": f"{size_u:.2f}",
        "down_size": f"{size_d:.2f}",
        "shares": f"{shares:.2f}" if shares else "",
        "locked_ps": f"{locked:.6f}",
        "locked_usd": f"{(shares or 0) * locked:.6f}",
        "fillable": "1" if fillable else "0",
        "status": status,
        "reason": reason,
        "latency_ms": f"{latency_ms:.0f}" if latency_ms is not None else "",
        "note": "Logged even if not fillable" if not fillable else "",
    }
    _append(row)
    log.info("SUM_ARB %s [%s] %s Up=%.2f Down=%.2f combined=%.4f fillable=%s shares=%s — %s",
             status, name, tf, ask_u, ask_d, comb, fillable, shares or "", reason)


_log_op._seen = set()  # type: ignore[attr-defined]


def _append(row: dict) -> None:
    OPS_CSV.parent.mkdir(parents=True, exist_ok=True)
    fresh = not OPS_CSV.exists() or OPS_CSV.stat().st_size == 0
    try:
        with OPS_CSV.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OPS_FIELDS)
            if fresh:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in OPS_FIELDS})
    except Exception:  # noqa: BLE001
        log.exception("could not write sum_arb_ops.csv")
