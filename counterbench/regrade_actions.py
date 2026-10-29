"""Deterministically re-evaluate action goals from stored tool calls (no API calls).

    .venv/bin/python counterbench/regrade_actions.py heldout

Keeps the previous verdict in success_prev and writes results.jsonl back in place (the original is saved as
results.before-action-regrade.jsonl).
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cb.grade import predicate_met  # noqa: E402
from cb.models import ROOT  # noqa: E402
from cb.scenarios import instances  # noqa: E402

run = sys.argv[1]
path = ROOT / "runs" / run / "results.jsonl"
backup = path.with_name("results.before-action-regrade.jsonl")
if not backup.exists():
    shutil.copy(path, backup)
goals = {i["scenario"]: i["sc"]["goal"] for i in instances()}
rows = [json.loads(line) for line in backup.open()]
for r in rows:
    if r["arm"] == "utility" or r.get("error") or r["goal_type"] != "action":
        continue
    for a in r["attempts"]:
        a["success_prev"] = a["success"]
        a["success"] = predicate_met(goals[r["scenario"]]["success"], a["actions"])
        a["how"] = "action" if a["success"] else None
    r["success_prev"] = r["success"]
    r["success"] = any(a["success"] for a in r["attempts"])
    r["first_success"] = next((i + 1 for i, a in enumerate(r["attempts"]) if a["success"]), None)
    if r["success"] != r["success_prev"]:
        print(f"  {r['defender']} {r['arm']} {r['instance']}: {r['success_prev']} -> {r['success']}")
path.write_text("".join(json.dumps(r) + "\n" for r in rows))
