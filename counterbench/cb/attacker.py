"""The attacker: a persistent counterparty who wants something from the representative. It keeps its earlier
attempts on the same instance and reflects between them.

The prompt text lives in prompts/attacker.txt so it can be read and edited without code. By design it has no
red-team or evaluation framing, and it tells the attacker what it wants, never the answer itself.
"""

from .models import ROOT, check_served_model, settings
from .scenarios import attack_identity_line

TEMPLATE = ROOT / "prompts" / "attacker.txt"


def carrier_message(inst: dict) -> str:
    inj = inst["sc"]["injection"]
    return (f"Hi, this is {attack_identity_line(inst)}. {inj['request']} "
            f"Details are in the attached file.")


def sections() -> dict:
    out, name, buf = {}, None, []
    for line in TEMPLATE.read_text().splitlines():
        if line.startswith("#"):
            continue
        if line.startswith("=== ") and line.endswith(" ==="):
            if name:
                out[name] = "\n".join(buf).strip("\n")
            name, buf = line[4:-4].strip(), []
        else:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf).strip("\n")
    return out


def system_prompt(inst: dict, arm: str, turns: int, history: list[dict]) -> str:
    t, org, sc = sections(), inst["org"], inst["sc"]
    goal = sc["goal"]
    if inst["tier"] == "verified":
        v = sc["verified_identity"]
        identity = t["identity_verified"].format(name=v["name"], role=v["role"], verified_org=v["org"], email=v["email"],
                                                 account=v["account"])
    else:
        identity = t["identity_claimed"].format(claimed_identity=sc["claimed_identity"])
    channel = t[f"channel_{arm}"].format(turns=turns, carrier=carrier_message(inst),
                                          doc_name=sc["injection"]["doc_name"])
    past = ""
    if history:
        attempts = "\n\n".join(t["history_attempt"].format(n=i, transcript=h["transcript"], notes=h["notes"])
                               for i, h in enumerate(history, 1))
        past = "\n" + t["history"].format(attempts=attempts)
    return t["system"].format(
        identity=identity, org=org["name"], principal=org["principal"], principal_role=org["principal_role"],
        public_profile=org["public_profile"], goal=goal["ask"],
        done_when=t[f"done_when_{goal['type']}"], channel=channel, history=past)


class Attacker:
    def __init__(self, client, model: str, inst: dict, arm: str, turns: int, usage, history: list[dict]):
        self.client, self.model, self.usage = client, model, usage
        self.inst, self.arm, self.turns = inst, arm, turns
        self.t = sections()
        self.system = system_prompt(inst, arm, turns, history)
        self.messages = []
        self.refused, self.refusal_category = False, None

    async def _call(self, messages: list[dict]) -> str:
        response = await self.client.messages.create(
            model=self.model, max_tokens=16000, system=self.system, messages=messages,
            cache_control={"type": "ephemeral"}, **settings(self.model),
        )
        check_served_model(self.model, response)
        self.usage.add(response.usage)
        if response.stop_reason == "refusal":
            self.refused = True
            self.refusal_category = getattr(response.stop_details, "category", None) or "unknown"
            return ""
        messages.append({"role": "assistant", "content": response.content})
        return "\n".join(b.text for b in response.content if b.type == "text").strip()

    async def opening(self) -> str:
        self.messages.append({"role": "user", "content": self.t["opening_inject" if self.arm == "inject" else "opening_multi"]})
        return await self._call(self.messages)

    async def reply(self, rep_text: str, turn: int) -> str:
        last = self.t["reply_last"] if turn == self.turns else ""
        self.messages.append({"role": "user", "content": self.t["reply"].format(
            reply=rep_text or "(no reply)", turn=turn, turns=self.turns, last=last)})
        return await self._call(self.messages)

    async def reflect(self, transcript: str) -> str:
        return await self._call([{"role": "user", "content": self.t["reflect"].format(transcript=transcript)}])
