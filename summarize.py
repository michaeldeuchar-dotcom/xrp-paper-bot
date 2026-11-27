#!/usr/bin/env python3
"""Plain-English summary of the paper bot's results.

Examples:
  python3 summarize.py                      # all variants: today and week-to-date, plus comparison table
  python3 summarize.py --variant main       # one variant only
  python3 summarize.py --since 2026-11-18   # only bets placed on/after this PT date (or "2026-11-18 09:00")
  python3 summarize.py --since 6h           # last 6 hours (also accepts 30m, 2d)
  python3 summarize.py --file data/trades.csv   # the original 2-hour run on Nov 16
  python3 summarize.py --scorecard         # real-money scorecard only (fake money; week-2 finalists)
"""
import argparse
import csv
import glob
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
ROOT = os.path.dirname(os.path.abspath(__file__))


def parse_since(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([mhd])", s.strip())
    if m:
        n, u = int(m.group(1)), m.group(2)
        return datetime.now(PT) - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[u]: n})
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=PT)
        except ValueError:
            pass
    sys.exit(f"could not understand --since {s!r}")


def fill_time(r):
    try:
        return datetime.strptime(r["ts_fill_pt"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=PT)
    except (KeyError, ValueError):
        return None


def money(x):
    return ("-" if x < 0 else "+") + f"${abs(x):,.2f}"


def stats(rows):
    res = [r for r in rows if r.get("status") == "resolved" and r.get("pnl_usd")]
    pnl = [float(r["pnl_usd"]) for r in res]
    staked = sum(float(r.get("total_cost_usd") or 0) for r in res)
    wins = [r for r in res if r.get("won") == "1"]
    best = max(res, key=lambda r: float(r["pnl_usd"]), default=None)
    worst = min(res, key=lambda r: float(r["pnl_usd"]), default=None)
    at_risk = sum(float(r.get("total_cost_usd") or 0) for r in rows if r.get("status") == "open")
    early = [r for r in res if r.get("outcome") == "sold_early"]
    early_pnl = sum(float(r["pnl_usd"]) for r in early)
    return dict(at_risk=at_risk, n=len(rows), settled=len(res), wins=len(wins), losses=len(res) - len(wins), early=len(early), early_pnl=early_pnl,
                open=sum(1 for r in rows if r.get("status") == "open"), pnl=sum(pnl), staked=staked,
                best=best, worst=worst)


def describe(label, s):
    if s["n"] == 0:
        return f"  {label}: no bets."
    wr = f"{s['wins'] / s['settled']:.0%}" if s["settled"] else "n/a"
    roi = f", {s['pnl'] / s['staked']:+.0%} on ${s['staked']:.2f} bet" if s["staked"] else ""
    out = [f"  {label}: {s['n']} bets — {s['wins']} won, {s['losses']} lost, {s['open']} still open "
           f"(win rate {wr})." + (f" ${s['at_risk']:.2f} riding on open bets." if s["open"] else "") + f" Net fake P&L {money(s['pnl'])}{roi}."]
    if s.get("early"):
        out.append(f"    {s['early']} of those were sold early (cash-out) for {money(s['early_pnl'])}.")
    for tag, r in (("Biggest win", s["best"]), ("Biggest loss", s["worst"])):
        if r is not None and ((tag == "Biggest win" and float(r["pnl_usd"]) > 0) or (tag == "Biggest loss" and float(r["pnl_usd"]) < 0)):
            out.append(f"    {tag}: {money(float(r['pnl_usd']))} — ${float(r['total_cost_usd']):.2f} on {r['side']} at "
                       f"{float(r['ask']) * 100:.0f}c, {r['ts_fill_pt'][5:16]} PT")
    return "\n".join(out)


def pick_line(run):
    """What auto_pick is following right now (from the bot's saved state)."""
    try:
        import json
        st = json.load(open(os.path.join(ROOT, "data", run, "state.json"))).get("auto_pick")
        if not st:
            return "  Current pick: not decided yet."
        sc = st.get("scores") or {}
        pos = sorted(((n, v) for n, v in sc.items() if v[1] >= 5 and v[0] > 0), key=lambda t: -t[1][0])
        lead = st.get("leader")
        txt = f"  Current pick: {lead or 'nobody (sitting out)'}" if lead else "  Current pick: nobody qualifies, sitting out"
        if lead and lead in sc:
            txt += f" ({st.get('scope')} net {money(sc[lead][0])} on {sc[lead][1]} bets)"
        ru = [t for t in pos if t[0] != lead][:1]
        if ru:
            txt += f"; runner-up {ru[0][0]} ({money(ru[0][1][0])} on {ru[0][1][1]} bets)"
        hist = st.get("history") or []
        if hist:
            txt += f". Last switch {hist[-1]['ts_pt']}: {hist[-1]['from']} -> {hist[-1]['to']}; {len(hist)} switch(es) logged"
        return txt + "."
    except Exception:
        return "  Current pick: unknown (no state file)."


def bankroll_line(run):
    """Fake-money bankroll_20 status (from the bot's saved state)."""
    try:
        import json
        st = json.load(open(os.path.join(ROOT, "data", run, "state.json"))).get("bankroll")
        if not st:
            return "  Bankroll: not funded yet."
        bal = float(st.get("balance") if st.get("balance") is not None else st.get("equity") or 0)
        start = float(st.get("start", 20))
        goal = float(st.get("goal", 1000))
        n = int(st.get("n_bets") or 0)
        txt = f"  Bankroll: ${bal:,.2f} of ${start:,.0f} start, goal ${goal:,.0f}, {n} bets"
        follow = st.get("following")
        if follow:
            txt += f"; following {follow}"
        elif st.get("funded") and n == 0:
            txt += "; waiting to pick a strategy"
        if st.get("goal_reached"):
            txt += f"; GOAL REACHED at {st.get('goal_reached_at')}"
        if st.get("busted"):
            txt += f"; BUSTED at {st.get('busted_at')} (too little left for a minimum bet)"
        return txt + "."
    except Exception:
        return "  Bankroll: unknown (no state file)."


def maxprofit_line(run):
    """Fake-money max_profit $100 account status (from the bot's saved state)."""
    try:
        import json
        st = json.load(open(os.path.join(ROOT, "data", run, "state.json"))).get("max_profit")
        if not st:
            return "  Max profit: not funded yet."
        bal = float(st.get("balance") if st.get("balance") is not None else st.get("equity") or 0)
        start = float(st.get("start", 100))
        floor = float(st.get("floor", 12))
        n = int(st.get("n_bets") or 0)
        txt = f"  Max profit: ${bal:,.2f} of ${start:,.0f} start (floor ${floor:,.0f}), {n} bets"
        sig = st.get("last_signal")
        if sig:
            txt += f"; last signal {sig}"
        if st.get("paused"):
            txt += f"; PAUSED at {st.get('paused_at')} (cash below the floor, sitting out so it cannot go to zero)"
        return txt + "."
    except Exception:
        return "  Max profit: unknown (no state file)."


def crowd_line(run):
    """How often the official comment feed actually had chat in this run."""
    try:
        import json
        st = json.load(open(os.path.join(ROOT, "data", run, "state.json"))).get("crowd_stats") or {}
        w = int(st.get("windows") or 0)
        if w <= 0:
            return "  Crowd comments: no windows evaluated yet."
        present = int(st.get("comments_present") or 0)
        empty = int(st.get("comments_empty") or 0)
        failed = int(st.get("comments_failed") or 0)
        last = st.get("last") or ""
        txt = (f"  Crowd comments: present in {present} of {w} evaluated windows"
               f" (empty {empty}, fetch failed {failed})")
        if last:
            txt += f". Last read: {last}"
        return txt + "."
    except Exception:
        return "  Crowd comments: unknown (no state file)."




def ab_hedge_line(run):
    """How the A/B hedge pairs did (from data/<run>/ab_hedge_pairs.csv)."""
    path = os.path.join(ROOT, "data", run, "ab_hedge_pairs.csv")
    if not os.path.exists(path):
        return "  A/B hedge pairs: none logged yet."
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return "  A/B hedge pairs: could not read the pairs file."
    if not rows:
        return "  A/B hedge pairs: none logged yet."
    def pnl_of(r):
        try:
            return float(r.get("pnl_usd") or 0)
        except (TypeError, ValueError):
            return 0.0
    typ_order = ("lock_profit", "soft_lock", "limit_loss", "cash_out", "a_only")
    def bits_of(subset):
        by = {}
        for r in subset:
            typ = r.get("status") or r.get("b_type") or "a_only"
            by.setdefault(typ, {"n": 0, "pnl": 0.0})
            by[typ]["n"] += 1
            by[typ]["pnl"] += pnl_of(r)
        bits = []
        for typ in typ_order:
            if typ in by:
                bits.append(f"{by[typ]['n']} {typ} {money(by[typ]['pnl'])}")
        for typ, s in by.items():
            if typ not in typ_order:
                bits.append(f"{s['n']} {typ} {money(s['pnl'])}")
        return bits, sum(pnl_of(r) for r in subset)
    bits, total = bits_of(rows)
    lines = [f"  A/B hedge pairs: {len(rows)} settled ({', '.join(bits)}). Combined pair P&L {money(total)}."]
    names = []
    for r in rows:
        n = r.get("variant") or ""
        if n and n not in names:
            names.append(n)
    if len(names) > 1:
        for n in names:
            sub = [r for r in rows if (r.get("variant") or "") == n]
            b, t = bits_of(sub)
            lines.append(f"    {n}: {len(sub)} settled ({', '.join(b)}). Pair P&L {money(t)}.")
    return "\n".join(lines)



def price_lag_line(run):
    """How often a fresh unpriced move appeared, and how those paper trades did."""
    path = os.path.join(ROOT, "data", run, "price_lag_trades.csv")
    if not os.path.exists(path):
        return "  Price lag: no opportunity log yet."
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return "  Price lag: could not read price_lag_trades.csv."
    if not rows:
        return "  Price lag: no opportunity log yet."
    def pnl_for(name):
        tp = os.path.join(ROOT, "data", run, f"trades_{name}.csv")
        if not os.path.exists(tp):
            return 0, 0.0
        n = s = 0
        with open(tp, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("status") == "resolved" and r.get("pnl_usd"):
                    try:
                        s += float(r["pnl_usd"]); n += 1
                    except (TypeError, ValueError):
                        pass
        return n, s
    n = len(rows)
    fresh = sum(1 for r in rows if r.get("status") in ("filling", "thin_book", "ask_band", "min_shares", "kill_switch"))
    stale = sum(1 for r in rows if r.get("status") == "stale_edge")
    fills = sum(1 for r in rows if r.get("status") == "filling")
    names = []
    for r in rows:
        v = r.get("variant") or ""
        if v and v not in names:
            names.append(v)
    bits = []
    total_n = total_p = 0
    for name in names:
        tn, tp = pnl_for(name)
        total_n += tn; total_p += tp
        if tn:
            bits.append(f"{name} {tn} settled {money(tp)}")
    settled = f" Settled paper P&L: {', '.join(bits)}." if bits else " No lag trades have settled yet."
    return (f"  Price lag: {n} logged edges ({fresh} fresh, {stale} stale/book-already-moved, "
            f"{fills} taken).{settled}")


def sum_arb_line(run):
    """How often both sides added up to less than $1, and whether we could fill."""
    path = os.path.join(ROOT, "data", run, "sum_arb_ops.csv")
    if not os.path.exists(path):
        return "  Sum arb: no opportunity log yet."
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return "  Sum arb: could not read sum_arb_ops.csv."
    if not rows:
        return "  Sum arb: no opportunity log yet."
    def pnl_for(name):
        tp = os.path.join(ROOT, "data", run, f"trades_{name}.csv")
        if not os.path.exists(tp):
            return 0, 0.0
        n = s = 0
        with open(tp, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("status") == "resolved" and r.get("pnl_usd"):
                    try:
                        s += float(r["pnl_usd"]); n += 1
                    except (TypeError, ValueError):
                        pass
        return n, s
    n = len(rows)
    fillable = sum(1 for r in rows if r.get("fillable") == "1" or r.get("status") == "filling")
    tight = sum(1 for r in rows if r.get("status") == "too_tight")
    thin = sum(1 for r in rows if r.get("status") == "not_fillable")
    taken = sum(1 for r in rows if r.get("status") == "filling")
    names = []
    for r in rows:
        v = r.get("variant") or ""
        if v and v not in names:
            names.append(v)
    bits = []
    for name in names:
        tn, tp = pnl_for(name)
        if tn:
            bits.append(f"{name} {tn} settled {money(tp)}")
    settled = f" Settled paper P&L: {', '.join(bits)}." if bits else " No sum-arb trades have settled yet."
    return (f"  Sum arb: {n} under-$1 prints ({tight} inside the 2c buffer, {thin} too thin, "
            f"{fillable} fillable, {taken} taken).{settled}")


def load(args):
    if args.file:
        files = {os.path.basename(args.file): args.file}
    else:
        files = {re.sub(r"^trades_|\.csv$", "", os.path.basename(p)): p
                 for p in sorted(glob.glob(os.path.join(ROOT, "data", args.run, "trades_*.csv")))}
    if not args.file:
        try:
            import json
            vj = json.load(open(os.path.join(ROOT, "variants.json")))
            for k, v in vj.items():
                if not k.startswith("_") and isinstance(v, dict) and v.get("enabled", True) and k not in files:
                    files[k] = None
        except Exception:
            pass
    out = {}
    for name, p in files.items():
        if p is None:
            if not args.variant or name == args.variant:
                out[name] = []
            continue
        if args.variant and name != args.variant:
            continue
        with open(p, newline="") as f:
            out[name] = list(csv.DictReader(f))
    return out


# --- real-money scorecard (reporting only; never used by the trading bot) ---
SCORECARD_MIN_SETTLED = 300
SCORECARD_WORST_DAY_FLOOR = -20.0
SCORECARD_SECOND_HALF_START = datetime(2026, 11, 30, 0, 0, 0, tzinfo=PT)


def resolve_time(r):
    """When a bet settled, in Pacific Time. Used for daily P&L and the second-half cut."""
    raw = (r.get("ts_resolve_pt") or "").strip()
    if raw:
        try:
            return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=PT)
        except ValueError:
            pass
    raw = (r.get("ts_resolve_utc") or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt.astimezone(PT)
        except ValueError:
            pass
    return None


def load_scorecard_config():
    """Finalist list from scorecard.json so it can change without code edits."""
    path = os.path.join(ROOT, "scorecard.json")
    if not os.path.exists(path):
        return None
    import json
    with open(path) as f:
        cfg = json.load(f)
    raw = cfg.get("finalists") or []
    labels = cfg.get("labels") or {}
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append((item, labels.get(item) or item.replace("_", " ")))
        elif isinstance(item, dict) and item.get("id"):
            vid = item["id"]
            out.append((vid, item.get("label") or labels.get(vid) or vid.replace("_", " ")))
    return out


def load_finalist_rows(run, name):
    path = os.path.join(ROOT, "data", run, f"trades_{name}.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def grade_finalist(name, rows):
    """Grade one finalist against the fixed week-2 bar. P&L figures already include fees."""
    settled = []
    for r in rows:
        if r.get("status") != "resolved" or not r.get("pnl_usd"):
            continue
        try:
            pnl = float(r["pnl_usd"])
        except (TypeError, ValueError):
            continue
        settled.append((r, pnl))
    n = len(settled)
    wins = sum(1 for r, _ in settled if r.get("won") == "1")
    net = sum(p for _, p in settled)
    win_pct = (wins / n) if n else None

    by_day = {}
    for r, pnl in settled:
        when = resolve_time(r) or fill_time(r)
        if when is None:
            continue
        day = when.astimezone(PT).date()
        by_day[day] = by_day.get(day, 0.0) + pnl
    if by_day:
        worst_day = min(by_day, key=lambda d: by_day[d])
        worst_pnl = by_day[worst_day]
    else:
        worst_day, worst_pnl = None, None

    half = []
    half_started = datetime.now(PT) >= SCORECARD_SECOND_HALF_START
    for r, pnl in settled:
        when = resolve_time(r)
        if when is not None and when >= SCORECARD_SECOND_HALF_START:
            half.append(pnl)
    half_n = len(half)
    half_pnl = sum(half) if half_n else 0.0
    half_evaluable = half_started and half_n > 0

    checks = {
        "bets": n >= SCORECARD_MIN_SETTLED,
        "profit": net > 0,
        "worst_day": (worst_pnl is None) or (worst_pnl >= SCORECARD_WORST_DAY_FLOOR),
        "second_half": (half_pnl > 0) if half_evaluable else None,  # None = not yet started / no bets
    }
    hard_fail = (not checks["profit"]) or (not checks["worst_day"]) or (checks["second_half"] is False)
    if not hard_fail and checks["bets"] and checks["second_half"] is True:
        verdict = "PASS"
    elif not hard_fail:
        verdict = "NOT YET"
    else:
        verdict = "FAIL"
    return dict(
        name=name, n=n, wins=wins, win_pct=win_pct, net=net,
        worst_day=worst_day, worst_pnl=worst_pnl,
        half_n=half_n, half_pnl=half_pnl, half_evaluable=half_evaluable,
        half_started=half_started, checks=checks, verdict=verdict,
    )


def _check_label(ok):
    if ok is True:
        return "PASS"
    if ok is False:
        return "FAIL"
    return "n/a"


def format_scorecard_table(grades):
    """Plain-English table; numbers here are what --scorecard prints."""
    lines = []
    hdr = (f"  {'Finalist':<38}{'Bets':>6}{'Win%':>7}{'Net P&L':>11}  "
           f"{'Worst day (PT)':<22}{'Second half':>18}  "
           f"{'300 bets':>8} {'Profit':>6} {'Worst day':>9} {'Second half':>11}  Verdict")
    lines.append(hdr)
    lines.append("  " + "-" * (len(hdr) - 2))
    for g in grades:
        wr = f"{g['win_pct']:.0%}" if g["win_pct"] is not None else "-"
        if g["worst_day"] is None:
            worst = "n/a"
        else:
            worst = f"{money(g['worst_pnl'])} on {g['worst_day']:%b %d}"
        if not g["half_started"]:
            half = "n/a, starts Nov 30"
        elif not g["half_evaluable"]:
            half = "n/a, no bets yet"
        else:
            half = money(g["half_pnl"])
        c = g["checks"]
        lines.append(
            f"  {g['label']:<38}{g['n']:>6}{wr:>7}{money(g['net']):>11}  "
            f"{worst:<22}{half:>18}  "
            f"{_check_label(c['bets']):>8} {_check_label(c['profit']):>6} "
            f"{_check_label(c['worst_day']):>9} {_check_label(c['second_half']):>11}  {g['verdict']}"
        )
    return "\n".join(lines)


def print_scorecard(run):
    """Print the real-money scorecard for this run's finalists. Reporting only."""
    cfg = load_scorecard_config()
    print("Real-money scorecard (fake money only; no real trades)")
    print(f"Grades the named finalists in data/{run} against a fixed pass/fail bar.")
    print("Bar (all four must pass):")
    print(f"  1. At least {SCORECARD_MIN_SETTLED} settled bets")
    print("  2. Net fake P&L above $0 after fees")
    print(f"  3. No single calendar day (Pacific Time) worse than -${abs(SCORECARD_WORST_DAY_FLOOR):.0f}")
    print(f"  4. Net P&L above $0 in the second half (bets settled on or after "
          f"{SCORECARD_SECOND_HALF_START:%b %d, %Y} PT)")
    print("Verdict: PASS if all four pass; FAIL if profit, worst-day, or (once it has bets) "
          "second-half fail; NOT YET if those have not failed but there are not yet "
          f"{SCORECARD_MIN_SETTLED} bets, and/or the second half has not started.")
    print("Daily and second-half P&L use the settlement time in Pacific Time.")
    if cfg is None:
        print("\n  No scorecard.json found in this repo, so there are no finalists to grade.")
        return
    if not cfg:
        print("\n  scorecard.json lists no finalists.")
        return
    grades = []
    for name, label in cfg:
        g = grade_finalist(name, load_finalist_rows(run, name))
        g["label"] = f"{label} ({name})"
        grades.append(g)
    print()
    print(format_scorecard_table(grades))
    passed = sum(1 for g in grades if g["verdict"] == "PASS")
    not_yet = sum(1 for g in grades if g["verdict"] == "NOT YET")
    failed = sum(1 for g in grades if g["verdict"] == "FAIL")
    print(f"\n  {passed} PASS, {not_yet} NOT YET, {failed} FAIL, of {len(grades)} finalist(s). "
          "Nothing here is a green light for real money until the verdict is PASS.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="PT date/time like 2026-11-18 or '2026-11-18 09:00', or 6h / 30m / 2d")
    ap.add_argument("--variant", help="only this variant")
    ap.add_argument("--run", default="week2", help="run folder under data/ (week2 is the current two-week test; use week1 for the first trial)")
    ap.add_argument("--file", help="summarize a single trades CSV instead (e.g. data/trades.csv)")
    ap.add_argument("--scorecard", action="store_true",
                    help="print only the real-money scorecard for the configured finalists (fake money; reporting only)")
    a = ap.parse_args()
    if a.scorecard:
        now = datetime.now(PT)
        print(f"Paper (fake money) results as of {now:%a %b %d, %I:%M %p} PT")
        print("No real money was used.")
        print()
        print_scorecard(a.run)
        return
    since = parse_since(a.since)
    data = load(a)
    if not data:
        print("No trade files found yet.")
        return
    now = datetime.now(PT)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"Paper (fake money) results as of {now:%a %b %d, %I:%M %p} PT" + (f", bets since {since:%b %d %I:%M %p} PT" if since else ""))
    print("No real money was used.")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_run_cfg", os.path.join(ROOT, "config.py"))
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        until = datetime.fromisoformat(cfg.RUN_UNTIL)
        if until.tzinfo is None:
            until = until.replace(tzinfo=PT)
        print(f"This paper run takes new bets until {until.astimezone(PT):%a %b %d, %I:%M %p} PT.")
    except Exception:
        pass
    print()

    try:
        import json
        c = json.load(open(os.path.join(ROOT, "data", a.run, "coverage.json")))
        tot = c["awake_sec"] + c["asleep_sec"]
        if tot > 0 and not a.file:
            print(f"Bot was awake and watching {c['awake_sec'] / tot:.0%} of the time since the week run started "
                  f"(the computer it runs on was paused {c['sleeps']} time(s), {c['asleep_sec'] / 3600:.1f} h total; no bets can happen while paused).\n")
    except Exception:
        pass
    table = []
    for name, rows in data.items():
        if since:
            rows = [r for r in rows if (fill_time(r) or now) >= since]
        today = [r for r in rows if (fill_time(r) or now) >= today0]
        s_all, s_today = stats(rows), stats(today)
        print(f"{name}:")
        if name == "auto_pick":
            print(pick_line(a.run))
        if name == "bankroll_20":
            print(bankroll_line(a.run))
        if name == "max_profit":
            print(maxprofit_line(a.run))
        if name == "crowd":
            print(crowd_line(a.run))
        print(describe("Today", s_today))
        print(describe("Since start" if not since else "In this period", s_all))
        print()
        table.append((name, s_all))
    if any(n.startswith("ab_hedge") for n, _ in table) or os.path.exists(os.path.join(ROOT, "data", a.run, "ab_hedge_pairs.csv")):
        print(ab_hedge_line(a.run))
    if any(n.startswith("price_lag") for n, _ in table) or os.path.exists(os.path.join(ROOT, "data", a.run, "price_lag_trades.csv")):
        print(price_lag_line(a.run))
    if any(n.startswith("sum_arb") for n, _ in table) or os.path.exists(os.path.join(ROOT, "data", a.run, "sum_arb_ops.csv")):
        print(sum_arb_line(a.run))
        print()
    if len(table) > 1:
        print("Comparison (best to worst by net fake P&L):")
        print(f"  {'variant':<26}{'bets':>5}{'won':>5}{'lost':>5}{'open':>5}{'win%':>6}{'bet $':>9}{'net P&L':>10}{'return':>8}")
        for name, s in sorted(table, key=lambda t: -t[1]["pnl"]):
            wr = f"{s['wins'] / s['settled']:.0%}" if s["settled"] else "-"
            roi = f"{s['pnl'] / s['staked']:+.0%}" if s["staked"] else "-"
            print(f"  {name:<26}{s['n']:>5}{s['wins']:>5}{s['losses']:>5}{s['open']:>5}{wr:>6}{s['staked']:>9.2f}{money(s['pnl']):>10}{roi:>8}")
        skip = {"auto_pick", "bankroll_20", "max_profit"}
        total = sum(s["pnl"] for n, s in table if n not in skip)
        extras = []
        if any(n == "auto_pick" for n, _ in table):
            extras.append("auto_pick left out because it only copies the others' bets")
        if any(n == "bankroll_20" for n, _ in table):
            extras.append("bankroll_20 left out because it is its own $20-to-$1,000 account that copies the current pick")
        if any(n == "max_profit" for n, _ in table):
            extras.append("max_profit left out because it is its own $100 fake account")
        print("  Note: auto_pick and bankroll_20 still rank and follow only the original 5-minute hold-to-settlement strategies; cash-out, 15-minute, crowd, max_profit, ab_hedge, price_lag, and sum_arb variants are scored separately and are not in that ranking.")
        print(f"\n  All variants together: {money(total)} (each variant is a separate fake account"
              + (("; " + "; ".join(extras)) if extras else "") + ").")

    if not a.file:
        print()
        print_scorecard(a.run)


if __name__ == "__main__":
    main()
