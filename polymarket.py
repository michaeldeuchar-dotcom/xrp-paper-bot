"""Public Polymarket Gamma + CLOB reads (no auth). All calls have hard deadlines."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import config
import net


def window_seconds(tf: str = "5m") -> int:
    return int(config.TIMEFRAMES[tf]["seconds"])


def window_start_ts(ts: Optional[float] = None, tf: str = "5m") -> int:
    t = int(ts if ts is not None else time.time())
    w = window_seconds(tf)
    return (t // w) * w


def slug_for(ws: int, tf: str = "5m") -> str:
    return config.TIMEFRAMES[tf]["slug"].format(start=ws)


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


@dataclass
class Market:
    slug: str
    window_start: int
    title: str
    token_ids: dict
    outcome_prices: dict
    closed: bool
    meta: dict

    @property
    def resolved_outcome(self) -> Optional[str]:
        u, d = self.outcome_prices.get("Up"), self.outcome_prices.get("Down")
        if u is None or d is None:
            return None
        if u >= 0.99 and u > d:
            return "Up"
        if d >= 0.99 and d > u:
            return "Down"
        if self.closed and u != d:
            return "Up" if u > d else "Down"
        return None


def fetch_event(ws: int, tf: str = "5m") -> Optional[Market]:
    data = net.hard_get_json(f"{config.GAMMA_BASE}/events?slug={slug_for(ws, tf)}")
    if not data:
        return None
    ev = data[0] if isinstance(data, list) else data
    ms = ev.get("markets") or []
    if not ms:
        return None
    m = ms[0] if isinstance(ms[0], dict) else json.loads(ms[0])
    outs = _j(m.get("outcomes")) or ["Up", "Down"]
    toks = _j(m.get("clobTokenIds")) or []
    px = _j(m.get("outcomePrices")) or []
    tid, op = {}, {}
    for i, n in enumerate(outs):
        if i < len(toks):
            tid[str(n)] = str(toks[i])
        if i < len(px):
            try:
                op[str(n)] = float(px[i])
            except (TypeError, ValueError):
                pass
    return Market(slug_for(ws, tf), ws, str(m.get("question") or ev.get("title") or ""), tid, op,
                  bool(m.get("closed") or ev.get("closed")), ev.get("eventMetadata") or {})


@dataclass
class Book:
    best_bid: Optional[float]
    best_ask: Optional[float]
    best_ask_size: float
    best_bid_size: float = 0.0


def fetch_book(token_id: str) -> Book:
    b = net.hard_get_json(f"{config.CLOB_BASE}/book?token_id={token_id}", 5)
    px = lambda l: float(l["price"] if isinstance(l, dict) else l[0])  # noqa: E731
    sz = lambda l: float(l["size"] if isinstance(l, dict) else l[1])  # noqa: E731
    asks, bids = b.get("asks") or [], b.get("bids") or []
    ba = min(asks, key=px) if asks else None
    bb = max(bids, key=px) if bids else None
    return Book(px(bb) if bb else None, px(ba) if ba else None, sz(ba) if ba else 0.0, sz(bb) if bb else 0.0)


def fetch_books(m: Market) -> dict:
    ok, err = net.parallel({side: (lambda t=t: fetch_book(t)) for side, t in m.token_ids.items()}, deadline=6)
    if err:
        raise RuntimeError(f"book fetch failed: {err}")
    return ok
