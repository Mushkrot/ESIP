"""Regression tests for deterministic Universal Chat temporal ranges."""

from __future__ import annotations

from datetime import datetime

from app.services import temporal_resolver


BUSINESS_TIME_NOW = datetime(2026, 7, 11, 10, 0, tzinfo=temporal_resolver.BUSINESS_TZ)


def test_past_month_is_a_business_time_rolling_30_day_window():
    period = temporal_resolver.resolve_period_text("how many tickets in the past month", now=BUSINESS_TIME_NOW)

    assert period is not None
    assert period.period_type == "rolling_range"
    assert period.matched_text == "past month"
    assert period.start_date.isoformat() == "2026-06-12"
    assert period.end_date_exclusive.isoformat() == "2026-07-12"


def test_last_month_remains_the_prior_calendar_month():
    period = temporal_resolver.resolve_period_text("how many tickets last month", now=BUSINESS_TIME_NOW)

    assert period is not None
    assert period.period_type == "calendar_month"
    assert period.start_date.isoformat() == "2026-06-01"
    assert period.end_date_exclusive.isoformat() == "2026-07-01"
