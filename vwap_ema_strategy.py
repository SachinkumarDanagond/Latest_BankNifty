"""Signal rules based on the linked 5-minute EMA(20)/VWAP Chartink scan.

The original scan is bearish. A symmetric bullish mirror is provided as an
explicit extension. This is a research example, not a performance claim.
"""
from __future__ import annotations

import pandas as pd


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Add intraday VWAP and EMA20; input index must be candle timestamps."""
    data = frame.copy().sort_index()
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"Missing OHLCV columns: {', '.join(sorted(missing))}")
    data["ema20"] = data.groupby(data.index.date)["close"].transform(
        lambda s: s.ewm(span=20, adjust=False, min_periods=20).mean()
    )
    typical = (data["high"] + data["low"] + data["close"]) / 3.0
    pv = typical * data["volume"].clip(lower=0)
    dates = pd.Series(data.index.date, index=data.index)
    data["vwap"] = pv.groupby(dates).cumsum() / data["volume"].clip(lower=0).groupby(dates).cumsum().replace(0, pd.NA)
    return data


def signals(frame: pd.DataFrame, allow_long: bool = True) -> pd.DataFrame:
    """Return per-candle signal: +1 long, -1 short, 0 none.

    Replicates visible Chartink filters where practical: price > 100, close
    within 0.1% of EMA20, EMA/VWAP separation >= 0.4%, EMA20 declining over
    1 and 3 bars (or rising for mirrored long), and separation widening vs
    prior bars. The source scan also filters futures-segment stocks and market
    cap; this implementation relies on the configured watchlist instead.
    """
    d = add_indicators(frame)
    ema, vwap, close = d.ema20, d.vwap, d.close
    ema_gap_pct = 100.0 * (ema - vwap) / vwap
    close_ema_pct = 100.0 * (close - ema).abs() / ema
    common = close.gt(100) & close_ema_pct.le(0.1) & ema.notna() & vwap.notna()
    bear = (
        common & ema.lt(vwap) & close.le(ema)
        & ((vwap - ema) * 100.0 / ema).ge(0.4)
        & ema.lt(ema.shift(1)) & ema.lt(ema.shift(3))
        & ema_gap_pct.lt(ema_gap_pct.shift(8))
        & (vwap - ema).gt((vwap - ema).shift(1))
        & ((vwap - ema).shift(1)).gt((vwap - ema).shift(2))
    )
    out = pd.Series(0, index=d.index, dtype="int64")
    out.loc[bear.fillna(False)] = -1
    if allow_long:
        bull = (
            common & ema.gt(vwap) & close.ge(ema)
            & ((ema - vwap) * 100.0 / ema).ge(0.4)
            & ema.gt(ema.shift(1)) & ema.gt(ema.shift(3))
            & ema_gap_pct.gt(ema_gap_pct.shift(8))
            & (ema - vwap).gt((ema - vwap).shift(1))
            & ((ema - vwap).shift(1)).gt((ema - vwap).shift(2))
        )
        out.loc[bull.fillna(False)] = 1
    d["signal"] = out
    return d
