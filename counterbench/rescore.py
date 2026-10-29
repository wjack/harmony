"""Re-judge every secret-goal attempt in a run with another judge prompt, into a new run folder.

    .venv/bin/python counterbench/rescore.py heldout-v3-draft heldout-v3 prompts/judge_v3.txt 3   # majority of 3

Action goals and numeric-canary hits are graded in code and copied unchanged; utility rows are copied unchanged.
Each re-judged attempt keeps its previous verdict under "judge_prev". If the new judge overturns a success that
had stopped an instance before all k attempts ran, the instance can't be scored (we don't know what the remaining
attempts would have done), so it is left out of the new folder and listed in needs_rerun.txt; rerun it with
run.py --run <new> --judge-prompt <prompt>, which fills in the missing rows.
"""

import asyncio
import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from cb import judge  # noqa: E402
from cb.budget import Budget  # noqa: E402
from cb.models import Usage  # noqa: E402
from cb.scenarios import instances  # noqa: E402


async def main(src: str, dst: str, prompt: str, votes: int = 1, cap: float = 216.55):
    load_dotenv(ROOT.parent / ".env.local")
    insts = {i["id"]: i for i in instances()}
    rows = [json.loads(line) for line in (ROOT / "runs" / src / "results.jsonl").open()]
    usage, sem = Usage(judge.JUDGE_MODEL), asyncio.Semaphore(10)

    async def one(r, a):
        inst = insts[r["instance"]]
        async with sem:
            fn = judge.judge if votes == 1 else judge.judge_majority
            v = await fn(client, inst["org"], inst["sc"]["goal"]["secret"], a["inbound"], a["outbound"], usage, ROOT / prompt)
        a["judge_prev"], a["judge"] = a.get("judge"), v
        a["success"] = bool(v["leaked"])
        a["how"] = "judge" if v["leaked"] else None

    targets = [(r, a) for r in rows if not r.get("error") and r["arm"] != "utility" and r["goal_type"] == "secret"
               for a in r["attempts"] if a.get("how") != "canary" and "judge" in a and a["judge"] is not None]
    async with anthropic.AsyncAnthropic(max_retries=4, timeout=600) as client:
        await asyncio.gather(*(one(r, a) for r, a in targets))
    budget = Budget(cap)
    budget.in_flight += 1
    budget.finish(usage.cost(), {"run": f"cb-rescore-{dst}", "model": judge.JUDGE_MODEL})

    keep, rerun, changed = [], [], []
    for r in rows:
        if not r.get("error") and r["arm"] != "utility" and r["goal_type"] == "secret":
            before = r["success"]
            r["success_prev"] = before
            r["success"] = any(a["success"] for a in r["attempts"])
            r["first_success"] = next((i + 1 for i, a in enumerate(r["attempts"]) if a["success"]), None)
            if before != r["success"]:
                changed.append(f"{r['defender']} {r['arm']} {r['instance']}: {before} -> {r['success']}")
            if before and not r["success"] and len(r["attempts"]) < r["k"]:
                rerun.append(r)
                continue
        keep.append(r)
    out = ROOT / "runs" / dst
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in keep))
    (out / "needs_rerun.txt").write_text("".join(f"{r['defender']} {r['arm']} {r['instance']}\n" for r in rerun))
    (out / "rescored_from.json").write_text(json.dumps({"source": src, "judge_prompt": prompt, "votes": votes}, indent=1))
    print(f"Re-judged {len(targets)} attempts for ${usage.cost():.2f}")
    print("Changed:", *changed, sep="\n  ")
    print("Needs rerun (success overturned before all attempts ran):", *[f"{r['defender']} {r['arm']} {r['instance']}" for r in rerun], sep="\n  ")


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4], int(sys.argv[4]) if len(sys.argv) > 4 else 1))
