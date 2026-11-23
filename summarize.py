#!/usr/bin/env python3
"""Plain-English summary of the paper bot's results.

Examples:
  python3 summarize.py                      # all variants: today and week-to-date, plus comparison table
  python3 summarize.py --variant main       # one variant only
  python3 summarize.py --since 2026-11-18   # only bets placed on/after this PT date (or "2026-11-18 09:00")
  python3 summarize.py --since 6h           # last 6 hours (also accepts 30m, 2d)
  python3 summarize.py --file data/trades.csv   # the original 2-hour run on Nov 16
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
    return dict(at_risk=at_risk, n=len(rows), settled=len(res), wins=len(wins), losses=len(res) - len(wins),
                open=sum(1 for r in rows if r.get("status") == "open"), pnl=sum(pnl), staked=staked,
                best=best, worst=worst)


def describe(label, s):
    if s["n"] == 0:
        return f"  {label}: no bets."
    wr = f"{s['wins'] / s['settled']:.0%}" if s["settled"] else "n/a"
    roi = f", {s['pnl'] / s['staked']:+.0%} on ${s['staked']:.2f} bet" if s["staked"] else ""
    out = [f"  {label}: {s['n']} bets — {s['wins']} won, {s['losses']} lost, {s['open']} still open "
           f"(win rate {wr})." + (f" ${s['at_risk']:.2f} riding on open bets." if s["open"] else "") + f" Net fake P&L {money(s['pnl'])}{roi}."]
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="PT date/time like 2026-11-18 or '2026-11-18 09:00', or 6h / 30m / 2d")
    ap.add_argument("--variant", help="only this variant")
    ap.add_argument("--run", default="week2", help="run folder under data/ (week2 is the current two-week test; use week1 for the first trial)")
    ap.add_argument("--file", help="summarize a single trades CSV instead (e.g. data/trades.csv)")
    a = ap.parse_args()
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
        print(describe("Today", s_today))
        print(describe("Since start" if not since else "In this period", s_all))
        print()
        table.append((name, s_all))
    if len(table) > 1:
        print("Comparison (best to worst by net fake P&L):")
        print(f"  {'variant':<19}{'bets':>5}{'won':>5}{'lost':>5}{'open':>5}{'win%':>6}{'bet $':>9}{'net P&L':>10}{'return':>8}")
        for name, s in sorted(table, key=lambda t: -t[1]["pnl"]):
            wr = f"{s['wins'] / s['settled']:.0%}" if s["settled"] else "-"
            roi = f"{s['pnl'] / s['staked']:+.0%}" if s["staked"] else "-"
            print(f"  {name:<19}{s['n']:>5}{s['wins']:>5}{s['losses']:>5}{s['open']:>5}{wr:>6}{s['staked']:>9.2f}{money(s['pnl']):>10}{roi:>8}")
        skip = {"auto_pick", "bankroll_20"}
        total = sum(s["pnl"] for n, s in table if n not in skip)
        extras = []
        if any(n == "auto_pick" for n, _ in table):
            extras.append("auto_pick left out because it only copies the others' bets")
        if any(n == "bankroll_20" for n, _ in table):
            extras.append("bankroll_20 left out because it is its own $20-to-$1,000 account that copies the current pick")
        print(f"\n  All variants together: {money(total)} (each variant is a separate fake account"
              + (("; " + "; ".join(extras)) if extras else "") + ").")


if __name__ == "__main__":
    main()
