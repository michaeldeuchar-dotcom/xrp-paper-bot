"""A/B hedge paper variants: buy the leading side (A), then optionally buy the
other side (B) if that locks a small guaranteed profit, or caps a likely loss.

Fake money only. No live orders, no wallet.
"""
from __future__ import annotations

import csv
import logging
import time
from typing import Optional

import config
import polymarket
import store
import strategy

log = logging.getLogger("ab_hedge")

PAIR_CSV = config.DATA_DIR / "ab_hedge_pairs.csv"
PAIR_FIELDS = [
    "ts_pt", "variant", "timeframe", "window_start", "slug",
    "a_side", "a_ask", "a_cost_ps", "a_shares", "a_total_cost", "a_trade_id",
    "b_side", "b_ask", "b_cost_ps", "b_total_cost", "b_trade_id", "b_type",
    "status", "combined_cost_ps", "locked_pnl_ps", "pnl_usd", "outcome", "note",
]


def cost_per_share(ask: float) -> float:
    """Taker cost of one share at this ask, including the fee."""
    a = float(ask)
    return a + strategy.fee_per_share(a)


def combined_cost_ps(ask_a: float, ask_b: float) -> float:
    return cost_per_share(ask_a) + cost_per_share(ask_b)


def locked_pnl_ps(ask_a: float, ask_b: float) -> float:
    """P&L per share at settlement if we hold the same number of both sides."""
    return 1.0 - combined_cost_ps(ask_a, ask_b)


def lock_profit_ok(ask_a: float, ask_b: float, min_margin: float = 0.02) -> bool:
    return locked_pnl_ps(ask_a, ask_b) >= float(min_margin) - 1e-12


def pair_key(tf: str, ws: int) -> str:
    return f"{tf}:{int(ws)}"


def _book(state: dict) -> dict:
    return state.setdefault("ab_hedge", {})


def get_pair(state: dict, name: str, tf: str, ws: int) -> Optional[dict]:
    return ((_book(state).get(name) or {}).get(pair_key(tf, ws)))


def has_open_a(state: dict, name: str, tf: str, ws: int) -> bool:
    p = get_pair(state, name, tf, ws)
    return bool(p and p.get("a_trade_id") and not p.get("b_trade_id"))


def _noise_usd(ptb: float) -> float:
    if hasattr(config, "BASIS_NOISE_USD"):
        return float(config.BASIS_NOISE_USD)
    if hasattr(config, "BASIS_NOISE_PCT") and ptb:
        return float(config.BASIS_NOISE_PCT) * float(ptb)
    return 0.0


def lead_side(spot, ptb, v: dict) -> Optional[str]:
    """The side the coin already leads, or None if the lead is too small to trust."""
    if spot is None or ptb is None:
        return None
    gap = float(spot) - float(ptb)
    need = max(_noise_usd(float(ptb)), float(v.get("min_lead_usd") or 0))
    if abs(gap) < need + 1e-12:
        return None
    return "Up" if gap > 0 else "Down"


def wanted(state: dict, name: str, v: dict, tf: str, ws: int, secs_left: float, wsec: float) -> bool:
    """Whether this variant should run on this pass (A entry window, or watching for B)."""
    p = get_pair(state, name, tf, ws)
    if p and p.get("b_trade_id"):
        return False
    if p and p.get("a_trade_id"):
        return secs_left >= float(v.get("b_min_secs_left", 8)) - 1e-9
    return float(v.get("min_secs_left", 0)) - 3 <= secs_left <= float(v.get("max_secs_left", wsec)) + 3


def _size_a(v: dict, ask: float) -> tuple[float, float, float, float, str]:
    """Size leg A at about $5 including the fee. Returns stake, shares, fee, total, note."""
    st, sh, note = strategy.size_bet(v, 0.0, ask)
    fps = strategy.fee_per_share(ask)
    notional = sh * ask
    fee = sh * fps
    total = notional + fee
    return st, sh, fee, total, note


def _can_fill(book, shares: float) -> bool:
    """Require the best ask itself to be large enough. We do not walk into worse prices."""
    if book is None or book.best_ask is None:
        return False
    return float(book.best_ask_size or 0) + 1e-9 >= float(shares)


