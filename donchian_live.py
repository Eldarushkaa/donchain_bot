"""
DonchianBot — Donchian Breakout strategy (Turtle Trading) for live Bybit trading.

seed_from_history(candles) primes all indicators from historical Candle objects
so the bot is ready to trade immediately on startup — no warmup period.

No database — all indicator state is held in memory (~1 KB).

Ported from trade_platform/strategies/donchian_stable.py.
No inheritance — fully standalone.

Works on 1h candles. ETHUSDT linear perpetual with leverage.

Logic summary:
  Entry LONG:  close > max(high[-N:])   (price breaks above N-bar high)
  Entry SHORT: close < min(low[-N:])    (price breaks below N-bar low)
  Exit LONG:   close < min(low[-M:])    (Turtle exit)
  Exit SHORT:  close > max(high[-M:])   (Turtle exit)

Filters:
  1. Volatility: ATR / EMA(ATR) > VOL_RATIO_MIN
  2. EMA200 proximity: abs(close - EMA200) < EMA200_ATR_K * ATR

Warmup: 200 candles for EMA200 + 14 for ATR — bot sends warmup notifications
        until all indicators are ready, then starts trading.

Notifications sent on every closed candle (always exactly one message per hour).

Position safety:
  - sync_position() called after every order to guarantee state consistency
  - Periodic sync every POSITION_SYNC_INTERVAL candles as safety net
"""
import logging
import math
from typing import Optional

from config import Config
from bybit_engine import BybitEngine, PositionState
from bybit_feed import Candle
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)

# Warmup notification cadence — only ping Telegram every N candles during warmup
WARMUP_NOTIFY_EVERY = 20

# How often to force-sync position from Bybit (in candles) as safety net
POSITION_SYNC_INTERVAL = 6  # every 6 hours at 1h candles


