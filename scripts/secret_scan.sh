#!/usr/bin/env bash
# Look for a key in what is staged for commit (deploy runbook step 2): an Anthropic key,
# `sk-ant-<kind><2 digits>-` and 40 or more key characters, on an added line of
# `git diff --cached`. The repo's fixtures and prefix checks (`sk-ant-test-only`,
# `sk-ant-fixture-...`, a bare `sk-ant-`) aren't key-shaped, so they never match.
#
#   scripts/secret_scan.sh
#
# Python reads the diff in process and prints only file names and counts, never a match.
# Exits 0 when nothing matches, 1 on any match, 2 when git can't produce the diff.
set -euo pipefail
set +x

python="$(command -v python3)"

exec "$python" -I - <<'PY'
"""Count key-shaped strings on the staged diff's added lines, per file."""

import re
import subprocess
import sys
from collections import Counter

KEY = re.compile(r"sk-ant-[a-z]+[0-9]{2}-[A-Za-z0-9_-]{40,}")


def staged_diff() -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--no-color", "--no-ext-diff", "--unified=0"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"secret_scan.sh: git diff --cached failed: {exc}", file=sys.stderr)
        sys.exit(2)
    return result.stdout.decode("utf-8", "surrogateescape")


def hits(diff: str) -> Counter[str]:
    found: Counter[str] = Counter()
    path, in_header = "?", False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header = True
        elif line.startswith("@@"):
            in_header = False
        elif in_header and line.startswith("+++ "):
            # Inside a hunk, an added line that starts with "++ " looks the same.
            path = line[4:].removeprefix("b/")
        elif not in_header and line.startswith("+") and KEY.search(line):
            found[path] += 1
    return found


def main() -> None:
    found = hits(staged_diff())
    for path, count in sorted(found.items()):
        print(f"secret_scan.sh: {path}: {count} added line(s) hold a key-shaped string")
    if found:
        sys.exit(1)
    print("secret_scan.sh: no key-shaped string in the staged diff")


main()
PY
