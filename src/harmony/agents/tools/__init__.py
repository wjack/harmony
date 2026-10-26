"""Tool schemas, input models and executors, one module per role (§5).

Tool names are exactly ARCHITECTURE.md §5's. Each tool's JSON definition is a constant,
written by hand because its description is prompt text; a representative's list is
frozen into `rep_sessions.tools_text` at the side's first turn, so editing a definition
only affects sides that start afterwards. Each tool also has a Pydantic input model the
executor validates against (the two must agree; E2 tests that).

Owner: E2.
"""

from __future__ import annotations

from harmony.llm.wire import JsonObject

type ToolDefinition = JsonObject

__all__ = ["ToolDefinition"]