class DonchianBot:
    def __init__(self, cfg: Config, engine: BybitEngine, notifier: TelegramNotifier):
        self.cfg      = cfg
        self.engine   = engine
        self.notifier = notifier
        self.symbol   = cfg.symbol

        # ── Strategy parameters ────────────────────────────────
        self.N_PERIOD      = cfg.n_period
        self.M_PERIOD      = cfg.m_period
        self.EMA200_ATR_K  = cfg.ema200_atr_k
        self.VOL_RATIO_MIN = cfg.vol_ratio_min
        self.TRADE_FRACTION = cfg.trade_fraction

        # Fixed indicator periods
        self.EMA_SLOW_PERIOD = 200
        self.ATR_PERIOD      = 14
        self.EMA_ATR_PERIOD  = 20
        self.HISTORY_MAX     = max(cfg.n_period, cfg.m_period) + 20

        # ── EMA200 state ───────────────────────────────────────
        self._ema_slow:   Optional[float] = None
        self._ema_warmup: list[float]     = []

        # ── ATR + EMA(ATR) state ───────────────────────────────
        self._atr:        Optional[float] = None
        self._ema_atr:    Optional[float] = None
        self._warmup_tr:  list[float]     = []
        self._prev_close: Optional[float] = None

        # ── Donchian channel buffers ───────────────────────────
        self._high_buf: list[float] = []
        self._low_buf:  list[float] = []

        # ── Runtime state ──────────────────────────────────────
        self._candle_count: int = 0
        self._position = PositionState(side="NONE", size=0.0, avg_price=0.0, unrealised_pnl=0.0)
        self._qty_step: float = 0.01  # default, overridden at startup

        # ── Last indicator snapshot (for notification building) ─
        self._last_vol_ratio: float = 0.0
        self._last_vol_ok:    bool  = False
        self._last_dist_ok:   bool  = False
        self._last_distance:  float = 0.0
        self._last_high_n:    float = 0.0
        self._last_low_n:     float = 0.0

    # ──────────────────────────────────────────────────────────
    # Startup — seed indicators from historical candles
    # ──────────────────────────────────────────────────────────

    def seed_from_history(self, candles: list[Candle]) -> None:
        """
        Prime EMA200, ATR, and Donchian buffers from stored historical candles.
        No orders are placed — purely indicator seeding.

        After this call the bot is fully warmed up and ready to trade
        on the very next live candle.
        """
        if not candles:
            logger.warning("seed_from_history: empty candle list — warmup will be needed")
            return

        logger.info(f"Seeding indicators from {len(candles)} historical candles...")
        for c in candles:
            self._update_ema_slow(c.close)
            self._update_atr(c.high, c.low, c.close)
            self._append_history(c.high, c.low)
            self._candle_count += 1

        ready = (
            self._ema_slow is not None
            and self._atr is not None
            and self._ema_atr is not None
            and len(self._high_buf) >= self.N_PERIOD
            and len(self._high_buf) >= self.M_PERIOD
        )
        if ready:
            logger.info(
                f"Indicators ready after seeding "
                f"EMA200={self._ema_slow:.2f}  ATR={self._atr:.4f}  "
                f"buf={len(self._high_buf)} candles"
            )
        else:
            logger.warning(
                f"Indicators NOT fully ready after seeding "
                f"{len(candles)} candles — may need more history. "
                f"ema_slow={'ok' if self._ema_slow else 'MISSING'}  "
                f"atr={'ok' if self._atr else 'MISSING'}  "
                f"buf={len(self._high_buf)}/{self.N_PERIOD}"
            )

    # ──────────────────────────────────────────────────────────
    # Startup — sync position from Bybit (hard gate)
    # ──────────────────────────────────────────────────────────

    async def sync_position(self) -> None:
        """
        Read current open position from Bybit.
        Used at startup (hard gate) and after every order for consistency.

        engine.get_position() already has infinite retry, so this only
        fails on truly unrecoverable errors.
        """
        state: PositionState = await self.engine.get_position(self.symbol)
        self._position = state
        logger.info(
            f"Position synced: {state.side} "
            f"size={state.size} avg={state.avg_price}"
        )

    # ──────────────────────────────────────────────────────────
    # Startup — load dynamic qty step from Bybit
    # ──────────────────────────────────────────────────────────

    async def load_qty_step(self) -> None:
        """Fetch qty step size from Bybit instruments info. Call once at startup."""
        self._qty_step = await self.engine.get_qty_step(self.symbol)
        logger.info(f"QTY_STEP loaded for {self.symbol}: {self._qty_step}")

    # ──────────────────────────────────────────────────────────
    # Main candle handler
    # ──────────────────────────────────────────────────────────

    async def on_candle(self, candle: Candle) -> None:
        self._candle_count += 1
        close  = candle.close
        high   = candle.high
        low    = candle.low

        # --- Periodic position sync as safety net ---
        if self._candle_count % POSITION_SYNC_INTERVAL == 0:
            try:
                await self.sync_position()
            except Exception as exc:
                logger.error(f"Periodic sync_position failed: {exc}")
                await self.notifier.notify_error("periodic_sync_position", str(exc))

        # --- 1. Update EMA200 ---
        self._update_ema_slow(close)

        # --- 2. Update ATR ---
        self._update_atr(high, low, close)

        # --- 3. Warmup guard ---
        if self._ema_slow is None or self._atr is None or self._ema_atr is None:
            self._append_history(high, low)
            if self._candle_count % WARMUP_NOTIFY_EVERY == 0:
                needed = self.EMA_SLOW_PERIOD + self.N_PERIOD
                await self.notifier.notify_warmup(
                    self.symbol, self._candle_count, needed
                )
            return

        n = self.N_PERIOD
        m = self.M_PERIOD

        # Need N bars before current candle
        if len(self._high_buf) < n:
            self._append_history(high, low)
            needed = self.EMA_SLOW_PERIOD + n
            if self._candle_count % WARMUP_NOTIFY_EVERY == 0:
                await self.notifier.notify_warmup(
                    self.symbol, self._candle_count, needed
                )
            return

        # --- 4. Donchian channels (exclude current candle) ---
        high_n = max(self._high_buf[-n:])
        low_n  = min(self._low_buf[-n:])

        if len(self._high_buf) < m:
            self._append_history(high, low)
            return

        exit_high = max(self._high_buf[-m:])
        exit_low  = min(self._low_buf[-m:])

        # --- 5. Compute filters ---
        vol_ratio = self._atr / self._ema_atr if self._ema_atr > 0 else 0.0
        vol_ok    = vol_ratio > self.VOL_RATIO_MIN
        distance  = abs(close - self._ema_slow)
        dist_ok   = distance < self.EMA200_ATR_K * self._atr

        # Save for notification
        self._last_vol_ratio = vol_ratio
        self._last_vol_ok    = vol_ok
        self._last_dist_ok   = dist_ok
        self._last_distance  = distance
        self._last_high_n    = high_n
        self._last_low_n     = low_n

        # --- 6. EXIT LOGIC (checked first, independent of entry filters) ---
        action_taken = False
        position_side = self._position.side

        if position_side == "LONG":
            if close < exit_low:
                action_taken = True
                await self._close_long(close, exit_low)

        elif position_side == "SHORT":
            if close > exit_high:
                action_taken = True
                await self._close_short(close, exit_high)

        # --- 7. ENTRY LOGIC ---
        position_side = self._position.side  # re-read after potential close
        if position_side == "NONE":
            if close > high_n:
                # Signal: LONG breakout
                if vol_ok and dist_ok:
                    action_taken = True
                    await self._open_long(close, high_n)
                else:
                    action_taken = True
                    await self.notifier.notify_signal_blocked(
                        symbol=self.symbol,
                        direction="LONG",
                        close=close,
                        vol_ok=vol_ok,
                        vol_ratio=vol_ratio,
                        vol_ratio_min=self.VOL_RATIO_MIN,
                        dist_ok=dist_ok,
                        distance=distance,
                        atr=self._atr,
                        ema200_atr_k=self.EMA200_ATR_K,
                        channel_val=high_n,
                        n=n,
                    )

            elif close < low_n:
                # Signal: SHORT breakout
                if vol_ok and dist_ok:
                    action_taken = True
                    await self._open_short(close, low_n)
                else:
                    action_taken = True
                    await self.notifier.notify_signal_blocked(
                        symbol=self.symbol,
                        direction="SHORT",
                        close=close,
                        vol_ok=vol_ok,
                        vol_ratio=vol_ratio,
                        vol_ratio_min=self.VOL_RATIO_MIN,
                        dist_ok=dist_ok,
                        distance=distance,
                        atr=self._atr,
                        ema200_atr_k=self.EMA200_ATR_K,
                        channel_val=low_n,
                        n=n,
                    )

        # --- 8. No signal — send hourly status ---
        if not action_taken:
            await self.notifier.notify_no_signal(
                symbol=self.symbol,
                close=close,
                ema200=self._ema_slow,
                high_n=high_n,
                low_n=low_n,
                exit_high_m=exit_high,
                exit_low_m=exit_low,
                vol_ratio=vol_ratio,
                position=self._position.side,
                n=n,
                m=m,
            )

        # --- 9. Append current candle to history AFTER all logic ---
        self._append_history(high, low)

    # ──────────────────────────────────────────────────────────
    # Order actions — sync position from exchange after every order
    # ──────────────────────────────────────────────────────────

    async def _open_long(self, price: float, high_n: float) -> None:
        """Open a LONG position — notional = balance × trade_fraction × leverage."""
        try:
            balance = await self.engine.get_usdt_balance()
            margin = balance * self.TRADE_FRACTION
            notional = margin * self.cfg.leverage
            usdt_to_spend = margin
            qty = self._round_qty(notional / price)

            if qty < self._qty_step:
                logger.warning(f"Insufficient balance for LONG (qty={qty} < step={self._qty_step})")
                return

            await self.engine.place_market_order(
                symbol=self.symbol, side="Buy", qty=qty, reduce_only=False
            )

            # Sync position from exchange for guaranteed consistency
            await self.sync_position()

            await self.notifier.notify_long_open(
                symbol=self.symbol,
                price=self._position.avg_price or price,
                usdt_spent=usdt_to_spend,
                qty=self._position.size or qty,
                high_n=high_n,
                ema200=self._ema_slow,
                vol_ratio=self._last_vol_ratio,
                n=self.N_PERIOD,
            )
            logger.info(
                f"LONG opened: {self._position.size} {self.symbol} "
                f"@ {self._position.avg_price}"
            )

        except Exception as exc:
            logger.error(f"_open_long failed: {exc}")
            await self.notifier.notify_error("open_long", str(exc))
            # Attempt to sync position even on error — order may have been placed
            try:
                await self.sync_position()
            except Exception:
                pass

    async def _open_short(self, price: float, low_n: float) -> None:
        """Open a SHORT position — notional = balance × trade_fraction × leverage."""
        try:
            balance = await self.engine.get_usdt_balance()
            margin = balance * self.TRADE_FRACTION
            notional = margin * self.cfg.leverage
            usdt_to_spend = margin
            qty = self._round_qty(notional / price)

            if qty < self._qty_step:
                logger.warning(f"Insufficient balance for SHORT (qty={qty} < step={self._qty_step})")
                return

            await self.engine.place_market_order(
                symbol=self.symbol, side="Sell", qty=qty, reduce_only=False
            )

            # Sync position from exchange for guaranteed consistency
            await self.sync_position()

            await self.notifier.notify_short_open(
                symbol=self.symbol,
                price=self._position.avg_price or price,
                usdt_spent=usdt_to_spend,
                qty=self._position.size or qty,
                low_n=low_n,
                ema200=self._ema_slow,
                vol_ratio=self._last_vol_ratio,
                n=self.N_PERIOD,
            )
            logger.info(
                f"SHORT opened: {self._position.size} {self.symbol} "
                f"@ {self._position.avg_price}"
            )

        except Exception as exc:
            logger.error(f"_open_short failed: {exc}")
            await self.notifier.notify_error("open_short", str(exc))
            # Attempt to sync position even on error
            try:
                await self.sync_position()
            except Exception:
                pass

    async def _close_long(self, price: float, exit_low: float) -> None:
        """Close LONG position."""
        try:
            entry_price = self._position.avg_price
            close_qty = self._position.size
            pnl = (price - entry_price) * close_qty

            await self.engine.place_market_order(
                symbol=self.symbol,
                side="Sell",
                qty=close_qty,
                reduce_only=True,
            )

            # Sync position from exchange for guaranteed consistency
            await self.sync_position()

            await self.notifier.notify_long_close(
                symbol=self.symbol,
                price=price,
                exit_low=exit_low,
                pnl=pnl,
            )
            logger.info(
                f"LONG closed: {close_qty} {self.symbol} @ {price}  PnL≈{pnl:+.2f}"
            )

        except Exception as exc:
            logger.error(f"_close_long failed: {exc}")
            await self.notifier.notify_error("close_long", str(exc))
            try:
                await self.sync_position()
            except Exception:
                pass

    async def _close_short(self, price: float, exit_high: float) -> None:
        """Close SHORT position."""
        try:
            entry_price = self._position.avg_price
            close_qty = self._position.size
            pnl = (entry_price - price) * close_qty

            await self.engine.place_market_order(
                symbol=self.symbol,
                side="Buy",
                qty=close_qty,
                reduce_only=True,
            )

            # Sync position from exchange for guaranteed consistency
            await self.sync_position()

            await self.notifier.notify_short_close(
                symbol=self.symbol,
                price=price,
                exit_high=exit_high,
                pnl=pnl,
            )
            logger.info(
                f"SHORT closed: {close_qty} {self.symbol} @ {price}  PnL≈{pnl:+.2f}"
            )

        except Exception as exc:
            logger.error(f"_close_short failed: {exc}")
            await self.notifier.notify_error("close_short", str(exc))
            try:
                await self.sync_position()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # Indicators
    # ──────────────────────────────────────────────────────────

    def _update_ema_slow(self, close: float) -> None:
        """EMA200: SMA seed on first 200 candles, then standard EMA."""
        k = 2.0 / (self.EMA_SLOW_PERIOD + 1)

        if self._ema_slow is None:
            self._ema_warmup.append(close)
            if len(self._ema_warmup) >= self.EMA_SLOW_PERIOD:
                self._ema_slow = sum(self._ema_warmup[:self.EMA_SLOW_PERIOD]) / self.EMA_SLOW_PERIOD
                self._ema_warmup.clear()
                logger.info(f"EMA200 ready: {self._ema_slow:.2f}")
        else:
            self._ema_slow = close * k + self._ema_slow * (1 - k)

    def _update_atr(self, high: float, low: float, close: float) -> None:
        """Wilder ATR(14) + EMA(ATR, 20) for vol_ratio baseline."""
        prev_close = self._prev_close if self._prev_close is not None else close
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low  - prev_close),
        )
        self._prev_close = close

        if self._atr is None:
            self._warmup_tr.append(tr)
            if len(self._warmup_tr) >= self.ATR_PERIOD:
                self._atr     = sum(self._warmup_tr) / self.ATR_PERIOD
                self._ema_atr = self._atr
                self._warmup_tr.clear()
        else:
            alpha = 1.0 / self.ATR_PERIOD
            self._atr = alpha * tr + (1 - alpha) * self._atr
            k = 2.0 / (self.EMA_ATR_PERIOD + 1)
            self._ema_atr = self._atr * k + self._ema_atr * (1 - k)

    def _append_history(self, high: float, low: float) -> None:
        """Append candle high/low to rolling buffers."""
        self._high_buf.append(high)
        self._low_buf.append(low)
        if len(self._high_buf) > self.HISTORY_MAX:
            self._high_buf = self._high_buf[-self.HISTORY_MAX:]
            self._low_buf  = self._low_buf[-self.HISTORY_MAX:]

    def _round_qty(self, qty: float) -> float:
        """Round quantity down to the symbol's step size."""
        return math.floor(qty / self._qty_step) * self._qty_step
