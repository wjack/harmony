"""Run the leak judge on its planted checks (paid calls).

    .venv/bin/python counterbench/prompts/check_judge.py        # v2 (prompts/judge.txt, judge_checks.json)
    .venv/bin/python counterbench/prompts/check_judge.py v3 3   # v3 (prompts/judge_v3.txt, judge_checks_v3.json), majority of 3
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import anthropic  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from cb.budget import Budget  # noqa: E402
from cb.judge import JUDGE_MODEL, judge, judge_majority  # noqa: E402
from cb.models import ROOT, Usage  # noqa: E402
from cb.scenarios import instances  # noqa: E402


async def main(version: str = "", votes: int = 1):
    load_dotenv(ROOT.parent / ".env.local")
    sc = {i["scenario"]: i for i in instances()}
    checks = json.loads((HERE / f"judge_checks{'_' + version if version else ''}.json").read_text())
    prompt = HERE / (f"judge_{version}.txt" if version else "judge.txt")
    usage = Usage(JUDGE_MODEL)
    async with anthropic.AsyncAnthropic(max_retries=3) as client:
        fn = judge if votes == 1 else judge_majority
        res = await asyncio.gather(*(fn(client, sc[c["scenario"]]["org"], sc[c["scenario"]]["sc"]["goal"]["secret"],
                                           [c["outside"]], [c["rep"]], usage, prompt) for c in checks))
    ok = 0
    for c, v in zip(checks, res):
        hit = v["label"] == c["expect"]
        ok += hit
        print(f"  {'ok  ' if hit else 'MISS'} {c['scenario']:12} expect {c['expect']:8} got {v['label']:8} {v.get('votes') or v.get('failures') or ''}")
    b = Budget(200.0)
    b.in_flight += 1
    b.finish(usage.cost(), {"run": "cb-judge-checks", "model": JUDGE_MODEL})
    print(f"{ok}/{len(checks)} exact; ${usage.cost():.3f}")


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "", int(sys.argv[2]) if len(sys.argv) > 2 else 1))
