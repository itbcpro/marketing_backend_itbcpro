"""Retry policy: exponential backoff with full jitter."""

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_seconds: float = 2.0
    cap_seconds: float = 900.0
    max_retries: int = 8

    def delay(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Seconds to wait before retry number ``attempt`` (0-based), with full jitter."""
        ceiling = min(self.cap_seconds, self.base_seconds * (2 ** max(attempt, 0)))
        return (rng or random).uniform(0.0, ceiling)

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_retries


DEFAULT_POLICY = RetryPolicy()
