"""Crowd-following paper variant: comments + trade-flow + book imbalance.

Uses only official public APIs (Gamma comments, Data API v2 trades, CLOB books).
No website scraping, no auth, fake money only.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import config
import polymarket
import strategy

log = logging.getLogger("crowd")

# Simple word lists for short Up/Down chat. A comment that hits both sides is ignored.
_UP_RE = re.compile(
    r"\b(up|pump|pumps|pumped|pumping|moon|mooning|moons|long|longs|green|bull|bullish|"
    r"rally|higher|bounce|call|calls|moonshot|send it|to the moon)\b",
    re.I,
)
_DOWN_RE = re.compile(
    r"\b(down|dump|dumps|dumped|dumping|short|shorts|red|rekt|bear|bearish|"
    r"crash|crashing|lower|drop|dropping|fall|falling|put|puts|rip)\b",
    re.I,
)

# Cache comments/trades briefly so a tight cycle loop does not hammer the public APIs.
_cache: dict = {}


def _cache_get(key, ttl: float):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_set(key, value):
    _cache[key] = (time.time(), value)
    if len(_cache) > 200:
        cutoff = time.time() - 120
        for k in [k for k, (ts, _) in list(_cache.items()) if ts < cutoff]:
            _cache.pop(k, None)


def _parse_ts(val) -> Optional[float]:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        t = float(val)
        return t / 1000.0 if t > 1e12 else t
    s = str(val).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def classify_comment(body: str) -> Optional[str]:
    """Return 'Up', 'Down', or None if the comment does not clearly lean one way."""
    text = (body or "").strip()
    if not text:
        return None
    up, down = bool(_UP_RE.search(text)), bool(_DOWN_RE.search(text))
    if up and not down:
        return "Up"
    if down and not up:
        return "Down"
    return None


def comment_tally(comments: list, *, ws: int, now: float) -> dict:
    """Count current-window comments leaning Up vs Down."""
    in_win = up = down = other = 0
    for c in comments or []:
        body = (c.get("body") if isinstance(c, dict) else None) or ""
        ts = _parse_ts((c or {}).get("createdAt") if isinstance(c, dict) else None)
        if ts is not None and ts < ws - 1:
            continue
        if ts is not None and ts > now + 5:
            continue
        in_win += 1
        side = classify_comment(body)
        if side == "Up":
            up += 1
        elif side == "Down":
            down += 1
        else:
            other += 1
    return {"n": in_win, "up": up, "down": down, "other": other, "n_fetched": len(comments or [])}


def flow_tally(trades: list, *, ws: int, now: float) -> dict:
    """Buy volume (notional and shares) per outcome in the current window. Sells are ignored."""
    up_n = down_n = up_sh = down_sh = 0.0
    n = 0
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        ts = _parse_ts(t.get("timestamp") or t.get("match_time") or t.get("createdAt"))
        if ts is not None and ts < ws - 1:
            continue
        if ts is not None and ts > now + 5:
            continue
        side = str(t.get("side") or "").upper()
        if side and side != "BUY":
            continue
        outcome = str(t.get("outcome") or "")
        if outcome not in ("Up", "Down"):
            continue
        try:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        notional = max(0.0, size * price)
        n += 1
        if outcome == "Up":
            up_n += notional
            up_sh += size
        else:
            down_n += notional
            down_sh += size
    return {"n": n, "up_notional": up_n, "down_notional": down_n, "up_shares": up_sh, "down_shares": down_sh}


def book_imbalance(books: dict) -> dict:
    """How much bid-size sits on Up vs Down. Positive imbalance leans Up."""
    up_sz = float(getattr(books.get("Up"), "best_bid_size", 0) or 0)
    down_sz = float(getattr(books.get("Down"), "best_bid_size", 0) or 0)
    tot = up_sz + down_sz
    imb = (up_sz - down_sz) / tot if tot > 1e-9 else 0.0
    return {"up_bid_size": up_sz, "down_bid_size": down_sz, "imbalance": imb}


def _lean(up: float, down: float, *, min_total: float, min_share: float) -> Optional[str]:
    tot = up + down
    if tot + 1e-12 < min_total:
        return None
    if up / tot >= min_share:
        return "Up"
    if down / tot >= min_share:
        return "Down"
    return None


def stats(state: dict) -> dict:
    return state.setdefault("crowd_stats", {
        "windows": 0, "comments_present": 0, "comments_empty": 0, "comments_failed": 0,
        "flow_ok": 0, "flow_failed": 0, "bets": 0, "last": "",
    })


def _fetch_comments(event_id) -> tuple[list, Optional[str]]:
    key = ("comments", str(event_id))
    hit = _cache_get(key, 20)
    if hit is not None:
        return hit, None
    try:
        rows = polymarket.fetch_comments(event_id)
        _cache_set(key, rows)
        return rows, None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)[:160]


def _fetch_trades(condition_id) -> tuple[list, Optional[str]]:
    key = ("trades", str(condition_id))
    hit = _cache_get(key, 15)
    if hit is not None:
        return hit, None
    try:
        rows = polymarket.fetch_recent_trades(condition_id)
        _cache_set(key, rows)
        return rows, None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)[:160]


def decide(name: str, v: dict, *, m, books: dict, secs_left: float, ws: int, now: float,
           state: dict, p_up: Optional[float] = None) -> strategy.Decision:
    D = lambda action, reason, english, **kw: strategy.Decision(  # noqa: E731
        name, action, reason, english, model_p_up=p_up, **kw)
    if not (float(v.get("min_secs_left", 0)) <= secs_left <= float(v.get("max_secs_left", 300))):
        return D("skip", "timing",
                 f"Only bets with {v.get('min_secs_left')}-{v.get('max_secs_left')}s left; {secs_left:.0f}s left now.")

    st = stats(state)
    meta = getattr(m, "meta", None) or {}
    event_id = meta.get("event_id")
    condition_id = meta.get("condition_id")

    comments, c_err = _fetch_comments(event_id) if event_id else ([], "no event_id")
    trades, t_err = _fetch_trades(condition_id) if condition_id else ([], "no condition_id")
    ct = comment_tally(comments, ws=ws, now=now)
    fl = flow_tally(trades, ws=ws, now=now)
    bk = book_imbalance(books)

    # One stats tick per window (first time we actually evaluate, i.e. inside the timing gate).
    seen = state.setdefault("crowd_windows", [])
    if ws not in seen:
        seen.append(ws)
        state["crowd_windows"] = seen[-200:]
        st["windows"] = int(st.get("windows", 0)) + 1
        if c_err:
            st["comments_failed"] = int(st.get("comments_failed", 0)) + 1
        elif ct["n"] > 0 or ct["n_fetched"] > 0:
            st["comments_present"] = int(st.get("comments_present", 0)) + 1
        else:
            st["comments_empty"] = int(st.get("comments_empty", 0)) + 1
        if t_err:
            st["flow_failed"] = int(st.get("flow_failed", 0)) + 1
        elif fl["n"] > 0:
            st["flow_ok"] = int(st.get("flow_ok", 0)) + 1
    st["last"] = (f"comments in-window={ct['n']} (Up {ct['up']}/Down {ct['down']}, fetched {ct['n_fetched']}"
                  + (f", err {c_err}" if c_err else "") + "); "
                  + f"buy flow Up ${fl['up_notional']:.0f} vs Down ${fl['down_notional']:.0f} ({fl['n']} buys"
                  + (f", err {t_err}" if t_err else "") + "); "
                  + f"bid size Up {bk['up_bid_size']:.0f} vs Down {bk['down_bid_size']:.0f}")

    comment_side = _lean(ct["up"], ct["down"], min_total=float(v.get("min_comments", 3)),
                         min_share=float(v.get("comment_share", 0.67)))
    flow_side = _lean(fl["up_notional"], fl["down_notional"],
                      min_total=float(v.get("min_flow_usd", 15)),
                      min_share=float(v.get("flow_share", 0.62)))
    book_side = None
    if abs(bk["imbalance"]) >= float(v.get("book_imbalance", 0.25)) and (bk["up_bid_size"] + bk["down_bid_size"]) >= 10:
        book_side = "Up" if bk["imbalance"] > 0 else "Down"

    used_comments = comment_side is not None
    side = None
    why = ""
    if comment_side and (flow_side is None or flow_side == comment_side):
        side, why = comment_side, "chat"
    elif comment_side and flow_side and flow_side != comment_side:
        return D("skip", "crowd_disagree",
                 f"Chat leans {comment_side} ({ct['up']}-{ct['down']}) but trade flow leans {flow_side} "
                 f"(Up ${fl['up_notional']:.0f} vs Down ${fl['down_notional']:.0f}). Not a clear crowd. {st['last']}")
    elif flow_side and (book_side is None or book_side == flow_side):
        side, why = flow_side, "trade flow"
    elif flow_side and book_side and book_side != flow_side:
        return D("skip", "crowd_disagree",
                 f"Trade flow leans {flow_side} but the book leans {book_side}. Not a clear crowd. {st['last']}")
    else:
        empty = "No comments in this short window (common on 5-minute markets). " if ct["n"] == 0 else ""
        return D("skip", "no_crowd",
                 f"{empty}No clear crowd yet. {st['last']}")

    book = books.get(side)
    if book is None or book.best_ask is None:
        return D("skip", "no_book", f"Crowd leans {side} ({why}), but there is no ask to buy.")
    ask = float(book.best_ask)
    min_ask, max_ask = float(v.get("min_ask", 0.25)), float(v.get("max_ask", 0.90))
    if not (min_ask - 1e-12 <= ask <= max_ask + 1e-12):
        return D("skip", "ask_range",
                 f"Crowd leans {side} ({why}), but {side} costs {ask*100:.0f}c, outside {min_ask*100:.0f}-{max_ask*100:.0f}c.")
    if book.best_ask_size + 1e-9 < config.MIN_ORDER_SHARES:
        return D("skip", "thin_book", f"Crowd leans {side} ({why}), but the ask is too thin for a minimum bet.")

    stake, shares, note = strategy.size_bet(v, edge=0.0, ask=ask)
    if shares < config.MIN_ORDER_SHARES - 1e-9:
        return D("skip", "too_small", f"Crowd leans {side} ({why}), but the size rounded below the 5-share minimum.")
    notional = shares * ask
    fee = shares * strategy.fee_per_share(ask)
    st["bets"] = int(st.get("bets", 0)) + 1
    src = "chat and trade flow" if used_comments and why == "chat" else why
    english = (f"Buying ${notional:.2f} of {side}: the crowd leans {side} via {src}. "
               f"Chat this window: {ct['up']} Up / {ct['down']} Down ({ct['n']} comments). "
               f"Buy flow: Up ${fl['up_notional']:.0f} vs Down ${fl['down_notional']:.0f}. "
               f"Bid size Up {bk['up_bid_size']:.0f} vs Down {bk['down_bid_size']:.0f}. {note}")
    return D("buy", "crowd", english, side=side, ask=ask, ask_size=book.best_ask_size,
             stake=stake, shares=shares, notional=notional, fee=fee, total_cost=notional + fee,
             est_prob=fl["up_notional"] / (fl["up_notional"] + fl["down_notional"]) if side == "Up" and (fl["up_notional"]+fl["down_notional"]) else (
                 fl["down_notional"] / (fl["up_notional"] + fl["down_notional"]) if (fl["up_notional"]+fl["down_notional"]) else None),
             extra={"crowd": st["last"], "used_comments": used_comments})
