"""Early-exit (cash-out) helpers for paper bets. FAKE MONEY ONLY.

A cash-out variant enters with the same rules as its source strategy, then may
sell into the live best BID (taker fee = shares * 0.07 * p * (1-p)) instead of
holding to official settlement. Never places a real order.
"""
from __future__ import annotations

import csv
import logging
from typing import Callable, Optional

import config
import polymarket
import store
import strategy

log = logging.getLogger("cashout")
EXIT_CSV = config.DATA_DIR / "cash_out_exits.csv"
EXIT_FIELDS = [
    "ts_pt", "variant", "trade_id", "timeframe", "side", "shares", "bid", "bid_size",
    "exit_fee_usd", "proceeds_usd", "total_cost_usd", "pnl_usd", "reason", "secs_left", "english",
]


def sell_proceeds(shares: float, bid: float) -> tuple[float, float]:
    """Return (proceeds, taker_fee) for selling `shares` at `bid`."""
    fee = float(shares) * strategy.fee_per_share(bid)
    return float(shares) * float(bid) - fee, fee


def should_exit(v: dict, *, bid: float, proceeds: float, cost: float, side: str,
                spot: Optional[float], ptb: Optional[float], secs_left: float,
                held_sec: float, wsec: int) -> Optional[tuple[str, str]]:
    """Return (reason_code, plain English) if we should sell now, else None."""
    min_hold = float(v.get("min_hold_sec", 20))
    if held_sec + 1e-9 < min_hold:
        return None
    if secs_left < 8:  # leave the last seconds for official settlement
        return None
    tp_pct = float(v.get("take_profit_pct", 0.20))
    tp_bid = float(v.get("take_profit_bid", 0.95))
    sl_frac = float(v.get("stop_loss_frac", 0.55))
    flip_secs = float(v.get("flip_secs_left", 60))
    ret = (proceeds - cost) / cost if cost > 1e-9 else 0.0
    if bid + 1e-12 >= tp_bid:
        return ("take_profit_price",
                f"Take profit: the bid reached {bid*100:.0f}c (target {tp_bid*100:.0f}c). "
                f"Selling locks in {ret:+.0%} after the sell fee.")
    if ret + 1e-12 >= tp_pct:
        return ("take_profit_pct",
                f"Take profit: selling at {bid*100:.0f}c would make {ret:+.0%} after the sell fee "
                f"(target {tp_pct:.0%}).")
    if proceeds <= sl_frac * cost + 1e-12:
        loss = (cost - proceeds) / cost if cost > 1e-9 else 0.0
        return ("stop_loss",
                f"Stop loss: the bid is {bid*100:.0f}c, so the position is worth ${proceeds:.2f} "
                f"({loss:.0%} below the ${cost:.2f} we paid; sell if it drops about {1-sl_frac:.0%}).")
    if (spot is not None and ptb is not None and ptb > 0 and secs_left <= flip_secs + 1e-9
            and secs_left < 0.5 * wsec):  # only in the back half of the window
        lead = (spot - ptb) if side == "Up" else (ptb - spot)
        if lead < 0:
            return ("lead_flip",
                    f"Lead flipped: we are on {side}, but price is ${abs(lead):.2f} the other way "
                    f"with only {secs_left:.0f}s left (exit when the lead is against us with little time left).")
    return None


def _log_exit(row: dict) -> None:
    EXIT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fresh = not EXIT_CSV.exists() or EXIT_CSV.stat().st_size == 0
    try:
        with EXIT_CSV.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=EXIT_FIELDS)
            if fresh:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in EXIT_FIELDS})
    except Exception:  # noqa: BLE001
        log.exception("could not write cash_out_exits.csv")


