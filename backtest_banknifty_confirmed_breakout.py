"""Underlying-points backtest for banknifty_confirmed_breakout.py.

This measures signal behavior only. It is not an options P&L backtest.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from banknifty_confirmed_breakout import generate_signals


def run(path: Path, points_cost: float = 0.0) -> pd.DataFrame:
    frame = pd.read_csv(path)
    time_col = next((c for c in ("date", "datetime", "timestamp") if c in frame), None)
    if time_col is None:
        raise ValueError("CSV needs a date, datetime, or timestamp column")
    frame[time_col] = pd.to_datetime(frame[time_col])
    frame = frame.set_index(time_col)
    frame.columns = [str(c).lower() for c in frame.columns]
    d = generate_signals(frame)
    trades: list[dict] = []
    for stamp, row in d[d.signal.ne(0)].iterrows():
        i = d.index.get_loc(stamp)
        if i + 1 >= len(d):
            continue
        entry_row = d.iloc[i + 1]
        side = int(row.signal)
        entry = float(entry_row.open)
        risk = float(row.stop_points)
        stop = entry - side * risk
        target = entry + side * float(row.target_points)
        session = stamp.date()
        exit_price, reason, exit_time = float(d.iloc[-1].close), "session_end", d.index[-1]
        for j in range(i + 1, len(d)):
            bar = d.iloc[j]
            if d.index[j].date() != session:
                exit_price, reason, exit_time = float(d.iloc[j - 1].close), "session_end", d.index[j - 1]
                break
            stop_hit = float(bar.low) <= stop if side == 1 else float(bar.high) >= stop
            target_hit = float(bar.high) >= target if side == 1 else float(bar.low) <= target
            if stop_hit:  # Conservative when both levels are touched in one candle.
                exit_price, reason, exit_time = stop, "stop", d.index[j]
                break
            if target_hit:
                exit_price, reason, exit_time = target, "target", d.index[j]
                break
            if d.index[j].hour * 60 + d.index[j].minute >= 910:
                exit_price, reason, exit_time = float(bar.close), "time_exit", d.index[j]
                break
        pnl = side * (exit_price - entry) - points_cost
        trades.append({"signal_time": stamp, "entry_time": d.index[i + 1], "exit_time": exit_time,
                       "side": side, "entry": entry, "exit": exit_price, "risk_points": risk,
                       "pnl_points": pnl, "r_multiple": pnl / risk, "exit_reason": reason})
    result = pd.DataFrame(trades)
    if result.empty:
        print("No trades generated.")
        return result
    wins = result.loc[result.pnl_points > 0, "pnl_points"].sum()
    losses = -result.loc[result.pnl_points < 0, "pnl_points"].sum()
    print(f"Trades: {len(result)} | win rate: {(result.pnl_points > 0).mean():.1%} | "
          f"net underlying points: {result.pnl_points.sum():.1f} | "
          f"profit factor: {wins / losses if losses else float('inf'):.2f}")
    result.to_csv(path.with_name(path.stem + "_confirmed_breakout_trades.csv"), index=False)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--points-cost", type=float, default=0.0,
                        help="round-trip underlying points deducted per trade")
    args = parser.parse_args()
    run(args.csv, args.points_cost)
