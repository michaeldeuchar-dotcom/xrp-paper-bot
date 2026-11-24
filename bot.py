#!/usr/bin/env python3
"""PAPER bot for Polymarket XRP (Ripple) Up/Down markets: 5-minute, 15-minute, and 1-hour.

PAPER ONLY: this build contains no order-signing or wallet code at all. It reads
public prices, simulates taker fills at the real best ask (with the taker fee),
runs several strategy variants (most hold to official resolution; cash-out variants may sell early into the best bid)
side by side on the same data, each with its own ledger (data/week2/trades_<variant>.csv).
"""
from __future__ import annotations

import argparse
import collections
import faulthandler
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import feeds
import polymarket
import store
import cashout
import crowd
import maxprofit
import ab_hedge
import strategy

PT = ZoneInfo("America/Los_Angeles")
log = logging.getLogger("xrpbot")
_SHUTDOWN = threading.Event()


def _on_signal(signum, _frame):
    if _SHUTDOWN.is_set():
        os._exit(0)
    _SHUTDOWN.set()
    log.info("received signal %s; stopping (forced exit in 20s if stuck)", signum)
    t = threading.Timer(20, lambda: os._exit(0))
    t.daemon = True
    t.start()


def setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    h = logging.handlers.RotatingFileHandler(config.LOG_PATH, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUPS)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)


def _reason(msg: str) -> None:
    if config.LOG_BET_REASONS:
        log.info("REASON %s", msg)


# ---------------------------------------------------------------- variants
_variants = {"mtime": 0.0, "data": {}, "checked": 0.0}


def _tweak_log(text: str) -> None:
    config.TWEAKS_LOG.parent.mkdir(parents=True, exist_ok=True)
    new = not config.TWEAKS_LOG.exists()
    with config.TWEAKS_LOG.open("a") as f:
        if new:
            f.write("# Tweaks log (paper bot)\n\nEvery strategy/setting change, with time (PT) and reason.\n\n")
        f.write(text.rstrip() + "\n\n")


def load_variants(force: bool = False) -> dict:
    now = time.time()
    if not force and now - _variants["checked"] < 30:
        return _variants["data"]
    _variants["checked"] = now
    try:
        mt = config.VARIANTS_PATH.stat().st_mtime
        if force or mt != _variants["mtime"]:
            raw = json.loads(config.VARIANTS_PATH.read_text())
            data = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict) and v.get("enabled", True)}
            for k, v in data.items():  # hard ceiling, regardless of the file (except paper-only max_profit)
                if v.get("allow_over_hard_max"):
                    continue
                for key in ("base_stake", "max_stake", "high_ask_max_stake", "max_round_up_usd", "max_loss_per_bet"):
                    if float(v.get(key, 0) or 0) > config.STAKE_HARD_MAX_USD:
                        log.warning("variant %s %s=%s clipped to $%.0f hard max", k, key, v[key], config.STAKE_HARD_MAX_USD)
                        v[key] = config.STAKE_HARD_MAX_USD
            if _variants["data"] and data != _variants["data"]:
                changed = []
                for k in sorted(set(data) | set(_variants["data"])):
                    a, b = _variants["data"].get(k), data.get(k)
                    if a != b:
                        diff = {x: f"{(a or {}).get(x)} -> {(b or {}).get(x)}" for x in sorted(set(a or {}) | set(b or {})) if (a or {}).get(x) != (b or {}).get(x)}
                        changed.append(f"- `{k}`: {diff}")
                _tweak_log(f"## {store.iso_pt()} — variants.json reloaded by running bot\n" + "\n".join(changed)
                           + "\n- Reason: see the entry written by whoever edited variants.json (above/below).")
                log.info("variants reloaded: %s", "; ".join(changed))
            _variants.update(mtime=mt, data=data)
    except Exception as exc:  # keep the last good set
        log.warning("variants.json reload failed (%s); keeping previous set", exc)
    return _variants["data"]


# ---------------------------------------------------------------- spot samples / price to beat
_samples: collections.deque = collections.deque(maxlen=2000)  # (ts, spot)


def own_twap(t0: float, t1: float, min_n: int = 10):
    xs = [p for (t, p) in _samples if t0 <= t <= t1]
    ts = [t for (t, p) in _samples if t0 <= t <= t1]
    if len(xs) >= min_n and ts[-1] - ts[0] >= 0.75 * (t1 - t0):
        return sum(xs) / len(xs)
    return None


def price_to_beat(state: dict, ws: int):
    cache = state.setdefault("ptb", {})
    if str(ws) in cache:
        return cache[str(ws)]["p"], cache[str(ws)]["src"]
    own = own_twap(ws - 60, ws)
    if own is not None:
        p, src = own, "own_spot_samples_60s"
    else:
        p, src = feeds.candle_ptb(ws)
    cache[str(ws)] = {"p": p, "src": src}
    for k in list(cache):
        if int(k) < ws - 7200:
            del cache[k]
    return p, src


# ---------------------------------------------------------------- hour filter (Tweak 2)
_hour_cache: dict = {}


def hour_filter_blocks(variant: str) -> str | None:
    if not config.HOUR_FILTER:
        return None
    hr = datetime.now(PT).hour
    key = (variant, hr)
    c = _hour_cache.get(key)
    if c and time.time() - c[0] < 300:
        return c[1]
    n, pnl = 0, 0.0
    for r in store.read_trades(variant):
        if r.get("status") != "resolved" or not r.get("pnl_usd"):
            continue
        try:
            if datetime.strptime(r["ts_fill_pt"][:19], "%Y-%m-%d %H:%M:%S").hour == hr:
                n += 1
                pnl += float(r["pnl_usd"])
        except ValueError:
            continue
    msg = None
    if n >= config.HOUR_FILTER_MIN_TRADES and pnl < 0:
        msg = f"hour_filter: {n} past trades in the {hr}:00 PT hour net ${pnl:.2f}"
    _hour_cache[key] = (time.time(), msg)
    return msg


# ---------------------------------------------------------------- scale modes + loss cooldown (breakout_scale)
SCALE_MODES = ("scale", "breakout_scale", "trend_scale")


def cooldown_blocks(state: dict, name: str, v: dict, ws: int, now: float):
    """After a losing round, skip the next `cooldown_skips` qualifying setups AND at least `cooldown_min` minutes
    (whichever is longer). Called only when a setup already passed every entry filter. Returns the reason or None."""
    if not v.get("cooldown_skips") and not v.get("cooldown_min"):
        return None
    cd = state.setdefault("cooldown", {}).get(name)
    if not cd:
        return None
    left = int(cd.get("skips_left", 0))
    counted = cd.setdefault("counted_ws", [])
    if left > 0 and ws not in counted:
        counted.append(ws)
        cd["skips_left"] = left = left - 1  # this qualifying setup is one of the skips
    mins_left = (float(cd.get("until", 0)) - now) / 60.0
    if left <= 0 and mins_left <= 0:
        state["cooldown"].pop(name, None)
        log.info("COOLDOWN over for [%s]", name)
        return None
    return (f"Cooldown after a losing round: {left} more qualifying setup(s) to skip and "
            f"{max(mins_left, 0):.0f} more minute(s); skipping this setup.")