def _a_badly_losing(v: dict, pair: dict, books: dict, spot, ptb, now: float, wsec: float, secs_left: float) -> tuple[bool, str]:
    """True if we should consider a damage-limit B buy."""
    hold = float(v.get("limit_loss_min_hold_sec", 20))
    if now - float(pair.get("a_ts") or 0) < hold - 1e-9:
        return False, "too soon after A to cap a loss"
    elapsed = wsec - secs_left
    if elapsed < 0.5 * wsec:
        return False, "still in the first half of the window"
    side = pair["a_side"]
    other = "Down" if side == "Up" else "Up"
    lead = lead_side(spot, ptb, v)
    if lead == side:
        return False, "price still leads A's way"
    book_a = books.get(side)
    bid = getattr(book_a, "best_bid", None) if book_a is not None else None
    ask_a = float(pair["a_ask"])
    dd = float(v.get("limit_loss_drawdown", 0.40))
    if bid is None or ask_a <= 0 or float(bid) > ask_a * (1.0 - dd) + 1e-12:
        # also allow if the other side is now a heavy favorite
        book_b = books.get(other)
        mid_b = strategy._mid(book_b) if book_b is not None else None
        if mid_b is None or mid_b < 0.70:
            return False, "A is not down enough to cap the loss"
    if lead is None and (bid is None or ask_a <= 0 or float(bid) > ask_a * (1.0 - dd) + 1e-12):
        return False, "lead has not clearly flipped against A"
    return True, "A is badly losing"


def decide(name: str, v: dict, *, state: dict, books: dict, secs_left: float, spot, ptb,
           ws: int, now: float, tf: str, m, p_up: float) -> strategy.Decision:
    D = lambda action, reason, english, **kw: strategy.Decision(  # noqa: E731
        name, action, reason, english, model_p_up=p_up, **kw)
    wsec = float(v.get("window_seconds") or polymarket.window_seconds(tf))
    pair = get_pair(state, name, tf, ws)

    if pair and pair.get("a_trade_id") and not pair.get("b_trade_id"):
        return _decide_b(name, v, pair=pair, books=books, secs_left=secs_left,
                         spot=spot, ptb=ptb, now=now, wsec=wsec, p_up=p_up, D=D)

    # -------- leg A: early-ish, ride the current price lead --------
    side = lead_side(spot, ptb, v)
    if side is None:
        gap = None if spot is None or ptb is None else float(spot) - float(ptb)
        have = "unknown" if gap is None else f"${gap:+.2f}"
        need = _noise_usd(float(ptb or 0))
        return D("skip", "no_lead",
                 f"No clear lead yet (gap {have}; need about ${need:.2f} past the price to beat).")
    book = books.get(side)
    if book is None or book.best_ask is None:
        return D("skip", "no_ask", f"Nobody is selling {side} right now.", side=side)
    ask = float(book.best_ask)
    if not (float(v.get("min_ask", 0)) <= ask <= float(v.get("max_ask", 1))):
        return D("skip", "ask_band",
                 f"{side} costs {ask*100:.0f}c, outside this variant's "
                 f"{v.get('min_ask')}-{v.get('max_ask')} price range.", side=side, ask=ask)
    st, sh, fee, total, note = _size_a(v, ask)
    if sh + 1e-9 < config.MIN_ORDER_SHARES:
        return D("skip", "min_shares",
                 f"${st:.2f} only buys {sh:.2f} shares of {side} at {ask*100:.0f}c; "
                 f"Polymarket's minimum is 5.", side=side, ask=ask, stake=st, shares=sh)
    if total > config.STAKE_HARD_MAX_USD + 1e-9:
        return D("skip", "hard_max", "Stake would exceed the $10 hard max.", side=side, ask=ask)
    if not _can_fill(book, sh):
        return D("skip", "thin_book",
                 f"Only {book.best_ask_size:.1f} shares offered at {ask*100:.0f}c; need {sh:.2f}.",
                 side=side, ask=ask, stake=st, shares=sh, ask_size=book.best_ask_size)
    fps = strategy.fee_per_share(ask)
    eng = (f"Leg A: the coin leads {side} (spot vs price to beat), so buying "
           f"${st:.2f} of {side} at {ask*100:.0f}c ({sh:.2f} sh, fee ${fee:.3f}, {secs_left:.0f}s left). "
           f"Will watch the other side for a cheap hedge.")
    if note:
        eng += f" ({note})"
    return D("buy", "ok", eng, side=side, ask=ask, ask_size=book.best_ask_size, fee_ps=fps,
             stake=st, shares=sh, notional=sh * ask, fee=fee, total_cost=total,
             extra={"leg": "A", "note": note})


