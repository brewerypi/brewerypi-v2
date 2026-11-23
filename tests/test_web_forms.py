"""Tests for the web form helpers' time boxes (web/forms.py)."""

from datetime import datetime

import pytest

from brewerypi.web.forms import box_changed, input_time

ZONE = "America/Chicago"


@pytest.mark.parametrize(
    "moment, shown",
    [
        # seconds are always written, even :00
        (datetime(2026, 1, 15, 18, 30), "2026-01-15T12:30:00"),
        (datetime(2026, 1, 15, 18, 30, 45), "2026-01-15T12:30:45"),
        # a fraction of a second is not shown
        (datetime(2026, 1, 15, 18, 30, 45, 500000), "2026-01-15T12:30:45"),
        (datetime(2026, 1, 15, 18, 30, 0, 500000), "2026-01-15T12:30:00"),
    ],
)
def test_input_time_shows_the_second(moment, shown):
    assert input_time(moment, ZONE) == shown


@pytest.mark.parametrize(
    "value, changed",
    [
        ("2026-01-15T12:30:45", False),
        (" 2026-01-15T12:30:45 ", False),
        ("2026-01-15T12:30:46", True),
        ("2026-01-15T12:30", True),
        ("not a time", True),
    ],
)
def test_box_changed_compares_to_the_second(value, changed):
    stored = datetime(2026, 1, 15, 18, 30, 45, 500000)
    assert box_changed(value, stored, ZONE) is changed


def test_box_changed_treats_zero_seconds_written_out_as_the_same():
    stored = datetime(2026, 1, 15, 18, 30)
    assert box_changed("2026-01-15T12:30", stored, ZONE) is False
    assert box_changed("2026-01-15T12:30:00", stored, ZONE) is False
