"""Time helpers.

:func:`utcnow` returns a naive UTC timestamp because every ``DateTime``
column in :mod:`app.models` is declared naive; mixing aware and naive
datetimes would raise ``TypeError`` on comparison.
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current UTC time as a naive ``datetime`` (no tzinfo)."""
    return datetime.now(UTC).replace(tzinfo=None)
