"""Public surface of the sending module."""

from crm.sending.feedback import record_bounce, record_complaint
from crm.sending.gateway import SendOutcome, send
from crm.sending.inbound_email import ingest as ingest_email
from crm.sending.rules import Block

__all__ = [
    "Block",
    "SendOutcome",
    "ingest_email",
    "record_bounce",
    "record_complaint",
    "send",
]
