"""max_profit: a fake-money account that tries to grow as fast as it can, with one hard rule:
it must not lose all of the money.

Starts at $100 (documented in the tweak note). Sizes with fractional Kelly, caps any single
bet at a fraction of the current bankroll, and pauses at a cash floor. May exceed the usual
$10-per-bet cap because this is paper only — do not use this as-is with real money.
"""
from __future__ import annotations

import csv
import logging
import math
import time
from typing import Callable, Optional

import config
import store
import strategy

log = logging.getLogger("maxprofit")
HIST = config.DATA_DIR / "max_profit_history.csv"
STATE_KEY = "max_profit"


def kelly_fraction(q: float, ask: float, fee_ps: float, frac: float = 0.5) -> float:
    """Half-Kelly (or `frac`-Kelly) share of bankroll to bet, given estimated win prob q and ask."""
    try:
        q = float(q)
        c = float(ask) + float(fee_ps)
    except (TypeError, ValueError):
        return 0.0
    if q <= 0 or q >= 1 or c <= 0 or c >= 1:
        return 0.0
    b = (1.0 - c) / c  # net odds if we win $1 face
    if b <= 0:
        return 0.0
    full = q - (1.0 - q) / b
    return max(0.0, min(1.0, float(frac) * full))


def size_for_budget(ask: float, budget: float, ask_size: float = 1e9) -> Optional[tuple[float, float, float, float]]:
    """Return (shares, notional, fee, total_cost) fitting under budget, or None."""
    fee_ps = strategy.fee_per_share(ask)
    cps = ask + fee_ps
    if cps <= 0 or budget + 1e-12 < config.MIN_ORDER_SHARES * cps:
        return None
    shares = math.floor(min(budget / cps, float(ask_size or 1e9)) * 100) / 100
    if shares + 1e-9 < config.MIN_ORDER_SHARES:
        return None
    notional = shares * ask
    fee = shares * fee_ps
    return shares, notional, fee, notional + fee


def ensure(state: dict, v: dict, name: str) -> dict:
    """Create the $100 fake account once. Never resets an existing balance."""
    start = float(v.get("start_balance", 100))
    floor = float(v.get("floor_balance", 12))
    b = state.setdefault(STATE_KEY, {})
    if not b.get("funded"):
        now = time.time()
        b.update({
            "variant": name, "start": start, "floor": floor, "balance": round(start, 4),
            "equity": round(start, 4), "paused": False, "paused_at": None,
            "n_bets": 0, "n_settled": 0, "funded": True, "funded_at": store.iso_pt(now),
            "last_signal": "",
        })
        _log("fund", balance=start, equity=start, note=f"started with ${start:.0f} fake money, floor ${floor:.0f}")
        log.info("MAX PROFIT funded $%.0f fake (floor $%.0f) — paper only, may exceed the usual $10 cap", start, floor)
    b.setdefault("variant", name)
    b.setdefault("floor", floor)
    b.setdefault("start", start)
    return b


