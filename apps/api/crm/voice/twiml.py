"""Build the small TwiML documents Twilio asks for during a call. Everything is escaped."""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

HEADER = '<?xml version="1.0" encoding="UTF-8"?>'


def _wrap(*parts: str) -> str:
    return f"{HEADER}<Response>{''.join(parts)}</Response>"


def empty() -> str:
    return _wrap()


def say(text: str) -> str:
    return f"<Say>{escape(text)}</Say>"


def dial(number: str, *, timeout: int, action: str, record: bool) -> str:
    mode = ' record="record-from-answer-dual"' if record else ""
    return _wrap(
        f"<Dial timeout={quoteattr(str(timeout))} action={quoteattr(action)}{mode}>"
        f"<Number>{escape(number)}</Number></Dial>"
    )


def bridge(number: str) -> str:
    """Connect the agent who just answered to the customer."""
    return _wrap(f"<Dial><Number>{escape(number)}</Number></Dial>")


def voicemail(greeting: str, *, max_seconds: int, action: str, transcribe_callback: str) -> str:
    prompt = say(greeting) if greeting else ""
    return _wrap(
        prompt,
        f"<Record maxLength={quoteattr(str(max_seconds))} action={quoteattr(action)}"
        f' transcribe="true" transcribeCallback={quoteattr(transcribe_callback)}/>',
    )
