"""Fail if something private is about to land in this public repo (see "This repo is public" in CLAUDE.md).

Checks every tracked text file, and the commit messages and author emails in a range, for:
- home-folder paths with a real user name (/Users/<name>/, /home/<name>/, C:\\Users\\<name>\\);
- email addresses, other than GitHub/Anthropic noreply ones and reserved example domains;
- private keys and common token formats;
- private terms (design and project names) from the CADMATE_PRIVATE_TERMS environment variable, one per line.

Private terms are never written in the repo: in CI they come from a repository secret, and a hit is reported by
file and line only, never by the matching text. Design names without that secret are left to review.

    python .github/check_public.py [<base>..<head>]
"""

import os
import re
import subprocess
import sys

# Placeholder names used in docs and tests, and CI runners' own home folders.
ALLOWED_USERS = {"me", "you", "user", "username", "name", "runner", "<name>", "...", "…", "*"}
ALLOWED_EMAIL = re.compile(
    r"(@users\.noreply\.github\.com|^noreply@(github|anthropic)\.com|^git@github\.com|@example\.(com|org|net)|@localhost)$", re.I)
SKIP_FILES = {"uv.lock", ".github/check_public.py"}

HOME = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\\\?Users\\\\?)([^/\\\s\"'`)|]+)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"
    r"|\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abpr]-[A-Za-z0-9-]{10,}|\bpypi-[A-Za-z0-9_-]{40,}")


def _norm(text: str) -> str:
    """Lower case, with -, _, . and runs of spaces as one space, so port-centre also finds port_centre."""
    return re.sub(r"[-_.\s]+", " ", text.lower())


def private_terms() -> list[str]:
    return [_norm(t).strip() for t in os.environ.get("CADMATE_PRIVATE_TERMS", "").splitlines() if t.strip()]


def problems(text: str, terms: list[str]) -> list[str]:
    found = []
    for m in HOME.finditer(text):
        if m.group(1).lower() not in ALLOWED_USERS and not m.group(1).startswith(("$", "{", "%", "<")):
            found.append(f"home folder path ({m.group(0)})")
    for m in EMAIL.finditer(text):
        if not ALLOWED_EMAIL.search(m.group(0)):
            found.append(f"email address ({m.group(0)})")
    if SECRET.search(text):
        found.append("what looks like a key or token")
    low = _norm(text)
    if any(t in low for t in terms):
        found.append("a private term (from CADMATE_PRIVATE_TERMS)")
    return found


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def main() -> int:
    terms = private_terms()
    bad = []
    for path in git("ls-files", "-z").split("\0"):
        if not path or path in SKIP_FILES:
            continue
        for p in problems(path, terms):
            # Don't print a path that holds a private term: the log is public.
            bad.append(f"a tracked file's name has {p}" if "private term" in p else f"{path}: file name has {p}")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except (IsADirectoryError, FileNotFoundError):
            continue  # submodules, deleted files
        if b"\0" in data:
            continue  # binary
        lines = data.decode("utf-8", errors="replace").splitlines()
        for n, line in enumerate(lines, 1):
            for p in problems(line, terms):
                bad.append(f"{path}:{n}: {p}")
    if len(sys.argv) > 1:
        for entry in git("log", "--format=%H%x00%ae%x00%ce%x00%B%x01", sys.argv[1]).split("\x01"):
            if not entry.strip():
                continue
            sha, author, committer, message = entry.strip().split("\0", 3)
            for who in {author, committer}:
                if not ALLOWED_EMAIL.search(who):
                    bad.append(f"commit {sha[:7]}: authored or committed with {who}; use a GitHub noreply email")
            for p in problems(message, terms):
                bad.append(f"commit {sha[:7]} message: {p}")
    for b in bad:
        print(f"::error::{b}" if os.environ.get("GITHUB_ACTIONS") else b)
    print(f"{len(bad)} problem(s); private terms checked: {len(terms) if terms else 'none set'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