def recompute(state: dict, name: str) -> dict:
    b = state.get(STATE_KEY) or ensure(state, {}, name)
    start = float(b.get("start", 100))
    settled_pnl = open_cost = 0.0
    n = n_settled = 0
    resolved_ids = set()
    for r in store.read_trades(name):
        n += 1
        if r.get("status") == "resolved" and r.get("pnl_usd") not in (None, ""):
            settled_pnl += float(r["pnl_usd"])
            n_settled += 1
            if r.get("trade_id"):
                resolved_ids.add(r["trade_id"])
        elif r.get("status") == "open":
            open_cost += float(r.get("total_cost_usd") or 0)
    # ignore opens that were already resolved in the CSV but still listed in state["open"]
    for item in state.get("open") or []:
        if item.get("variant") != name:
            continue
        if item.get("trade_id") in resolved_ids:
            continue
        # already counted if status=open in CSV; if only in state, add
    # Prefer CSV open + settled. Also count state opens not yet in a stale read:
    csv_open_ids = {r.get("trade_id") for r in store.read_trades(name) if r.get("status") == "open"}
    for item in state.get("open") or []:
        if item.get("variant") == name and item.get("trade_id") not in csv_open_ids and item.get("trade_id") not in resolved_ids:
            open_cost += float(item.get("total_cost") or 0)

    cash = start + settled_pnl - open_cost
    equity = start + settled_pnl  # mark-to-cost: open bets still valued at what we paid
    # if we already subtracted open from cash, equity should include open cost (capital in play)
    equity = cash + open_cost
    b["balance"] = round(cash, 4)
    b["equity"] = round(equity, 4)
    b["n_bets"] = n
    b["n_settled"] = n_settled
    floor = float(b.get("floor", 12))
    if cash + 1e-9 < floor:
        if not b.get("paused"):
            b["paused"] = True
            b["paused_at"] = store.iso_pt()
            _log("pause", balance=cash, equity=equity, note=f"paused: cash ${cash:.2f} is below the ${floor:.0f} floor")
            log.info("MAX PROFIT paused: cash $%.2f below $%.0f floor", cash, floor)
    # do not auto-unpause if it later rises (only rises on settlement); unpause if cash >= floor
    elif b.get("paused") and cash >= floor - 1e-9:
        b["paused"] = False
        b["paused_at"] = None
        _log("resume", balance=cash, equity=equity, note=f"cash ${cash:.2f} is back at or above the ${floor:.0f} floor")
    return b


def sync(state: dict) -> None:
    b = state.get(STATE_KEY)
    if not b or not b.get("variant"):
        return
    prev = (b.get("balance"), b.get("equity"), b.get("paused"))
    recompute(state, b["variant"])
    if (b.get("balance"), b.get("equity"), b.get("paused")) != prev:
        # settlement or fill already logs explicitly; this catches cash-out settles
        pass


def _log(event: str, *, balance: float, equity: float, delta: float = 0.0,
         trade_id: str = "", note: str = "", now: float | None = None) -> None:
    HIST.parent.mkdir(parents=True, exist_ok=True)
    fresh = not HIST.exists() or HIST.stat().st_size == 0
    try:
        with HIST.open("a", newline="") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(["ts_pt", "event", "balance", "equity", "delta", "trade_id", "note"])
            w.writerow([store.iso_pt(now), event, f"{balance:.4f}", f"{equity:.4f}", f"{delta:.4f}", trade_id, note])
    except Exception:  # noqa: BLE001
        log.exception("could not write max_profit_history.csv")


def on_fill(state: dict, name: str, trade_id: str, total_cost: float, note: str = "") -> None:
    b = recompute(state, name)
    _log("fill", balance=b["balance"], equity=b["equity"], delta=-float(total_cost),
         trade_id=trade_id, note=note or f"placed bet costing ${float(total_cost):.2f}")
    log.info("MAX PROFIT FILL %s cost=$%.2f cash=$%.2f equity=$%.2f", trade_id, total_cost, b["balance"], b["equity"])


def on_resolve(state: dict, name: str, trade_id: str, pnl: float, payout: float) -> None:
    b = recompute(state, name)
    _log("settle", balance=b["balance"], equity=b["equity"], delta=float(payout),
         trade_id=trade_id, note=f"settled pnl ${pnl:+.2f}, payout ${payout:.2f}")
    log.info("MAX PROFIT SETTLE %s pnl=$%+.2f cash=$%.2f equity=$%.2f", trade_id, pnl, b["balance"], b["equity"])


def has_rest(state: dict, tf: str, ws: int) -> bool:
    r = (state.get(STATE_KEY) or {}).get("rest") or {}
    return bool(r) and r.get("tf") == tf and int(r.get("ws") or 0) == int(ws)


