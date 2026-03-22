"""
Donchian Live Bot — entrypoint.

Usage:
    cd donchain
    pip install -r requirements.txt
    cp .env.example .env      # fill in API keys
    python run.py

On startup:
  1. Validates Telegram credentials (test message)
  2. Loads dynamic QTY_STEP from Bybit instruments info
  3. Fetches last 250 1h candles from Bybit API (with infinite retry)
  4. DonchianBot seeds indicators from those candles (no warmup period)
  5. Hard gate: asserts all indicators are ready — refuses to trade otherwise
  6. Bybit position is synced (hard gate — refuses to start on failure)
  7. WebSocket feed starts — bot is live immediately
  8. Heartbeat task runs every 6 hours

All API calls have infinite retry with exponential backoff (cap 15min).
Trading does NOT start until candle history is complete and indicators are ready.

No database — all indicator state is held in memory (~1 KB).

The script runs indefinitely. Stop with Ctrl+C.
"""
import asyncio
import logging
import logging.handlers
import signal
import sys
import time

from config import load_config
from notifier import TelegramNotifier
from bybit_engine import BybitEngine
from bybit_feed import BybitCandleFeed, Candle
from donchian_live import DonchianBot

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "donchain.log",
            encoding="utf-8",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3,
        ),
    ],
)
logger = logging.getLogger("run")

# How many historical 1h candles to seed (≥ EMA200_PERIOD + N_PERIOD + buffer)
SEED_CANDLES = 250

# Heartbeat interval in seconds (6 hours)
HEARTBEAT_INTERVAL = 6 * 3600

# Bybit interval string for 1h candles
BYBIT_INTERVAL = "60"

# 1 hour in milliseconds
INTERVAL_MS = 3_600_000


def fetch_history_sync(engine: BybitEngine, symbol: str, need: int) -> list[Candle]:
    """
    Fetch `need` historical 1h candles from Bybit /v5/market/kline.
    Paginates automatically (Bybit returns max 200 per request).
    Returns oldest-first list of Candle objects.
    Uses engine's built-in infinite retry.
    """
    now_ms = int(time.time() * 1000)
    # Start from `need` candles ago
    start_ms = now_ms - (need + 1) * INTERVAL_MS
    end_ms = now_ms - INTERVAL_MS  # exclude current (unclosed) candle

    all_rows: list[list] = []
    current_start = start_ms

    while current_start <= end_ms:
        rows = engine.get_klines_sync(
            symbol=symbol,
            interval=BYBIT_INTERVAL,
            start_ms=current_start,
            end_ms=end_ms,
            limit=200,
        )
        if not rows:
            break

        all_rows.extend(rows)

        last_open_time = int(rows[-1][0])
        if last_open_time >= end_ms or len(rows) < 200:
            break
        current_start = last_open_time + INTERVAL_MS

    # Convert to Candle objects (oldest first)
    candles = []
    for row in all_rows:
        open_time = int(row[0])
        candles.append(Candle(
            symbol=symbol,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            open_time=open_time,
            close_time=open_time + INTERVAL_MS - 1,
        ))

    # Deduplicate by open_time (in case of overlap between pages)
    seen = set()
    unique = []
    for c in candles:
        if c.open_time not in seen:
            seen.add(c.open_time)
            unique.append(c)

    # Take last `need` candles
    unique.sort(key=lambda c: c.open_time)
    if len(unique) > need:
        unique = unique[-need:]

    logger.info(f"Fetched {len(unique)} candles from Bybit [{symbol} {BYBIT_INTERVAL}]")
    return unique


