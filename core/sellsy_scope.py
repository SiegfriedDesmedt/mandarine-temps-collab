from __future__ import annotations

from datetime import date, datetime


SELLSY_DOCUMENT_START_DATE = date(2021, 1, 1)
SELLSY_DOCUMENT_START_ISO = SELLSY_DOCUMENT_START_DATE.isoformat()


def sellsy_document_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def is_sellsy_document_in_scope(value) -> bool:
    document_date = sellsy_document_date(value)
    return bool(document_date and document_date >= SELLSY_DOCUMENT_START_DATE)
