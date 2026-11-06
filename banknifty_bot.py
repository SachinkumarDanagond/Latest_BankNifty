"""Candidate Bank Nifty long-options bot using Zerodha Kite Connect.

Paper trading is the default. Live orders require KITE_LIVE=YES.
This is an educational strategy example, not a recommendation or guarantee.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as day_time
from pathlib import Path
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect, KiteTicker

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
STATE_LOCK = threading.RLock()
STOP = threading.Event()


def load_credentials() -> tuple[str, str]:
    """Read API credentials from credentials.txt; token is supplied separately."""
    values: dict[str, str] = {}
    for line in (ROOT / "credentials.txt").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    api_key = os.getenv("KITE_API_KEY", values.get("api_key", ""))
    api_secret = os.getenv("KITE_API_SECRET", values.get("api_secret", ""))
    if not api_key or not api_secret:
        raise RuntimeError("Set KITE_API_KEY and KITE_API_SECRET or fill credentials.txt")
    return api_key, api_secret


def env_int(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass
class Position:
    symbol: str
    token: int
    quantity: int
    entry: float
    stop: float
    target: float
    entry_order_id: str
    paper: bool


class BankNiftyBot:
    # NSE index instrument token for NIFTY BANK.
    BANKNIFTY_TOKEN = 260105
    FAST_EMA = 9
    SLOW_EMA = 21
    OR_START = day_time(9, 15)
    OR_END = day_time(9, 30)

    def __init__(self) -> None:
        api_key, _ = load_credentials()
        access_token = os.getenv("KITE_ACCESS_TOKEN", "")
        if not access_token:
            raise RuntimeError("Set KITE_ACCESS_TOKEN to today's Kite access token")
        self.live = False
        self.paper = True
        self.access_token = access_token
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)
        self.ticker = KiteTicker(api_key, access_token)
        self.max_trades = env_int("MAX_TRADES_PER_DAY", 1)
        self.lots = env_int("LOTS", 1)
        self.max_daily_loss = float(os.getenv("MAX_DAILY_LOSS_INR", "1500"))
        if self.max_daily_loss <= 0:
            raise ValueError("MAX_DAILY_LOSS_INR must be positive")
        self.stop_pct = float(os.getenv("STOP_LOSS_PCT", "0.20"))
        self.target_pct = float(os.getenv("TARGET_PCT", "0.30"))
        if not 0 < self.stop_pct < 1 or not 0 < self.target_pct < 3:
            raise ValueError("STOP_LOSS_PCT and TARGET_PCT must be sensible positive fractions")
        self.instrument_rows: list[dict] = []
        self.bank_ticks: list[tuple[datetime, float]] = []
        self.option_ltp: dict[int, tuple[datetime, float]] = {}
        self.position: Position | None = None
        self.trade_count = 0
        self.daily_pnl = 0.0
        self.last_signal_bar: datetime | None = None
        self.last_exit_reason = ""
        self.day = datetime.now(IST).date()
        self.option_token: int | None = None
        self.option_symbol: str | None = None
        self.last_reconcile = 0.0
        self.reconcile_seconds = 10
        self.log = logging.getLogger("banknifty")

    def load_instruments(self) -> None:
        self.instrument_rows = self.kite.instruments("NFO")
        if not self.instrument_rows:
            raise RuntimeError("Kite returned an empty NFO instrument list")

    def nearest_option(self, underlying: float, side: str) -> dict:
        now = datetime.now(IST)
        today = now.date()
        options = [
            row for row in self.instrument_rows
            if row.get("name") == "BANKNIFTY" and row.get("instrument_type") == side
            and row.get("expiry") and row["expiry"] >= today and int(row.get("lot_size", 0)) > 0
        ]
        if not options:
            raise RuntimeError("No future BANKNIFTY option contracts found in Kite instruments")
        expiry = min(row["expiry"] for row in options)
        chain = [row for row in options if row["expiry"] == expiry]
        return min(chain, key=lambda row: abs(float(row["strike"]) - underlying))

    @staticmethod
    def ema(values: list[float], period: int) -> float:
        alpha = 2.0 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = alpha * value + (1 - alpha) * result
        return result

    def crossover_signal(self) -> tuple[str | None, datetime | None]:
        """Strong intraday breakout signal for Bank Nifty.

        The strategy waits for a valid opening range and then requires a completed
        5-minute close beyond that range with EMA confirmation before taking a trade.
        """
        with STATE_LOCK:
            ticks = list(self.bank_ticks)
        if len(ticks) < 30:
            return None, None

        bars: dict[datetime, float] = {}
        for stamp, price in ticks:
            bucket = stamp.replace(minute=(stamp.minute // 5) * 5, second=0, microsecond=0)
            bars[bucket] = price
        ordered = sorted(bars.items())
        current_bucket = datetime.now(IST).replace(minute=(datetime.now(IST).minute // 5) * 5, second=0, microsecond=0)
        closed = [(stamp, price) for stamp, price in ordered if stamp < current_bucket]

        opening_ticks = [(stamp, price) for stamp, price in ticks if stamp.time() >= self.OR_START and stamp.time() < self.OR_END]
        opening_buckets = {stamp.replace(minute=(stamp.minute // 5) * 5, second=0, microsecond=0) for stamp, _ in opening_ticks}
        if len(opening_buckets) < 3 or len(closed) < self.SLOW_EMA + 2 or not opening_ticks:
            return None, None

        opening_high = max(price for _, price in opening_ticks)
        opening_low = min(price for _, price in opening_ticks)
        closes = [price for _, price in closed]
        fast = self.ema(closes, self.FAST_EMA)
        slow = self.ema(closes, self.SLOW_EMA)
        latest_close = closes[-1]

        if latest_close > opening_high and fast > slow:
            print(f"BANKNIFTY SIGNAL: CE | close={latest_close:.2f} | range_high={opening_high:.2f} | fast={fast:.2f} | slow={slow:.2f}")
            return "CE", closed[-1][0]
        if latest_close < opening_low and fast < slow:
            print(f"BANKNIFTY SIGNAL: PE | close={latest_close:.2f} | range_low={opening_low:.2f} | fast={fast:.2f} | slow={slow:.2f}")
            return "PE", closed[-1][0]
        return None, None

    def place_order(self, symbol: str, side: str, quantity: int) -> str:
        if self.paper:
            order_id = f"PAPER-{int(time.time() * 1000)}"
            self.log.info("PAPER %s %s quantity=%d", side, symbol, quantity)
            return order_id
        return self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=side,
            quantity=quantity,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET,
            validity=self.kite.VALIDITY_DAY,
            tag="BNFbot",
        )

    def order_average_price(self, order_id: str, fallback: float) -> float:
        if self.paper:
            return fallback
        deadline = time.time() + 8
        while time.time() < deadline:
            for order in self.kite.orders():
                if order.get("order_id") == order_id:
                    status = order.get("status")
                    if status == "COMPLETE":
                        return float(order.get("average_price") or fallback)
                    if status in {"REJECTED", "CANCELLED"}:
                        raise RuntimeError(f"Order {status}: {order.get('status_message')}")
            time.sleep(0.5)
        raise RuntimeError(f"Order {order_id} did not fill within 8 seconds; check Kite order book")

    def enter(self, side: str, index_price: float) -> None:
        if self.position or self.trade_count >= self.max_trades:
            return
        contract = self.nearest_option(index_price, side)
        token, symbol = int(contract["instrument_token"]), str(contract["tradingsymbol"])
        lot_size = int(contract["lot_size"])
        quantity = lot_size * self.lots
        quote = self.kite.ltp([f"NFO:{symbol}"])[f"NFO:{symbol}"]["last_price"]
        quote = float(quote)
        if quote <= 0:
            raise RuntimeError(f"Invalid option quote for {symbol}")
        with STATE_LOCK:
            self.option_token, self.option_symbol = token, symbol
            self.option_ltp[token] = (datetime.now(IST), quote)
        # Subscribe before submitting so option ticks are available for live exits.
        self.ticker.subscribe([token])
        self.ticker.set_mode(self.ticker.MODE_LTP, [token])
        order_id = self.place_order(symbol, self.kite.TRANSACTION_TYPE_BUY, quantity)
        entry = self.order_average_price(order_id, quote)
        self.position = Position(symbol, token, quantity, entry, entry * (1 - self.stop_pct), entry * (1 + self.target_pct), order_id, self.paper)
        self.trade_count += 1
        print(f"TRADE BUY {side} {symbol} x{quantity} @ {entry:.2f} | stop {self.position.stop:.2f} | target {self.position.target:.2f} | mode {'PAPER' if self.paper else 'LIVE'}")
        self.log.warning("OPEN %s %s x%d at %.2f stop %.2f target %.2f (%s)", side, symbol, quantity, entry, self.position.stop, self.position.target, "PAPER" if self.paper else "LIVE")

    def exit(self, price: float, reason: str) -> None:
        pos = self.position
        if not pos:
            return
        # Clear position only after order acceptance; on failure retain it and stop automation.
        order_id = self.place_order(pos.symbol, self.kite.TRANSACTION_TYPE_SELL, pos.quantity)
        exit_price = self.order_average_price(order_id, price)
        pnl = (exit_price - pos.entry) * pos.quantity
        self.daily_pnl += pnl
        print(f"TRADE EXIT {pos.symbol} @ {exit_price:.2f} | reason={reason} | P&L {pnl:.2f} | order={order_id}")
        self.log.warning("CLOSE %s at %.2f, P&L %.2f, reason=%s order=%s", pos.symbol, exit_price, pnl, reason, order_id)
        self.position = None
        self.last_exit_reason = reason
        self.ticker.unsubscribe([pos.token])

    def reconcile_live_position(self) -> None:
        """Fail closed if Kite reports a different position than this process tracks."""
        if self.paper or time.time() - self.last_reconcile < self.reconcile_seconds:
            return
        self.last_reconcile = time.time()
        broker_positions = self.kite.positions().get("net", [])
        matches = [p for p in broker_positions if p.get("exchange") == "NFO" and p.get("product") == "MIS" and p.get("quantity", 0) != 0]
        if self.position is None and matches:
            raise RuntimeError("An existing NFO MIS position was found. Bot stopped to avoid managing unknown risk.")
        if self.position is not None:
            ours = [p for p in matches if p.get("tradingsymbol") == self.position.symbol]
            if len(matches) != 1 or len(ours) != 1 or int(ours[0]["quantity"]) != self.position.quantity:
                raise RuntimeError("Broker position differs from bot state. Bot stopped; inspect positions and order book immediately.")

    def on_ticks(self, ws, ticks) -> None:
        now = datetime.now(IST)
        for tick in ticks:
            token = int(tick["instrument_token"])
            price = float(tick.get("last_price") or 0)
            if price <= 0:
                continue
            with STATE_LOCK:
                if token == self.BANKNIFTY_TOKEN:
                    self.bank_ticks.append((now, price))
                    cutoff = now.timestamp() - 60 * 60 * 4
                    self.bank_ticks = [(ts, p) for ts, p in self.bank_ticks if ts.timestamp() >= cutoff]
                else:
                    self.option_ltp[token] = (now, price)
        if now.time() >= day_time(15, 20):
            if self.position:
                with STATE_LOCK:
                    quote = self.option_ltp.get(self.position.token)
                if quote:
                    self.exit(quote[1], "EOD")
            STOP.set()
            return
        if now.time() < day_time(9, 30) or now.time() >= day_time(15, 10):
            return
        try:
            self.reconcile_live_position()
            signal_side, bar = self.crossover_signal()
            if self.position:
                with STATE_LOCK:
                    tick = self.option_ltp.get(self.position.token)
                if not tick:
                    return
                age = (now - tick[0]).total_seconds()
                if age > 5:
                    return
                option_price = tick[1]
                exit_reason = "STOP_LOSS" if option_price <= self.position.stop else "TARGET" if option_price >= self.position.target else None
                if exit_reason:
                    self.exit(option_price, exit_reason)
                elif signal_side and bar != self.last_signal_bar:
                    self.last_signal_bar = bar
                    self.exit(option_price, "EMA_CROSS")
                return
            if signal_side and bar != self.last_signal_bar and self.trade_count < self.max_trades:
                if self.daily_pnl <= -self.max_daily_loss:
                    self.log.error("Daily loss limit reached (%.2f); no more entries", self.daily_pnl)
                    return
                self.last_signal_bar = bar
                with STATE_LOCK:
                    latest = self.bank_ticks[-1][1] if self.bank_ticks else 0
                if latest:
                    self.enter(signal_side, latest)
        except Exception:
            self.log.exception("Trading safeguard triggered; stopping new actions")
            STOP.set()

    def validate_access_token(self) -> None:
        try:
            profile = self.kite.profile()
            print(f"Kite profile validation OK: {profile.get('user_name') or profile.get('user_id') or 'authenticated'}")
        except Exception as exc:
            raise RuntimeError(
                "Kite auth failed before WebSocket connect. Invalid or expired access token, wrong API key, or missing option permissions. "
                f"Regenerate the token from get_login_url.py. Original error: {exc}"
            ) from exc

    def run(self) -> None:
        self.validate_access_token()
        self.load_instruments()
        self.log.warning("Starting PAPER ONLY Bank Nifty bot. All orders are simulated and no live order will be sent.")

        def on_connect(ws, response):
            ws.subscribe([self.BANKNIFTY_TOKEN])
            ws.set_mode(ws.MODE_LTP, [self.BANKNIFTY_TOKEN])
            self.log.info("Subscribed to NSE:NIFTY BANK ticks")

        def on_close(ws, code, reason):
            if not STOP.is_set():
                self.log.error(
                    "Market data connection closed (%s): %s. This is usually an expired/invalid Kite access token or a WebSocket auth issue.",
                    code,
                    reason,
                )
                STOP.set()

        self.ticker.on_ticks = self.on_ticks
        self.ticker.on_connect = on_connect
        self.ticker.on_close = on_close
        try:
            self.ticker.connect(threaded=True)
        except Exception as exc:
            raise RuntimeError(
                "KiteTicker failed to connect. Likely expired access token or invalid API auth. Regenerate the access token and try again. "
                f"Original error: {exc}"
            ) from exc
        while not STOP.wait(0.5):
            if not self.ticker.is_connected():
                self.log.error("Market data disconnected; stopping")
                STOP.set()
        self.ticker.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    if os.getenv("KITE_LIVE", "NO").strip().upper() == "YES":
        print("Paper trading only: ignoring KITE_LIVE=YES and running in paper mode.", file=sys.stderr)
    print("Paper mode enabled: no live order will be sent.")
    try:
        BankNiftyBot().run()
    except Exception as exc:
        logging.exception("Bot stopped: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
