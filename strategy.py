"""Probability model + per-variant decision rules (paper only).

Model: Polymarket resolves Up if the Chainlink 60-second TWAP at window end is >=
the 60-second TWAP at window start. We estimate P(Up) with a random walk on the
END TWAP, not the spot price:
  * more than 60s left: end-TWAP variance = sigma^2 * ((s-60) + 60/3)
  * inside the last 60s:  part of the TWAP is already observed (our own spot samples);
    only the remaining s seconds are uncertain: variance = sigma^2 * s^3 / (3*60^2)
  * plus a fixed basis noise (our Coinbase proxy vs Chainlink, measured ~$18 sd).
This replaces the old model, which jumped to exactly 0% or 100% once 40s or less
remained (that is what produced the $10 "100% sure" Up bet at 63c that lost).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import config

SPY = 365.25 * 24 * 3600


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fee_per_share(p: float) -> float:
    p = min(max(float(p), 0.0), 1.0)
    return config.TAKER_FEE_RATE * p * (1.0 - p)


def p_up_model(spot: float, ptb: float, secs_left: float, annual_vol: float,
               observed_twap: Optional[float] = None) -> float:
    if spot <= 0 or ptb <= 0:
        return 0.5
    s = max(float(secs_left), 0.0)
    sig2 = (annual_vol * config.VOL_MULT) ** 2 / SPY * spot * spot  # $^2 per second
    if s > 60:
        mean = spot
        var = sig2 * ((s - 60.0) + 20.0)
    else:
        obs = observed_twap if observed_twap is not None else spot
        mean = ((60.0 - s) * obs + s * spot) / 60.0
        var = sig2 * s ** 3 / (3.0 * 3600.0)
    var += (config.BASIS_NOISE_PCT * spot) ** 2
    return ncdf((mean - ptb) / math.sqrt(var))


@dataclass
class Decision:
    variant: str
    action: str                 # buy / skip
    reason: str
    english: str
    side: Optional[str] = None
    est_prob: Optional[float] = None
    model_p_up: Optional[float] = None
    market_p_up: Optional[float] = None
    ask: Optional[float] = None
    ask_size: float = 0.0
    fee_ps: Optional[float] = None
    edge: Optional[float] = None
    stake: float = 0.0
    shares: float = 0.0
    notional: float = 0.0
    fee: float = 0.0
    total_cost: float = 0.0
    extra: dict = field(default_factory=dict)


def _mid(book) -> Optional[float]:
    if book is None:
        return None
    if book.best_bid is not None and book.best_ask is not None:
        return (book.best_bid + book.best_ask) / 2.0
    return book.best_ask if book.best_ask is not None else book.best_bid


def size_bet(v: dict, edge: float, ask: float) -> tuple[float, float, str]:
    """Return (stake, shares, note). Stake never exceeds the $10 hard max."""
    hard = config.STAKE_HARD_MAX_USD
    if v.get("mode") in ("favorite", "contrarian", "scale"):
        stake = float(v.get("base_stake", 1))
    else:
        steps = math.floor((edge - float(v.get("min_edge", 0))) / float(v.get("edge_per_dollar", 0.02)) + 1e-9)
        stake = float(v.get("base_stake", 1)) + max(steps, 0)
    stake = min(stake, float(v.get("max_stake", 5)))
    if ask > float(v.get("high_ask", 1.0)):
        stake = min(stake, float(v.get("high_ask_max_stake", stake)))
    stake = min(stake, hard)
    shares = math.floor(stake / ask * 100) / 100
    # the $10 hard max covers the whole cost of the bet, including the taker fee
    hard_shares = math.floor(hard / (ask + fee_per_share(ask)) * 100) / 100
    shares = min(shares, hard_shares)
    ml = v.get("max_loss_per_bet")
    if ml is not None:  # explicit worst-case loss per bet (a loss costs the whole stake + fee); holds even if max_stake is raised
        ml = min(float(ml), hard)
        stake = min(stake, ml)
        shares = min(shares, math.floor(ml / (ask + fee_per_share(ask)) * 100) / 100)
    if v.get("cap_incl_fee"):  # max_stake applies to cost PLUS fee, not just the shares' price
        shares = min(shares, math.floor(float(v.get("max_stake", 5)) / (ask + fee_per_share(ask)) * 100) / 100)
    note = ""
    if shares + 1e-9 < config.MIN_ORDER_SHARES and v.get("round_up_to_min_shares"):
        need = config.MIN_ORDER_SHARES * ask
        ru_cap = min(float(v.get("max_round_up_usd", 0)), hard, float(v["max_loss_per_bet"]) if ml is not None else hard)
        if need + config.MIN_ORDER_SHARES * fee_per_share(ask) <= ru_cap + 1e-9:
            note = f"rounded ${stake:.2f} up to ${need:.2f} for the 5-share minimum"
            stake, shares = need, float(config.MIN_ORDER_SHARES)
    return stake, shares, note


def trend_direction(v: dict, sig: Optional[dict]) -> tuple[Optional[str], str, str]:
    """trend_volume gate. Returns (side or None, reason_code, plain-English why)."""
    if not sig or not sig.get("ok"):
        return None, "trend_na", f"No trend reading ({(sig or {}).get('why', 'none')})."
    z, z15 = sig["mom_z"], sig["mom15_z"]
    sp, mid, sh = sig.get("span_label", "60m"), sig.get("mid_label", "15m"), sig.get("short_label", "5m")
    if sig["ann_vol"] > float(v.get("max_ann_vol", 0.9)):
        return None, "too_volatile", f"Market too wild: {sp} volatility {sig['ann_vol']:.0%} a year (limit {float(v.get('max_ann_vol', 0.9)):.0%})."
    if sig["efficiency"] < float(v.get("min_efficiency", 0.15)):
        return None, "choppy", f"Choppy: price went back and forth (straightness {sig['efficiency']:.2f}, need {float(v.get('min_efficiency', 0.15)):.2f})."
    if abs(z) < float(v.get("min_mom_z", 1.0)):
        return None, "weak_momentum", f"The {sp} move ({sig['ret60_pct']:+.2f}%) too small to call a trend (z {z:+.1f}, need {float(v.get('min_mom_z', 1.0)):.1f})."
    side = "Up" if z > 0 else "Down"
    if (z15 > 0) != (z > 0) or abs(z15) < float(v.get("min_mom15_z", 0.0)):
        return None, "momentum_conflict", f"The {sp} trend says {side} but the last {mid} ({sig['ret15_pct']:+.2f}%) disagree."
    want = "up" if side == "Up" else "down"
    if sig["structure"] != want:
        return None, "structure_conflict", (f"Momentum says {side} but price isn't making "
                                            f"{'higher highs and higher lows' if side == 'Up' else 'lower highs and lower lows'} (structure {sig['structure']}).")
    steps_need = int(v.get("min_struct_steps", 0))
    if steps_need:
        got = sig["struct_up_steps"] if side == "Up" else sig["struct_dn_steps"]
        if got < steps_need:
            return None, "structure_weak", f"Structure not clean enough: only {got} of 3 chunk steps show {'higher highs and lows' if side == 'Up' else 'lower highs and lows'} (need {steps_need})."
    if v.get("require_5m_agree") and (sig["ret5_pct"] > 0) != (side == "Up"):
        return None, "momentum_conflict", f"The {sp} and {mid} trends say {side} but the last {sh} ({sig['ret5_pct']:+.2f}%) disagree."
    if float(v.get("min_spike_ratio", 0)) > 0:
        if sig["spike_ratio"] < float(v["min_spike_ratio"]):
            return None, "no_spike", f"No volume spike: last 5 candles' volume is {sig['spike_ratio']:.1f}x the {sp} average (need {float(v['min_spike_ratio']):.1f}x)."
        n_dir = sig["up5"] if side == "Up" else sig["dn5"]
        if n_dir < int(v.get("min_spike_candles", 3)):
            return None, "spike_wrong_way", f"Only {n_dir} of the last 5 candles closed {side} (need {int(v.get('min_spike_candles', 3))})."
    ratio = sig["up_vol_ratio"] if side == "Up" else sig["dn_vol_ratio"]
    if ratio < float(v.get("min_vol_ratio", 1.15)):
        return None, "volume_conflict", f"Volume doesn't back the {side} move: trend-direction candles average only {ratio:.2f}x the volume of opposite ones (need {float(v.get('min_vol_ratio', 1.15)):.2f}x)."
    why = (f"trend {side}: {sp} {sig['ret60_pct']:+.2f}% (z {z:+.1f}), {mid} {sig['ret15_pct']:+.2f}%, "
           f"{'higher highs/lows' if side == 'Up' else 'lower highs/lows'}, volume {ratio:.2f}x on trend candles, straightness {sig['efficiency']:.2f}")
    return side, "trend_ok", why


def breakout_direction(v: dict, sig: Optional[dict], spot: Optional[float]) -> tuple[Optional[str], str, str]:
    """breakout gate: a finished 1-min candle closed past the prior 30-min high/low on >= N x average volume,
    and the live price is still past that level (the break hasn't failed)."""
    if not sig or not sig.get("ok") or not spot:
        return None, "trend_na", f"No candle reading ({(sig or {}).get('why', 'none')})."
    need = float(v.get("min_breakout_vol", 2.0))
    hi, lo = sig["range_hi"], sig["range_lo"]
    if sig["bo_up_vol"] > 0:
        if sig["bo_up_vol"] < need:
            return None, "weak_breakout", f"Broke above the 30-min high ${hi:,.{config.PRICE_DP}f} but volume only {sig['bo_up_vol']:.1f}x normal (need {need:.1f}x)."
        if spot <= hi:
            return None, "failed_breakout", f"Broke above ${hi:,.{config.PRICE_DP}f} but price fell back to ${spot:,.{config.PRICE_DP}f}."
        return "Up", "breakout_ok", f"breakout Up: closed above the 30-min high ${hi:,.{config.PRICE_DP}f} on {sig['bo_up_vol']:.1f}x volume, now ${spot:,.{config.PRICE_DP}f}"
    if sig["bo_dn_vol"] > 0:
        if sig["bo_dn_vol"] < need:
            return None, "weak_breakout", f"Broke below the 30-min low ${lo:,.{config.PRICE_DP}f} but volume only {sig['bo_dn_vol']:.1f}x normal (need {need:.1f}x)."
        if spot >= lo:
            return None, "failed_breakout", f"Broke below ${lo:,.{config.PRICE_DP}f} but price bounced back to ${spot:,.{config.PRICE_DP}f}."
        return "Down", "breakout_ok", f"breakout Down: closed below the 30-min low ${lo:,.{config.PRICE_DP}f} on {sig['bo_dn_vol']:.1f}x volume, now ${spot:,.{config.PRICE_DP}f}"
    return None, "no_breakout", f"Price is inside its 30-min range (${lo:,.{config.PRICE_DP}f}-${hi:,.{config.PRICE_DP}f}); no breakout."


def decide(name: str, v: dict, *, p_model: float, books: dict, secs_left: float, trend: Optional[dict] = None,
           spot: Optional[float] = None, ptb: Optional[float] = None) -> Decision:
    D = lambda action, reason, english, **kw: Decision(name, action, reason, english, **kw)  # noqa: E731
    up, down = books.get("Up"), books.get("Down")
    m_up = _mid(up)
    if m_up is None and down is not None and _mid(down) is not None:
        m_up = 1.0 - _mid(down)
    base = dict(model_p_up=p_model, market_p_up=m_up)

    if not (float(v.get("min_secs_left", 0)) <= secs_left <= float(v.get("max_secs_left", 300))):
        return D("skip", "timing", f"Only bets with {v.get('min_secs_left')}-{v.get('max_secs_left')}s left; {secs_left:.0f}s left now.", **base)

    trend_why = ""
    if v.get("mode") in ("trend", "breakout", "guarded_favorite", "breakout_scale", "trend_scale"):
        if v.get("mode") in ("trend", "guarded_favorite", "trend_scale"):
            tside, tcode, trend_why = trend_direction(v, trend)
        else:
            tside, tcode, trend_why = breakout_direction(v, trend, spot)
        if tside is None:
            return D("skip", tcode, trend_why, **base)
        if v.get("mode") == "trend_scale" and secs_left < float(v.get("first_min_secs_left", 90)):
            return D("skip", "timing", f"First ultimate bet needs {v.get('first_min_secs_left')}s+ left; {secs_left:.0f}s left now.", side=tside, **base)
        if v.get("mode") == "breakout_scale":
            if secs_left < float(v.get("first_min_secs_left", 120)):
                return D("skip", "timing", f"First breakout_scale bet needs {v.get('first_min_secs_left')}s+ left; {secs_left:.0f}s left now.", **base)
            if trend is None or (trend.get("mom15_z", 0) > 0) != (tside == "Up") or abs(trend.get("mom15_z", 0)) < float(v.get("min_mom15_z", 0.0)):
                return D("skip", "trend_conflict", f"Breakout says {tside} but the last 15 min moved {trend.get('ret15_pct', 0):+.2f}%.", side=tside, **base)
        if v.get("mode") == "guarded_favorite":
            if m_up is None:
                return D("skip", "no_book", "No prices in the order book.", **base)
            fav = "Up" if m_up >= 0.5 else "Down"
            if fav != tside:
                return D("skip", "favorite_mismatch", f"Trend filters say {tside} but the market favorite is {fav} ({max(m_up, 1 - m_up)*100:.0f}c); sitting out.", **base)
        w = float(v.get("blend_market", 0.5))
        p = p_model if m_up is None else (1 - w) * p_model + w * m_up
        side = tside
        est = min(0.99, (p if side == "Up" else 1 - p) + float(v.get("trend_bonus", 0.03)))
    elif v.get("mode") in ("favorite", "contrarian"):
        if m_up is None:
            return D("skip", "no_book", "No prices in the order book.", **base)
        fav = "Up" if m_up >= 0.5 else "Down"
        side = fav if v.get("mode") == "favorite" else ("Down" if fav == "Up" else "Up")
        est = None
        if v.get("mode") == "contrarian":
            trend_why = f"underdog {side} while the market has {fav} at about {max(m_up, 1 - m_up)*100:.0f}c"
    elif v.get("mode") == "scale":
        if secs_left < float(v.get("first_min_secs_left", 150)):
            return D("skip", "timing", f"First scale_in bet needs {v.get('first_min_secs_left')}s+ left; {secs_left:.0f}s left now.", **base)
        w = float(v.get("blend_market", 0.5))
        p = p_model if m_up is None else (1 - w) * p_model + w * m_up
        side = "Up" if p >= 0.5 else "Down"
        est = p if side == "Up" else 1 - p
        if trend and trend.get("ok") and v.get("require_trend_agree", True):
            if (trend["mom15_z"] > 0) != (side == "Up"):
                return D("skip", "trend_conflict", f"Model says {side} but the last 15 min moved {trend['ret15_pct']:+.2f}%.", side=side, est_prob=est, **base)
        trend_why = "first piece of a scale-in (adds only if the move keeps going)"
    else:
        w = float(v.get("blend_market", 0.0))
        p = p_model if m_up is None else (1 - w) * p_model + w * m_up
        side = "Up" if p >= 0.5 else "Down"
        est = p if side == "Up" else 1 - p
    allowed = v.get("sides")
    if allowed and side not in allowed:
        return D("skip", "side_not_allowed", f"This variant only bets {'/'.join(allowed)}; the signal says {side}.", side=side, est_prob=est, **base)
    rts = v.get("require_trend_sign")   # "down": only when the broader trend read is net down (used as a filter, not a trigger)
    if rts:
        if not trend or not trend.get("ok"):
            return D("skip", "trend_na", "No trend reading for the broader-trend filter.", side=side, est_prob=est, **base)
        r = float(trend["ret60_pct"])
        if (rts == "down" and r >= 0) or (rts == "up" and r <= 0):
            return D("skip", "trend_filter", f"Broader trend filter: the {trend.get('span_label')} move is {r:+.2f}%, not {rts}.", side=side, est_prob=est, **base)
    mlp = float(v.get("min_lead_pct", 0) or 0)   # minimum lead, in percent of price (e.g. 0.055 = 0.055% = about 0.53c when XRP is $0.967)
    if mlp > 0 and spot and ptb:
        gap = (spot - ptb) if side == "Up" else (ptb - spot)   # how far XRP is past the price to beat, in our direction
        need = mlp / 100.0 * spot
        if gap < need:
            return D("skip", "lead_small", f"Lead gate: XRP is ${gap:+.{config.PRICE_DP}f} past the price to beat our way ({side}); need at least ${need:.{config.PRICE_DP}f} ({mlp:g}% of price).",
                     side=side, est_prob=est, **base)
    book = up if side == "Up" else down
    if book is None or book.best_ask is None:
        return D("skip", "no_ask", f"Nobody is selling {side} right now.", side=side, est_prob=est, **base)
    ask = float(book.best_ask)
    fps = fee_per_share(ask)
    edge = None if est is None else est - ask - fps
    kw = dict(side=side, est_prob=est, ask=ask, ask_size=book.best_ask_size, fee_ps=fps, edge=edge, **base)
    if not (float(v.get("min_ask", 0)) <= ask <= float(v.get("max_ask", 1))):
        return D("skip", "ask_band", f"{side} costs {ask*100:.0f}c, outside this variant's {v.get('min_ask')}-{v.get('max_ask')} price range.", **kw)
    if edge is not None and edge < float(v.get("min_edge", 0)) - 1e-12:
        return D("skip", "edge_small", f"{side} at {ask*100:.0f}c: my estimate {est:.0%} beats price+fee by only {edge*100:.1f}c (need {float(v.get('min_edge', 0))*100:.0f}c).", **kw)
    stake, shares, note = size_bet(v, edge or 0.0, ask)
    if shares + 1e-9 < config.MIN_ORDER_SHARES:
        return D("skip", "min_shares", f"${stake:.2f} only buys {shares:.2f} shares of {side} at {ask*100:.0f}c; Polymarket's minimum is 5.", stake=stake, shares=shares, **kw)
    if book.best_ask_size + 1e-9 < shares:
        return D("skip", "thin_book", f"Only {book.best_ask_size:.1f} shares offered at {ask*100:.0f}c; need {shares:.2f}.", stake=stake, shares=shares, **kw)
    if stake > config.STAKE_HARD_MAX_USD + 1e-9:
        return D("skip", "hard_max", "Stake would exceed the $10 hard max.", **kw)
    notional = shares * ask
    fee = shares * fps
    why = (f"market favorite {side} at {ask*100:.0f}c with {secs_left:.0f}s left" if est is None else
           f"I put {side} at {est:.0%}, price+fee is {(ask+fps)*100:.1f}c, edge {edge*100:.1f}c")
    if v.get("mode") == "contrarian":
        why = f"{trend_why}, {side} at {ask*100:.0f}c with {secs_left:.0f}s left"
    elif trend_why:
        why += f"; {trend_why}"
    return D("buy", "ok", f"Buying ${notional:.2f} of {side}: {why}." + (f" ({note})" if note else ""),
             stake=stake, shares=shares, notional=notional, fee=fee, total_cost=notional + fee, extra={"note": note}, **kw)


def decide_scale_add(name: str, v: dict, pos: dict, *, books: dict, secs_left: float, spot: float, ptb: float,
                     now: float) -> Decision:
    """scale_in add-on. pos = {"side","fills":[{"ts","ask","gap","cost"}]}. Whole round (all pieces, fees in) <= $10."""
    D = lambda action, reason, english, **kw: Decision(name, action, reason, english, **kw)  # noqa: E731
    side, fills = pos["side"], pos["fills"]
    cap = min(float(v.get("round_cap", config.STAKE_HARD_MAX_USD)), float(v.get("max_loss_per_bet", config.STAKE_HARD_MAX_USD)),
              config.STAKE_HARD_MAX_USD)  # whole-round limit incl. fees
    spent = sum(float(f["cost"]) for f in fills)
    last = fills[-1]
    gap = (spot - ptb) if side == "Up" else (ptb - spot)   # how far XRP is past the price to beat, our way
    kw = dict(side=side)
    if len(fills) - 1 >= int(v.get("max_adds", 2)):
        return D("skip", "adds_done", f"Already added {len(fills) - 1} times this round.", **kw)
    if secs_left < float(v.get("min_secs_left", 60)):
        return D("skip", "timing", f"Too late to add ({secs_left:.0f}s left).", **kw)
    if now - float(last["ts"]) < float(v.get("add_min_gap_sec", 30)):
        return D("skip", "add_wait", "Waiting at least 30s between adds.", **kw)
    book = books.get(side)
    if book is None or book.best_ask is None:
        return D("skip", "no_ask", f"Nobody is selling {side} right now.", **kw)
    ask = float(book.best_ask)
    fps = fee_per_share(ask)
    kw.update(ask=ask, ask_size=book.best_ask_size, fee_ps=fps)
    step = float(v.get("add_min_move_usd", 15))
    if gap < float(last["gap"]) + step:
        return D("skip", "no_follow_through", f"XRP is ${gap:+.{config.PRICE_DP}f} past the price to beat our way; need ${float(last['gap']) + step:+.{config.PRICE_DP}f} (${step:.{config.PRICE_DP}f} more than last fill) to add.", **kw)
    if ask < float(last["ask"]) + float(v.get("add_min_ask_rise", 0.02)):
        return D("skip", "price_not_rising", f"{side} is {ask*100:.0f}c, not at least {float(v.get('add_min_ask_rise', 0.02))*100:.0f}c above the last fill ({float(last['ask'])*100:.0f}c).", **kw)
    if ask > float(v.get("add_max_ask", 0.9)):
        return D("skip", "ask_band", f"{side} at {ask*100:.0f}c is too expensive to add (max {float(v.get('add_max_ask', 0.9))*100:.0f}c).", **kw)
    budget = min(float(v.get("add_stake", 3)), cap - spent)
    shares = math.floor(budget / (ask + fps) * 100) / 100
    if shares + 1e-9 < config.MIN_ORDER_SHARES:
        if config.MIN_ORDER_SHARES * (ask + fps) <= cap - spent + 1e-9:
            shares = float(config.MIN_ORDER_SHARES)
        else:
            return D("skip", "round_cap", f"Round already has ${spent:.2f} in; 5 more shares would push it over ${cap:.0f}.", **kw)
    if book.best_ask_size + 1e-9 < shares:
        return D("skip", "thin_book", f"Only {book.best_ask_size:.1f} shares offered at {ask*100:.0f}c; need {shares:.2f}.", **kw)
    notional, fee = shares * ask, shares * fps
    if spent + notional + fee > cap + 1e-9:
        return D("skip", "round_cap", f"Add would push the round over ${cap:.0f} with fees.", **kw)
    return D("buy", "ok", f"Adding ${notional:.2f} of {side} (add #{len(fills)}): XRP now ${gap:+.{config.PRICE_DP}f} past the price to beat (was ${float(last['gap']):+.{config.PRICE_DP}f}), "
             f"{side} up to {ask*100:.0f}c from {float(last['ask'])*100:.0f}c; round total ${spent + notional + fee:.2f} incl. fees.",
             stake=notional, shares=shares, notional=notional, fee=fee, total_cost=notional + fee, **kw)