def manage_open(state: dict, variants: dict, *, spot: float, now: float,
                price_to_beat_fn: Callable, get_market_fn: Callable) -> int:
    """Look at open cash-out paper bets and sell any that hit an exit rule. Returns number sold."""
    sold = 0
    still = []
    by_mkt: dict = {}
    for item in state.get("open", []):
        name = item.get("variant")
        v = variants.get(name) or {}
        if not v.get("cash_out"):
            still.append(item)
            continue
        tf = item.get("tf") or v.get("timeframe") or "5m"
        ws = int(item["window_start"])
        try:
            wsec = polymarket.window_seconds(tf) if hasattr(polymarket, "window_seconds") else int(getattr(config, "TIMEFRAMES", {}).get(tf, {}).get("seconds") or config.WINDOW_SECONDS)
        except Exception:
            wsec = int(getattr(config, "WINDOW_SECONDS", 300))
        secs_left = wsec - (now - ws)
        if secs_left < 8:
            still.append(item)
            continue
        key = (tf, ws)
        if key not in by_mkt:
            try:
                try:
                    m = get_market_fn(ws, tf)
                except TypeError:
                    m = get_market_fn(ws)
                books = polymarket.fetch_books(m) if m is not None and m.token_ids else None
                by_mkt[key] = (m, books)
            except Exception as exc:  # noqa: BLE001
                log.warning("cash-out book fetch failed for %s %s: %s", tf, ws, exc)
                by_mkt[key] = (None, None)
        m, books = by_mkt[key]
        if not books:
            still.append(item)
            continue
        book = books.get(item["side"])
        if book is None or book.best_bid is None:
            still.append(item)
            continue
        shares = float(item["shares"])
        bid = float(book.best_bid)
        bid_size = float(getattr(book, "best_bid_size", 0.0) or 0.0)
        if bid_size + 1e-9 < shares:
            still.append(item)
            continue
        proceeds, fee = sell_proceeds(shares, bid)
        if proceeds <= 0:
            still.append(item)
            continue
        cost = float(item["total_cost"])
        held = now - float(item["ts_fill"]) if item.get("ts_fill") else 999.0
        try:
            ptb, _src = price_to_beat_fn(state, ws)
        except Exception:
            ptb = None
        hit = should_exit(v, bid=bid, proceeds=proceeds, cost=cost, side=item["side"],
                          spot=spot, ptb=ptb, secs_left=secs_left, held_sec=held, wsec=wsec)
        if not hit:
            still.append(item)
            continue
        reason, english = hit
        pnl = proceeds - cost
        prior = sum(float(r["pnl_usd"]) for r in store.read_trades(name) if r.get("status") == "resolved" and r.get("pnl_usd"))
        store.update_trade(name, item["trade_id"], {
            "status": "resolved", "ts_resolve_utc": store.iso_utc(now),
            "outcome": "sold_early", "won": "1" if pnl > 0 else "0",
            "payout_usd": f"{proceeds:.4f}", "pnl_usd": f"{pnl:.6f}",
            "cumulative_pnl_usd": f"{prior + pnl:.6f}",
            "exit_type": "cash_out", "exit_reason": reason, "exit_bid": f"{bid:.4f}",
            "exit_shares": f"{shares:.2f}", "exit_fee_usd": f"{fee:.6f}",
            "exit_proceeds_usd": f"{proceeds:.4f}", "ts_exit_pt": store.iso_pt(now),
        })
        _log_exit({
            "ts_pt": store.iso_pt(now), "variant": name, "trade_id": item["trade_id"],
            "timeframe": tf, "side": item["side"], "shares": f"{shares:.2f}",
            "bid": f"{bid:.4f}", "bid_size": f"{bid_size:.2f}", "exit_fee_usd": f"{fee:.6f}",
            "proceeds_usd": f"{proceeds:.4f}", "total_cost_usd": f"{cost:.4f}",
            "pnl_usd": f"{pnl:.6f}", "reason": reason, "secs_left": f"{secs_left:.1f}",
            "english": english,
        })
        log.info("CASH OUT [%s] %s sold %.2f %s @ %.2f bid (fee $%.3f) proceeds $%.2f pnl=$%+.2f (%s) %.0fs left — %s",
                 name, item["trade_id"], shares, item["side"], bid, fee, proceeds, pnl, reason, secs_left, english)
        sold += 1
    if sold:
        state["open"] = still
    else:
        # still may equal open (no change) or we may have skipped nothing; assign anyway if we rebuilt the list
        state["open"] = still
    return sold
