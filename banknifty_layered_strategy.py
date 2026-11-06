"""Three-layer Bank Nifty signal framework for research and paper trading.

This module emits underlying directional signals only. It does not place orders,
select option contracts, or model short-option risk. Optional confirmation data
must be merged by timestamp from a reliable market-data source.
"""
from __future__ import annotations

import pandas as pd

from banknifty_confirmed_breakout import _indicators


def evaluate(candles: pd.DataFrame) -> pd.DataFrame:
    """Annotate 5-minute OHLC candles with regime, trigger, confirmation, signal.

    Optional input columns: vwap, india_vix, futures_oi_change_pct,
    option_chain_score (-1/0/+1), volume. A valid signal requires at least two
    of the three confirmation families; missing data is not counted as support.
    """
    required = {"open", "high", "low", "close"}
    if required - set(candles.columns):
        raise ValueError(f"Required OHLC columns missing: {sorted(required - set(candles.columns))}")
    d = candles.copy().sort_index()
    idx = pd.DatetimeIndex(d.index)
    d.index = idx.tz_localize("Asia/Kolkata") if idx.tz is None else idx.tz_convert("Asia/Kolkata")
    d = _indicators(d)
    d["session"] = d.index.date
    # Use session-to-date realized range as a volatility measure; percentile is
    # calculated from prior observations only to avoid look-ahead.
    d["atr_pct"] = d.atr / d.close
    d["atr_rank"] = d.atr_pct.shift(1).rolling(252, min_periods=60).rank(pct=True)
    d["previous_high"] = d.groupby("session").high.transform("max").groupby(d.session).shift(1)
    d["previous_low"] = d.groupby("session").low.transform("min").groupby(d.session).shift(1)
    # Replace with true prior-session levels. These are constant throughout the day.
    daily = d.groupby("session").agg(day_high=("high", "max"), day_low=("low", "min"))
    daily["prior_high"], daily["prior_low"] = daily.day_high.shift(1), daily.day_low.shift(1)
    d["previous_high"] = d.session.map(daily.prior_high)
    d["previous_low"] = d.session.map(daily.prior_low)

    if "vwap" not in d:
        if "volume" in d and d.volume.fillna(0).gt(0).any():
            typical = (d.high + d.low + d.close) / 3
            pv = typical * d.volume.clip(lower=0)
            d["vwap"] = pv.groupby(d.session).cumsum() / d.volume.clip(lower=0).groupby(d.session).cumsum().replace(0, float("nan"))
        else:
            d["vwap"] = float("nan")

    # India VIX is a NIFTY-option-derived 30-day volatility measure. Use its
    # relative direction/rank as context, not as a BANKNIFTY-specific IV quote.
    if "india_vix" in d:
        d["vix_rising"] = d.india_vix.diff(3) > 0
        d["vix_rank"] = d.india_vix.shift(1).rolling(252 * 75, min_periods=500).rank(pct=True)
    else:
        d["vix_rising"], d["vix_rank"] = False, float("nan")

    d["regime"] = "NO_TRADE"
    trend = (d.adx >= 20) & (d.atr_rank >= 0.60)
    balanced = (d.adx < 18) & (d.atr_rank <= 0.60)
    d.loc[trend, "regime"] = "TREND"
    d.loc[balanced, "regime"] = "BALANCED"
    # Extreme/rising volatility vetoes new entries pending calibrated limits.
    vix_veto = d.vix_rising & (d.vix_rank >= 0.95)
    d.loc[vix_veto, "regime"] = "NO_TRADE"

    d["trigger"] = "NONE"
    d["trigger_side"] = 0
    # Opening range breakout: two completed closes outside the first 15m range.
    d["or_high"] = d["or_low"] = float("nan")
    for session, group in d.groupby("session", sort=False):
        opening = group[(group.index.hour == 9) & group.index.minute.isin([15, 20, 25])]
        if len(opening) != 3:
            continue
        hi, lo = opening.high.max(), opening.low.min()
        d.loc[group.index, "or_high"], d.loc[group.index, "or_low"] = hi, lo
        long_or = (group.close > hi) & (group.close.shift() > hi)
        short_or = (group.close < lo) & (group.close.shift() < lo)
        trend_mask = group.regime.eq("TREND")
        d.loc[group.index[long_or & trend_mask], "trigger"] = "ORB"
        d.loc[group.index[long_or & trend_mask], "trigger_side"] = 1
        d.loc[group.index[short_or & trend_mask], "trigger"] = "ORB"
        d.loc[group.index[short_or & trend_mask], "trigger_side"] = -1

    # Balanced-regime trigger family: VWAP reclaim/rejection with close confirmation.
    long_vwap = d.regime.eq("BALANCED") & (d.close > d.vwap) & (d.close.shift() <= d.vwap.shift())
    short_vwap = d.regime.eq("BALANCED") & (d.close < d.vwap) & (d.close.shift() >= d.vwap.shift())
    d.loc[long_vwap, "trigger"] = "VWAP_RECLAIM"
    d.loc[long_vwap, "trigger_side"] = 1
    d.loc[short_vwap, "trigger"] = "VWAP_REJECTION"
    d.loc[short_vwap, "trigger_side"] = -1

    # Three independent confirmation families. Each is directional and counts
    # once; unknown/unavailable fields remain neutral, never an assumed pass.
    side = d.trigger_side
    vol_ok = pd.Series(False, index=d.index)
    if "volume" in d:
        vol_base = d.volume.shift(1).rolling(20, min_periods=10).mean()
        vol_ok = (d.volume > 0) & (vol_base > 0) & (d.volume >= 1.3 * vol_base)
    oi_ok = pd.Series(False, index=d.index)
    if "futures_oi_change_pct" in d:
        price_direction = d.close.pct_change() * side > 0
        oi_ok = price_direction & d.futures_oi_change_pct.ge(0.25) & side.ne(0)
    chain_ok = pd.Series(False, index=d.index)
    if "option_chain_score" in d:
        chain_ok = d.option_chain_score.eq(side) & side.ne(0)
    d["confirmations"] = vol_ok.astype(int) + oi_ok.astype(int) + chain_ok.astype(int)
    d["signal"] = 0
    eligible_time = (d.index.hour * 60 + d.index.minute >= 600) & (d.index.hour * 60 + d.index.minute <= 870)
    # Only trend-regime directional entries are actionable in this first
    # version. Balanced-regime VWAP events are labeled for research; short
    # premium requires BANKNIFTY IV and defined-risk payoff validation first.
    valid = side.ne(0) & d.regime.eq("TREND") & d.confirmations.ge(2) & eligible_time
    # Keep only the first qualified signal of each session.
    for _, group in d[valid].groupby("session", sort=False):
        d.loc[group.index[0], "signal"] = int(d.loc[group.index[0], "trigger_side"])
    d["stop_points"] = 1.2 * d.atr
    d["target_points"] = 1.8 * d.stop_points
    return d
