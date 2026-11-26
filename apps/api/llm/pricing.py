"""Per-model prices in USD per million tokens (which equals micro-dollars per token).

Verified against the Claude API model table on 2026-09-30. Unknown models cost zero and are
flagged ``cost_known=False`` in the ledger, so a new model never silently under-reports.
"""

from dataclasses import dataclass

from llm.ports import Usage


@dataclass(frozen=True, slots=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
    "claude-haiku-4-5": Price(input=1.0, output=5.0, cache_read=0.10, cache_write=1.25),
    "claude-haiku-4-5-20251001": Price(input=1.0, output=5.0, cache_read=0.10, cache_write=1.25),
}


def cost_micros(model: str, usage: Usage) -> tuple[int, bool]:
    """Return ``(cost in micro-dollars, whether the price of the model is known)``."""
    price = PRICES.get(model)
    if price is None:
        return 0, False
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    )
    return round(total), True
