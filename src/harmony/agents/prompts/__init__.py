"""Prompt templates, one per role, versioned in git (§5).

Templates are plain text with `{placeholders}`, drafted by the coordinator:

| Template            | Role / use                                                 |
|---------------------|------------------------------------------------------------|
| `internal.md`       | internal Claude's system prompt                            |
| `representative.md` | every representative's frozen system prompt                |
| `side_requester.md` | the requester side's section of `representative.md`        |
| `side_responder.md` | the responder side's section of `representative.md`        |
| `consult.md`        | the consult answerer and router (a short read-tool loop)   |
| `consult_redraft.md`| what a redraft adds to `consult.md`: the draft it revises  |
| `consult_reuse.md`  | what a draft from a member's earlier answer adds instead   |
| `same_question.md`  | the matcher: does a consult ask an earlier one again       |
| `sharing_check.md`  | the sharing check on a draft consult answer                |
| `screener.md`       | one discovery screener call                                |
| `steer_guidance.md` | a steer's guidance call: the guidance from a member's Edit |
| `held_rewrite.md`   | a steer's rewrite of a held message (no tools)             |

Syntax: `{name}` (lowercase letters, digits and underscores) is a placeholder; `{{` and
`}}` are literal braces. Any other brace is a malformed template.

`render` fills every placeholder in one pass, so a value that contains braces is
inserted as is and never expanded. It fails on a missing or unused value, so a renamed
placeholder breaks a test instead of a prompt. Rendered bytes feed frozen prompts, so
rendering is deterministic: no dates, no dict ordering surprises, and trailing
whitespace (the file's final newline) is dropped so sections nest cleanly.

Owner: R-agents.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Final, Literal

PROMPTS_DIR: Final = Path(__file__).resolve().parent

PromptName = Literal[
    "internal",
    "representative",
    "side_requester",
    "side_responder",
    "consult",
    "consult_redraft",
    "consult_reuse",
    "same_question",
    "sharing_check",
    "screener",
    "steer_guidance",
    "held_rewrite",
]

# One token per match: an escaped brace, a placeholder, or a stray brace (an error).
_TOKEN: Final = re.compile(r"\{\{|\}\}|\{([a-z_][a-z0-9_]*)\}|[{}]")


class TemplateError(ValueError):
    """A template with a brace that is neither a placeholder nor an escape."""


def template_path(name: PromptName) -> Path:
    return PROMPTS_DIR / f"{name}.md"


def template(name: PromptName) -> str:
    """The raw template text."""
    return template_path(name).read_text(encoding="utf-8")


def placeholders(name: PromptName) -> frozenset[str]:
    """The placeholder names a template uses."""
    return _names(template(name), name=name)


def render(name: PromptName, values: Mapping[str, str]) -> str:
    """The template with every placeholder replaced. Raises `KeyError` for a missing
    value and `ValueError` for a value the template doesn't use."""
    return render_text(template(name), values, name=name)


def render_text(text: str, values: Mapping[str, str], *, name: str = "template") -> str:
    """`render` for a template given as text (tests and composed sections)."""
    used = _names(text, name=name)
    missing = sorted(used - values.keys())
    if missing:
        raise KeyError(f"{name}: no value for {', '.join(missing)}")
    unused = sorted(values.keys() - used)
    if unused:
        raise ValueError(f"{name}: unused values {', '.join(unused)}")

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return values[match.group(1)]

    return _TOKEN.sub(replace, text).rstrip()


@cache
def _names(text: str, *, name: str = "template") -> frozenset[str]:
    names: set[str] = set()
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if token in ("{{", "}}"):
            continue
        if match.group(1) is None:
            line = text.count("\n", 0, match.start()) + 1
            raise TemplateError(f"{name}: stray {token!r} on line {line}")
        names.add(match.group(1))
    return frozenset(names)


__all__ = [
    "PROMPTS_DIR",
    "PromptName",
    "TemplateError",
    "placeholders",
    "render",
    "render_text",
    "template",
    "template_path",
]
