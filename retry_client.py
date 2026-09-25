import asyncio
import re
from types import SimpleNamespace


class AsyncRetryingGroq:
    """Async-обёртка над Groq, повторяющая запросы при 429.

    Задержку берёт из текста ошибки Groq
    ('Please try again in 1.07s'). Экспоненциальный backoff
    включается, если текст не распознан.
    """

    MAX_RETRIES = 6
    BACKOFF_BASE = 2.0
    BACKOFF_CAP = 60.0

    def __init__(self, inner):
        self.inner = inner
        self.rate_limit_waits = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    @staticmethod
    def _extract_retry_delay(message: str, default: float) -> float:
        match = re.search(
            r"try again in ([\d.]+)(ms|s)", message,
        )
        if not match:
            return default
        value = float(match.group(1))
        unit = match.group(2)
        return value / 1000 if unit == "ms" else value

    async def _create(self, **kwargs):
        delay = self.BACKOFF_BASE
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            try:
                return await self.inner.chat.completions.create(
                    **kwargs
                )
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                message = str(exc)

                if status == 429 or "429" in message:
                    last_error = exc
                    sleep_for = self._extract_retry_delay(
                        message, delay,
                    )
                    sleep_for = max(sleep_for, 1.0) + 0.5
                    self.rate_limit_waits += 1
                    print(
                        f"  [rate-limit] retry "
                        f"{attempt + 1}/{self.MAX_RETRIES} "
                        f"in {sleep_for:.2f}s"
                    )
                    await asyncio.sleep(sleep_for)
                    delay = min(
                        delay * 2, self.BACKOFF_CAP,
                    )
                    continue

                raise

        raise last_error

    async def close(self):
        close = getattr(self.inner, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False