"""The guidance fact check (`steers.added_details`, EDIT.md §3.3): guidance may restate
the members' instructions, never add a number, date, name, email or link of its own."""

from __future__ import annotations

from harmony.domain.steers import added_details, checked_names

NAMES = ("Northlake", "Vantage", "Henderson", "Helen Park", "Helen", "Raj Mehta", "Raj")
ORGS = ("Northlake", "Vantage")


def added(text: str, instruction: str) -> list[str]:
    return added_details(text, allowed=(instruction, *ORGS), names=NAMES)


def test_numbers_must_come_from_the_instruction() -> None:
    assert added("Quote no more than $3,000.", "Never go above 3000 dollars.") == []
    assert added("Quote 1,620 per run.", "Don't discount.") == ["1620"]


def test_dates_must_come_from_the_instruction_in_any_form() -> None:
    assert added("Propose November 12.", "Propose Nov 12 only.") == []
    assert added("Propose 12th of November.", "Propose Nov 12 only.") == []
    assert added("Propose Tuesday.", "Propose an early slot.") == ["tuesday"]
    # A month on its own, or a modal verb, isn't a date.
    assert added("You may propose the reserve.", "Propose the reserve.") == []


def test_names_must_come_from_the_instruction_unless_they_are_the_parties() -> None:
    assert added("Ask Helen Park first.", "Check before you commit.") == ["Helen Park", "Helen"]
    assert added("Tell Henderson nothing.", "Say nothing about clients.") == ["Henderson"]
    assert added("Tell Vantage only what Northlake shares.", "Keep it short.") == []
    assert added("Ask Raj first.", "Ask raj before you commit.") == []


def test_a_titled_members_names_are_checked_and_the_title_is_not() -> None:
    names = checked_names(["Northlake"], ["Dr. Helen Ward", "Raj Patel"])
    assert names == ["Northlake", "Dr. Helen Ward", "Helen Ward", "Helen", "Raj Patel", "Raj"]
    allowed = ("Don't quote Kalrez.", "Northlake")
    assert added_details("Helen will confirm it.", allowed=allowed, names=names) == ["Helen"]
    assert added_details("Leave it to the Dr. here.", allowed=allowed, names=names) == []


def test_emails_and_links_must_come_from_the_instruction() -> None:
    assert added("Point them to ops@northlake.example.", "Send them to ops.") == [
        "ops@northlake.example"
    ]
    assert added("See https://northlake.example/prices.", "Share the price page.") == [
        "https://northlake.example/prices"
    ]
    link = "Link https://northlake.example/prices, nothing else."
    assert added("Use https://northlake.example/prices.", link) == []


def test_spelled_out_numbers_get_through() -> None:
    """A cheap check, not a judge (EDIT.md §6): the writer's prompt does the rest."""
    assert added("Quote three runs.", "Quote some runs.") == []
