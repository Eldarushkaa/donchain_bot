"""
BybitEngine — thin wrapper over pybit.unified_trading.HTTP.

All pybit REST calls are synchronous and wrapped with infinite retry
(exponential backoff, cap 15min). On failure: Telegram notification.
On recovery after failure: Telegram notification.

Market orders use orderLinkId (UUID) for idempotent retry — if an order
succeeds on Bybit but the response is lost (timeout), the retry with the
same orderLinkId will be rejected as duplicate instead of doubling the position.

Async wrappers run sync calls in executor to avoid blocking the event loop.
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Optional, Any

from pybit.unified_trading import HTTP
from retry import retry_sync

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    side: str       # "LONG" / "SHORT" / "NONE"
    size: float     # qty in base asset
    avg_price: float
    unrealised_pnl: float


class BybitEngine:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self._session = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
        )
        self._notifier: Optional[Any] = None  # set after construction to avoid circular deps

    def set_notifier(self, notifier: Any) -> None:
        """Inject notifier after construction (avoids circular dependency)."""
        self._notifier = notifier

    # ──────────────────────────────────────────────────────────
    # Sync REST methods — all wrapped with infinite retry
    # ──────────────────────────────────────────────────────────

    def set_leverage_sync(self, symbol: str, leverage: int) -> None:
        lev = str(leverage)

        def _do():
            resp = self._session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=lev,
                sellLeverage=lev,
            )
            # retCode 110043 = "leverage not modified" — not an error
            if resp["retCode"] not in (0, 110043):
                raise RuntimeError(f"set_leverage failed: {resp}")
            logger.info(f"Leverage set to {leverage}x for {symbol}")

        retry_sync(_do, "set_leverage", self._notifier)

    def get_position_sync(self, symbol: str) -> PositionState:
        """Return current position state for symbol."""
        def _do():
            resp = self._session.get_positions(category="linear", symbol=symbol)
            if resp["retCode"] != 0:
                raise RuntimeError(f"get_positions failed: {resp}")
            return resp

        resp = retry_sync(_do, "get_position", self._notifier)

        items = resp["result"]["list"]
        if not items:
            return PositionState(side="NONE", size=0.0, avg_price=0.0, unrealised_pnl=0.0)

        item = items[0]
        raw_side = item.get("side", "None")
        size = float(item.get("size", 0) or 0)
        avg_price = float(item.get("avgPrice", 0) or 0)
        upnl = float(item.get("unrealisedPnl", 0) or 0)

        if size == 0 or raw_side == "None":
            return PositionState(side="NONE", size=0.0, avg_price=avg_price, unrealised_pnl=0.0)

        side = "LONG" if raw_side == "Buy" else "SHORT"
        return PositionState(side=side, size=size, avg_price=avg_price, unrealised_pnl=upnl)

    def get_usdt_balance_sync(self) -> float:
        """Return available USDT equity from Unified wallet."""
        def _do():
            resp = self._session.get_wallet_balance(accountType="UNIFIED")
            if resp["retCode"] != 0:
                raise RuntimeError(f"get_wallet_balance failed: {resp}")
            return resp

        resp = retry_sync(_do, "get_balance", self._notifier)

        accounts = resp["result"]["list"]
        if not accounts:
            return 0.0

        for coin in accounts[0].get("coin", []):
            if coin.get("coin") == "USDT":
                return float(coin.get("equity", 0) or 0)
        return 0.0

    def get_ticker_price_sync(self, symbol: str) -> float:
        """Return the last traded price for symbol from Bybit tickers."""
        def _do():
            resp = self._session.get_tickers(category="linear", symbol=symbol)
            if resp["retCode"] != 0:
                raise RuntimeError(f"get_tickers failed: {resp}")
            return resp

        resp = retry_sync(_do, "get_ticker_price", self._notifier)
        items = resp["result"]["list"]
        if not items:
            return 0.0
        return float(items[0].get("lastPrice", 0) or 0)

    def get_qty_step_sync(self, symbol: str) -> float:
        """
        Fetch the minimum quantity step size for a symbol from Bybit instruments info.
        Falls back to 0.01 if not found.
        """
        def _do():
            resp = self._session.get_instruments_info(category="linear", symbol=symbol)
            if resp["retCode"] != 0:
                raise RuntimeError(f"get_instruments_info failed: {resp}")
            return resp

        resp = retry_sync(_do, f"get_qty_step({symbol})", self._notifier)

        items = resp["result"]["list"]
        if not items:
            logger.warning(f"No instrument info for {symbol} — using default qtyStep=0.01")
            return 0.01

        lot_filter = items[0].get("lotSizeFilter", {})
        qty_step = float(lot_filter.get("qtyStep", 0.01) or 0.01)
        logger.info(f"QTY_STEP for {symbol}: {qty_step}")
        return qty_step

    def get_klines_sync(
        self, symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 200
    ) -> list[list]:
        """
        Fetch klines from Bybit /v5/market/kline with infinite retry.

        Returns list of raw kline rows: [[open_time, open, high, low, close, volume, turnover], ...]
        Bybit returns newest-first, so we reverse to oldest-first.

        Args:
            symbol:   e.g. "ETHUSDT"
            interval: e.g. "60" (minutes) — Bybit uses "1","3","5","15","30","60","120","240","360","720","D","W","M"
            start_ms: start timestamp in milliseconds
            end_ms:   end timestamp in milliseconds
            limit:    max candles per request (Bybit max = 200)
        """
        def _do():
            resp = self._session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                start=start_ms,
                end=end_ms,
                limit=limit,
            )
            if resp["retCode"] != 0:
                raise RuntimeError(f"get_kline failed: {resp}")
            return resp

        resp = retry_sync(_do, f"get_klines({symbol})", self._notifier)
        rows = resp["result"]["list"]
        # Bybit returns newest first — reverse to oldest first
        rows.reverse()
        return rows

    def place_market_order_sync(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = False,
    ) -> dict:
        """
        Place a market order with infinite retry.

        Uses orderLinkId (UUID) for idempotent retry — if the order succeeds on
        Bybit but the HTTP response is lost, the retry sends the same orderLinkId
        and Bybit rejects it as duplicate (retCode 110071) instead of placing
        a second order.
        """
        qty_str = str(round(qty, 4))
        order_link_id = str(uuid.uuid4())

        params = dict(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty_str,
            timeInForce="IOC",
            reduceOnly=reduce_only,
            orderLinkId=order_link_id,
        )
        logger.info(f"Placing order: {params}")

        def _do():
            resp = self._session.place_order(**params)
            ret_code = resp["retCode"]
            # 110072 = "OrderLinkedID is duplicate" — order already placed, treat as success
            if ret_code == 110072:
                logger.warning(
                    f"Duplicate orderLinkId {order_link_id} — order already filled"
                )
                return resp.get("result", {})
            if ret_code != 0:
                raise RuntimeError(f"place_order failed: {resp}")
            return resp["result"]

        result = retry_sync(_do, f"place_order({side} {qty_str} {symbol})", self._notifier)
        logger.info(f"Order placed: {result}")
        return result

    # ──────────────────────────────────────────────────────────
    # Async wrappers (run sync+retry in executor)
    # ──────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.set_leverage_sync, symbol, leverage)

    async def get_position(self, symbol: str) -> PositionState:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_position_sync, symbol)

    async def get_usdt_balance(self) -> float:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_usdt_balance_sync)

    async def get_ticker_price(self, symbol: str) -> float:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_ticker_price_sync, symbol)

    async def get_qty_step(self, symbol: str) -> float:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_qty_step_sync, symbol)

    async def get_klines(
        self, symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 200
    ) -> list[list]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.get_klines_sync(symbol, interval, start_ms, end_ms, limit),
        )

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = False,
    ) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.place_market_order_sync(symbol, side, qty, reduce_only),
        )
