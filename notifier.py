"""
Telegram notifier — sends messages via Bot API using httpx.
All methods are async. Messages use HTML parse_mode.

Also provides send_sync() for use from sync contexts (retry.py).

Rate limited: max 1 message per second to avoid Telegram Bot API throttling.
"""
import asyncio
import logging
import time
import threading
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# Minimum interval between Telegram messages (seconds)
RATE_LIMIT_INTERVAL = 1.0


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        self._async_client: httpx.AsyncClient | None = None
        # Rate limiting state
        self._last_send_time: float = 0.0
        self._rate_lock = threading.Lock()  # protects _last_send_time for sync sends
        if not self._enabled:
            logger.warning("Telegram not configured — notifications disabled")

    # ──────────────────────────────────────────────────────────
    # Rate limiting helpers
    # ──────────────────────────────────────────────────────────

    def _wait_for_rate_limit_sync(self) -> None:
        """Block until rate limit interval has passed (sync context)."""
        with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_send_time
            if elapsed < RATE_LIMIT_INTERVAL:
                time.sleep(RATE_LIMIT_INTERVAL - elapsed)
            self._last_send_time = time.monotonic()

    async def _wait_for_rate_limit_async(self) -> None:
        """Wait until rate limit interval has passed (async context)."""
        now = time.monotonic()
        elapsed = now - self._last_send_time
        if elapsed < RATE_LIMIT_INTERVAL:
            await asyncio.sleep(RATE_LIMIT_INTERVAL - elapsed)
        self._last_send_time = time.monotonic()

    # ──────────────────────────────────────────────────────────
    # Startup validation
    # ──────────────────────────────────────────────────────────

    def validate_credentials(self) -> bool:
        """
        Send a test message to verify Telegram bot token and chat_id are valid.
        Call once at startup. Returns True if successful, False otherwise.
        """
        if not self._enabled:
            logger.warning("Telegram validation skipped — not configured")
            return False
        try:
            resp = httpx.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": "✅ Telegram connection verified",
                    "parse_mode": "HTML",
                },
                timeout=10.0,
            )
            if resp.status_code == 200:
                logger.info("Telegram credentials validated successfully")
                return True
            logger.error(f"Telegram validation failed: {resp.status_code} — {resp.text}")
            return False
        except Exception as exc:
            logger.error(f"Telegram validation failed: {exc}")
            return False

    # ──────────────────────────────────────────────────────────
    # Async send (primary)
    # ──────────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazily create and reuse a single async HTTP client."""
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(timeout=10.0)
        return self._async_client

    async def close(self) -> None:
        """Close the async HTTP client. Call on shutdown."""
        if self._async_client is not None and not self._async_client.is_closed:
            await self._async_client.aclose()
            self._async_client = None

    async def send(self, text: str) -> None:
        """Send a raw text message (HTML parse_mode). Rate limited. Retries 3 times with backoff."""
        if not self._enabled:
            return
        await self._wait_for_rate_limit_async()
        delay = 2.0
        client = await self._get_client()
        for attempt in range(3):
            try:
                resp = await client.post(self._url, json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                })
                if resp.status_code == 200:
                    return
                logger.error(f"Telegram error {resp.status_code}: {resp.text}")
            except Exception as exc:
                logger.error(f"Telegram send attempt {attempt+1}/3 failed: {exc}")
            if attempt < 2:
                await asyncio.sleep(delay)
                delay *= 3
        logger.error("Telegram send failed after 3 attempts — giving up")

    # ──────────────────────────────────────────────────────────
    # Sync send (for retry.py and other sync contexts)
    # ──────────────────────────────────────────────────────────

    def send_sync(self, text: str) -> None:
        """
        Send a notification from a synchronous context.
        Uses httpx sync client. Rate limited. Does NOT raise on failure.
        """
        if not self._enabled:
            return
        self._wait_for_rate_limit_sync()
        try:
            resp = httpx.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error(f"Telegram sync send error: {resp.status_code}")
        except Exception as exc:
            logger.error(f"Telegram sync send failed: {exc}")

    # ──────────────────────────────────────────────────────────
    # Typed message builders
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _ts() -> str:
        """Current UTC time formatted for messages."""
        return datetime.now(timezone.utc).strftime("🕐 %d.%m.%Y %H:%M UTC")

    async def notify_long_open(
        self, symbol: str, price: float, usdt_spent: float, qty: float,
        high_n: float, ema200: float, vol_ratio: float, n: int,
    ) -> None:
        text = (
            f"🟢 <b>LONG открыт — {symbol}</b>\n"
            f"Цена входа: <b>${price:,.2f}</b>\n"
            f"Объём: <b>${usdt_spent:,.2f}</b> ({qty:.4f} {symbol.replace('USDT','')})\n"
            f"Канал N={n}: high = ${high_n:,.2f}\n"
            f"EMA200: ${ema200:,.2f} | vol_ratio: {vol_ratio:.2f}\n"
            f"{self._ts()}"
        )
        await self.send(text)

    async def notify_short_open(
        self, symbol: str, price: float, usdt_spent: float, qty: float,
        low_n: float, ema200: float, vol_ratio: float, n: int,
    ) -> None:
        text = (
            f"🔴 <b>SHORT открыт — {symbol}</b>\n"
            f"Цена входа: <b>${price:,.2f}</b>\n"
            f"Объём: <b>${usdt_spent:,.2f}</b> ({qty:.4f} {symbol.replace('USDT','')})\n"
            f"Канал N={n}: low = ${low_n:,.2f}\n"
            f"EMA200: ${ema200:,.2f} | vol_ratio: {vol_ratio:.2f}\n"
            f"{self._ts()}"
        )
        await self.send(text)

    async def notify_long_close(
        self, symbol: str, price: float, exit_low: float, pnl: float,
    ) -> None:
        pnl_icon = "📈" if pnl >= 0 else "📉"
        text = (
            f"✅ <b>LONG закрыт — {symbol}</b>\n"
            f"Цена выхода: <b>${price:,.2f}</b>\n"
            f"Причина: close &lt; exit_low (${exit_low:,.2f})\n"
            f"PnL: {pnl_icon} <b>≈{pnl:+.2f} USDT</b>\n"
            f"{self._ts()}"
        )
        await self.send(text)

    async def notify_short_close(
        self, symbol: str, price: float, exit_high: float, pnl: float,
    ) -> None:
        pnl_icon = "📈" if pnl >= 0 else "📉"
        text = (
            f"✅ <b>SHORT закрыт — {symbol}</b>\n"
            f"Цена выхода: <b>${price:,.2f}</b>\n"
            f"Причина: close &gt; exit_high (${exit_high:,.2f})\n"
            f"PnL: {pnl_icon} <b>≈{pnl:+.2f} USDT</b>\n"
            f"{self._ts()}"
        )
        await self.send(text)

    async def notify_signal_blocked(
        self, symbol: str, direction: str, close: float,
        vol_ok: bool, vol_ratio: float, vol_ratio_min: float,
        dist_ok: bool, distance: float, atr: float, ema200_atr_k: float,
        channel_val: float, n: int,
    ) -> None:
        """Entry signal fired but at least one filter blocked it."""
        reasons = []
        if not vol_ok:
            reasons.append(
                f"vol_ratio = {vol_ratio:.2f} &lt; {vol_ratio_min:.4f} (нет волатильности)"
            )
        if not dist_ok:
            threshold = atr * ema200_atr_k
            reasons.append(
                f"dist = {distance:.2f} &gt; {ema200_atr_k:.2f}×ATR ({threshold:.2f}) — цена далеко от EMA200"
            )
        reasons_str = "\n".join(f"  • {r}" for r in reasons)
        ch_label = "high" if direction == "LONG" else "low"
        text = (
            f"⚠️ <b>Сигнал {direction} заблокирован — {symbol}</b>\n"
            f"Причина:\n{reasons_str}\n"
            f"close = ${close:,.2f} | {ch_label}_{n} = ${channel_val:,.2f}\n"
            f"{self._ts()}"
        )
        await self.send(text)

    async def notify_no_signal(
        self, symbol: str, close: float, ema200: float,
        high_n: float, low_n: float,
        exit_high_m: float, exit_low_m: float,
        vol_ratio: float, position: str, n: int, m: int,
    ) -> None:
        text = (
            f"📊 <b>Нет сигнала — {symbol}</b>\n"
            f"close = ${close:,.2f} | EMA200 = ${ema200:,.2f}\n"
            f"Вход N={n}: high = ${high_n:,.2f} | low = ${low_n:,.2f}\n"
            f"Выход M={m}: high = ${exit_high_m:,.2f} | low = ${exit_low_m:,.2f}\n"
            f"vol_ratio = {vol_ratio:.2f} | позиция: {position}\n"
            f"{self._ts()}"
        )
        await self.send(text)

    async def notify_warmup(self, symbol: str, candles: int, needed: int) -> None:
        text = (
            f"⏳ <b>Прогрев — {symbol}</b>\n"
            f"Накоплено свечей: {candles} / {needed}\n"
            f"{self._ts()}"
        )
        await self.send(text)

    async def notify_error(self, context: str, error: str) -> None:
        text = (
            f"❌ <b>Ошибка</b> [{context}]\n"
            f"<code>{error}</code>\n"
            f"{self._ts()}"
        )
        await self.send(text)

    async def notify_heartbeat(
        self, symbol: str, position: str, balance: float, uptime_hours: float,
    ) -> None:
        """Periodic heartbeat — confirms bot is alive."""
        text = (
            f"💓 <b>Heartbeat — {symbol}</b>\n"
            f"Позиция: {position}\n"
            f"Баланс: <b>${balance:,.2f} USDT</b>\n"
            f"Аптайм: {uptime_hours:.1f}ч\n"
            f"{self._ts()}"
        )
        await self.send(text)
