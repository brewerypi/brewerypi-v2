"""Tests for how the web app words times and durations."""

from datetime import datetime, timedelta

import pytest

from brewerypi.web.rendering import elapsed, local_time


@pytest.mark.parametrize(
    "iso, shown",
    [
        ("2026-10-20T09:45:00-05:00", "20 Oct 2026, 9:45 AM"),
        ("2026-01-05T00:05:00+00:00", "5 Jan 2026, 12:05 AM"),
        ("2026-12-31T12:00:00-06:00", "31 Dec 2026, 12:00 PM"),
        ("2026-07-04T23:59:59.5+01:00", "4 Jul 2026, 11:59 PM"),
    ],
)
def test_local_time_reads_the_wall_clock_from_the_text(iso, shown):
    assert local_time(iso) == shown


@pytest.mark.parametrize(
    "value, shown", [(None, ""), ("", ""), ("not a time", "not a time")]
)
def test_local_time_passes_through_what_it_cant_read(value, shown):
    assert local_time(value) == shown


@pytest.mark.parametrize(
    "span, shown",
    [
        (timedelta(minutes=0), "0 minutes"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(minutes=59), "59 minutes"),
        (timedelta(hours=1), "1 hour"),
        (timedelta(hours=47), "47 hours"),
        (timedelta(hours=48), "2 days"),
        (timedelta(days=14, hours=23), "14 days"),
    ],
)
def test_elapsed_uses_the_largest_natural_unit(span, shown):
    start = datetime(2026, 1, 1)
    assert elapsed(start, start + span) == shown


def test_elapsed_treats_a_start_in_the_future_as_no_time():
    start = datetime(2026, 1, 2)
    assert elapsed(start, start - timedelta(hours=3)) == "0 minutes"