def place_rest(state: dict, name: str, d: strategy.Decision, *, tf: str, ws: int, now: float, m) -> None:
    b = state.setdefault(STATE_KEY, {})
    b["rest"] = {
        "variant": name, "tf": tf, "ws": ws, "side": d.side, "limit": float(d.extra.get("rest_limit")),
        "shares": d.shares, "est_prob": d.est_prob, "edge": d.edge, "signal": (d.extra or {}).get("signal"),
        "ts": now, "english": d.english, "model_p_up": d.model_p_up,
        "slug": getattr(m, "slug", ""), "title": getattr(m, "title", ""),
    }
    log.info("MAX PROFIT REST [%s] %s %s limit %.2f x %.2f sh (%s)", name, tf, d.side, d.extra.get("rest_limit"), d.shares, (d.extra or {}).get("signal"))


def manage_rest(state: dict, variants: dict, *, tf: str, ws: int, books: dict, secs_left: float,
                m, spot, ptb, ptb_src, vol, now: float, paper_fill: Callable) -> bool:
    """Fill a resting limit if the ask has crossed it, or take the ask if time is almost gone."""
    b = state.get(STATE_KEY) or {}
    rest = b.get("rest") or {}
    if not rest or rest.get("tf") != tf or int(rest.get("ws") or 0) != int(ws):
        return False
    name = rest.get("variant") or b.get("variant") or "max_profit"
    v = variants.get(name) or {}
    book = books.get(rest["side"])
    if book is None:
        return False
    limit = float(rest["limit"])
    ask = book.best_ask
    filled = False
    reason = ""
    px = None
    maker = False
    if ask is not None and float(ask) <= limit + 1e-12:
        px = float(ask)  # crossed; fill at the (possibly better) ask
        maker = True
        reason = "maker"
    elif secs_left <= float(v.get("rest_take_secs", 40)) and ask is not None:
        px = float(ask)
        maker = False
        reason = "take"
    else:
        return False

    # Re-size at the actual fill price, still under the bankroll cap.
    recompute(state, name)
    cash = float(b.get("balance") or 0)
    if b.get("paused") or cash <= 0:
        b["rest"] = {}
        return False
    cap = min(cash, float(b.get("equity") or cash) * float(v.get("max_frac", 0.25)))
    sized = size_for_budget(px, cap, getattr(book, "best_ask_size", 1e9) or 1e9)
    if not sized:
        b["rest"] = {}
        return False
    shares, notional, fee, total = sized
    if maker:
        # paper maker: no taker fee (Polymarket maker fee is typically 0 on these markets)
        fee = 0.0
        total = shares * px
        notional = total
    d = strategy.Decision(
        name, "buy", f"max_profit_{reason}",
        f"{'Maker fill' if maker else 'Time ran short, taking the ask'} at {px*100:.0f}c for {shares:.2f} {rest['side']} "
        f"(limit was {limit*100:.0f}c). Signal: {rest.get('signal') or 'combined'}.",
        side=rest["side"], ask=px, ask_size=getattr(book, "best_ask_size", 0) or 0,
        est_prob=rest.get("est_prob"), model_p_up=rest.get("model_p_up"), edge=rest.get("edge"),
        stake=notional, shares=shares, notional=notional, fee=fee, total_cost=total,
        extra={"allow_over_hard_max": True, "hard_max": cap, "signal": rest.get("signal"), "maker": maker},
    )
    b["rest"] = {}
    paper_fill(state, name, d, m, ws, secs_left, spot, ptb, ptb_src, vol, tf=tf)
    return True


def _cancel_stale_rest(state: dict, tf: str, ws: int) -> None:
    rest = (state.get(STATE_KEY) or {}).get("rest") or {}
    if rest and (rest.get("tf") != tf or int(rest.get("ws") or 0) != int(ws)):
        # different window — drop if the window has clearly passed
        if int(rest.get("ws") or 0) < int(ws):
            (state.get(STATE_KEY) or {}).pop("rest", None)


