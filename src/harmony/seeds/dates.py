"""Scenario dates, written relative to the scenario's anchor (ARCHITECTURE.md §11.1).

Seeds say `+24d` or `-3d` rather than a calendar date, so the content stays coherent
with the world clock (anchor + time since the world was created) whenever the demo is
opened. Absolute dates are accepted too, for facts that never move.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_RELATIVE = re.compile(r"(?P<sign>[+-])(?P<days>\d{1,4})d")


def is_relative_date(value: str) -> bool:
    """True for `+<n>d` / `-<n>d` (at most 9999 days)."""
    return _RELATIVE.fullmatch(value) is not None


def resolve_date(value: date | str, anchor: date) -> date:
    """`value` as a calendar date: `+24d` with anchor 2026-10-26 is 2026-11-19.

    Raises `ValueError` for anything but a date or a relative date.
    """
    if isinstance(value, datetime):
        raise ValueError(f"expected a date, not a date and time: {value.isoformat()}")
    if isinstance(value, date):
        return value
    match = _RELATIVE.fullmatch(value)
    if match is None:
        raise ValueError(
            f"expected a date such as 2026-11-06 or a relative date such as +11d, got {value!r}"
        )
    days = int(match["days"])
    return anchor + timedelta(days=days if match["sign"] == "+" else -days)


# `{+11d}` in story text: a day on the world calendar, counted from the anchor.
_TEMPLATE = re.compile(r"\{([^{}]*)\}")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def date_templates(text: str) -> list[str]:
    """The contents of every `{...}` in `text`, in order: `["+11d"]`."""
    return _TEMPLATE.findall(text)


def short_date(value: date) -> str:
    """`Nov 6`: how prose names a day. Fixed English, whatever the process locale."""
    return f"{_MONTHS[value.month - 1]} {value.day}"


def render_dates(text: str, anchor: date) -> str:
    """`text` with each `{+<n>d}` replaced by that day counted from `anchor`, as
    `Nov 6`. Raises `ValueError` for a `{...}` that isn't a relative date.

    Pass the scenario's anchor, not the world's current date: a story's dates sit on
    the world calendar beside the seeds' (Northlake's calendar says Nov 10 whenever it
    is read), so "by Nov 6" stays the deadline the story is built on however long the
    world has lived or been fast-forwarded."""
    return _TEMPLATE.sub(lambda match: short_date(resolve_date(match[1], anchor)), text)
