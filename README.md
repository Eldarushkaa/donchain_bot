# Donchian Live Bot — Bybit ETHUSDT 1h

Standalone live trading bot implementing the **Donchian Breakout** (Turtle Trading) strategy on Bybit linear perpetual futures. Operates on **ETHUSDT 1h** candles with configurable leverage.

Sends **Telegram notifications on every closed candle** — trade opened, trade closed, signal blocked (with reason), or no-signal status report.

---

## Quick Start

```bash
cd donchain
pip install -r requirements.txt
cp .env.example .env      # fill in API keys
python run.py
```

## Project Structure

```
donchain/
├── run.py              # Entrypoint — startup sequence, event loop, shutdown, heartbeat
├── config.py           # @dataclass Config loaded from .env
├── donchian_live.py    # DonchianBot — strategy logic, indicators, order actions
├── bybit_engine.py     # BybitEngine — Bybit REST API (pybit) with retry
├── bybit_feed.py       # BybitCandleFeed — WebSocket kline stream → asyncio bridge
├── notifier.py         # TelegramNotifier — async/sync Telegram Bot API (rate limited)
├── retry.py            # retry_sync() / retry_async() — infinite retry with escalation
├── requirements.txt    # pybit, httpx, python-dotenv
├── .env.example        # Template for secrets and strategy parameters
└── .gitignore          # Excludes .env, key.txt, *.log
```

## Architecture

```
┌──────────────┐   WebSocket    ┌───────────────┐   on_candle()   ┌──────────────┐
│  Bybit WS    │──────────────▶│ BybitCandleFeed│──────────────▶│  DonchianBot  │
│  kline.60    │  (bg thread)   │  (thread→async) │  (asyncio)    │  (strategy)   │
└──────────────┘               └───────────────┘               └──────┬───────┘
                                                                       │
                                        ┌──────────────────────────────┤
                                        ▼                              ▼
                                 ┌──────────────┐              ┌──────────────┐
                                 │ BybitEngine   │              │  Notifier    │
                                 │ (REST + retry)│              │  (Telegram)  │
                                 └──────────────┘              └──────────────┘
                                                                  rate limited
```

All data — both historical candles and live trading — comes from **Bybit only**. No database — all indicator state is held in memory (~1 KB).

### Startup Sequence

1. **Validate** Telegram credentials (test message)
2. **Load QTY_STEP** from Bybit instruments info (dynamic, not hardcoded)
3. **Fetch 250 candles** from Bybit `/v5/market/kline` with infinite retry — held in memory, no database
4. **seed_from_history()** — primes EMA200, ATR(14), EMA(ATR,20), Donchian buffers. No warmup period needed
5. **Hard gate** — refuses to start if any indicator is not ready
6. **sync_position()** — reads current open position from Bybit (hard gate)
7. **set_leverage()** — one-time leverage setup
8. **WebSocket feed starts** — bot is live immediately
9. **Heartbeat task starts** — sends periodic "bot alive" notifications

### Trading Logic

| Signal | Condition |
|--------|-----------|
| Entry LONG | `close > max(high[-N:])` + filters pass |
| Entry SHORT | `close < min(low[-N:])` + filters pass |
| Exit LONG | `close < min(low[-M:])` |
| Exit SHORT | `close > max(high[-M:])` |

**Entry filters** (both must pass):
- **Volatility**: `ATR / EMA(ATR) > VOL_RATIO_MIN`
- **EMA200 proximity**: `abs(close - EMA200) < EMA200_ATR_K × ATR`

**Position sizing**: `notional = balance × trade_fraction × leverage`, `qty = notional / price`

### Memory Footprint

No database. All indicator state lives in memory:
- `_high_buf` / `_low_buf` — max 62 floats (trimmed automatically)
- `_ema_slow`, `_atr`, `_ema_atr` — 3 floats total
- Temporary warmup buffers — cleared after seeding

Total runtime memory: ~1 KB. No leaks possible.

### Retry & Resilience

- **All API calls** (Bybit REST) use infinite retry with exponential backoff (2s → 4s → ... → 15min cap)
- **Escalation alerts** 🚨 after 1 hour of continuous API failure — repeated every hour
- **Market orders** use Bybit `orderLinkId` (UUID) for idempotent retry — duplicate orders are rejected, not doubled
- **Telegram notifications** on every retry failure (🔄) and on recovery (✅)
- **Telegram rate limited** — max 1 message/second to avoid Bot API throttling
- **WebSocket health monitor** — alerts if no candle arrives within interval + 15min, auto-reconnects
- **on_candle exceptions** caught via Future `done_callback` — logged and notified instead of silently swallowed

### Position Safety

- **sync_position()** called after **every order** (open/close) — guarantees state consistency even if HTTP response is lost
- **Periodic sync** every 6 candles (~6 hours) as safety net
- **Error recovery sync** — if an order fails, sync_position() is still attempted to detect if the order went through
- **Entry price from exchange** — uses actual `avgPrice` from Bybit, not candle close price

### Monitoring

- **Heartbeat** 💓 every 6 hours — confirms bot is alive with balance and position info
- **Error notifications** ❌ on every exception with context
- **Escalation** 🚨 after 1 hour of continuous API unavailability

## Configuration

All settings via `.env` file (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `BYBIT_API_KEY` | — | Bybit API key |
| `BYBIT_API_SECRET` | — | Bybit API secret |
| `BYBIT_TESTNET` | `false` | Use testnet for paper trading |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Target chat ID |
| `SYMBOL` | `ETHUSDT` | Trading pair |
| `LEVERAGE` | `3` | Leverage multiplier |
| `TRADE_FRACTION` | `0.5` | Fraction of balance per trade (50%) |
| `N_PERIOD` | `42` | Breakout channel lookback |
| `M_PERIOD` | `23` | Exit channel lookback |
| `EMA200_ATR_K` | `3.8437` | EMA200 proximity filter coefficient |
| `VOL_RATIO_MIN` | `1.1976` | Min ATR/EMA_ATR for entry |

## Notifications

Every closed candle produces exactly one Telegram message:

- 🟢 **LONG opened** — price, volume, channel value, EMA200, vol_ratio
- 🔴 **SHORT opened** — same fields
- ✅ **Position closed** — exit price, reason, approximate PnL
- ⚠️ **Signal blocked** — direction, which filter(s) failed and why
- 📊 **No signal** — current price, indicators, position status
- ❌ **Error** — context and exception details
- 🔄 **API retry** — which API, attempt number, next retry delay
- ✅ **API recovered** — after successful retry following failure(s)
- 🚨 **API critical** — after 1+ hour of continuous failure (escalation)
- ⚠️ **WebSocket timeout** — no candle received, reconnecting
- 💓 **Heartbeat** — periodic "bot alive" with balance and position (every 6h)

## Dependencies

```
pybit>=5.8.0          # Bybit REST + WebSocket
httpx>=0.27.0         # HTTP client (Telegram Bot API)
python-dotenv>=1.0.1  # .env file loading
```

## Stopping

`Ctrl+C` or `SIGTERM` → graceful shutdown: WebSocket closed, heartbeat cancelled, shutdown notification sent, resources cleaned up.
