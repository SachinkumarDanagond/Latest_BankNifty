"""Kite Connect implementation of the EMA20/VWAP 5-minute scan.

Paper mode is the default. Live market orders require two explicit env flags.
This sample is long/short cash MIS; short trades must be closed the same day.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect

from vwap_ema_strategy import signals

IST = ZoneInfo("Asia/Kolkata")
LOG = logging.getLogger("kite_vwap_ema")
OPEN = dt_time(9, 15)
LAST_ENTRY = dt_time(14, 45)
SQUARE_OFF = dt_time(15, 15)


def env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().upper() == "YES"


def start():
    api_key = os.environ["KITE_API_KEY"]
    access_token = os.environ["KITE_ACCESS_TOKEN"]
    live = env_bool("KITE_LIVE") and os.getenv("CONFIRM_LIVE_ORDERS") == "I_UNDERSTAND"
    if env_bool("KITE_LIVE") and not live:
        raise RuntimeError("Live requested but CONFIRM_LIVE_ORDERS=I_UNDERSTAND is missing")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    # Refuse to start with existing broker positions: internal state is in-memory.
    positions = kite.positions().get("net", [])
    open_positions = [p for p in positions if p.get("quantity", 0)]
    if open_positions:
        raise RuntimeError("Close/reconcile all existing positions before starting this process")
    watch = [s.strip().upper() for s in os.getenv(
        "WATCHLIST", "RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN,AXISBANK,ITC,BHARTIARTL"
    ).split(",") if s.strip()]
    if not watch:
        raise RuntimeError("WATCHLIST is empty")
    instruments = {i["tradingsymbol"]: i for i in kite.instruments("NSE") if i["tradingsymbol"] in watch}
    missing = set(watch) - set(instruments)
    if missing:
        raise RuntimeError(f"Symbols not found in NSE instrument list: {', '.join(sorted(missing))}")
    risk_inr = float(os.getenv("RISK_PER_TRADE_INR", "500"))
    max_trades = int(os.getenv("MAX_TRADES_PER_DAY", "2"))
    poll_seconds = max(60, int(os.getenv("POLL_SECONDS", "60")))
    positions: dict[str, dict] = {}
    last_seen: dict[str, datetime] = {}
    trade_count = 0
    session_date = None
    LOG.warning("Starting %s mode; symbols=%s", "LIVE ORDERS" if live else "PAPER", ",".join(watch))
    while True:
        now = datetime.now(IST)
        if now.date() != session_date:
            session_date, trade_count, positions, last_seen = now.date(), 0, {}, {}
        if now.time() < OPEN or now.time() >= SQUARE_OFF:
            if now.time() >= SQUARE_OFF and positions:
                for symbol in list(positions):
                    close_position(kite, symbol, positions.pop(symbol), live)
            time.sleep(15)
            continue
        start_at = datetime.combine(now.date(), OPEN, tzinfo=IST)
        for symbol, instrument in instruments.items():
            try:
                candles = kite.historical_data(instrument["instrument_token"], start_at,
                                               now, "5minute")
                if not candles:
                    continue
                frame = pd.DataFrame(candles).set_index("date")
                frame.index = pd.to_datetime(frame.index)
                if frame.index.tz is None:
                    frame.index = frame.index.tz_localize(IST)
                else:
                    frame.index = frame.index.tz_convert(IST)
                frame = frame.rename(columns={"open": "open", "high": "high", "low": "low",
                                              "close": "close", "volume": "volume"})
                # Kite labels candles by their start. Exclude the still-forming candle.
                frame = frame.loc[frame.index + pd.Timedelta(minutes=5) <= now]
                if len(frame) < 24:
                    continue
                bar_time = frame.index[-1].to_pydatetime()
                if last_seen.get(symbol) == bar_time:
                    continue
                last_seen[symbol] = bar_time
                d = signals(frame)
                bar = d.iloc[-1]
                sig = int(bar.signal)
                pos = positions.get(symbol)
                if pos:
                    ltp = float(kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["last_price"])
                    hit = (ltp <= pos["stop"] or ltp >= pos["target"]) if pos["side"] == 1 else (ltp >= pos["stop"] or ltp <= pos["target"])
                    opposite = sig == -pos["side"]
                    if hit or opposite:
                        close_position(kite, symbol, positions.pop(symbol), live)
                    continue
                if not sig or now.time() >= LAST_ENTRY or trade_count >= max_trades:
                    continue
                entry_ref = float(kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["last_price"])
                side = sig
                stop = float(bar.low if side == 1 else bar.high)
                per_share = abs(entry_ref - stop)
                qty = int(risk_inr / per_share) if per_share > 0 else 0
                if qty < 1:
                    continue
                target = entry_ref + side * per_share * float(os.getenv("REWARD_RISK", "1.5"))
                order_id = None
                if live:
                    order_id = kite.place_order(variety=kite.VARIETY_REGULAR,
                        exchange=kite.EXCHANGE_NSE, tradingsymbol=symbol,
                        transaction_type=kite.TRANSACTION_TYPE_BUY if side == 1 else kite.TRANSACTION_TYPE_SELL,
                        quantity=qty, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET,
                        validity=kite.VALIDITY_DAY)
                positions[symbol] = {"side": side, "qty": qty, "stop": stop,
                                     "target": target, "entry_ref": entry_ref, "order_id": order_id}
                trade_count += 1
                LOG.warning("%s ENTRY %s qty=%d ref=%.2f stop=%.2f target=%.2f order=%s",
                            "LIVE" if live else "PAPER", "BUY" if side == 1 else "SELL",
                            qty, entry_ref, stop, target, order_id or "paper")
            except Exception:
                LOG.exception("Scan failed for %s; no new trade for this symbol on this pass", symbol)
        time.sleep(poll_seconds)


def close_position(kite, symbol: str, pos: dict, live: bool):
    order_id = None
    if live:
        order_id = kite.place_order(variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE, tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_SELL if pos["side"] == 1 else kite.TRANSACTION_TYPE_BUY,
            quantity=pos["qty"], product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET,
            validity=kite.VALIDITY_DAY)
    LOG.warning("%s EXIT %s qty=%s order=%s", "LIVE" if live else "PAPER",
                symbol, pos["qty"], order_id or "paper")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start()