def _decide_b(name, v, *, pair, books, secs_left, spot, ptb, now, wsec, p_up, D):
    other = "Down" if pair["a_side"] == "Up" else "Up"
    shares = float(pair["a_shares"])
    ask_a = float(pair["a_ask"])
    book = books.get(other)
    if not _can_fill(book, shares):
        size = getattr(book, "best_ask_size", 0) if book is not None else 0
        ask = getattr(book, "best_ask", None) if book is not None else None
        why = "Nobody is selling the other side right now." if ask is None else (
            f"Other side ({other}) only has {size:.1f} shares at {float(ask)*100:.0f}c; need {shares:.2f} to match A.")
        return D("skip", "thin_book", why, side=other, ask=ask, shares=shares)
    ask_b = float(book.best_ask)
    fps = strategy.fee_per_share(ask_b)
    fee = shares * fps
    notional = shares * ask_b
    total = notional + fee
    if total > config.STAKE_HARD_MAX_USD + 1e-9:
        return D("skip", "hard_max",
                 f"Leg B would cost ${total:.2f} (same {shares:.2f} shares as A), over the $10 cap.",
                 side=other, ask=ask_b, shares=shares)
    margin = float(v.get("lock_margin_ps", 0.02))
    locked = locked_pnl_ps(ask_a, ask_b)
    comb = combined_cost_ps(ask_a, ask_b)
    if lock_profit_ok(ask_a, ask_b, margin):
        eng = (f"Leg B lock_profit: buying {shares:.2f} sh of {other} at {ask_b*100:.0f}c "
               f"(fee ${fee:.3f}). Combined cost {comb*100:.1f}c/share, locks about "
               f"{locked*100:.1f}c/share (${locked * shares:.2f} total) at settlement.")
        return D("buy", "ok", eng, side=other, ask=ask_b, ask_size=book.best_ask_size, fee_ps=fps,
                 stake=notional, shares=shares, notional=notional, fee=fee, total_cost=total,
                 extra={"leg": "B", "b_type": "lock_profit", "combined_cost_ps": comb, "locked_pnl_ps": locked})
    # optional damage-limit hedge
    ok, why = _a_badly_losing(v, pair, books, spot, ptb, now, wsec, secs_left)
    cap = float(v.get("limit_loss_ps", 0.10))
    if ok and (-locked) <= cap + 1e-12:
        eng = (f"Leg B limit_loss: A is badly losing, so buying {shares:.2f} sh of {other} at "
               f"{ask_b*100:.0f}c (fee ${fee:.3f}) to cap the loss at about {(-locked)*100:.1f}c/share "
               f"(${-locked * shares:.2f} total) instead of risking all of A's ${float(pair['a_total_cost']):.2f}.")
        return D("buy", "ok", eng, side=other, ask=ask_b, ask_size=book.best_ask_size, fee_ps=fps,
                 stake=notional, shares=shares, notional=notional, fee=fee, total_cost=total,
                 extra={"leg": "B", "b_type": "limit_loss", "combined_cost_ps": comb, "locked_pnl_ps": locked})
    if ok:
        return D("skip", "limit_too_wide",
                 f"A is badly losing, but hedging {other} at {ask_b*100:.0f}c would lock a "
                 f"{(-locked)*100:.1f}c/share loss (cap is {cap*100:.0f}c). Waiting.",
                 side=other, ask=ask_b, shares=shares)
    return D("skip", "no_hedge",
             f"Watching {other} at {ask_b*100:.0f}c: combined cost {comb*100:.1f}c/share "
             f"(need ≤ {(1 - margin)*100:.1f}c to lock a ≥{margin*100:.0f}c profit). {why}.",
             side=other, ask=ask_b, shares=shares)


def on_fill(state: dict, name: str, tid: str, d: strategy.Decision, *, tf: str, ws: int, slug: str) -> None:
    extra = d.extra or {}
    leg = extra.get("leg") or "A"
    key = pair_key(tf, ws)
    bucket = _book(state).setdefault(name, {})
    if leg == "A":
        bucket[key] = {
            "tf": tf, "window_start": int(ws), "slug": slug,
            "a_side": d.side, "a_ask": d.ask, "a_cost_ps": cost_per_share(d.ask),
            "a_shares": d.shares, "a_total_cost": d.total_cost, "a_trade_id": tid,
            "a_ts": time.time(),
            "b_side": None, "b_ask": None, "b_cost_ps": None, "b_total_cost": None,
            "b_trade_id": None, "b_type": None, "logged": False,
        }
        log.info("AB_HEDGE A opened [%s] %s %s %s @ %.2f (%.2f sh, $%.2f)",
                 name, tid, key, d.side, d.ask, d.shares, d.total_cost)
        return
    p = bucket.get(key)
    if not p:
        log.warning("AB_HEDGE B fill [%s] %s has no matching A pair %s", name, tid, key)
        return
    p.update({
        "b_side": d.side, "b_ask": d.ask, "b_cost_ps": cost_per_share(d.ask),
        "b_total_cost": d.total_cost, "b_trade_id": tid,
        "b_type": extra.get("b_type") or "lock_profit",
        "combined_cost_ps": extra.get("combined_cost_ps"),
        "locked_pnl_ps": extra.get("locked_pnl_ps"),
    })
    log.info("AB_HEDGE B %s [%s] %s %s %s @ %.2f (%.2f sh, $%.2f); combined=%.3f lock_ps=%s",
             p.get("b_type"), name, tid, key, d.side, d.ask, d.shares, d.total_cost,
             p.get("combined_cost_ps") or 0, p.get("locked_pnl_ps"))


