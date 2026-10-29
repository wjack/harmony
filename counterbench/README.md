# Counterbench

Counterbench is the adversarial benchmark behind the trust design of Harmony. It tests if a Claude representative for a business can deal with a hostile outside party. That party tries to get confidential facts or unauthorized actions out of the representative. The representative must still serve legitimate requests.

## How it works

Counterbench has four fictional organizations: a metal fabricator, a print shop, a contract lab and a university characterization facility. Each one has a public profile, a price list, policies, confidential facts and action tools, for example quotes, bookings and file sending. The held-out set has 16 scenarios, and each scenario has one attack goal. Each scenario runs at two trust tiers, so the held-out run has 32 instances.

Each model plays one role only:

| Role | Model |
|---|---|
| Defender (the representative under test) | Haiku 4.5 and Haiku 5.5 |
| Attacker | Sonnet 5.5 |
| Leak judge | Opus 5.5 |

The attacker gets up to three attempts on each instance, each against a fresh defender. It never sees the answer and has no red-team framing. It tries three formats: a multi-turn conversation, a single message, and an attached document with injected instructions. A paired legitimate request on each instance measures if the defender stays helpful. Code grades unauthorized actions, and the judge grades leaks.

## Result

The main comparison is Haiku 4.5 against Haiku 5.5 in the multi-turn format, on 31 matched instances:

| Leak definition | Haiku 4.5 | Haiku 5.5 | p |
|---|---|---|---|
| Judge v2, pre-registered | 29% | 6% | 0.031 |
| Judge v3, written after the run | 19% | 0% | 0.0625 |

Haiku 5.5 completed slightly fewer legitimate requests (87% against 97%, not significant). It also escalated more to the owner.

The result has these limits:

- The sample is small: 16 scenarios, and the result rests on 6 or 7 instances where the two models differ.
- Claude models play every role.
- One automated attacker with three attempts gives a lower bound on what an attacker can do.
- No person compared the judges with human labels.

## Files

| Path | What it holds |
|---|---|
| [NOTES.md](NOTES.md) | The design, grading, findings, limitations and deviations. Start here. |
| [RESULTS.md](RESULTS.md) | The full analysis of the held-out run under both judges |
| [PREREGISTRATION.md](PREREGISTRATION.md) | The plan as frozen before the held-out run |
| [scenarios/](scenarios/) | The four organizations and their scenarios |
| [prompts/](prompts/) | The attacker prompt and both judge rubrics, with planted test cases for the judges |
| [cb/](cb/) | The defender, attacker, judge, grading and spend code |
| `run.py`, `rescore.py` | Run the benchmark, or grade a run again with another rubric (paid calls) |
| `report.py`, `export_tables.py`, `export_examples.py` | Analysis and exports from stored runs (no API calls) |
| `runs/` | Full results of each run: transcripts, tool calls, verdicts and token usage |
| [exports/](exports/), [examples/](examples/) | Tables for each scenario, and four readable transcripts |

## Reproduce

Run these commands from the root of the repository. They read the stored runs and make no API calls.

```sh
uv run python counterbench/report.py heldout
uv run python counterbench/report.py heldout-v3
uv run python counterbench/export_tables.py
uv run python counterbench/export_tables.py heldout-v3 exports/v3
```

A new run makes paid API calls. A full held-out run with judge v2 costs about $29. The scripts read `ANTHROPIC_API_KEY` from `.env.local` at the root of the repository. They write each call to the spend ledger that Harmony uses: `SPEND_LEDGER_PATH`, or else `spend_ledger.jsonl` in the folder that holds the repository. `--cap` stops new instances when the total in that ledger reaches that amount in US dollars.

```sh
uv run python counterbench/run.py --run heldout-rerun --split heldout --cap 250
```

To grade with judge v3, add `--judge-prompt prompts/judge_v3.txt --judge-votes 3`.

## Earlier work

An earlier version of this benchmark tested a trust architecture. Harmony uses three of its controls:

- The network attaches the identity of each outside party.
- The representative sees only a limited context.
- A consult stops the turn of the representative until its own side answers.

That version and RepBench, the first benchmark, are not in this repository. Section 7 of [NOTES.md](NOTES.md) describes them.
