"""Event data models of the usage module."""

from pydantic import BaseModel

from crm.events.registry import event


@event("crm.wallet.low")
class WalletLow(BaseModel):
    balance_micros: int
    currency: str
