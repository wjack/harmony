"""Append harmony's local defaults to `.env.local`, adding only keys that are absent.

Existing lines are never rewritten or reordered. SESSION_SECRET and ADMIN_SECRET are
generated fresh when missing. Values are never printed, only key names, and the file
is kept at mode 600.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env.local"
LOCAL_DB = "postgresql://harmony:harmony@localhost:5432/harmony"

_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _defaults() -> dict[str, str]:
    return {
        "DATABASE_URL": LOCAL_DB,
        "DATABASE_DIRECT_URL": LOCAL_DB,
        "TEMPORAL_ADDRESS": "localhost:7233",
        "TEMPORAL_NAMESPACE": "default",
        "MODEL_MODE": "scripted",
        "MODEL_OVERRIDE": "claude-haiku-5-5",
        "APP_ENV": "local",
        "SPEND_LEDGER_PATH": str(REPO_ROOT.parent / "spend_ledger.jsonl"),
        "OBJECT_STORE": "localfs",
        "SCENARIO_ANCHOR": "2026-10-26",
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "ADMIN_SECRET": secrets.token_urlsafe(32),
    }


def _existing_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _KEY.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def _ends_with_newline(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return True
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def main() -> None:
    present = _existing_keys(ENV_FILE)
    missing = {k: v for k, v in _defaults().items() if k not in present}
    if missing:
        lines = [] if _ends_with_newline(ENV_FILE) else [""]
        lines.append("# Local defaults (scripts/ensure_env_local.py)")
        lines += [f"{key}={value}" for key, value in missing.items()]
        fd = os.open(ENV_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)
    print("added: " + ", ".join(missing) if missing else "nothing to add")


if __name__ == "__main__":
    main()
