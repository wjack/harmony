#!/usr/bin/env bash
# Look for a key in what is staged for commit (deploy runbook step 2) on an added line
# of `git diff --cached`: an Anthropic key (`sk-ant-<kind><2 digits>-` and 40 or more key
# characters), a Render API key (`rnd_` and 20 or more), or a signed JWT, the shape of
# most bearer tokens. .env.local and terraform.tfvars hold all three kinds of provider
# credential. The repo's fixtures and prefix checks (`sk-ant-test-only`,
# `sk-ant-fixture-...`, a bare `sk-ant-`, `rnd_fixture-...`) aren't key-shaped, so they
# never match.
#
#   scripts/secret_scan.sh                  the staged diff (before a commit)
#   scripts/secret_scan.sh origin/main..    every commit a push would send (before a
#                                           push), including a key a later commit removed,
#                                           a merge's own changes and commit messages
#
# Python reads the diff in process and prints only file names and counts, never a match.
# Exits 0 when nothing matches, 1 on any match, 2 on bad usage or when git can't produce
# the diff.
set -euo pipefail
set +x

if (($# > 1)) || [[ ${1:-} == -* ]]; then
  echo "usage: $0 [<revision range>]" >&2
  exit 2
fi

python="$(command -v python3)"

exec "$python" -I - "$@" <<'PY'
"""Count key-shaped strings on added lines, per file: the staged diff, or a range's
commits."""

from __future__ import annotations  # macOS's /usr/bin/python3 is 3.9

import re
import subprocess
import sys
from collections import Counter

KEY = re.compile(
    r"sk-ant-[a-z]+[0-9]{2}-[A-Za-z0-9_-]{40,}"  # Anthropic
    r"|rnd_[A-Za-z0-9]{20,}"  # Render
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}"  # a signed JWT
)


def read_diff(revisions: str | None) -> str:
    if revisions is None:
        return git(["diff", "--cached", "--no-color", "--no-ext-diff", "--unified=0"])
    # Every commit's own patch, so a key added and later removed still counts: the push
    # sends both commits. A merge's patch is against its first parent, so a key typed
    # while resolving a conflict counts too.
    return git(["log", "-p", "--diff-merges=first-parent", "--format=", "--no-color",
                "--no-ext-diff", "--unified=0", revisions, "--"])


def git(arguments: list[str]) -> str:
    command = ["git", *arguments]
    try:
        result = subprocess.run(command, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"secret_scan.sh: {' '.join(command[:3])} failed: {exc}", file=sys.stderr)
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
    revisions = sys.argv[1] if len(sys.argv) > 1 else None
    found = hits(read_diff(revisions))
    if revisions is not None:
        messages = git(["log", "--format=%B", "--no-color", revisions, "--"])
        count = sum(1 for line in messages.splitlines() if KEY.search(line))
        if count:
            found["(commit messages)"] = count
    for path, count in sorted(found.items()):
        print(f"secret_scan.sh: {path}: {count} added line(s) hold a key-shaped string")
    if found:
        sys.exit(1)
    where = "the staged diff" if revisions is None else f"the commits in {revisions}"
    print(f"secret_scan.sh: no key-shaped string in {where}")


main()
PY
