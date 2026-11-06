"""Live Kite market-data monitor with simulated Bank Nifty option trades.

This version never submits broker orders. It reads live/historical data from
Kite and paper-tracks signal entries and exits using the selected option's LTP.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect

from banknifty_layered_strategy import evaluate

IST = ZoneInfo("Asia/Kolkata")
LOG = logging.getLogger("kite_banknifty_live")
OPEN = dt_time(9, 15)
LAST_ENTRY = dt_time(14, 30)
SQUARE_OFF = dt_time(15, 10)


def _full_quote(kite: KiteConnect, key: str) -> dict:
    result = kite.quote([key])
    if key not in result:
        raise RuntimeError(f"No live quote returned for {key}")
    return result[key]


def _get_futures(instruments: list[dict], today):
    rows = [i for i in instruments if i.get("name") == "BANKNIFTY"
            and i.get("segment") == "NFO-FUT" and i.get("expiry") and i["expiry"] >= today]
    if not rows:
        raise RuntimeError("No current BANKNIFTY futures contract found")
    return sorted(rows, key=lambda i: i["expiry"])[0]


def _option_contract(instruments: list[dict], expiry, spot: float, side: int) -> dict:
    kind = "CE" if side == 1 else "PE"
    candidates = [i for i in instruments if i.get("name") == "BANKNIFTY"
                  and i.get("segment") == "NFO-OPT" and i.get("instrument_type") == kind
                  and i.get("expiry") == expiry]
    if not candidates:
        raise RuntimeError(f"No BANKNIFTY {kind} options for {expiry}")
    return min(candidates, key=lambda i: abs(float(i["strike"]) - spot))


def _expiry(instruments: list[dict], today):
    expiries = sorted({i["expiry"] for i in instruments if i.get("name") == "BANKNIFTY"
                       and i.get("segment") == "NFO-OPT" and i.get("expiry")
                       and i["expiry"] > today})
    if not expiries:
        raise RuntimeError("No future BANKNIFTY option expiry found; expiry-day entries are disabled")
    return expiries[0]


def _chain_score(kite: KiteConnect, instruments: list[dict], expiry, spot: float,
                 previous_oi: dict | None) -> tuple[int, dict]:
    """Directional nearby-strike OI shift: put OI build vs call OI build."""
    calls = [i for i in instruments if i.get("name") == "BANKNIFTY" and i.get("segment") == "NFO-OPT"
             and i.get("instrument_type") == "CE" and i.get("expiry") == expiry]
    puts = [i for i in instruments if i.get("name") == "BANKNIFTY" and i.get("segment") == "NFO-OPT"
            and i.get("instrument_type") == "PE" and i.get("expiry") == expiry]
    strikes = sorted({float(i["strike"]) for i in calls + puts}, key=lambda x: abs(x - spot))[:5]
    selected = [i for i in calls + puts if float(i["strike"]) in strikes]
    keys = [f"NFO:{i['tradingsymbol']}" for i in selected]
    quotes = kite.quote(keys)
    current = {key: int((quotes.get(key) or {}).get("oi", 0)) for key in keys}
    if not previous_oi:
        return 0, current
    call_delta = sum(current.get(f"NFO:{i['tradingsymbol']}", 0) - previous_oi.get(f"NFO:{i['tradingsymbol']}", 0)
                     for i in selected if i.get("instrument_type") == "CE")
    put_delta = sum(current.get(f"NFO:{i['tradingsymbol']}", 0) - previous_oi.get(f"NFO:{i['tradingsymbol']}", 0)
                    for i in selected if i.get("instrument_type") == "PE")
    # Positive score is supportive of upside (put OI builds more than call OI).
    return (1 if put_delta > call_delta else -1 if call_delta > put_delta else 0), current


def _position_qty(entry_premium: float, lot_size: int) -> int:
    max_lots = max(1, int(os.getenv("MAX_LOTS", "1")))
    risk_budget = float(os.getenv("RISK_PER_TRADE_INR", "1000"))
    stop_pct = float(os.getenv("PREMIUM_STOP_PCT", "0.20"))
    lots = min(max_lots, int(risk_budget / max(entry_premium * stop_pct * lot_size, 1)))
    return lots * lot_size


def run() -> None:
    api_key =  "zv7zhx52bee3u1rg"
    access_token = "GJcj1uCtd5cxsWqKjoicdWnfVYbNeQ1h"
    # access_token = os.getenv("KITE_ACCESS_TOKEN", "GJcj1uCtd5cxsWqKjoicdWnfVYbNeQ1h").strip()
    # if not api_key or not access_token:
    #     raise RuntimeError("Set KITE_API_KEY and today's KITE_ACCESS_TOKEN in environment variables")
    # Deliberately fixed to paper mode: this module contains no order-submit path.
    live = False
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    kite.profile()
    all_instruments = kite.instruments("NFO")
    today = datetime.now(IST).date()
    future = _get_futures(all_instruments, today)
    vix_symbol = os.getenv("INDIA_VIX_SYMBOL", "INDIA VIX").strip().upper()
    nse_instruments = kite.instruments("NSE")
    vix_row = next((i for i in nse_instruments if str(i.get("tradingsymbol", "")).upper() == vix_symbol), None)
    if not vix_row:
        raise RuntimeError(f"India VIX instrument {vix_symbol!r} was not found; configure INDIA_VIX_SYMBOL")
    # Never start into an unreconciled position or pending order.
    open_positions = [p for p in kite.positions().get("net", []) if p.get("quantity", 0)]
    if open_positions:
        raise RuntimeError("Open broker positions exist. Reconcile/manage them before starting this runner")
    pending = [o for o in kite.orders() if o.get("status") in {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED"}]
    if pending:
        raise RuntimeError("Pending broker orders exist. Reconcile/cancel them before starting this runner")

    LOG.warning("Starting LIVE DATA / PAPER TRADES ONLY for %s; future expiry=%s. No broker orders will be sent.",
                future["tradingsymbol"], future["expiry"])
    prior_oi: dict | None = None
    open_trade: dict | None = None
    trade_count = 0
    realized = 0.0
    session_date = None
    last_bar = None
    last_chain_time = None
    chain_score = 0
    history_start = datetime.now(IST) - timedelta(days=40)
    # Historical loads provide enough ATR/ADX warm-up for the regime classifier.
    while True:
        now = datetime.now(IST)
        if now.date() != session_date:
            if open_trade:
                raise RuntimeError("Date changed with an open position; manual reconciliation required")
            all_instruments = kite.instruments("NFO")
            future = _get_futures(all_instruments, now.date())
            session_date, trade_count, realized, last_bar = now.date(), 0, 0.0, None
            prior_oi, last_chain_time, chain_score = None, None, 0
        if now.time() < OPEN or now.time() >= dt_time(15, 16):
            time.sleep(10)
            continue
        try:
            start_today = datetime.combine(now.date(), OPEN, tzinfo=IST)
            raw = kite.historical_data(int(future["instrument_token"]), history_start, now,
                                       "5minute", oi=True)
            if not raw:
                time.sleep(5)
                continue
            frame = pd.DataFrame(raw).set_index("date")
            frame.index = pd.to_datetime(frame.index)
            frame.index = frame.index.tz_localize(IST) if frame.index.tz is None else frame.index.tz_convert(IST)
            frame = frame.loc[frame.index + pd.Timedelta(minutes=5) <= now]
            # Ignore the unfinished bar; the signal engine acts only on closed bars.
            if len(frame) < 100:
                time.sleep(5)
                continue
            frame["futures_oi_change_pct"] = frame.oi.pct_change() * 100 if "oi" in frame else float("nan")
            vix_hist = kite.historical_data(int(vix_row["instrument_token"]), history_start, now, "5minute")
            if vix_hist:
                vix = pd.DataFrame(vix_hist).set_index("date")
                vix.index = pd.to_datetime(vix.index)
                vix.index = vix.index.tz_localize(IST) if vix.index.tz is None else vix.index.tz_convert(IST)
                frame["india_vix"] = vix.close.reindex(frame.index, method="ffill")
            else:
                raise RuntimeError("India VIX history unavailable; refusing to evaluate live regime")

            latest = frame.index[-1].to_pydatetime()
            if latest == last_bar:
                if open_trade:
                    closed, pnl, exit_price, reason = _manage_position(kite, live, open_trade)
                    if closed:
                        realized += pnl
                        LOG.warning("TRADE CLOSED %s | entry=%.2f exit=%.2f | reason=%s | P&L=₹%.2f",
                                    open_trade["symbol"], open_trade["entry_ref"], exit_price, reason, pnl)
                        open_trade = None
                time.sleep(2)
                continue
            last_bar = latest
            spot = float(frame.close.iloc[-1])
            listed_expiries = {i["expiry"] for i in all_instruments if i.get("name") == "BANKNIFTY"
                               and i.get("segment") == "NFO-OPT" and i.get("expiry")}
            if now.date() in listed_expiries:
                LOG.info("Expiry session: new entries disabled")
                time.sleep(2)
                continue
            chosen_expiry = _expiry(all_instruments, now.date())
            chain_score, prior_oi = _chain_score(kite, all_instruments, chosen_expiry, spot, prior_oi)
            frame["option_chain_score"] = float("nan")
            frame.loc[frame.index[-1], "option_chain_score"] = chain_score
            signals = evaluate(frame)
            row = signals.iloc[-1]
            if open_trade:
                closed, pnl, exit_price, reason = _manage_position(kite, live, open_trade)
                if closed:
                    realized += pnl
                    LOG.warning("TRADE CLOSED %s | entry=%.2f exit=%.2f | reason=%s | P&L=₹%.2f",
                                open_trade["symbol"], open_trade["entry_ref"], exit_price, reason, pnl)
                    open_trade = None
                continue
            if now.time() >= LAST_ENTRY or trade_count >= int(os.getenv("MAX_TRADES_PER_DAY", "1")):
                continue
            if realized <= -abs(float(os.getenv("MAX_DAILY_LOSS_INR", "2000"))):
                LOG.warning("Daily loss limit reached; entries disabled")
                continue
            if int(row.signal) == 0:
                continue
            # Only trend-regime signals may reach the order path.
            if row.regime != "TREND" or row.trigger != "ORB" or int(row.confirmations) < 2:
                continue
            contract = _option_contract(all_instruments, chosen_expiry, spot, int(row.signal))
            key = f"NFO:{contract['tradingsymbol']}"
            quote = _full_quote(kite, key)
            ltp = float(quote["last_price"])
            qty = _position_qty(ltp, int(contract["lot_size"]))
            if qty < int(contract["lot_size"]):
                LOG.warning("Risk budget too small for one lot; skipping %s", key)
                continue
            max_premium = float(os.getenv("MAX_PREMIUM_PER_TRADE_INR", "25000"))
            if ltp * qty > max_premium:
                LOG.warning("Premium outlay ₹%.0f exceeds cap ₹%.0f; skipping", ltp * qty, max_premium)
                continue
            entry_price = ltp
            LOG.warning("PAPER TRADE OPENED %s | qty=%d | entry=%.2f | stop=%.2f | target=%.2f | time=%s | confirmations=%d",
                        contract["tradingsymbol"], qty, entry_price,
                        entry_price * (1 - float(os.getenv("PREMIUM_STOP_PCT", "0.20"))),
                        entry_price * (1 + float(os.getenv("PREMIUM_TARGET_PCT", "0.30"))),
                        now.strftime("%Y-%m-%d %H:%M:%S %Z"), int(row.confirmations))
            open_trade = {"key": key, "symbol": contract["tradingsymbol"], "qty": qty,
                          "entry_ref": entry_price,
                          "stop": entry_price * (1 - float(os.getenv("PREMIUM_STOP_PCT", "0.20"))),
                          "target": entry_price * (1 + float(os.getenv("PREMIUM_TARGET_PCT", "0.30"))),
                          "opened": now}
            trade_count += 1
        except Exception:
            LOG.exception("Kite scan failed; no new trade on this pass")
        time.sleep(5)


def _manage_position(kite, live: bool, trade: dict) -> tuple[bool, float, float | None, str | None]:
    now = datetime.now(IST)
    quote = _full_quote(kite, trade["key"])
    price = float(quote["last_price"])
    why = ("stop" if price <= trade["stop"] else "target" if price >= trade["target"]
           else "time" if now.time() >= SQUARE_OFF else None)
    if why:
        exit_price = price
        pnl = (exit_price - trade["entry_ref"]) * trade["qty"]
        return True, pnl, exit_price, why
    return False, 0.0, None, None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
