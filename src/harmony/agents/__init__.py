"""The agent layer (ARCHITECTURE.md §2, §5, §6, §10, as REVISION_UNSTRUCTURED.md revises
them). docs/P1_P2_CONTRACT.md lists what each module exposes and who owns it.

- `models`: the value types every layer shares: briefs, card payloads, resolutions,
  chat items.
- `keys`: ScriptedModel cassette key conventions per role.
- `runtime`: `run_turn`, the checkpointed tool loop, and its Protocols.
- `context`: representative and internal-Claude context assembly, and notices.
- `transcript`: raw wire JSON transcripts for rep sessions and conversations.
- `prompts`: the role templates (`*.md`) and their renderer.
- `tools.internal`, `tools.rep`: tool JSON schemas, input models and executors.
- `consult`: the consult answerer (a read-tool loop), the sharing check, and settling
  a consult.
- `release`: the release decision by mode (`decide`).
- `discovery`: `DiscoveryStrategy` and `AgentScreen`.

Nothing in this package calls the model directly: every call goes through
`llm.checkpoint.call_model_checkpointed` inside a Temporal activity (§3).
"""
