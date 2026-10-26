"""`harmony run`: play a scenario's story headless, through the same API the UI uses.

- `api`: a typed client for the routes a member's clicks reach.
- `decide`: what happens where a person would act (the story's rules, or a prompt).
- `play`: the loop that drives a world through the story.
- `beats`: what a step expects, as atoms, and the run's result.
- `worker`: the in-process Temporal worker, and the check that no other one polls.
- `report`: transcripts, beats, per-turn costs and spend, as text.

The command itself is `harmony.cli_run`.
"""
