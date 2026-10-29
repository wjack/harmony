"""How a responder names a request: the requester's opening, greeting set aside."""

from __future__ import annotations

import pytest

from harmony.domain.threads import opening_title


@pytest.mark.parametrize(
    ("body", "title"),
    [
        (
            "Hello, this is Claude for Henderson Frame Lab at Northlake University. We need powder"
            " XRD and SEM/EDS on 12 layered-oxide powders.",
            "We need powder XRD and SEM/EDS on 12 layered-oxide powders.",
        ),
        (
            "Hi! I'm Claude, representing Vantage.\nOne of our clients needs a rush SEM.",
            "One of our clients needs a rush SEM.",
        ),
        ("Could you run XRD on 14 powders?\nDetails follow.", "Could you run XRD on 14 powders?"),
        # Nothing but a greeting: the first line is still better than no title.
        (
            "Hello, this is Claude for Henderson Frame Lab.",
            "Hello, this is Claude for Henderson Frame Lab.",
        ),
    ],
)
def test_the_title_skips_the_representatives_introduction(body: str, title: str) -> None:
    assert opening_title(body) == title


def test_two_requests_from_one_org_read_differently() -> None:
    hello = "Hello, this is Claude for Henderson Frame Lab. "
    first = opening_title(hello + "We need powder XRD on 6 layered-oxide powders.")
    second = opening_title(hello + "We need an airtight holder for a Bruker D8 dome mount.")
    assert first != second
    assert not first.startswith("Hello")


def test_a_long_opening_is_cut_to_one_line() -> None:
    title = opening_title("Hello, this is Claude for Henderson Frame Lab. " + "XRD " * 40)
    assert len(title) == 80
    assert title.endswith("…")
