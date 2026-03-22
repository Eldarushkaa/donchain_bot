"""
Retry utilities — infinite retry with exponential backoff, capped at 15 minutes.

On every failed attempt:  sends a short Telegram notification (🔄)
On recovery after failure: sends a recovery notification (✅ API restored)
After prolonged failure (>ESCALATION_THRESHOLD): sends escalation alert (🚨)

Usage:
    result = retry_sync(lambda: session.get_positions(...), "get_position", notifier)
    result = await retry_async(lambda: engine.get_position("ETHUSDT"), "get_position", notifier)
"""
import asyncio
import logging
import time
from typing import Callable, TypeVar, Any, Optional

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Maximum delay between retries (15 minutes)
MAX_DELAY = 900.0
# Initial delay
INITIAL_DELAY = 2.0
# Backoff multiplier
BACKOFF = 2.0
# Escalation: send 🚨 alert after this many seconds of continuous failure
ESCALATION_THRESHOLD = 3600.0  # 1 hour
# Escalation: repeat 🚨 alert every N seconds while still failing
ESCALATION_REPEAT = 3600.0     # every hour


def retry_sync(
    fn: Callable[[], T],
    label: str,
    notifier: Optional[Any] = None,
) -> T:
    """
    Infinite sync retry with exponential backoff (cap 15min).
    Sends Telegram notification on every failure and on recovery.
    Sends escalation alert after prolonged failure (>1 hour).

    Args:
        fn:       callable to execute (no args — use lambda for binding)
        label:    human-readable name for logs/notifications
        notifier: TelegramNotifier instance (or None to skip notifications)

    Returns:
        Result of fn() on success.
    """
    delay = INITIAL_DELAY
    attempt = 0
    had_error = False
    first_fail_time: Optional[float] = None
    last_escalation_time: Optional[float] = None

    while True:
        try:
            result = fn()
            # If we had errors before, notify recovery
            if had_error and notifier is not None:
                elapsed = time.time() - first_fail_time if first_fail_time else 0
                _notify_sync(
                    notifier,
                    f"✅ <b>API восстановлен</b> [{label}] "
                    f"после {attempt} попыток ({_format_delay(elapsed)})"
                )
            return result

        except Exception as exc:
            attempt += 1
            now = time.time()

            if not had_error:
                had_error = True
                first_fail_time = now

            logger.warning(f"Retry #{attempt} [{label}] in {delay:.0f}s: {exc}")

            if notifier is not None:
                _notify_sync(
                    notifier,
                    f"🔄 Retry #{attempt} [{label}]: <code>{exc}</code>\n"
                    f"Следующая попытка через {_format_delay(delay)}"
                )

                # Escalation after prolonged failure
                elapsed = now - first_fail_time if first_fail_time else 0
                if elapsed >= ESCALATION_THRESHOLD:
                    should_escalate = (
                        last_escalation_time is None
                        or (now - last_escalation_time) >= ESCALATION_REPEAT
                    )
                    if should_escalate:
                        last_escalation_time = now
                        _notify_sync(
                            notifier,
                            f"🚨 <b>КРИТИЧНО: API недоступен</b> [{label}]\n"
                            f"Время простоя: <b>{_format_delay(elapsed)}</b>\n"
                            f"Попыток: {attempt}\n"
                            f"Последняя ошибка: <code>{exc}</code>"
                        )

            time.sleep(delay)
            delay = min(delay * BACKOFF, MAX_DELAY)


async def retry_async(
    fn: Callable[[], Any],
    label: str,
    notifier: Optional[Any] = None,
) -> Any:
    """
    Infinite async retry with exponential backoff (cap 15min).
    Sends Telegram notification on every failure and on recovery.
    Sends escalation alert after prolonged failure (>1 hour).

    Args:
        fn:       async callable (or sync callable — will be awaited if coroutine)
        label:    human-readable name for logs/notifications
        notifier: TelegramNotifier instance (or None to skip notifications)

    Returns:
        Result of fn() on success.
    """
    delay = INITIAL_DELAY
    attempt = 0
    had_error = False
    first_fail_time: Optional[float] = None
    last_escalation_time: Optional[float] = None

    while True:
        try:
            result = fn()
            # If fn returns a coroutine, await it
            if asyncio.iscoroutine(result):
                result = await result
            # Recovery notification
            if had_error and notifier is not None:
                elapsed = time.time() - first_fail_time if first_fail_time else 0
                await notifier.send(
                    f"✅ <b>API восстановлен</b> [{label}] "
                    f"после {attempt} попыток ({_format_delay(elapsed)})"
                )
            return result

        except Exception as exc:
            attempt += 1
            now = time.time()

            if not had_error:
                had_error = True
                first_fail_time = now

            logger.warning(f"Retry #{attempt} [{label}] in {delay:.0f}s: {exc}")

            if notifier is not None:
                try:
                    await notifier.send(
                        f"🔄 Retry #{attempt} [{label}]: <code>{exc}</code>\n"
                        f"Следующая попытка через {_format_delay(delay)}"
                    )
                except Exception:
                    pass  # don't let notification failure break the retry loop

                # Escalation after prolonged failure
                elapsed = now - first_fail_time if first_fail_time else 0
                if elapsed >= ESCALATION_THRESHOLD:
                    should_escalate = (
                        last_escalation_time is None
                        or (now - last_escalation_time) >= ESCALATION_REPEAT
                    )
                    if should_escalate:
                        last_escalation_time = now
                        try:
                            await notifier.send(
                                f"🚨 <b>КРИТИЧНО: API недоступен</b> [{label}]\n"
                                f"Время простоя: <b>{_format_delay(elapsed)}</b>\n"
                                f"Попыток: {attempt}\n"
                                f"Последняя ошибка: <code>{exc}</code>"
                            )
                        except Exception:
                            pass

            await asyncio.sleep(delay)
            delay = min(delay * BACKOFF, MAX_DELAY)


def _format_delay(seconds: float) -> str:
    """Format delay for human-readable messages."""
    if seconds < 60:
        return f"{seconds:.0f}с"
    elif seconds < 3600:
        return f"{seconds / 60:.0f}мин"
    else:
        return f"{seconds / 3600:.1f}ч"


def _notify_sync(notifier: Any, text: str) -> None:
    """
    Send a notification from a sync context via notifier.send_sync().
    Falls back gracefully if notifier has no send_sync method.
    """
    try:
        if hasattr(notifier, 'send_sync'):
            notifier.send_sync(text)
        else:
            logger.warning("Notifier has no send_sync method — skipping sync notification")
    except Exception as exc:
        logger.error(f"Telegram sync notify failed: {exc}")
