"""Current time, overridable so trial data can be generated with past dates."""
from datetime import datetime, timezone

_fixed: datetime | None = None


def now() -> datetime:
    return _fixed or datetime.now(timezone.utc)


def set_now(t: datetime | None):
    global _fixed
    _fixed = t
