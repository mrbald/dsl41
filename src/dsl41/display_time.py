"""Host-local timestamps for human-facing CLI and TUI output (DL-473)."""

from datetime import UTC, datetime

from dsl41.canon import naive_utc


def format_local_at(value: object) -> str:
    """Render an instant to seconds, absence as '-', and malformed input explicitly."""
    if value is None:
        return "-"
    if not isinstance(value, (str, datetime)):
        return f"invalid timestamp: {value!r}"
    try:
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        local = naive_utc(instant).replace(tzinfo=UTC).astimezone()
        return local.replace(tzinfo=None).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return f"invalid timestamp: {value!r}"
