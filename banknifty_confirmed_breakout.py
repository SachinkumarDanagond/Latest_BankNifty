"""Standalone Bank Nifty intraday signal engine (research/paper use).

Input: timestamped 5-minute Bank Nifty OHLC candles. Signals are for the
underlying direction and can be mapped to long CE/PE option ideas. This does
not select contracts or model option premiums.
"""
from __future__ import annotations

import pandas as pd


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).where(loss.ne(0), 100)


def _indicators(data: pd.DataFrame) -> pd.DataFrame:
    d = data.copy().sort_index()
    tr = pd.concat([
        d.high - d.low,
        (d.high - d.close.shift()).abs(),
        (d.low - d.close.shift()).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    up = d.high.diff()
    down = -d.low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = d.atr.replace(0, float("nan"))
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    d["adx"] = dx.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    d["plus_di"], d["minus_di"] = plus_di, minus_di
    d["ema20"] = d.close.ewm(span=20, min_periods=20, adjust=False).mean()
    d["ema50"] = d.close.ewm(span=50, min_periods=50, adjust=False).mean()
    d["rsi"] = _rsi(d.close)
    return d


def generate_signals(candles: pd.DataFrame) -> pd.DataFrame:
    """Return candles plus signal (+1 CE bias, -1 PE bias, 0 no trade).

    Candle index must be timezone-aware Asia/Kolkata or local exchange time,
    labeled by candle start. Requires lowercase open/high/low/close columns.
    The first 15 minutes define the opening range. A trigger needs two
    consecutive closes outside that range, aligned EMA/DI trend, ADX >= 20,
    and RSI confirmation. Use the signal only after its candle has closed.
    """
    required = {"open", "high", "low", "close"}
    missing = required - set(candles.columns)
    if missing:
        raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
    d = _indicators(candles)
    idx = pd.DatetimeIndex(d.index)
    if idx.tz is None:
        idx = idx.tz_localize("Asia/Kolkata")
        d.index = idx
    else:
        d.index = idx.tz_convert("Asia/Kolkata")
    d["session"] = d.index.date
    mins = d.index.hour * 60 + d.index.minute
    d["signal"] = 0
    d["opening_high"] = float("nan")
    d["opening_low"] = float("nan")
    d["stop_points"] = float("nan")
    d["target_points"] = float("nan")
    for _, group in d.groupby("session", sort=False):
        # Start-labeled bars at 09:15, 09:20, 09:25 form the 09:15-09:30 range.
        opening = group[(group.index.hour == 9) & (group.index.minute >= 15) & (group.index.minute < 30)]
        if len(opening) < 3:
            continue
        hi, lo = float(opening.high.max()), float(opening.low.min())
        d.loc[group.index, "opening_high"] = hi
        d.loc[group.index, "opening_low"] = lo
        close = group.close
        prior_close = close.shift(1)
        long_ok = (
            (close > hi) & (prior_close > hi) & (group.ema20 > group.ema50)
            & (group.close > group.ema20) & (group.plus_di > group.minus_di)
            & (group.adx >= 20) & group.rsi.between(55, 75)
        )
        short_ok = (
            (close < lo) & (prior_close < lo) & (group.ema20 < group.ema50)
            & (group.close < group.ema20) & (group.minus_di > group.plus_di)
            & (group.adx >= 20) & group.rsi.between(25, 45)
        )
        # Limit entries to 10:00-14:30; take at most the first qualified setup/day.
        allowed = (group.index.hour * 60 + group.index.minute >= 600) & (group.index.hour * 60 + group.index.minute <= 870)
        candidates = (long_ok | short_ok) & allowed & group.atr.notna()
        if candidates.any():
            pos = candidates.to_numpy().argmax()
            stamp = group.index[pos]
            side = 1 if bool(long_ok.iloc[pos]) else -1
            risk = max(1.2 * float(group.atr.iloc[pos]), 0.35 * (hi - lo))
            d.loc[stamp, "signal"] = side
            d.loc[stamp, "stop_points"] = risk
            d.loc[stamp, "target_points"] = 1.8 * risk
    return d
