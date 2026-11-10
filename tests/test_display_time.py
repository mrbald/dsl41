"""BLK-6 host-local display, including historical offsets and the repeated hour."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from dsl41.display_time import format_local_at

pytestmark = pytest.mark.usefixtures("local_timezone")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "-"),
        ("broken", "invalid timestamp: 'broken'"),
        ("", "invalid timestamp: ''"),
        ("2026-02-30T00:00:00", "invalid timestamp: '2026-02-30T00:00:00'"),
        (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T04:04:05"),
        (datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "2026-01-02T04:04:05"),
        ("2026-07-02T03:04:05.123456Z", "2026-07-02T05:04:05"),
        ("2026-01-02T03:04:05+05:45", "2026-01-01T22:19:05"),
        ("2026-07-01T23:30:00", "2026-07-02T01:30:00"),
        ("2026-10-25T00:30:00", "2026-10-25T02:30:00"),
        ("2026-10-25T01:30:00", "2026-10-25T02:30:00"),
        ("2026-03-29T00:59:59", "2026-03-29T01:59:59"),
        ("2026-03-29T01:00:00", "2026-03-29T03:00:00"),
        (
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5))),
            "2026-01-02T09:04:05",
        ),
    ],
)
def test_local_at(value: datetime | str | None, expected: str) -> None:
    assert format_local_at(value) == expected


def test_fractional_hour_host_zone(local_timezone) -> None:
    local_timezone("Asia/Kathmandu")
    assert format_local_at("2026-01-01T23:30:00") == "2026-01-02T05:15:00"


@pytest.mark.parametrize(
    ("zone", "value"),
    [
        ("Europe/Zurich", 5),
        ("Europe/Zurich", b"x"),
        ("Europe/Zurich", "9999-12-31T23:59:59"),
        ("America/New_York", "0001-01-01T00:00:00"),
        ("Europe/Zurich", "0001-01-01T00:00:00+01:00"),
    ],
)
def test_invalid_types_and_overflow_are_explicit(local_timezone, zone: str, value: object) -> None:
    local_timezone(zone)
    assert format_local_at(value) == f"invalid timestamp: {value!r}"