def _source_names(v: dict, tf: str) -> list[str]:
    src = v.get("signal_sources") or {}
    if isinstance(src, dict):
        return list(src.get(tf) or src.get("5m") or [])
    return list(src)


def decide(state: dict, name: str, v: dict, variants: dict, *, p_up, books, secs_left, trend, spot, ptb,
           ws, now, tf, m) -> strategy.Decision:
    D = lambda action, reason, english, **kw: strategy.Decision(name, action, reason, english, model_p_up=p_up, **kw)  # noqa: E731
    b = ensure(state, v, name)
    recompute(state, name)
    _cancel_stale_rest(state, tf, ws)
    if has_rest(state, tf, ws):
        return D("skip", "resting", f"A limit order is already resting in this {tf} window; waiting for a fill or to take later.")
    if b.get("paused"):
        return D("skip", "paused",
                 f"Max-profit is paused: cash ${b['balance']:.2f} is below the ${float(b.get('floor', 12)):.0f} floor, so it sits out rather than risk going to zero.")
    cash = float(b.get("balance") or 0)
    equity = float(b.get("equity") or cash)
    max_frac = float(v.get("max_frac", 0.25))
    cap = min(cash, equity * max_frac)
    floor = float(b.get("floor", 12))
    # never place a bet that would leave cash below the floor if it lost
    cap = min(cap, max(0.0, cash - floor))
    if cap <= 0:
        return D("skip", "floor_cap",
                 f"Any new bet would leave less than the ${floor:.0f} floor (cash ${cash:.2f}). Sitting out.")

    # Collect buy signals from the configured source strategies (and crowd, if present).
    votes: dict[str, list] = {"Up": [], "Down": []}
    for src_name in _source_names(v, tf):
        src = variants.get(src_name)
        if not src or src.get("mode") in ("max_profit", "bankroll", "auto_pick", "meta"):
            continue
        try:
            if src.get("mode") == "crowd":
                import crowd
                d = crowd.decide(src_name, src, m=m, books=books, secs_left=secs_left, ws=ws, now=now, state=state, p_up=p_up)
            else:
                d = strategy.decide(src_name, src, p_model=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot, ptb=ptb)
        except Exception as exc:  # noqa: BLE001
            log.warning("max_profit source %s failed: %s", src_name, exc)
            continue
        if d.action == "buy" and d.side in ("Up", "Down") and d.ask:
            q = d.est_prob if d.est_prob is not None else (p_up if d.side == "Up" else (1 - p_up if p_up is not None else None))
            votes[d.side].append((src_name, d, q))

    # Prefer the side with more agreeing sources; then the one with the better edge.
    best = None  # (score, side, src, d, q)
    for side, rows in votes.items():
        if not rows:
            continue
        # score: agreements + best edge
        edges = []
        for src_name, d, q in rows:
            edge = d.edge if d.edge is not None else ((q or 0) - (d.ask or 0) - strategy.fee_per_share(d.ask or 0))
            edges.append((edge, src_name, d, q))
        edges.sort(reverse=True, key=lambda x: x[0])
        edge, src_name, d, q = edges[0]
        score = len(rows) * 0.05 + (edge or 0)
        if best is None or score > best[0]:
            best = (score, side, src_name, d, q, [r[0] for r in rows])
    if not best:
        return D("skip", "no_signal",
                 f"No source strategy has a clear entry in this {tf} window ({secs_left:.0f}s left). Cash ${cash:.2f}.")

    _, side, src_name, src_d, q, names = best
    ask = float(src_d.ask)
    book = books.get(side)
    if book is None or book.best_ask is None:
        return D("skip", "no_book", f"Signal from {src_name} says {side}, but there is no ask.")
    if q is None:
        q = p_up if side == "Up" else (1 - p_up if p_up is not None else 0.55)
        try:
            q = float(q)
        except (TypeError, ValueError):
            q = 0.55
    fee_ps = strategy.fee_per_share(ask)
    edge = (q - ask - fee_ps)
    min_edge = float(v.get("min_edge", 0.02))
    if edge < min_edge:
        # still allow strong agreement even if modeled edge is a bit thin
        if len(names) < 2:
            return D("skip", "thin_edge",
                     f"{', '.join(names)} like {side} at {ask*100:.0f}c, but estimated edge is only {edge*100:.1f}c (need {min_edge*100:.0f}c).")

    kfrac = kelly_fraction(q, ask, fee_ps, frac=float(v.get("kelly_frac", 0.5)))
    budget = min(cap, equity * kfrac) if kfrac > 0 else 0.0
    # If Kelly is tiny but a source fired with enough edge, use the minimum 5-share bet when it still fits the cap.
    sized = size_for_budget(ask, budget, book.best_ask_size or 1e9)
    if sized is None:
        sized = size_for_budget(ask, cap, book.best_ask_size or 1e9)
        if sized is None:
            return D("skip", "too_small",
                     f"Kelly wanted {kfrac:.0%} of ${equity:.2f}, which is too small for a 5-share bet at {ask*100:.0f}c under the 25% / floor caps.")
    shares, notional, fee, total = sized
    if total > cap + 1e-9:
        return D("skip", "cap", f"Sized cost ${total:.2f} would exceed the {max_frac:.0%} / floor cap of ${cap:.2f}.")

    agree = ", ".join(names)
    english = (f"Buying ${notional:.2f} of {side} on the {tf} market: {agree} "
               f"{'agree' if len(names)>1 else 'signals'} {side} at {ask*100:.0f}c "
               f"(estimated chance {q:.0%}, edge {edge*100:.1f}c). "
               f"Half-Kelly is {kfrac:.0%} of ${equity:.2f} equity, capped at {max_frac:.0%} / leaving the ${floor:.0f} floor "
               f"(this paper account may go above the usual $10 cap; cash now ${cash:.2f}).")

    extra = {"allow_over_hard_max": True, "hard_max": cap, "signal": agree, "kelly": kfrac, "maker": False}
    # Rest on the bid when the spread is at least 2c and there is time — fill later if the ask crosses.
    bid = getattr(book, "best_bid", None)
    spread = (ask - bid) if bid is not None else 0
    if (v.get("try_maker", True) and bid is not None and spread >= float(v.get("maker_min_spread", 0.02))
            and secs_left > float(v.get("rest_take_secs", 40)) + 5):
        # size at the bid (better price)
        sized_bid = size_for_budget(float(bid), cap, getattr(book, "best_bid_size", 1e9) or 1e9)
        if sized_bid:
            shares, notional, fee, total = sized_bid
            extra["rest_limit"] = float(bid)
            d = D("buy", "max_profit_rest",
                  f"Resting a paper limit at the {bid*100:.0f}c bid for {shares:.2f} {side} ({agree}; "
                  f"ask is {ask*100:.0f}c). Will take the ask if it does not fill with {v.get('rest_take_secs', 40)}s left. "
                  f"Cash ${cash:.2f}.",
                  side=side, ask=ask, ask_size=book.best_ask_size, est_prob=q, edge=edge,
                  stake=notional, shares=shares, notional=notional, fee=fee, total_cost=total, extra=extra)
            place_rest(state, name, d, tf=tf, ws=ws, now=now, m=m)
            # cycle should not taker-fill this; mark as skip for the fill path
            d.action = "skip"
            d.reason = "resting_limit"
            b["last_signal"] = agree
            return d

    b["last_signal"] = agree
    return D("buy", "max_profit", english, side=side, ask=ask, ask_size=book.best_ask_size,
             est_prob=q, edge=edge, stake=notional, shares=shares, notional=notional, fee=fee,
             total_cost=total, extra=extra)
