"""Injectable clock so time-dependent logic is deterministic in tests."""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    def __init__(self, moment: datetime) -> None:
        self._moment = self._validated(moment)

    @staticmethod
    def _validated(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        return moment

    def now(self) -> datetime:
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = self._validated(moment)


system_clock: Clock = SystemClock()
