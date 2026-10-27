"""People's names as the demo uses them, mirroring `web/src/shell/names.ts`: a title
("Dr.", "Prof.") is never anyone's name, so it is dropped before a name is split."""

from __future__ import annotations

from typing import Final

HONORIFICS: Final = frozenset(
    {"dr", "dr.", "prof", "prof.", "mr", "mr.", "ms", "ms.", "mrs", "mrs."}
)


def name_parts(name: str | None) -> list[str]:
    """'Dr. Helen Ward' -> ['Helen', 'Ward']."""
    return [part for part in (name or "").split() if part.lower() not in HONORIFICS]


def first_name(name: str | None) -> str | None:
    """'Dr. Helen Ward' -> 'Helen', as the web's `firstName` addresses people."""
    parts = name_parts(name)
    return parts[0] if parts else None


__all__ = ["HONORIFICS", "first_name", "name_parts"]
