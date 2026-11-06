"""Candle-based backtest for vwap_ema_strategy.py; no look-ahead entries."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from vwap_ema_strategy import signals


def run_backtest(path: Path, risk_pct: float = 0.005, reward_risk: float = 1.5,
                 fee_bps: float = 10.0, initial_capital: float = 100_000.0):
    df = pd.read_csv(path)
    time_col = next((c for c in ("date", "datetime", "timestamp") if c in df.columns), None)
    if not time_col:
        raise ValueError("CSV needs a date/datetime/timestamp column")
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.set_index(time_col)
    df.columns = [str(c).lower() for c in df.columns]
    d = signals(df)
    equity, trades, position = initial_capital, [], None
    for i in range(1, len(d)):
        row, prev = d.iloc[i], d.iloc[i - 1]
        # Signals are confirmed at this bar close; enter at next bar open.
        if position:
            exit_px, reason = None, ""
            if position["side"] == 1:
                if row.low <= position["stop"]:
                    exit_px, reason = position["stop"], "stop"
                elif row.high >= position["target"]:
                    exit_px, reason = position["target"], "target"
                elif row.signal == -1:
                    exit_px, reason = row.close, "opposite_signal"
            else:
                if row.high >= position["stop"]:
                    exit_px, reason = position["stop"], "stop"
                elif row.low <= position["target"]:
                    exit_px, reason = position["target"], "target"
                elif row.signal == 1:
                    exit_px, reason = row.close, "opposite_signal"
            if exit_px is not None:
                gross = (exit_px - position["entry"]) * position["qty"] * position["side"]
                fees = (exit_px + position["entry"]) * position["qty"] * fee_bps / 10_000
                net = gross - fees
                equity += net
                trades.append({**position, "exit_time": d.index[i], "exit": exit_px,
                               "gross_pnl": gross, "fees_est": fees, "net_pnl": net,
                               "reason": reason, "equity": equity})
                position = None
        if position is None and row.signal and i + 1 < len(d):
            entry = float(d.iloc[i + 1].open)
            # Signal candle extreme is the invalidation level; gap-adjusted risk.
            side = int(row.signal)
            stop = float(row.low if side == 1 else row.high)
            risk_per_share = abs(entry - stop)
            if risk_per_share <= 0 or entry <= 0:
                continue
            qty = int((equity * risk_pct) / risk_per_share)
            if qty < 1:
                continue
            target = entry + side * risk_per_share * reward_risk
            position = {"entry_time": d.index[i + 1], "signal_time": d.index[i],
                        "side": side, "entry": entry, "stop": stop,
                        "target": target, "qty": qty}
    if position:
        last = d.iloc[-1]
        exit_px = float(last.close)
        gross = (exit_px - position["entry"]) * position["qty"] * position["side"]
        fees = (exit_px + position["entry"]) * position["qty"] * fee_bps / 10_000
        net = gross - fees
        equity += net
        trades.append({**position, "exit_time": d.index[-1], "exit": exit_px,
                       "gross_pnl": gross, "fees_est": fees, "net_pnl": net,
                       "reason": "end_of_data", "equity": equity})
    result = pd.DataFrame(trades)
    if result.empty:
        print("No trades found. Ensure the CSV contains 5-minute OHLCV candles across full sessions.")
    else:
        wins = result.net_pnl.gt(0).sum()
        print(f"Trades: {len(result)} | Win rate: {wins / len(result):.1%} | Net P&L: ₹{result.net_pnl.sum():,.2f}")
        print(f"Ending equity: ₹{equity:,.2f} | Fees estimate: ₹{result.fees_est.sum():,.2f}")
        print(f"Profit factor: {result.loc[result.net_pnl > 0, 'net_pnl'].sum() / abs(result.loc[result.net_pnl < 0, 'net_pnl'].sum()) if (result.net_pnl < 0).any() else float('inf'):.2f}")
        out = path.with_name(path.stem + "_trades.csv")
        result.to_csv(out, index=False)
        print(f"Trade log: {out}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path, help="5-minute OHLCV CSV")
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--reward-risk", type=float, default=1.5)
    parser.add_argument("--fee-bps", type=float, default=10.0, help="estimated round-trip charges in bps of turnover")
    parser.add_argument("--capital", type=float, default=100_000)
    args = parser.parse_args()
    run_backtest(args.csv, args.risk_pct, args.reward_risk, args.fee_bps, args.capital)
