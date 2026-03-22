"""
BybitCandleFeed — subscribes to Bybit WebSocket kline stream and bridges
confirmed (closed) candles into the asyncio event loop.

How it works:
  - pybit WebSocket runs in a background thread and calls the callback sync.
  - When a candle is confirmed (closed), we use asyncio.run_coroutine_threadsafe()
    to safely schedule the async on_candle handler in the main event loop.
  - A done_callback on the Future catches and logs/notifies any unhandled exception
    in on_candle — prevents silent failures.
  - A health monitor task checks that candles keep arriving within the expected
    interval. If no candle arrives for interval + buffer, it sends an alert.
"""
import asyncio
import logging
import time
import threading
from dataclasses import dataclass
from typing import Callable, Coroutine, Any, Optional

from pybit.unified_trading import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class Candle:
    symbol:     str
    open:       float
    high:       float
    low:        float
    close:      float
    volume:     float
    open_time:  int   # unix ms
    close_time: int   # unix ms


# Type alias for the async candle handler
AsyncCandleCallback = Callable[[Candle], Coroutine[Any, Any, None]]


class BybitCandleFeed:
    """
    Subscribes to kline.{interval}.{symbol} on Bybit public linear WebSocket.
    Filters for confirmed (closed) candles only, then bridges them into asyncio.

    Includes a health monitor that alerts if no candle is received within
    the expected time window (interval + 15min buffer).
    """

    # How long past the expected candle time before alerting (seconds)
    HEALTH_CHECK_BUFFER = 900  # 15 minutes

    def __init__(
        self,
        symbol: str,
        interval: int,                    # 60 for 1h
        on_candle: AsyncCandleCallback,   # async def on_candle(candle: Candle)
        loop: asyncio.AbstractEventLoop,
        testnet: bool = False,
        notifier: Optional[Any] = None,
    ):
        self.symbol   = symbol
        self.interval = interval
        self._on_candle = on_candle
        self._loop  = loop
        self._testnet = testnet
        self._notifier = notifier
        self._ws: WebSocket | None = None
        self._last_candle_time: float = time.time()
        self._health_task: Optional[asyncio.Task] = None
        self._stopped = False

    def start(self) -> None:
        """
        Start the WebSocket subscription in a background thread (pybit manages it).
        Also starts the health monitor coroutine.
        Call this once from the main thread / event loop.
        """
        logger.info(f"Connecting to Bybit kline.{self.interval}.{self.symbol} stream...")
        self._ws = WebSocket(
            testnet=self._testnet,
            channel_type="linear",
        )
        self._ws.kline_stream(
            interval=self.interval,
            symbol=self.symbol,
            callback=self._handle_message,
        )
        self._last_candle_time = time.time()
        logger.info("WebSocket subscription active — waiting for candles")

        # Start health monitor in the event loop
        self._health_task = asyncio.run_coroutine_threadsafe(
            self._health_monitor(), self._loop
        )

    def stop(self) -> None:
        """Stop the WebSocket connection and health monitor."""
        self._stopped = True
        if self._health_task is not None:
            self._health_task.cancel()
        if self._ws is not None:
            try:
                self._ws.exit()
            except Exception as exc:
                logger.warning(f"WebSocket exit error: {exc}")

    def _handle_message(self, msg: dict) -> None:
        """
        Called from the pybit background thread on every kline update.
        We only care about confirmed (closed) candles.

        Bybit kline message format:
        {
            "topic": "kline.60.ETHUSDT",
            "data": [{
                "start":   1711065600000,   # open_time ms
                "end":     1711069200000,   # close_time ms
                "interval": "60",
                "open":    "3500.00",
                "close":   "3520.00",
                "high":    "3540.00",
                "low":     "3490.00",
                "volume":  "1234.56",
                "confirm": true,            # ← True only when candle is closed
                ...
            }],
            "ts": 1711069200123
        }
        """
        try:
            data_list = msg.get("data", [])
            if not data_list:
                return

            d = data_list[0]
            if not d.get("confirm", False):
                return  # candle still open — skip

            candle = Candle(
                symbol     = self.symbol,
                open       = float(d["open"]),
                high       = float(d["high"]),
                low        = float(d["low"]),
                close      = float(d["close"]),
                volume     = float(d["volume"]),
                open_time  = int(d["start"]),
                close_time = int(d["end"]),
            )
            logger.info(
                f"Candle confirmed: {self.symbol} "
                f"O={candle.open} H={candle.high} L={candle.low} C={candle.close}"
            )

            self._last_candle_time = time.time()

            # Bridge from pybit thread → asyncio event loop
            future = asyncio.run_coroutine_threadsafe(
                self._on_candle(candle),
                self._loop,
            )
            # Catch unhandled exceptions in on_candle
            future.add_done_callback(self._on_candle_done)

        except Exception as exc:
            logger.error(f"Error handling kline message: {exc}", exc_info=True)

    def _on_candle_done(self, future: asyncio.futures.Future) -> None:
        """
        Callback attached to the on_candle Future.
        Logs and notifies if on_candle raised an unhandled exception.
        """
        exc = future.exception()
        if exc is not None:
            logger.error(f"on_candle raised unhandled exception: {exc}", exc_info=exc)
            # Send notification from the thread (sync)
            if self._notifier is not None and hasattr(self._notifier, 'send_sync'):
                try:
                    self._notifier.send_sync(
                        f"❌ <b>on_candle exception</b>\n"
                        f"<code>{type(exc).__name__}: {exc}</code>"
                    )
                except Exception:
                    pass

    async def _health_monitor(self) -> None:
        """
        Periodically checks that candles are arriving on time.
        If no candle has been received within interval + buffer, sends an alert
        and attempts to reconnect the WebSocket.
        """
        interval_seconds = self.interval * 60  # interval is in minutes
        timeout = interval_seconds + self.HEALTH_CHECK_BUFFER
        check_every = 60  # check once per minute

        while not self._stopped:
            try:
                await asyncio.sleep(check_every)

                elapsed = time.time() - self._last_candle_time
                if elapsed > timeout:
                    logger.warning(
                        f"WebSocket health: no candle received for "
                        f"{elapsed:.0f}s (timeout={timeout}s) — possible disconnect"
                    )
                    if self._notifier is not None:
                        await self._notifier.send(
                            f"⚠️ <b>WebSocket: нет свечей</b> уже {elapsed / 60:.0f} мин\n"
                            f"Возможно отключение. Попытка переподключения..."
                        )
                    # Attempt reconnect
                    try:
                        await self._reconnect()
                    except Exception as exc:
                        logger.error(f"Reconnect failed: {exc}")
                        if self._notifier is not None:
                            await self._notifier.send(
                                f"❌ <b>Reconnect failed</b>: <code>{exc}</code>"
                            )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Health monitor error: {exc}", exc_info=True)
                await asyncio.sleep(60)

    async def _reconnect(self) -> None:
        """Attempt to reconnect the WebSocket."""
        logger.info("Attempting WebSocket reconnect...")
        # Close existing
        if self._ws is not None:
            try:
                self._ws.exit()
            except Exception:
                pass

        # Re-create
        self._ws = WebSocket(
            testnet=self._testnet,
            channel_type="linear",
        )
        self._ws.kline_stream(
            interval=self.interval,
            symbol=self.symbol,
            callback=self._handle_message,
        )
        self._last_candle_time = time.time()
        logger.info("WebSocket reconnected")

        if self._notifier is not None:
            await self._notifier.send(
                f"✅ <b>WebSocket переподключён</b> — kline.{self.interval}.{self.symbol}"
            )