def start_cooldown(state: dict, name: str, v: dict, ws: int, now: float) -> None:
    if not v.get("cooldown_skips") and not v.get("cooldown_min"):
        return
    cds = state.setdefault("cooldown", {})
    if cds.get(name, {}).get("loss_ws") == ws:
        return  # both pieces of the same lost round
    cds[name] = {"skips_left": int(v.get("cooldown_skips", 2)), "until": now + 60 * float(v.get("cooldown_min", 15)),
                 "loss_ws": ws, "counted_ws": []}
    log.info("COOLDOWN started for [%s] after a losing round: skip next %s setups and at least %s min",
             name, v.get("cooldown_skips", 2), v.get("cooldown_min", 15))


# ---------------------------------------------------------------- meta variant (copies the recent leader)
def variant_scores(variants: dict, now: float, exclude: str, hours: float = 3.0, min_bets: int = 5) -> dict:
    """{variant: (return_fraction, n_bets, net_pnl)} over settled bets filled in the last `hours`. Excludes `exclude`."""
    out = {}
    cutoff = now - hours * 3600
    for n in variants:
        if n == exclude:
            continue
        pnl = cost = 0.0
        k = 0
        for r in store.read_trades(n):
            if r.get("status") != "resolved" or not r.get("pnl_usd"):
                continue
            try:
                t = datetime.strptime(r["ts_fill_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            except (ValueError, KeyError):
                continue
            if t >= cutoff:
                pnl += float(r["pnl_usd"]); cost += float(r["total_cost_usd"]); k += 1
        if k >= min_bets and cost > 0:
            out[n] = (pnl / cost, k, pnl)
    return out


def meta_update_leader(state: dict, name: str, scores: dict, ws: int) -> dict:
    """One check per 5-minute round. A new leader needs to be the best positive score at 2 straight checks."""
    m = state.setdefault("meta", {"leader": None, "cand": None, "cand_n": 0, "last_check_ws": 0})
    if m.get("last_check_ws") == ws:
        return m
    m["last_check_ws"] = ws
    pos = {n: sc for n, sc in scores.items() if sc[0] > 0}
    best = max(pos, key=lambda n: pos[n][0]) if pos else None
    if best is None:
        if m.get("leader") or m.get("cand"):
            log.info("META: nobody is positive over 3h; sitting out (was leader=%s)", m.get("leader"))
        m.update(leader=None, cand=None, cand_n=0)
    elif best == m.get("leader"):
        m.update(cand=None, cand_n=0)
    else:
        m["cand_n"] = int(m.get("cand_n", 0)) + 1 if m.get("cand") == best else 1
        m["cand"] = best
        if m["cand_n"] >= 2:
            log.info("META: new leader %s (3h return %+.1f%% on %d bets) after 2 straight checks; was %s",
                     best, pos[best][0] * 100, pos[best][1], m.get("leader"))
            m.update(leader=best, cand=None, cand_n=0)
    return m


_meta_cache: dict = {}


def meta_decide(state, name, v, variants, *, p_up, books, secs_left, trend, spot, ws, now):
    D = lambda action, reason, english, **kw: strategy.Decision(name, action, reason, english, model_p_up=p_up, **kw)  # noqa: E731
    c = _meta_cache.get(name)
    if c and c[0] == ws:  # scores only change when bets settle; compute once per round, not every 2s
        scores = c[1]
    else:
        scores = variant_scores(variants, now, exclude=name, hours=float(v.get("score_hours", 3)), min_bets=int(v.get("min_bets", 5)))
        _meta_cache[name] = (ws, scores)
    m = meta_update_leader(state, name, scores, ws)
    if not any(sc[0] > 0 for sc in scores.values()):
        return D("skip", "all_losing", "No other variant is positive over the last 3 hours (with 5+ settled bets); sitting out.")
    leader = m.get("leader")
    if not leader or leader not in scores or scores[leader][0] <= 0:
        why = f"waiting for a leader (candidate {m.get('cand')} has led {m.get('cand_n')} of 2 checks)" if m.get("cand") else "no stable leader yet"
        return D("skip", "no_leader", f"No confirmed leader: {why}.")
    ranked = sorted((n for n, sc in scores.items() if sc[0] > 0 and n != leader), key=lambda n: -scores[n][0])
    order = [leader] + (ranked[:1] if v.get("fallback_to_second", True) else [])
    notes = []
    for rank, src in enumerate(order, 1):
        ret, nb, pnl = scores[src]
        sv = variants.get(src)
        if sv is None:
            continue
        if hour_filter_blocks(src):
            notes.append(f"#{rank} {src}: its hour filter blocks"); continue
        dl = strategy.decide(src, sv, p_model=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot)
        if dl.action != "buy":
            notes.append(f"#{rank} {src} not betting ({dl.reason})"); continue
        stake = float(v.get("base_stake", 2)) + (1 if ret > 0.10 else 0) + (1 if ret > 0.20 else 0)
        v2 = dict(v, mode="favorite", base_stake=stake)
        st, sh, note = strategy.size_bet(v2, 0.0, dl.ask)
        if sh + 1e-9 < config.MIN_ORDER_SHARES:
            notes.append(f"#{rank} {src}: ${st:.2f} only buys {sh:.2f} shares (min 5)"); continue
        book = books.get(dl.side)
        if book is None or book.best_ask is None or book.best_ask_size + 1e-9 < sh:
            notes.append(f"#{rank} {src}: book too thin"); continue
        ask = float(book.best_ask); fps = strategy.fee_per_share(ask)
        notional, fee = sh * ask, sh * fps
        if notional + fee > min(float(v.get("max_loss_per_bet", 10)), config.STAKE_HARD_MAX_USD) + 1e-9:
            notes.append(f"#{rank} {src}: cost over max_loss_per_bet"); continue
        eng = (f"Copying #{rank} {src} (3h return {ret*100:+.1f}% on {nb} settled bets, net ${pnl:+.2f}): it would buy {dl.side} -- {dl.english} "
               f"Meta size ${notional:.2f}+${fee:.2f} fee (base $2, +$1 over 10%, +$1 over 20%, max $4)." + (f" ({note})" if note else ""))
        return strategy.Decision(name, "buy", "ok", eng, side=dl.side, est_prob=dl.est_prob, model_p_up=p_up, market_p_up=dl.market_p_up,
                                 ask=ask, ask_size=book.best_ask_size, fee_ps=fps, edge=dl.edge, stake=st, shares=sh, notional=notional,
                                 fee=fee, total_cost=notional + fee, extra={"copied": src, "note": note})
    return D("skip", "leader_idle", f"Leader {leader} ({scores[leader][0]*100:+.1f}% over 3h) isn't betting this round" + (": " + "; ".join(notes) if notes else "") + ".")


# ---------------------------------------------------------------- auto_pick variant (follows whichever 5m variant is hottest)
AUTO_SWITCH_CSV = config.DATA_DIR / "auto_pick_switches.csv"
COPY_MODES = frozenset({"auto_pick", "bankroll", "meta"})  # copiers are never ranking candidates
_auto_cache: dict = {}


def _settled_stats(name: str, since_ts):
    """(net_pnl, n_bets, dollars_bet) over bets SETTLED at/after since_ts (None = since start)."""
    pnl = cost = 0.0
    k = 0
    for r in store.read_trades(name):
        if r.get("status") != "resolved" or not r.get("pnl_usd"):
            continue
        if since_ts is not None:
            try:
                t = datetime.strptime(r.get("ts_resolve_utc") or r["ts_fill_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            except (ValueError, KeyError):
                continue
            if t < since_ts:
                continue
        pnl += float(r["pnl_usd"]); cost += float(r.get("total_cost_usd") or 0); k += 1
    return pnl, k, cost


def auto_scores(variants: dict, me: str, tf: str, now: float, hours: float, min_bets: int):
    """Rank same-timeframe variants (never itself) by net fake P&L on settled bets. Returns (scope, scores) where scores is
    {variant: (net_pnl, n_bets, dollars_bet)} for EVERY candidate; 'scope' is '6h' normally, or 'since_start' when nobody qualifies in the window."""
    cands = [n for n, v in variants.items() if n != me and v.get("timeframe", "5m") == tf and v.get("mode") not in COPY_MODES and not v.get("cash_out") and not v.get("exclude_from_rank") and v.get("mode") not in ("crowd", "max_profit", "ab_hedge")]
    def qual(sc):
        return {n: s for n, s in sc.items() if s[1] >= min_bets and s[0] > 0}
    win = {n: _settled_stats(n, now - hours * 3600) for n in cands}
    if qual(win):
        return f"{hours:g}h", win
    return "since_start", {n: _settled_stats(n, None) for n in cands}


def auto_update_leader(state: dict, scores: dict, scope: str, ws: int, min_bets: int, now: float) -> dict:
    """One check per round. First leader, or a replacement when the old leader stops qualifying, is taken at once; a switch
    between two qualifying variants needs the new one to be best at 2 straight checks (no flapping on one lucky bet)."""
    a = state.setdefault("auto_pick", {"leader": None, "cand": None, "cand_n": 0, "last_check_ws": 0, "history": []})
    if a.get("last_check_ws") == ws:
        return a
    a["last_check_ws"] = ws
    pos = {n: s for n, s in scores.items() if s[1] >= min_bets and s[0] > 0}
    ranked = sorted(pos, key=lambda n: -pos[n][0])
    table = ", ".join(f"{n} {s[0]:+.2f}/{s[1]}" for n, s in sorted(scores.items(), key=lambda t: -t[1][0]))
    a["scope"], a["scores"] = scope, {n: [round(s[0], 2), s[1]] for n, s in scores.items()}
    old = a.get("leader")
    new, why = old, ""
    if not ranked:
        new, why = None, "nobody has 5+ settled bets and positive P&L"
        a.update(cand=None, cand_n=0)
    elif old not in pos:
        new, why = ranked[0], ("first pick" if old is None else f"{old} no longer qualifies")
        a.update(cand=None, cand_n=0)
    elif ranked[0] == old:
        a.update(cand=None, cand_n=0)
    else:
        best = ranked[0]
        a["cand_n"] = int(a.get("cand_n", 0)) + 1 if a.get("cand") == best else 1
        a["cand"] = best
        if a["cand_n"] >= 2:
            new, why = best, "best at 2 straight checks"
            a.update(cand=None, cand_n=0)
    log.info("AUTO_PICK CHECK scope=%s leader=%s | net P&L/bets: %s", scope, new, table)
    if new != old:
        ru = [n for n in ranked if n != new][:1]
        msg = (f"AUTO_PICK SWITCH {old} -> {new} ({why}); scope={scope}; "
               + (f"{new} {pos[new][0]:+.2f} on {pos[new][1]} bets" if new else "sitting out")
               + (f"; runner-up {ru[0]} {pos[ru[0]][0]:+.2f} on {pos[ru[0]][1]} bets" if ru else "") + f"; all: {table}")
        log.info(msg)
        a["history"] = (a.get("history", []) + [{"ts_pt": store.iso_pt(now), "from": old, "to": new, "scope": scope, "why": why, "scores": a["scores"]}])[-50:]
        try:
            AUTO_SWITCH_CSV.parent.mkdir(parents=True, exist_ok=True)
            fresh = not AUTO_SWITCH_CSV.exists()
            with AUTO_SWITCH_CSV.open("a", newline="") as f:
                import csv as _csv
                w = _csv.writer(f)
                if fresh:
                    w.writerow(["ts_pt", "from", "to", "scope", "why", "scores_net_pnl_bets"])
                w.writerow([store.iso_pt(now), old or "", new or "", scope, why, table])
        except Exception:  # noqa: BLE001
            log.exception("could not write auto_pick switch csv")
        a["leader"] = new
    return a


def auto_decide(state, name, v, variants, *, p_up, books, secs_left, trend, spot, ptb, ws, now):
    D = lambda action, reason, english, **kw: strategy.Decision(name, action, reason, english, model_p_up=p_up, **kw)  # noqa: E731
    tf = v.get("timeframe", "5m")
    min_bets = int(v.get("min_bets", 5))
    c = _auto_cache.get(name)
    if c and c[0] == ws:  # scores only change when bets settle; compute once per round, not every 2s
        scope, scores = c[1], c[2]
    else:
        scope, scores = auto_scores(variants, name, tf, now, float(v.get("score_hours", 6)), min_bets)
        _auto_cache[name] = (ws, scope, scores)
    a = auto_update_leader(state, scores, scope, ws, min_bets, now)
    pos = {n: s for n, s in scores.items() if s[1] >= min_bets and s[0] > 0}
    leader = a.get("leader")
    if not pos or not leader or leader not in pos:
        return D("skip", "nobody_qualifies", f"No {tf} variant has {min_bets}+ settled bets and positive P&L ({scope}); sitting out.")
    order = [leader]   # follow ONLY the current pick; if it doesn't bet this window, sit out (no fallback to other qualifiers)
    notes = []
    for rank, src in enumerate(order, 1):
        pnl, nb, _cost = pos[src]
        sv = variants.get(src)
        if sv is None:
            continue
        dl = strategy.decide(src, sv, p_model=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot, ptb=ptb)   # ptb passed so lead gates really apply
        if dl.action != "buy":
            notes.append(f"#{rank} {src} not betting ({dl.reason})"); continue
        v2 = dict(v, mode="favorite", base_stake=float(v.get("base_stake", 3)))
        st, sh, note = strategy.size_bet(v2, 0.0, dl.ask)
        if sh + 1e-9 < config.MIN_ORDER_SHARES:
            notes.append(f"#{rank} {src}: ${st:.2f} only buys {sh:.2f} shares (min 5)"); continue
        book = books.get(dl.side)
        if book is None or book.best_ask is None or book.best_ask_size + 1e-9 < sh:
            notes.append(f"#{rank} {src}: book too thin"); continue
        ask = float(book.best_ask); fps = strategy.fee_per_share(ask)
        notional, fee = sh * ask, sh * fps
        cap = min(float(v.get("max_loss_per_bet", 5)), config.STAKE_HARD_MAX_USD)
        if notional + fee > cap + 1e-9:
            notes.append(f"#{rank} {src}: cost ${notional+fee:.2f} over ${cap:.0f} cap"); continue
        eng = (f"Following the pick {src} ({scope} net ${pnl:+.2f} on {nb} settled bets): it would buy {dl.side} -- {dl.english} "
               f"auto_pick size ${notional:.2f}+${fee:.2f} fee (flat $3 target incl. fee)." + (f" ({note})" if note else ""))
        return strategy.Decision(name, "buy", "ok", eng, side=dl.side, est_prob=dl.est_prob, model_p_up=p_up, market_p_up=dl.market_p_up,
                                 ask=ask, ask_size=book.best_ask_size, fee_ps=fps, edge=dl.edge, stake=st, shares=sh, notional=notional,
                                 fee=fee, total_cost=notional + fee, extra={"copied": src, "note": note})
    return D("skip", "leader_idle", f"Current pick {leader} ({scope} {pos[leader][0]:+.2f}) isn't betting this round; sitting out" + (": " + "; ".join(notes) if notes else "") + ".")



# ---------------------------------------------------------------- bankroll_20 (fake $20 start, $1,000 goal; follows auto_pick's leader)
BANKROLL_CSV = config.DATA_DIR / "bankroll_history.csv"
FLOOR_ASK = 0.05  # cheapest ask we treat as a real minimum-order quote when judging "busted"


def _min_order_cost(ask: float) -> float:
    """Cost of Polymarket's 5-share minimum at this ask, including the taker fee."""
    return config.MIN_ORDER_SHARES * (float(ask) + strategy.fee_per_share(ask))


def bankroll_size(available: float, ask: float, frac: float) -> tuple | None:
    """Size a bankroll bet. Returns (stake/notional, shares, fee, total_cost) or None if unaffordable.

    Target is `frac` of cash on hand (default 20%), but always at least the 5-share minimum,
    never more than the $10 hard max including fee, and never more than cash on hand.
    """
    import math
    if ask <= 0 or available <= 0 or frac <= 0:
        return None
    hard = min(float(config.STAKE_HARD_MAX_USD), float(available))
    min_cost = _min_order_cost(ask)
    if min_cost > hard + 1e-9:
        return None
    budget = min(max(float(frac) * float(available), min_cost), hard)
    fps = strategy.fee_per_share(ask)
    cost_ps = float(ask) + fps
    shares = math.floor(budget / cost_ps * 100) / 100
    if shares + 1e-9 < config.MIN_ORDER_SHARES:
        shares = float(config.MIN_ORDER_SHARES)
    notional = shares * float(ask)
    fee = shares * fps
    total = notional + fee
    if total > hard + 1e-9 or shares + 1e-9 < config.MIN_ORDER_SHARES:
        return None
    return notional, shares, fee, total


def bankroll_log(event: str, *, balance: float, equity: float, delta: float = 0.0,
                 trade_id: str = "", following: str = "", note: str = "", now: float | None = None) -> None:
    import csv as _csv
    BANKROLL_CSV.parent.mkdir(parents=True, exist_ok=True)
    fresh = not BANKROLL_CSV.exists()
    try:
        with BANKROLL_CSV.open("a", newline="") as f:
            w = _csv.writer(f)
            if fresh:
                w.writerow(["ts_pt", "event", "balance", "equity", "delta", "trade_id", "following", "note"])
            w.writerow([store.iso_pt(now), event, f"{balance:.4f}", f"{equity:.4f}", f"{delta:.4f}",
                        trade_id, following, note])
    except Exception:  # noqa: BLE001
        log.exception("could not write bankroll history csv")


def bankroll_ensure(state: dict, v: dict, name: str) -> dict:
    """Create the fake-money bankroll account once, then return it. Never resets an existing balance."""
    start = float(v.get("start_balance", 20))
    goal = float(v.get("goal_balance", 1000))
    b = state.setdefault("bankroll", {})
    if not b.get("funded"):
        now = time.time()
        b.update({
            "variant": name, "start": start, "goal": goal, "balance": round(start, 4),
            "equity": round(start, 4), "busted": False, "busted_at": None,
            "goal_reached": False, "goal_reached_at": None, "n_bets": 0, "n_settled": 0,
            "following": None, "funded": True,
        })
        bankroll_log("fund", balance=start, equity=start, note=f"starting fake-money balance; goal ${goal:,.0f}", now=now)
        log.info("BANKROLL funded $%.2f fake money, goal $%.2f (variant=%s)", start, goal, name)
    return b


def bankroll_recompute(state: dict, name: str) -> dict:
    b = state.get("bankroll") or bankroll_ensure(state, {}, name)
    start = float(b.get("start", 20))
    settled_pnl = open_cost = 0.0
    n = n_settled = 0
    csv_open = set()
    for r in store.read_trades(name):
        n += 1
        if r.get("status") == "resolved" and r.get("pnl_usd") not in (None, ""):
            settled_pnl += float(r["pnl_usd"])
            n_settled += 1
        elif r.get("status") == "open":
            open_cost += float(r.get("total_cost_usd") or 0)
            csv_open.add(r.get("trade_id"))
    resolved_ids = {r.get("trade_id") for r in store.read_trades(name) if r.get("status") == "resolved"}
    for item in state.get("open", []):
        if item.get("variant") == name and item.get("trade_id") not in csv_open and item.get("trade_id") not in resolved_ids:
            open_cost += float(item["total_cost"])
    b["equity"] = round(start + settled_pnl, 4)
    b["balance"] = round(start + settled_pnl - open_cost, 4)
    b["n_bets"] = n
    b["n_settled"] = n_settled
    return b


def bankroll_check_flags(state: dict, name: str, now: float | None = None) -> dict:
    """Mark goal-reached or busted after a settlement (or on a skip). Open bets can still change the outcome."""
    b = bankroll_recompute(state, name)
    now = time.time() if now is None else now
    open_n = sum(1 for item in state.get("open", []) if item.get("variant") == name)
    if (not b.get("goal_reached")) and b["equity"] + 1e-9 >= float(b.get("goal", 1000)) and open_n == 0:
        b["goal_reached"] = True
        b["goal_reached_at"] = store.iso_pt(now)
        bankroll_log("goal", balance=b["balance"], equity=b["equity"], following=b.get("following") or "",
                     note=f"goal ${float(b['goal']):,.0f} reached; no new bets", now=now)
        log.info("BANKROLL GOAL REACHED equity=$%.2f (start $%.2f, goal $%.2f)", b["equity"], b["start"], b["goal"])
    floor = _min_order_cost(FLOOR_ASK)
    if (not b.get("busted")) and (not b.get("goal_reached")) and open_n == 0 and b["balance"] + 1e-9 < floor:
        b["busted"] = True
        b["busted_at"] = store.iso_pt(now)
        bankroll_log("busted", balance=b["balance"], equity=b["equity"], following=b.get("following") or "",
                     note=f"cash ${b['balance']:.2f} cannot cover a 5-share minimum (floor ${floor:.2f})", now=now)
        log.info("BANKROLL BUSTED balance=$%.2f (cannot cover a minimum order); stopping new bets", b["balance"])
    return b


def bankroll_on_fill(state: dict, name: str, trade_id: str, total_cost: float, copied: str = "") -> None:
    b = bankroll_recompute(state, name)
    if copied:
        b["following"] = copied
    bankroll_log("fill", balance=b["balance"], equity=b["equity"], delta=-float(total_cost),
                 trade_id=trade_id, following=b.get("following") or copied,
                 note=f"placed bet costing ${float(total_cost):.2f} incl. fee")
    log.info("BANKROLL FILL %s cost=$%.2f cash=$%.2f equity=$%.2f following=%s",
             trade_id, total_cost, b["balance"], b["equity"], b.get("following"))


def bankroll_on_resolve(state: dict, name: str, trade_id: str, pnl: float, payout: float) -> None:
    b = bankroll_recompute(state, name)
    bankroll_log("settle", balance=b["balance"], equity=b["equity"], delta=float(payout),
                 trade_id=trade_id, following=b.get("following") or "",
                 note=f"settled pnl ${pnl:+.2f}, payout ${payout:.2f}")
    log.info("BANKROLL SETTLE %s pnl=$%+.2f cash=$%.2f equity=$%.2f", trade_id, pnl, b["balance"], b["equity"])
    bankroll_check_flags(state, name)


def bankroll_decide(state, name, v, variants, *, p_up, books, secs_left, trend, spot, ptb, ws, now, breaker_on=None):
    """Follow the same pick as auto_pick, but size the bet from this variant's own $20 fake bankroll."""
    D = lambda action, reason, english, **kw: strategy.Decision(name, action, reason, english, model_p_up=p_up, **kw)  # noqa: E731
    b = bankroll_ensure(state, v, name)
    bankroll_recompute(state, name)
    if b.get("busted"):
        return D("skip", "busted", f"Bankroll busted at {b.get('busted_at')}: ${b['balance']:.2f} left, not enough for a minimum bet. Sitting out.")
    if b.get("goal_reached"):
        return D("skip", "goal_reached", f"Bankroll hit its ${float(b['goal']):,.0f} goal at {b.get('goal_reached_at')} (now ${b['equity']:.2f}). No new bets.")
    tf = v.get("timeframe", "5m")
    min_bets = int(v.get("min_bets", 5))
    c = _auto_cache.get(name)
    if c and c[0] == ws:
        scope, scores = c[1], c[2]
    else:
        scope, scores = auto_scores(variants, name, tf, now, float(v.get("score_hours", 6)), min_bets)
        _auto_cache[name] = (ws, scope, scores)
    a = auto_update_leader(state, scores, scope, ws, min_bets, now)
    pos = {n: s for n, s in scores.items() if s[1] >= min_bets and s[0] > 0}
    leader = a.get("leader")
    b["following"] = leader
    if not pos or not leader or leader not in pos:
        return D("skip", "nobody_qualifies", f"No {tf} variant has {min_bets}+ settled bets and positive P&L ({scope}); bankroll sitting out.")
    sv = variants.get(leader)
    if sv is None:
        return D("skip", "leader_missing", f"Current pick {leader} is not in the active set; sitting out.")
    if breaker_on and (sv.get("circuit_breaker") or "watch") == "block":
        return D("skip", "leader_blocked", f"Current pick {leader} is paused by the jumpy-market safety switch; sitting out.")
    dl = strategy.decide(leader, sv, p_model=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot, ptb=ptb)
    if dl.action != "buy":
        return D("skip", "leader_idle", f"Current pick {leader} ({scope} {pos[leader][0]:+.2f}) isn't betting this round ({dl.reason}); sitting out.")
    book = books.get(dl.side)
    if book is None or book.best_ask is None:
        return D("skip", "no_book", f"Current pick {leader} would buy {dl.side} but there is no ask on the book.")
    ask = float(book.best_ask)
    sized = bankroll_size(b["balance"], ask, float(v.get("stake_frac", 0.20)))
    if sized is None:
        bankroll_check_flags(state, name, now)
        b = state["bankroll"]
        if b.get("busted"):
            return D("skip", "busted", f"Bankroll busted: ${b['balance']:.2f} left, not enough for a minimum bet. Sitting out.")
        need = _min_order_cost(ask)
        return D("skip", "bankroll_short", f"Bankroll has ${b['balance']:.2f} cash, but 5 shares of {dl.side} at {ask*100:.0f}c would cost ${need:.2f} incl. fee; sitting this one out.")
    notional, sh, fee, total = sized
    if book.best_ask_size + 1e-9 < sh:
        return D("skip", "book_thin", f"Current pick {leader} would buy {dl.side}, but the book only has {book.best_ask_size:.2f} shares (need {sh:.2f}).")
    fps = strategy.fee_per_share(ask)
    pnl, nb, _cost = pos[leader]
    eng = (f"Bankroll following {leader} ({scope} net ${pnl:+.2f} on {nb} settled bets): it would buy {dl.side} -- {dl.english} "
           f"bankroll size ${notional:.2f}+${fee:.2f} fee ({float(v.get('stake_frac', 0.20)):.0%} of ${b['balance']:.2f} cash, "
           f"5-share min, cap $10 incl. fee). Cash after fill ${b['balance']-total:.2f}.")
    return strategy.Decision(name, "buy", "ok", eng, side=dl.side, est_prob=dl.est_prob, model_p_up=p_up,
                             market_p_up=dl.market_p_up, ask=ask, ask_size=book.best_ask_size, fee_ps=fps,
                             edge=dl.edge, stake=notional, shares=sh, notional=notional, fee=fee, total_cost=total,
                             extra={"copied": leader})



# ---------------------------------------------------------------- resolution
_last_resolve = [0.0]


def resolve_open(state: dict, force: bool = False) -> None:
    if not force and time.time() - _last_resolve[0] < 15:
        return
    _last_resolve[0] = time.time()
    still = []
    by_window: dict = {}
    for item in state.get("open", []):
        ws = int(item["window_start"])
        tf = item.get("tf", "5m")
        if time.time() < ws + polymarket.window_seconds(tf) + 5:
            still.append(item)
            continue
        if (tf, ws) not in by_window:
            try:
                by_window[(tf, ws)] = polymarket.fetch_event(ws, tf)
            except Exception as exc:  # noqa: BLE001
                log.warning("resolve fetch failed for %s %s: %s", tf, ws, exc)
                by_window[(tf, ws)] = None
        m = by_window[(tf, ws)]
        outcome = m.resolved_outcome if m else None
        if outcome is None:
            still.append(item)
            continue
        won = outcome == item["side"]
        pnl = (float(item["shares"]) if won else 0.0) - float(item["total_cost"])
        prior = sum(float(r["pnl_usd"]) for r in store.read_trades(item["variant"]) if r.get("status") == "resolved" and r.get("pnl_usd"))
        store.update_trade(item["variant"], item["trade_id"], {
            "status": "resolved", "ts_resolve_utc": store.iso_utc(), "outcome": outcome, "won": "1" if won else "0",
            "payout_usd": f"{float(item['shares']) if won else 0.0:.4f}", "pnl_usd": f"{pnl:.6f}",
            "cumulative_pnl_usd": f"{prior + pnl:.6f}",
            "official_ptb": (m.meta or {}).get("priceToBeat", ""), "official_final": (m.meta or {}).get("finalPrice", ""),
        })
        if not won:
            vcfg = (_variants.get("data") or {}).get(item["variant"]) or {}
            if vcfg.get("mode") in SCALE_MODES + ("meta",) and (vcfg.get("cooldown_skips") or vcfg.get("cooldown_min")):
                start_cooldown(state, item["variant"], vcfg, ws, time.time())
        log.info("RESOLVED [%s] %s %s outcome=%s %s pnl=$%+.2f variant_total=$%+.2f",
                 item["variant"], item["trade_id"], item["side"], outcome, "WON" if won else "LOST", pnl, prior + pnl)
        if item["variant"] == (state.get("bankroll") or {}).get("variant") or ((_variants.get("data") or {}).get(item["variant"]) or {}).get("mode") == "bankroll":
            payout = float(item["shares"]) if won else 0.0
            bankroll_on_resolve(state, item["variant"], item["trade_id"], pnl, payout)
        if item["variant"] == (state.get("max_profit") or {}).get("variant") or ((_variants.get("data") or {}).get(item["variant"]) or {}).get("mode") == "max_profit":
            payout = float(item["shares"]) if won else 0.0
            maxprofit.on_resolve(state, item["variant"], item["trade_id"], pnl, payout)
    state["open"] = still
    try:
        ab_hedge.after_resolve(state)
    except Exception:  # noqa: BLE001
        log.exception("ab_hedge resolve log failed")


# ---------------------------------------------------------------- main cycle
_mkt_cache: dict = {}
_last_decision_log: dict = {}
_reason_seen: set = set()
_stats = collections.Counter()


def get_market(ws: int, tf: str = "5m"):
    key = (tf, ws)
    if key in _mkt_cache:
        return _mkt_cache[key]
    m = polymarket.fetch_event(ws, tf)
    if m is not None and m.token_ids:
        for k in [k for k in _mkt_cache if k[0] == tf]:
            del _mkt_cache[k]
        _mkt_cache[key] = m
    return m


def paper_fill(state, name, d, m, ws, secs_left, spot, ptb, ptb_src, vol, tf="5m"):
    hard = config.STAKE_HARD_MAX_USD
    if (d.extra or {}).get("allow_over_hard_max"):
        hard = float((d.extra or {}).get("hard_max") or 0)
        if hard <= 0:
            hard = d.total_cost
    if d.stake > hard + 1e-9 or d.total_cost > hard + 1e-9:
        log.error("REFUSED [%s] stake $%.2f above cap $%.2f", name, d.stake, hard)
        return
    state["seq"] = int(state.get("seq", 0)) + 1
    tid = f"W{state['seq']:06d}"
    now = time.time()
    store.append_trade(name, {
        "trade_id": tid, "variant": name, "timeframe": tf, "mode": "paper", "status": "open", "ts_fill_utc": store.iso_utc(now),
        "ts_fill_pt": store.iso_pt(now), "window_start": ws, "slug": m.slug, "title": m.title, "side": d.side,
        "ask": f"{d.ask:.4f}", "stake": f"{d.stake:.2f}", "shares": f"{d.shares:.2f}", "notional_usd": f"{d.notional:.4f}",
        "fee_usd": f"{d.fee:.6f}", "total_cost_usd": f"{d.total_cost:.6f}",
        "est_prob": "" if d.est_prob is None else f"{d.est_prob:.4f}", "model_p_up": f"{d.model_p_up:.4f}",
        "market_p_up": "" if d.market_p_up is None else f"{d.market_p_up:.4f}",
        "edge": "" if d.edge is None else f"{d.edge:.4f}", "spot": f"{spot:.4f}", "price_to_beat": f"{ptb:.4f}",
        "ptb_source": ptb_src, "seconds_left": f"{secs_left:.1f}", "annual_vol": f"{vol:.4f}", "reason": d.english,
    })
    state.setdefault("open", []).append({"trade_id": tid, "variant": name, "window_start": ws, "tf": tf, "side": d.side,
                                         "shares": d.shares, "total_cost": d.total_cost, "ts_fill": now})
    tw_key = f"{tf}:{ws}" if ((_variants.get("data") or {}).get(name) or {}).get("mode") == "max_profit" else ws
    state.setdefault("traded_windows", {}).setdefault(name, []).append(tw_key)
    state["traded_windows"][name] = state["traded_windows"][name][-80:]
    log.info("PAPER FILL [%s] %s %s %s $%.2f = %.2f sh @ %.2f (fee $%.3f) %.0fs left", name, tf, tid, d.side, d.notional, d.shares, d.ask, d.fee, secs_left)
    _reason(f"[{name}] {d.english}")
    _stats["fills"] += 1
    if name == (state.get("bankroll") or {}).get("variant") or ((_variants.get("data") or {}).get(name) or {}).get("mode") == "bankroll":
        bankroll_on_fill(state, name, tid, d.total_cost, (d.extra or {}).get("copied") or "")
    if name == (state.get("max_profit") or {}).get("variant") or ((_variants.get("data") or {}).get(name) or {}).get("mode") == "max_profit":
        maxprofit.on_fill(state, name, tid, d.total_cost, (d.extra or {}).get("signal") or "")
    if ((_variants.get("data") or {}).get(name) or {}).get("mode") == "ab_hedge":
        ab_hedge.on_fill(state, name, tid, d, tf=tf, ws=ws, slug=m.slug)


def cycle(state: dict, accepting: bool) -> None:
    now = time.time()
    try:
        spot, readings = feeds.current_spot()
        _samples.append((now, spot))
        _stats["spot_ok"] += 1
    except Exception as exc:  # noqa: BLE001
        _stats["spot_fail"] += 1
        log.warning("spot fetch failed: %s", str(exc)[:300])
        resolve_open(state)
        return
    resolve_open(state)
    try:
        cashout.manage_open(state, load_variants(), spot=spot, now=now,
                            price_to_beat_fn=price_to_beat, get_market_fn=get_market)
        ab_hedge.manage_exits(state, load_variants(), spot=spot, now=now,
                              price_to_beat_fn=price_to_beat, get_market_fn=get_market)
        maxprofit.sync(state)
    except Exception:  # noqa: BLE001
        _stats["errors"] += 1
        log.exception("cash-out check failed (continuing)")
    if not accepting:
        return
    variants = load_variants()
    for tf in sorted({t for v in variants.values() for t in (list(v.get("timeframes") or []) or [v.get("timeframe", "5m")])}):
        try:
            cycle_tf(state, tf, variants, spot, now)
        except Exception:  # noqa: BLE001 - one timeframe failing must not block the other
            _stats["errors"] += 1
            log.exception("cycle error in %s family (continuing)", tf)


def cycle_tf(state: dict, tf: str, variants: dict, spot: float, now: float) -> None:
    wsec = polymarket.window_seconds(tf)
    ws = polymarket.window_start_ts(now, tf)
    elapsed = now - ws
    secs_left = wsec - elapsed
    skip_s = 0
    if config.SKIP_FIRST_MINUTE:
        skip_s = 60 if tf == "5m" else (180 if tf == "15m" else 0)
    if skip_s and elapsed < skip_s:
        key = ("*", ws, "first_minute", tf)
        if key not in _reason_seen:
            _reason_seen.add(key)
            _reason(f"[all {tf}] Skipping: Tweak 1 SKIP_FIRST_MINUTE is on and this window is in its first {skip_s} seconds.")
        return
    traded = state.setdefault("traded_windows", {})
    spos = state.setdefault("scale_pos", {})

    def _scale_open(n):  # scale_in may add to its own position later in the same round
        pos = spos.get(n)
        return bool(pos) and int(pos.get("ws", 0)) == ws

    def _wanted(n, v):
        tfs = list(v.get("timeframes") or [v.get("timeframe", "5m")]) if v.get("mode") == "max_profit" else [v.get("timeframe", "5m")]
        if tf not in tfs:
            return False
        if v.get("mode") == "ab_hedge":
            return ab_hedge.wanted(state, n, v, tf, ws, secs_left, wsec)
        already = ws in traded.get(n, []) or f"{tf}:{ws}" in traded.get(n, [])
        if already and not (v.get("mode") in SCALE_MODES and _scale_open(n)):
            return False
        if v.get("mode") == "max_profit":
            return True
        return float(v.get("min_secs_left", 0)) - 3 <= secs_left <= float(v.get("max_secs_left", wsec)) + 3
    todo = {n: v for n, v in variants.items() if _wanted(n, v)}
    has_rest = maxprofit.has_rest(state, tf, ws)
    if not todo and not has_rest:
        return
    m = get_market(ws, tf)
    if m is None or not m.token_ids:
        log.info("no market yet for %s", polymarket.slug_for(ws, tf))
        return
    books = polymarket.fetch_books(m)
    vol = feeds.realized_annual_vol()
    ptb, ptb_src = price_to_beat(state, ws)
    try:
        maxprofit.manage_rest(state, variants, tf=tf, ws=ws, books=books, secs_left=secs_left,
                              m=m, spot=spot, ptb=ptb, ptb_src=ptb_src, vol=vol, now=now, paper_fill=paper_fill)
    except Exception:  # noqa: BLE001
        _stats["errors"] += 1
        log.exception("max_profit rest check failed (continuing)")
    if not todo:
        return
    # If a resting limit just filled, drop max_profit from this round so it cannot also take the ask.
    todo = {n: v for n, v in todo.items()
            if not (v.get("mode") == "max_profit" and (ws in traded.get(n, []) or f"{tf}:{ws}" in traded.get(n, [])))}
    obs = own_twap(ws + wsec - 60, now, min_n=3) if secs_left <= 60 else None
    p_up = strategy.p_up_model(spot, ptb, secs_left, vol, obs)
    _stats["evals"] += 1
    trend = feeds.trend_signals(tf)  # cached 30s, one candle call; never raises
    tk = ("*", ws, "trend", tf)
    if tk not in _reason_seen:  # record the trend read once per window for every variant to compare later
        _reason_seen.add(tk)
        log.info("TREND %s window=%s spot=%.4f ptb=%.4f %s", tf, ws, spot, ptb, feeds.trend_summary(trend))
    for name, v in todo.items():
        if v.get("mode") == "max_profit" or (v.get("mode") == "ab_hedge" and ab_hedge.has_open_a(state, name, tf, ws)):
            block = None
        else:
            block = hour_filter_blocks(name)
        try:
            if block:
                d = strategy.Decision(name, "skip", "hour_filter", f"Tweak 2 HOUR_FILTER: {block}.", model_p_up=p_up)
            else:
                if v.get("mode") == "meta":
                    d = meta_decide(state, name, v, variants, p_up=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot, ws=ws, now=now)
                    if d.action == "buy":
                        cd = cooldown_blocks(state, name, v, ws, now)
                        if cd:
                            d = strategy.Decision(name, "skip", "cooldown", cd + f" (would have copied {d.extra.get('copied')} {d.side})", side=d.side, model_p_up=p_up)
                elif v.get("mode") == "auto_pick":
                    d = auto_decide(state, name, v, variants, p_up=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot, ptb=ptb, ws=ws, now=now)
                elif v.get("mode") == "bankroll":
                    d = bankroll_decide(state, name, v, variants, p_up=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot, ptb=ptb, ws=ws, now=now)
                elif v.get("mode") == "crowd":
                    d = crowd.decide(name, v, m=m, books=books, secs_left=secs_left, ws=ws, now=now, state=state, p_up=p_up)
                elif v.get("mode") == "max_profit":
                    d = maxprofit.decide(state, name, v, variants, p_up=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot, ptb=ptb, ws=ws, now=now, tf=tf, m=m)
                elif v.get("mode") == "ab_hedge":
                    d = ab_hedge.decide(name, v, state=state, books=books, secs_left=secs_left, spot=spot, ptb=ptb, ws=ws, now=now, tf=tf, m=m, p_up=p_up)
                elif v.get("mode") in SCALE_MODES and _scale_open(name):
                    d = strategy.decide_scale_add(name, v, spos[name], books=books, secs_left=secs_left, spot=spot, ptb=ptb, now=now)
                    d.model_p_up = p_up
                else:
                    d = strategy.decide(name, v, p_model=p_up, books=books, secs_left=secs_left, trend=trend, spot=spot, ptb=ptb)
        except Exception:  # noqa: BLE001 - one variant must never kill the rest of this family
            _stats["errors"] += 1
            log.exception("variant %s error in %s family (continuing)", name, tf)
            continue
        if d.action == "buy" and v.get("mode") in SCALE_MODES:
            pos = spos.get(name) if _scale_open(name) else None
            spent = sum(float(f["cost"]) for f in pos["fills"]) if pos else 0.0
            rcap = min(float(v.get("round_cap", config.STAKE_HARD_MAX_USD)), float(v.get("max_loss_per_bet", config.STAKE_HARD_MAX_USD)), config.STAKE_HARD_MAX_USD)
            cd = cooldown_blocks(state, name, v, ws, now) if pos is None else None
            if cd:
                d = strategy.Decision(name, "skip", "cooldown", cd, side=d.side, model_p_up=p_up)
            elif spent + d.total_cost > rcap + 1e-9:
                log.error("REFUSED [%s] round total $%.2f would exceed $%.0f", name, spent + d.total_cost, rcap)
                d = strategy.Decision(name, "skip", "round_cap", f"Round total would exceed ${rcap:.0f}.", model_p_up=p_up)
            else:
                if pos is None:
                    pos = spos[name] = {"ws": ws, "side": d.side, "fills": []}
                gap = (spot - ptb) if d.side == "Up" else (ptb - spot)
                pos["fills"].append({"ts": now, "ask": d.ask, "gap": gap, "cost": d.total_cost})
        if d.action == "buy":
            paper_fill(state, name, d, m, ws, secs_left, spot, ptb, ptb_src, vol, tf)
        else:
            rk = (name, ws, d.reason)
            if rk not in _reason_seen:
                _reason_seen.add(rk)
                _reason(f"[{name}] Not betting: {d.english}")
        if d.action == "buy" or now - _last_decision_log.get(name, 0) >= config.DECISION_LOG_EVERY_SEC:
            _last_decision_log[name] = now
            f = lambda x, n=4: "" if x is None else f"{x:.{n}f}"  # noqa: E731
            store.append_decision({"ts_pt": store.iso_pt(now), "variant": name, "timeframe": tf, "window_start": ws, "secs_left": f(secs_left, 1),
                                   "action": d.action, "reason": d.reason, "side": d.side or "", "est_prob": f(d.est_prob),
                                   "model_p_up": f(p_up), "market_p_up": f(d.market_p_up), "ask": f(d.ask), "edge": f(d.edge),
                                   "stake": f"{d.stake:.2f}", "shares": f"{d.shares:.2f}", "spot": f"{spot:.4f}", "price_to_beat": f"{ptb:.4f}",
                                   "english": d.english[:240]})
    if len(_reason_seen) > 5000:
        _reason_seen.clear()


def heartbeat(state: dict, cycles: int, force_log: bool, last_log: list) -> None:
    now = time.time()
    hb = {"ts": now, "ts_pt": store.iso_pt(now), "pid": os.getpid(), "cycles": cycles, "open": len(state.get("open", [])),
          "last_spot": _samples[-1][1] if _samples else None, "stats": dict(_stats)}
    ap_ = state.get("auto_pick")
    if ap_:
        hb["auto_pick"] = {"leader": ap_.get("leader"), "scope": ap_.get("scope"), "last_check_ws": ap_.get("last_check_ws"), "scores": ap_.get("scores")}
    br_ = state.get("bankroll")
    if br_:
        hb["bankroll"] = {k: br_.get(k) for k in ("variant", "start", "goal", "balance", "equity", "busted", "goal_reached", "n_bets", "n_settled", "following")}
    mp_ = state.get("max_profit")
    if mp_:
        hb["max_profit"] = {k: mp_.get(k) for k in ("variant", "start", "floor", "balance", "equity", "paused", "n_bets", "n_settled", "last_signal")}
    if state.get("crowd_stats"):
        hb["crowd_stats"] = dict(state["crowd_stats"])
    tmp = config.HEARTBEAT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(hb))
    os.replace(tmp, config.HEARTBEAT_PATH)
    if force_log or now - last_log[0] >= config.HEARTBEAT_EVERY_SEC:
        last_log[0] = now
        log.info("HEARTBEAT pid=%s cycles=%d spot=%s open_bets=%d fills=%d spot_ok=%d spot_fail=%d errors=%d",
                 os.getpid(), cycles, hb["last_spot"], hb["open"], _stats["fills"], _stats["spot_ok"], _stats["spot_fail"], _stats["errors"])


COV_PATH = config.DATA_DIR / "coverage.json"


def _load_cov() -> dict:
    try:
        c = json.loads(COV_PATH.read_text())
        return {"awake_sec": float(c.get("awake_sec", 0)), "asleep_sec": float(c.get("asleep_sec", 0)), "sleeps": int(c.get("sleeps", 0))}
    except Exception:  # noqa: BLE001
        return {"awake_sec": 0.0, "asleep_sec": 0.0, "sleeps": 0}


def _save_cov(c: dict) -> None:
    tmp = COV_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(c))
    os.replace(tmp, COV_PATH)


def main() -> int:
    ap = argparse.ArgumentParser(description="PAPER-ONLY XRP Up/Down bot (5m + 1h)")
    ap.add_argument("--mode", default="paper", help="only 'paper' is accepted")
    ap.add_argument("--until", default=config.RUN_UNTIL)
    a = ap.parse_args()
    if a.mode != "paper":
        print("This build is paper-only. Refusing to start.", file=sys.stderr)
        return 2
    until = datetime.fromisoformat(a.until)
    if until.tzinfo is None:
        until = until.replace(tzinfo=PT)
    setup_logging()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    fault_f = open(config.FAULT_PATH, "a")
    faulthandler.enable(fault_f)
    config.PID_PATH.write_text(f"{os.getpid()}\n")
    vs = load_variants(force=True)
    log.info("START paper-only bot pid=%s until=%s variants=%s hard_max=$%.0f toggles: SKIP_FIRST_MINUTE=%s HOUR_FILTER=%s(min %d) LOG_BET_REASONS=%s",
             os.getpid(), until.isoformat(), ",".join(vs), config.STAKE_HARD_MAX_USD, config.SKIP_FIRST_MINUTE,
             config.HOUR_FILTER, config.HOUR_FILTER_MIN_TRADES, config.LOG_BET_REASONS)
    state = store.load_state()
    for n, v in vs.items():
        if v.get("mode") == "bankroll":
            bankroll_ensure(state, v, n)
        if v.get("mode") == "max_profit":
            maxprofit.ensure(state, v, n)
    store.save_state(state)
    cycles, last_log = 0, [0.0]
    grace_end = None
    last_pass = time.time()
    cov = _load_cov()
    while not _SHUTDOWN.is_set():
        gap = time.time() - last_pass
        last_pass = time.time()
        if gap > 30:
            # The box (VM) was paused: nothing on it ran, including this bot. Count it, don't treat it as a bot hang.
            cov["asleep_sec"] += gap
            cov["sleeps"] += 1
            _stats["box_sleeps"] += 1
            log.warning("BOX WAS ASLEEP for %.0fs (VM paused; no prices seen, no bets possible). Resuming.", gap)
        else:
            cov["awake_sec"] += gap
        if cycles % 15 == 0:
            _save_cov(cov)
        if config.KILL_SWITCH_PATH.exists():
            log.info("STOP file present; exiting")
            break
        accepting = datetime.now(PT) < until
        if not accepting and grace_end is None:
            grace_end = time.time() + config.RESOLVE_GRACE_SEC
            log.info("reached end time %s; no new bets, settling open ones for up to %ds", until.isoformat(), config.RESOLVE_GRACE_SEC)
        if grace_end and (not state.get("open") or time.time() > grace_end):
            (config.DATA_DIR / "FINISHED").write_text(store.iso_pt() + "\n")
            log.info("run finished; open bets left: %d", len(state.get("open", [])))
            break
        t0 = time.time()
        faulthandler.dump_traceback_later(config.CYCLE_STALL_DUMP_SEC, exit=False, file=fault_f)
        try:
            cycle(state, accepting)
        except Exception:  # noqa: BLE001 - one bad pass must never kill or hang the loop
            _stats["errors"] += 1
            log.exception("cycle error (continuing)")
        finally:
            faulthandler.cancel_dump_traceback_later()
        try:
            store.save_state(state)
        except Exception:  # noqa: BLE001
            log.exception("state save failed")
        cycles += 1
        dt = time.time() - t0
        if dt > 20:
            log.warning("slow cycle: %.1fs", dt)
        heartbeat(state, cycles, False, last_log)
        _SHUTDOWN.wait(max(0.2, config.POLL_INTERVAL_SEC - dt))
    store.save_state(state)
    log.info("bot exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
