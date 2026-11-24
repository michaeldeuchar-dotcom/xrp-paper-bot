"""Daily loss kill switch for selected paper variants. Fake money only."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import store

log = logging.getLogger("safety")
PT = ZoneInfo("America/Los_Angeles")


def _pt_day(ts: float | None = None) -> str:
    if ts is None:
        return datetime.now(PT).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(float(ts), PT).strftime("%Y-%m-%d")


def _row_day(row: dict) -> str | None:
    for key in ("ts_resolve_pt", "ts_fill_pt", "ts_pt"):
        s = (row.get(key) or "")[:10]
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return s
    for key in ("ts_resolve_utc", "ts_fill_utc"):
        s = row.get(key) or ""
        if not s:
            continue
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).astimezone(PT).strftime("%Y-%m-%d")
        except Exception:
            continue
    return None


def daily_pnl(name: str, now: float | None = None) -> float:
    """Settled P&L for this variant on today's PT calendar date."""
    day = _pt_day(now)
    total = 0.0
    try:
        rows = store.read_trades(name)
    except Exception:
        return 0.0
    for r in rows:
        if r.get("status") != "resolved" or not r.get("pnl_usd"):
            continue
        if _row_day(r) != day:
            continue
        try:
            total += float(r["pnl_usd"])
        except (TypeError, ValueError):
            pass
    return total


def paused(state: dict, name: str, v: dict, now: float) -> tuple[bool, str]:
    """True if this variant has hit its daily loss limit (PT day). New variants only; opt-in via daily_loss_limit."""
    cap = v.get("daily_loss_limit")
    if cap is None:
        return False, ""
    cap = float(cap)
    if cap <= 0:
        return False, ""
    pnl = daily_pnl(name, now)
    day = _pt_day(now)
    book = state.setdefault("daily_loss", {}).setdefault(name, {})
    if pnl <= -cap + 1e-12:
        if book.get("paused_day") != day:
            book["paused_day"] = day
            book["paused_pnl"] = pnl
            log.info("DAILY LOSS KILL SWITCH [%s] paused for the rest of %s PT (today's settled P&L $%+.2f, limit -$%.2f). Fake money only.",
                     name, day, pnl, cap)
        return True, (f"Daily loss kill switch: today's settled P&L is ${pnl:+.2f}, "
                      f"at or past the -${cap:.2f} pause for this variant. New bets wait until tomorrow (PT).")
    if book.get("paused_day") == day:
        book.pop("paused_day", None)
    return False, ""
