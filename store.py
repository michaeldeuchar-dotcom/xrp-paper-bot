"""Per-variant paper ledgers (CSV) + JSON state + size-capped decisions log."""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import config

PT = ZoneInfo("America/Los_Angeles")

TRADE_FIELDS = [
    "trade_id", "variant", "timeframe", "mode", "status", "ts_fill_utc", "ts_fill_pt", "ts_resolve_utc",
    "window_start", "slug", "title", "side", "ask", "stake", "shares", "notional_usd", "fee_usd",
    "total_cost_usd", "est_prob", "model_p_up", "market_p_up", "edge", "spot", "price_to_beat",
    "ptb_source", "official_ptb", "official_final", "seconds_left", "annual_vol", "outcome", "won",
    "payout_usd", "pnl_usd", "cumulative_pnl_usd", "reason",
]
DECISION_FIELDS = [
    "ts_pt", "variant", "timeframe", "window_start", "secs_left", "action", "reason", "side", "est_prob",
    "model_p_up", "market_p_up", "ask", "edge", "stake", "shares", "spot", "price_to_beat", "english",
]


def iso_utc(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else datetime.now().timestamp(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_pt(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else datetime.now().timestamp(), tz=PT).strftime("%Y-%m-%d %H:%M:%S PT")


def trades_path(variant: str) -> Path:
    return config.DATA_DIR / f"trades_{variant}.csv"


def _ensure(path: Path, fields: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def read_trades(variant: str) -> list[dict]:
    p = trades_path(variant)
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def append_trade(variant: str, row: dict) -> None:
    p = trades_path(variant)
    _ensure(p, TRADE_FIELDS)
    with p.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def update_trade(variant: str, trade_id: str, updates: dict) -> None:
    rows = read_trades(variant)
    for r in rows:
        if r.get("trade_id") == trade_id:
            r.update({k: "" if v is None else v for k, v in updates.items()})
    p = trades_path(variant)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRADE_FIELDS})
    tmp.replace(p)


def append_decision(row: dict) -> None:
    p = config.DECISIONS_CSV
    if p.exists() and p.stat().st_size > config.DECISIONS_MAX_BYTES:
        for i in range(config.DECISIONS_BACKUPS - 1, 0, -1):
            src = p.with_name(f"{p.name}.{i}")
            if src.exists():
                src.replace(p.with_name(f"{p.name}.{i + 1}"))
        p.replace(p.with_name(f"{p.name}.1"))
    _ensure(p, DECISION_FIELDS)
    with p.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=DECISION_FIELDS).writerow({k: row.get(k, "") for k in DECISION_FIELDS})


def load_state() -> dict:
    try:
        with config.STATE_PATH.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seq": 0, "open": [], "traded_windows": {}}


def save_state(state: dict) -> None:
    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.STATE_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, config.STATE_PATH)