async def heartbeat_loop(
    bot: DonchianBot,
    engine: BybitEngine,
    notifier: TelegramNotifier,
    start_time: float,
) -> None:
    """Send periodic heartbeat notifications to confirm bot is alive."""
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            balance = await engine.get_usdt_balance()
            uptime_hours = (time.time() - start_time) / 3600
            await notifier.notify_heartbeat(
                symbol=bot.symbol,
                position=bot._position.side,
                balance=balance,
                uptime_hours=uptime_hours,
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(f"Heartbeat error: {exc}")


async def main() -> None:
    cfg = load_config()

    # ── Validate config ────────────────────────────────────────
    if not cfg.bybit_api_key or not cfg.bybit_api_secret:
        logger.error("BYBIT_API_KEY / BYBIT_API_SECRET not set in .env — aborting")
        sys.exit(1)

    logger.info(
        f"Starting Donchian Live Bot | symbol={cfg.symbol} | "
        f"leverage={cfg.leverage}x | testnet={cfg.bybit_testnet}"
    )

    # ── Wire components ────────────────────────────────────────
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    engine   = BybitEngine(cfg.bybit_api_key, cfg.bybit_api_secret, cfg.bybit_testnet)
    engine.set_notifier(notifier)  # inject for retry notifications
    bot      = DonchianBot(cfg, engine, notifier)

    # ── Validate Telegram credentials ──────────────────────────
    if not notifier.validate_credentials():
        logger.warning(
            "Telegram validation failed — notifications may not work. "
            "Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
        )

    # ── Step 1: load dynamic QTY_STEP from Bybit ──────────────
    await bot.load_qty_step()

    # ── Step 2: fetch candle history from Bybit API (blocks until complete) ──
    logger.info(f"Fetching candle history ({SEED_CANDLES} × 1h) from Bybit...")
    loop = asyncio.get_running_loop()
    historical = await loop.run_in_executor(
        None,
        lambda: fetch_history_sync(engine, cfg.symbol, SEED_CANDLES),
    )
    logger.info(f"Got {len(historical)} candles for seeding")

    # ── Step 3: seed indicators from historical data ───────────
    bot.seed_from_history(historical)

    # ── Hard gate: refuse to start if indicators not ready ─────
    if bot._ema_slow is None or bot._atr is None or bot._ema_atr is None:
        error_msg = (
            f"FATAL: Indicators not ready after seeding {len(historical)} candles. "
            f"EMA200={'ok' if bot._ema_slow else 'MISSING'}, "
            f"ATR={'ok' if bot._atr else 'MISSING'}, "
            f"buf={len(bot._high_buf)}. "
            f"Need at least {bot.EMA_SLOW_PERIOD} candles for EMA200."
        )
        logger.error(error_msg)
        await notifier.send(f"❌ <b>Бот НЕ запущен</b>\n{error_msg}")
        sys.exit(1)

    if len(bot._high_buf) < bot.N_PERIOD:
        error_msg = (
            f"FATAL: Donchian buffer too short: {len(bot._high_buf)} < N_PERIOD={bot.N_PERIOD}. "
            f"Need more history."
        )
        logger.error(error_msg)
        await notifier.send(f"❌ <b>Бот НЕ запущен</b>\n{error_msg}")
        sys.exit(1)

    # ── Step 4: one-time Bybit setup ──────────────────────────
    try:
        await engine.set_leverage(cfg.symbol, cfg.leverage)
    except Exception as exc:
        logger.warning(f"set_leverage failed (continuing): {exc}")

    # ── Step 5: sync current open position from Bybit (hard gate) ──
    await bot.sync_position()

    # ── Step 6: fetch live balance and current price for startup report ──
    balance = await engine.get_usdt_balance()
    current_price = await engine.get_ticker_price(cfg.symbol)

    pos = bot._position  # PositionState dataclass
    if pos.side == "NONE":
        position_line = "нет открытой позиции"
    else:
        pnl_label = f"≈{pos.unrealised_pnl:+.2f} USDT"
        coin = cfg.symbol.replace("USDT", "")
        position_line = (
            f"{pos.side} | {pos.size:.4f} {coin} | "
            f"вход ${pos.avg_price:,.2f} | PnL {pnl_label}"
        )

    # ── Notify startup ─────────────────────────────────────────
    mode_label = "🧪 TESTNET" if cfg.bybit_testnet else "🔴 LIVE"
    await notifier.send(
        f"🚀 <b>Donchian Bot запущен</b> {mode_label}\n"
        f"\n"
        f"💰 Баланс: <b>${balance:,.2f} USDT</b>\n"
        f"📍 Позиция: {position_line}\n"
        f"💵 Цена {cfg.symbol}: <b>${current_price:,.2f}</b>\n"
        f"\n"
        f"⚙️ {cfg.symbol} | Плечо: {cfg.leverage}x | "
        f"N={cfg.n_period} | M={cfg.m_period} | "
        f"vol_min={cfg.vol_ratio_min} | k={cfg.ema200_atr_k}\n"
        f"📊 Прогрев: ✅ ({len(historical)} свечей) | "
        f"EMA200=${bot._ema_slow:.2f} | ATR={bot._atr:.4f}\n"
        f"📐 QTY_STEP: {bot._qty_step}"
    )

    # ── Step 7: start WebSocket feed ──────────────────────────
    feed = BybitCandleFeed(
        symbol=cfg.symbol,
        interval=60,
        on_candle=bot.on_candle,
        loop=loop,
        testnet=cfg.bybit_testnet,
        notifier=notifier,
    )
    feed.start()

    logger.info("Bot is running — press Ctrl+C to stop")

    # ── Step 8: start heartbeat task ──────────────────────────
    start_time = time.time()
    heartbeat_task = asyncio.create_task(
        heartbeat_loop(bot, engine, notifier, start_time)
    )

    # ── Keep the event loop alive ──────────────────────────────
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT,  _signal_handler)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler)

    await stop_event.wait()

    # ── Graceful shutdown ──────────────────────────────────────
    logger.info("Stopping WebSocket feed...")
    feed.stop()
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    await notifier.send(f"🛑 <b>Donchian Bot остановлен</b> — {cfg.symbol}")
    await notifier.close()
    logger.info("Bot stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
