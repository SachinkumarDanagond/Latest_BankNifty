# Bank Nifty option bot (Kite Connect)

This Python candidate watches Bank Nifty through Kite's WebSocket. It defines the 09:15–09:30 IST opening range, then considers a completed 5-minute close beyond that range only when EMA(9) is aligned above/below EMA(21). A breakout selects a nearest-expiry, nearest-ATM CE or PE. It exits on a premium stop, premium target, opposite signal, or at 15:20 IST. It allows one position at a time and defaults to one trade per day, with a gross daily loss cutoff (`MAX_DAILY_LOSS_INR`, default ₹1,500).

**Paper mode is the default.** This is an unvalidated strategy candidate, not a claim of profitability. It has not been backtested on historical option prices and does not account for brokerage, taxes, spread, slippage, gaps, API outages, rejected orders, or rapid option-premium moves. Paper trade it and independently validate net results across different market regimes before considering live use. Its daily loss figure is a software guard based on completed trades, not a guaranteed cap on losses.

## Setup

1. Use Python 3.10 or newer and install dependencies: `python -m pip install -r requirements.txt`.
2. The existing `credentials.txt` is read for the API key and secret. Keep it private and out of source control. The bot never prints credentials.
3. Create a daily Kite access token using the official login flow and set `KITE_ACCESS_TOKEN`. The bot cannot refresh the token itself because Kite requires the interactive request-token login flow.
4. Run `python banknifty_bot.py`. This simulates order fills using the REST option LTP and logs intended orders without sending them.

## Configuration

- `LOTS` (default `1`): number of exchange lots.
- `MAX_TRADES_PER_DAY` (default `1`): entry limit.
- `MAX_DAILY_LOSS_INR` (default `1500`): stop opening new positions after realized gross P&L reaches this loss.
- `STOP_LOSS_PCT` (default `0.20`): premium loss from fill price.
- `TARGET_PCT` (default `0.30`): premium gain from fill price.
- `KITE_API_KEY` and `KITE_API_SECRET`: optional overrides for credentials file.
- `KITE_LIVE=YES` **and** `CONFIRM_LIVE_ORDERS=I_UNDERSTAND`: both are required to send actual MIS market orders. Live orders can lose money. Confirm the selected contract, lot size, order book, and broker positions before enabling.

The bot stops if its WebSocket disconnects or if broker position reconciliation does not match its internal state. If it stops with a position open, manage that position from Kite immediately. Before API-based live algo trading, check that your broker and strategy meet current SEBI and exchange requirements.

## Strategy and safeguards

Entries use completed 5-minute closes only; the opening range and at least 23 completed bars must be available. Start before 09:15 IST and keep the process running through the session; a restart or late start intentionally prevents entries. The script uses LTP market orders in live mode, so execution prices can differ from observed prices. The premium stop/target and daily loss exclude charges and slippage.

## EMA20/VWAP stock strategy from the linked video

The video points to a Chartink 5-minute scan for futures-segment stocks using EMA(20), VWAP, proximity to EMA, a widening EMA/VWAP gap, and a falling EMA trend. The code in `vwap_ema_strategy.py` implements the visible bearish scan conditions and an explicitly mirrored bullish version. The original scan also uses a market-cap filter; the Python implementation leaves universe selection to `WATCHLIST`. This is a rule translation, not a claim that the strategy is profitable.

- `backtest_vwap_ema.py` reads timestamped 5-minute OHLCV CSV data. It enters at the next bar open, uses the signal candle high/low for the initial stop, sizes from a fixed percentage of current equity, and includes a configurable approximate turnover fee. Same-bar stop and target touches assume the stop happened first. It does not model bid/ask spread, market impact, exchange taxes precisely, or missing bars.
- `kite_vwap_ema_live.py` scans an NSE watchlist through Kite historical candles and places equity MIS market orders. It is paper mode unless both `KITE_LIVE=YES` and `CONFIRM_LIVE_ORDERS=I_UNDERSTAND` are set. Live exits are polled, software-managed market orders; they are not broker-side protective stops. A crash, disconnect, rejection, or process shutdown can leave exposure requiring manual action. Use paper mode first.

Example backtest: `python backtest_vwap_ema.py sample_5m.csv --capital 100000 --risk-pct 0.005 --reward-risk 1.5 --fee-bps 10`.

For the new Kite module, set `KITE_API_KEY` and `KITE_ACCESS_TOKEN`, then start with `python kite_vwap_ema_live.py`. Optional settings: `WATCHLIST` (comma-separated NSE symbols), `RISK_PER_TRADE_INR` (default 500), `MAX_TRADES_PER_DAY` (default 2), `REWARD_RISK` (default 1.5), and `POLL_SECONDS` (minimum 60). The daily access token must be generated through Kite's login flow. Verify every symbol and order manually; backtest with historical data and paper trade before enabling live orders.
