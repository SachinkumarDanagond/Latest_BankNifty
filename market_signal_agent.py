"""Kite Connect stock option alert monitor.

This script watches only individual stock symbols and prints CALL/PUT alerts when
recent price action suggests a directional breakout or breakdown.
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent

WATCHLIST = [
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "AXISBANK",
    "KOTAKBANK",
    "ITC",
    "WIPRO",
    "HINDUNILVR",
    "SUNPHARMA",
    "BHARTIARTL",
    "COFORGE",
    "JSWENERGY",
    "LTIM",
    "TATAMOTORS",
    "TATASTEEL",
    "HCLTECH",
    "TECHM",
    "INDUSINDBK",
    "BAJFINANCE",
    "MARUTI",
    "TITAN",
    "ASIANPAINT",
    "ULTRACEMCO",
    "NTPC",
    "POWERGRID",
    "IOC",
    "GAIL",
    "BRITANNIA",
    "CIPLA",
    "DRREDDY",
    "SUNPHARMA",
    "EICHERMOT",
    "HEROMOTOCO",
    "M&M",
    "BEL",
    "TRENT",
    "HDFCLIFE",
    "METROPOLIS",
    "APOLLOHOSP",
    "NESTLEIND",
    "SHREECEM",
    "DIVISLAB",
    "BPCL",
    "GRASIM",
    "HINDALCO",
    "ONGC",
    "BAJAJ-AUTO",
    "UPL",
    "CHOLAFIN",
    "ADANIPORTS",
    "INDIGO",
    "ABB",
    "BHEL",
    "PIDILITIND",
    "DMART",
    "TATACONSUM",
    "TORNTPHARM",
    "SYNGENE",
    "PAGEIND",
    "NMDC",
    "PAYTM",
    "LTTS",
    "LUPIN",
    "TATAELXSI",
    "BANKBARODA",
    "PFC",
    "RBLBANK",
    "AUBANK",
    "ZEEL",
    "HDFCAMC",
]


@dataclass
class PaperTrade:
    label: str
    signal: str
    entry_price: float
    stop_price: float
    target_price: float
    entry_time: datetime
    exit_price: float | None = None
    exit_time: datetime | None = None
    pnl: float | None = None
    result: str | None = None
    exit_reason: str | None = None


class PaperTradeEvaluator:
    def __init__(self) -> None:
        self.open_trades: dict[str, PaperTrade] = {}
        self.completed: list[PaperTrade] = []
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.net_pnl = 0.0
        self.csv_path = ROOT / "paper_trades.csv"
        self._ensure_csv_header()

    def _ensure_csv_header(self) -> None:
        file_exists = self.csv_path.exists()
        with self.csv_path.open("a", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            if not file_exists:
                writer.writerow([
                    "label",
                    "signal",
                    "entry_price",
                    "exit_price",
                    "pnl",
                    "result",
                    "exit_reason",
                    "entry_time",
                    "exit_time",
                ])

    def open_trade(self, label: str, signal: str, price: float, now: datetime) -> None:
        if label in self.open_trades:
            return
        stop = price * 0.995 if signal == "BUY" else price * 1.005
        target = price * 1.02 if signal == "BUY" else price * 0.98
        self.open_trades[label] = PaperTrade(
            label=label,
            signal=signal,
            entry_price=price,
            stop_price=stop,
            target_price=target,
            entry_time=now,
        )
        print(
            f"[PAPER] OPEN {label}: {signal} @ {price:.2f} | stop={stop:.2f} | target={target:.2f}"
        )

    def _finalize_trade(self, label: str, current_price: float, now: datetime, exit_reason: str) -> None:
        trade = self.open_trades.get(label)
        if trade is None:
            return

        if trade.signal == "BUY":
            pnl = current_price - trade.entry_price
        else:
            pnl = trade.entry_price - current_price

        trade.exit_price = current_price
        trade.exit_time = now
        trade.pnl = pnl
        trade.exit_reason = exit_reason
        trade.result = "WIN" if pnl > 0 else "LOSS"

        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        self.net_pnl += pnl
        self.completed.append(trade)

        with self.csv_path.open("a", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow([
                trade.label,
                trade.signal,
                f"{trade.entry_price:.2f}",
                f"{trade.exit_price:.2f}",
                f"{trade.pnl:.2f}",
                trade.result,
                trade.exit_reason,
                trade.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                trade.exit_time.strftime("%Y-%m-%d %H:%M:%S") if trade.exit_time else "",
            ])

        del self.open_trades[label]

        print(
            f"[PAPER] CLOSE {label}: {trade.signal} entry={trade.entry_price:.2f} "
            f"exit={current_price:.2f} pnl={pnl:.2f} result={trade.result} reason={exit_reason}"
        )

    def close_on_opposite_signal(self, label: str, signal: str, current_price: float, now: datetime) -> None:
        trade = self.open_trades.get(label)
        if trade is None:
            return
        if trade.signal == signal:
            return
        self._finalize_trade(label, current_price, now, "OPPOSITE_SIGNAL")

    def print_summary(self) -> None:
        win_rate = (self.winning_trades / self.total_trades * 100.0) if self.total_trades else 0.0
        print(
            "[PAPER] SUMMARY: total="
            f"{self.total_trades} wins={self.winning_trades} losses={self.losing_trades} "
            f"win_rate={win_rate:.2f}% net_pnl={self.net_pnl:.2f}"
        )


def load_credentials() -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / "credentials.txt").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    api_key = os.getenv("KITE_API_KEY", values.get("api_key", "qlyrlqmykrr3ld2h"))
    api_secret = os.getenv("KITE_API_SECRET", values.get("api_secret", "2do4ifsheml8hvluc1xgfc47sr2vvt4k"))
    if not api_key or not api_secret:
        raise RuntimeError("Set KITE_API_KEY and KITE_API_SECRET or fill credentials.txt")
    return api_key, api_secret


def build_instrument_lookup(kite: KiteConnect) -> dict[str, dict]:
    targets = {item.upper() for item in WATCHLIST}
    lookup: dict[str, dict] = {}
    option_tradable: set[str] = set()

    for row in kite.instruments("NFO"):
        name = str(row.get("name", "")).strip().upper()
        instrument_type = str(row.get("instrument_type", "")).strip().upper()
        if name in targets and instrument_type in {"CE", "PE"}:
            option_tradable.add(name)

    for row in kite.instruments("NSE"):
        name = str(row.get("name", "")).strip().upper()
        symbol = str(row.get("tradingsymbol", "")).strip().upper()
        for key in (name, symbol):
            if key in targets and name in option_tradable:
                lookup[key] = row
    return lookup


def find_instrument_map(kite: KiteConnect) -> dict[str, dict]:
    lookup = build_instrument_lookup(kite)
    selected: dict[str, dict] = {}
    for item in WATCHLIST:
        key = item.upper()
        if key in lookup:
            selected[item] = lookup[key]
        else:
            print(f"[WARN] {item} not option-tradable on Kite; skipped")
    return selected


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = (alpha * value) + ((1 - alpha) * out)
    return out


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0

    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(delta, 0.0) for delta in deltas[-period:]]
    losses = [max(-delta, 0.0) for delta in deltas[-period:]]
    avg_gain = sum(gains) / len(gains) if gains else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def detect_signal(candles: list[dict]) -> tuple[str | None, float, float, float, float]:
    if len(candles) < 20:
        return None, 0.0, 0.0, 0.0, 0.0

    closes = [float(c.get("close", 0.0)) for c in candles if c.get("close") is not None]
    if len(closes) < 20:
        return None, 0.0, 0.0, 0.0, 0.0

    last = closes[-1]
    recent = closes[-10:]
    sma = sum(recent) / len(recent)
    low = min(recent)
    high = max(recent)
    mid = (low + high) / 2.0

    fast_ema = ema(closes, 9)
    slow_ema = ema(closes, 21)
    rsi_value = rsi(closes, 14)
    trend_up = closes[-3] < closes[-2] < closes[-1]
    trend_down = closes[-3] > closes[-2] > closes[-1]

    intraday_buy = last > sma and last > mid and fast_ema > slow_ema and rsi_value > 55 and trend_up
    intraday_put = last < sma and last < mid and fast_ema < slow_ema and rsi_value < 45 and trend_down

    if intraday_buy:
        return "BUY", last, sma, low, high
    if intraday_put:
        return "PUT", last, sma, low, high
    return None, last, sma, low, high


def stock_option_contract_for_signal(kite: KiteConnect, stock_name: str, signal: str, current_price: float) -> dict | None:
    option_type = "CE" if signal == "BUY" else "PE"
    today = datetime.now(IST).date()
    candidates: list[dict] = []

    for row in kite.instruments("NFO"):
        name = str(row.get("name", "")).strip().upper()
        instrument_type = str(row.get("instrument_type", "")).strip().upper()
        expiry = row.get("expiry")
        if not expiry or name != stock_name.upper() or instrument_type != option_type:
            continue
        if expiry < today:
            continue
        strike = float(row.get("strike", 0.0) or 0.0)
        candidates.append({"row": row, "diff": abs(strike - current_price)})

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item["diff"], row_expiry_sort(item["row"])))
    return candidates[0]["row"]


def row_expiry_sort(row: dict):
    expiry = row.get("expiry")
    if expiry is None:
        return datetime.max.date()
    return expiry


def place_trigger_order(kite: KiteConnect, instrument: dict, signal: str, limit_price: float | None = None) -> None:
    live_mode = False
    if os.getenv("KITE_LIVE", "NO").strip().upper() == "YES":
        print("[PAPER MODE ONLY] KITE_LIVE=YES is ignored; live orders are disabled.")
        return
    print("[PAPER MODE] no real order will be placed.")
    return

    qty = int(os.getenv("ORDER_QUANTITY", "1"))
    tradingsymbol = str(instrument.get("tradingsymbol") or instrument.get("symbol") or "")
    exchange = str(instrument.get("exchange") or "NFO").upper()
    if not tradingsymbol:
        raise ValueError("Missing tradingsymbol for order placement")

    if limit_price is None:
        raw_price = instrument.get("last_price") or instrument.get("price") or 0
        if not raw_price:
            raise ValueError(f"No valid last_price available for {tradingsymbol}; order must be a limit order")
        limit_price = float(raw_price)

    if signal == "BUY":
        final_price = max(float(limit_price) * 0.998, float(limit_price) - 0.05)
    else:
        final_price = min(float(limit_price) * 1.002, float(limit_price) + 0.05)

    order_type = kite.ORDER_TYPE_LIMIT
    transaction_type = kite.TRANSACTION_TYPE_BUY if signal == "BUY" else kite.TRANSACTION_TYPE_SELL
    product = kite.PRODUCT_MIS

    order = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        transaction_type=transaction_type,
        quantity=qty,
        product=product,
        order_type=order_type,
        price=round(float(final_price), 2),
        validity=kite.VALIDITY_DAY,
        tag="stock_option_alert",
    )
    print(
        f"ORDER PLACED: {tradingsymbol} {transaction_type} qty={qty} exchange={exchange} "
        f"type=LIMIT product={product} price={float(final_price):.2f} "
        f"order_id={order.get('order_id') if isinstance(order, dict) else order}"
    )


def monitor() -> None:
    api_key, _ = load_credentials()
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()

    print(f"KITE_API_KEY loaded: {'YES' if api_key else 'NO'}")
    print(f"KITE_ACCESS_TOKEN loaded: {'YES' if access_token else 'NO'}")
    if not access_token:
        raise RuntimeError("Set KITE_ACCESS_TOKEN to today's Kite access token")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    try:
        kite.profile()
        print("Kite credentials check: OK")
    except Exception as exc:
        raise RuntimeError(f"Kite credentials check failed: {exc}") from exc

    instruments = find_instrument_map(kite)
    if not instruments:
        raise RuntimeError("No matching instruments found in the configured stock watch list")

    live_mode = False
    print(f"Monitoring {len(instruments)} stock symbols for CALL/PUT alerts")
    print("Paper trading only: no real order will be placed.")
    print("If Kite rejects the request with 'Incorrect api_key or access_token', regenerate the access token from get_login_url.py.")
    last_signal: dict[str, str | None] = {label: None for label in instruments}
    evaluator = PaperTradeEvaluator()

    while True:
        now = datetime.now(IST)
        start = now - timedelta(days=1)
        print(f"\n[{now:%Y-%m-%d %H:%M:%S}] INTRADAY STOCK OPTION ALERT CHECK START")

        for label, row in instruments.items():
            token = int(row["instrument_token"])
            try:
                candles = kite.historical_data(token, start, now, "5minute")
            except Exception as exc:
                print(f"[{now:%H:%M:%S}] {label}: data error -> {exc}")
                continue

            signal, current, sma, low, high = detect_signal(candles)
            if not signal:
                last_signal[label] = None
                continue

            if last_signal.get(label) == signal:
                continue

            if label in evaluator.open_trades and signal != evaluator.open_trades[label].signal:
                evaluator.close_on_opposite_signal(label, signal, current, now)

            option_type = "CALL" if signal == "BUY" else "PUT"
            stop = current * 0.995 if signal == "BUY" else current * 1.005
            target = current * 1.02 if signal == "BUY" else current * 0.98
            print(
                f"[{now:%Y-%m-%d %H:%M:%S}] ALERT {label}: {signal} signal triggered | "
                f"price={current:.2f}, SMA={sma:.2f}, range={low:.2f}-{high:.2f}, token={token} | "
                f"option={option_type} | stop={stop:.2f} | target={target:.2f}"
            )

            evaluator.open_trade(label, signal, current, now)

            try:
                option_row = stock_option_contract_for_signal(kite, label, signal, current)
                if option_row is None:
                    print(f"[{now:%Y-%m-%d %H:%M:%S}] OPTION NOT FOUND {label}: no matching {signal} option for stock-option trading")
                    continue
                option_row = dict(option_row)
                option_row["exchange"] = "NFO"
                place_trigger_order(kite, option_row, signal, limit_price=current)
            except Exception as exc:
                print(f"[{now:%Y-%m-%d %H:%M:%S}] ORDER ERROR {label}: {exc}")

            last_signal[label] = signal

        evaluator.print_summary()
        print(f"[{now:%Y-%m-%d %H:%M:%S}] INTRADAY STOCK OPTION ALERT CHECK END\n")
        time.sleep(60)


if __name__ == "__main__":
    monitor()
