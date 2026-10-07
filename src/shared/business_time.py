"""The business day, on top of timestamps stored naive UTC.

Every timestamp is written with ``datetime.utcnow()`` and stays that way; only
the READING of a day changes here. A UTC day runs 19:00 to 19:00 in Ecuador, so
a report cut on UTC midnights moved a 19:30 cut to the next day and opened a
"Este mes" after 19:00 on the 2nd. Reports and per-day figures cut on the
business zone's midnight (``config.DEFAULT_TIMEZONE``) instead.

Every function takes and returns NAIVE datetimes: UTC in, UTC out, except the
explicit ``to_local``.
"""

from datetime import date, datetime, time, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from src.shared.config import config


@lru_cache(maxsize=1)
def business_tz() -> ZoneInfo:
    return ZoneInfo(config.DEFAULT_TIMEZONE)


def to_local(dt_utc: datetime) -> datetime:
    """A naive UTC instant as the business's naive wall-clock time."""
    return (
        dt_utc.replace(tzinfo=timezone.utc)
        .astimezone(business_tz())
        .replace(tzinfo=None)
    )


def local_date(dt_utc: datetime) -> date:
    """The business day a naive UTC instant falls on."""
    return to_local(dt_utc).date()


def local_midnight_utc(day: date) -> datetime:
    """The naive UTC instant the business day ``day`` starts at."""
    return (
        datetime.combine(day, time.min, tzinfo=business_tz())
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def local_today() -> date:
    """Today, on the business's calendar (not the server's, not UTC's)."""
    return datetime.now(business_tz()).date()


def minute_of_day(dt_utc: datetime) -> int:
    """Minutes after the business's midnight for a naive UTC instant."""
    local = to_local(dt_utc)
    return local.hour * 60 + local.minute
