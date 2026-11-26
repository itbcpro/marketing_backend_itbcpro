"""Value handling per field type. New type = one new handler in ``HANDLERS`` (open/closed)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from crm.common.normalize import (
    InvalidValueError,
    normalise_email,
    normalise_phone,
    normalise_url,
)
from crm.fields.models import FieldType, FieldValue

MAX_TEXT = 500
MAX_LONG_TEXT = 20_000
MAX_NUMBER = Decimal("999999999999.999999")
CURRENCY_LENGTH = 3


@dataclass(frozen=True)
class Stored:
    """The column values to save; unset columns stay empty."""

    text: str | None = None
    number: Decimal | None = None
    day: date | None = None
    json: Any = None


class Handler(Protocol):
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored: ...

    def read(self, row: FieldValue) -> Any: ...


def _text(raw: Any, limit: int) -> str:
    if not isinstance(raw, str):
        raise InvalidValueError("invalid_text")
    value = raw.strip()
    if len(value) > limit:
        raise InvalidValueError("too_long")
    return value


def _number(raw: Any) -> Decimal:
    if isinstance(raw, bool):
        raise InvalidValueError("invalid_number")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise InvalidValueError("invalid_number") from exc
    if not value.is_finite() or abs(value) > MAX_NUMBER:
        raise InvalidValueError("invalid_number")
    return value


class TextHandler:
    limit = MAX_TEXT

    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        return Stored(text=_text(raw, self.limit))

    def read(self, row: FieldValue) -> Any:
        return row.value_text


class LongTextHandler(TextHandler):
    limit = MAX_LONG_TEXT


class NumberHandler:
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        return Stored(number=_number(raw))

    def read(self, row: FieldValue) -> Any:
        return None if row.value_number is None else float(row.value_number)


class MoneyHandler:
    """``{"amount": "12.50", "currency": "CAD"}``."""

    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        if not isinstance(raw, dict):
            raise InvalidValueError("invalid_money")
        currency = str(raw.get("currency", "")).upper()
        if len(currency) != CURRENCY_LENGTH or not currency.isalpha():
            raise InvalidValueError("invalid_currency")
        return Stored(number=_number(raw.get("amount")), json={"currency": currency})

    def read(self, row: FieldValue) -> Any:
        if row.value_number is None:
            return None
        return {"amount": str(row.value_number), "currency": (row.value_json or {}).get("currency")}


class PhoneHandler:
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        return Stored(text=normalise_phone(_text(raw, MAX_TEXT)))

    def read(self, row: FieldValue) -> Any:
        return row.value_text


class EmailHandler:
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        return Stored(text=normalise_email(_text(raw, MAX_TEXT)))

    def read(self, row: FieldValue) -> Any:
        return row.value_text


class UrlHandler:
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        return Stored(text=normalise_url(_text(raw, MAX_TEXT)))

    def read(self, row: FieldValue) -> Any:
        return row.value_text


class DateHandler:
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        try:
            return Stored(day=date.fromisoformat(_text(raw, MAX_TEXT)))
        except ValueError as exc:
            raise InvalidValueError("invalid_date") from exc

    def read(self, row: FieldValue) -> Any:
        return row.value_date.isoformat() if row.value_date else None


def _allowed(options: list[dict[str, str]]) -> set[str]:
    return {o["value"] for o in options}


class SelectHandler:
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        value = _text(raw, MAX_TEXT)
        if value not in _allowed(options):
            raise InvalidValueError("invalid_option")
        return Stored(text=value)

    def read(self, row: FieldValue) -> Any:
        return row.value_text


class MultiSelectHandler:
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        if not isinstance(raw, list):
            raise InvalidValueError("invalid_option")
        values = [_text(item, MAX_TEXT) for item in raw]
        if not set(values) <= _allowed(options):
            raise InvalidValueError("invalid_option")
        return Stored(json=sorted(set(values)))

    def read(self, row: FieldValue) -> Any:
        return row.value_json or []


class CheckboxHandler:
    def normalise(self, raw: Any, options: list[dict[str, str]]) -> Stored:
        if not isinstance(raw, bool):
            raise InvalidValueError("invalid_checkbox")
        return Stored(json=raw)

    def read(self, row: FieldValue) -> Any:
        return bool(row.value_json)


HANDLERS: dict[str, Handler] = {
    FieldType.TEXT: TextHandler(),
    FieldType.LONG_TEXT: LongTextHandler(),
    FieldType.NUMBER: NumberHandler(),
    FieldType.MONEY: MoneyHandler(),
    FieldType.PHONE: PhoneHandler(),
    FieldType.EMAIL: EmailHandler(),
    FieldType.URL: UrlHandler(),
    FieldType.DATE: DateHandler(),
    FieldType.SELECT: SelectHandler(),
    FieldType.MULTI_SELECT: MultiSelectHandler(),
    FieldType.CHECKBOX: CheckboxHandler(),
}
OPTION_TYPES = frozenset({FieldType.SELECT, FieldType.MULTI_SELECT})
