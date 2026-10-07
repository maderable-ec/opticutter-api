"""The report axis (``analytics/dates.py``): bucket keys and the dense axis.

Pure date arithmetic, no database: the bottlenecks' series is the only report
that buckets now, and it reads these two functions.
"""

from datetime import date

from src.modules.analytics.constants import Granularity
from src.modules.analytics.dates import bucket_key, iter_buckets


def test_daily_axis_is_dense():
    assert iter_buckets(date(2026, 6, 1), date(2026, 6, 3), Granularity.day) == [
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
    ]


def test_weekly_buckets_start_on_the_iso_monday():
    # 2026-06-01 is a Monday; a Wednesday and the next Wednesday land on two weeks.
    assert bucket_key(date(2026, 6, 3), Granularity.week) == date(2026, 6, 1)
    assert bucket_key(date(2026, 6, 10), Granularity.week) == date(2026, 6, 8)
    assert iter_buckets(date(2026, 6, 1), date(2026, 6, 14), Granularity.week) == [
        date(2026, 6, 1),
        date(2026, 6, 8),
    ]


def test_monthly_buckets_cover_partial_months():
    assert iter_buckets(date(2026, 5, 15), date(2026, 7, 10), Granularity.month) == [
        date(2026, 5, 1),
        date(2026, 6, 1),
        date(2026, 7, 1),
    ]


def test_monthly_buckets_cross_the_year():
    assert iter_buckets(date(2025, 12, 1), date(2026, 1, 31), Granularity.month) == [
        date(2025, 12, 1),
        date(2026, 1, 1),
    ]
    assert bucket_key(date(2026, 1, 10), Granularity.month) == date(2026, 1, 1)
