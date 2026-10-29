"""Render attacker v2 prompts exactly as the attacker sees them: .venv/bin/python counterbench/prompts/render_examples.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from cb.attacker import sections, system_prompt  # noqa: E402
from cb.scenarios import instances  # noqa: E402

CASES = [("ridgeline-3-claimed", "multi"), ("ridgeline-1-verified", "multi"), ("calder-1-claimed", "single"),
         ("northlake-2-verified", "inject")]
t = sections()
out = ["# Attacker prompt: rendered examples\n",
       "Generated from [attacker.txt](attacker.txt). Edit that file, then rerun "
       "`.venv/bin/python counterbench/prompts/render_examples.py` to refresh this page.\n"]
for iid, arm in CASES:
    inst = instances(None, [iid])[0]
    out.append(f"\n## {iid}, {arm} arm, first attempt\n\n**System prompt:**\n\n```text\n{system_prompt(inst, arm, 6, [])}\n```\n")
    out.append(f"**First user message:** `{t['opening_inject' if arm == 'inject' else 'opening_multi']}`\n")
inst = instances(None, ["ridgeline-3-claimed"])[0]
hist = [{"transcript": "OUTSIDE PARTY: (attempt 1 message)\n\nREPRESENTATIVE: (reply)", "notes": "(the attacker's own notes)"}]
out.append("\n## Third attempt: how earlier attempts are appended (ridgeline-3, claimed tier, multi-turn)\n\n```text\n"
           + system_prompt(inst, "multi", 6, hist)[len(system_prompt(inst, "multi", 6, []).rstrip()):].strip()
           + "\n```\n")
out.append("\n## Other messages the attacker receives\n")
for k in ("reply", "reflect"):
    out.append(f"\n**{k}:**\n\n```text\n{t[k]}\n```\n")
(HERE / "attacker_examples.md").write_text("\n".join(out))
print("wrote", HERE / "attacker_examples.md")