def after_resolve(state: dict) -> None:
    """Write a pairs-CSV row once every leg of a window has settled."""
    open_ids = {(it["variant"], it["trade_id"]) for it in state.get("open", [])}
    book = _book(state)
    for name, pairs in list(book.items()):
        if not isinstance(pairs, dict):
            continue
        for key, p in list(pairs.items()):
            if not isinstance(p, dict) or p.get("logged") or not p.get("a_trade_id"):
                continue
            if (name, p["a_trade_id"]) in open_ids:
                continue
            if p.get("b_trade_id") and (name, p["b_trade_id"]) in open_ids:
                continue
            _log_resolved_pair(name, p)
            p["logged"] = True
        # drop logged pairs older than ~2 days of keys to keep state small
        if len(pairs) > 80:
            keep = {}
            for k, v in pairs.items():
                if not (isinstance(v, dict) and v.get("logged")):
                    keep[k] = v
                    continue
                # keep recent logged ones only
                keep[k] = v
            # keep last 40 logged + all open
            logged = [k for k, v in keep.items() if isinstance(v, dict) and v.get("logged")]
            for k in logged[:-40]:
                keep.pop(k, None)
            book[name] = keep


def _log_resolved_pair(name: str, p: dict) -> None:
    rows = {r.get("trade_id"): r for r in store.read_trades(name)}
    a = rows.get(p["a_trade_id"]) or {}
    b = rows.get(p.get("b_trade_id") or "") or {}
    pnl = 0.0
    for r in (a, b):
        if r.get("pnl_usd"):
            try:
                pnl += float(r["pnl_usd"])
            except (TypeError, ValueError):
                pass
    outcome = a.get("outcome") or ""
    if b:
        bo = b.get("outcome") or ""
        if outcome and bo and outcome != bo:
            outcome = f"{outcome}/{bo}"
        elif bo and not outcome:
            outcome = bo
    b_type = p.get("b_type") or ""
    status = b_type if b_type else "a_only"
    comb = p.get("combined_cost_ps")
    if comb is None and p.get("b_ask") is not None:
        comb = combined_cost_ps(float(p["a_ask"]), float(p["b_ask"]))
        p["combined_cost_ps"] = comb
        p["locked_pnl_ps"] = 1.0 - comb
    note = "A rode to settlement alone" if status == "a_only" else (
        "Both sides held to settlement; one pays $1/share" if p.get("b_trade_id") else "")
    row = {
        "ts_pt": store.iso_pt(),
        "variant": name,
        "timeframe": p.get("tf") or "",
        "window_start": p.get("window_start") or "",
        "slug": p.get("slug") or a.get("slug") or "",
        "a_side": p.get("a_side") or a.get("side") or "",
        "a_ask": f"{float(p['a_ask']):.4f}" if p.get("a_ask") is not None else "",
        "a_cost_ps": f"{float(p['a_cost_ps']):.6f}" if p.get("a_cost_ps") is not None else "",
        "a_shares": f"{float(p['a_shares']):.2f}" if p.get("a_shares") is not None else "",
        "a_total_cost": f"{float(p['a_total_cost']):.6f}" if p.get("a_total_cost") is not None else "",
        "a_trade_id": p.get("a_trade_id") or "",
        "b_side": p.get("b_side") or "",
        "b_ask": f"{float(p['b_ask']):.4f}" if p.get("b_ask") is not None else "",
        "b_cost_ps": f"{float(p['b_cost_ps']):.6f}" if p.get("b_cost_ps") is not None else "",
        "b_total_cost": f"{float(p['b_total_cost']):.6f}" if p.get("b_total_cost") is not None else "",
        "b_trade_id": p.get("b_trade_id") or "",
        "b_type": b_type,
        "status": status,
        "combined_cost_ps": f"{float(comb):.6f}" if comb is not None else "",
        "locked_pnl_ps": f"{float(p['locked_pnl_ps']):.6f}" if p.get("locked_pnl_ps") is not None else "",
        "pnl_usd": f"{pnl:.6f}",
        "outcome": outcome,
        "note": note,
    }
    _append_pair(row)
    log.info("AB_HEDGE PAIR [%s] %s A=%s@%s B=%s@%s type=%s pnl=$%+.2f",
             name, p.get("slug") or p.get("window_start"), p.get("a_side"), p.get("a_ask"),
             p.get("b_side") or "none", p.get("b_ask") or "", status, pnl)


def _append_pair(row: dict) -> None:
    PAIR_CSV.parent.mkdir(parents=True, exist_ok=True)
    fresh = not PAIR_CSV.exists()
    try:
        with PAIR_CSV.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=PAIR_FIELDS)
            if fresh:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in PAIR_FIELDS})
    except Exception:  # noqa: BLE001
        log.exception("could not write ab_hedge_pairs.csv")
