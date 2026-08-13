#!/usr/bin/env python
"""Small, auditable secret scanner for this public repository.

Runs entirely locally (no external service, no paid tool). It scans every
file tracked by git plus every untracked-but-not-ignored file (``git
ls-files --cached --others --exclude-standard``), so it catches secrets in
new files before they are ever staged or committed, for:

- AWS access key IDs and secret access key assignments.
- Private key / X.509 certificate PEM headers.
- Files with an extension that must never be committed (certificates, keys,
  keystores), even if their content wasn't otherwise flagged.

Usage: ``python scripts/check_secrets.py``. Exit code 0 = no findings,
1 = findings (or `git ls-files` unavailable), 2 = usage error.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_EXTENSIONS = {
    ".pem",
    ".key",
    ".crt",
    ".cer",
    ".p12",
    ".pfx",
    ".jks",
    ".der",
}

# High-confidence secret patterns. Kept deliberately narrow to avoid noisy
# false positives (e.g. this file's own pattern strings).
CONTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS session/temporary access key ID", re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    (
        "AWS secret access key assignment",
        re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?"),
    ),
    ("PEM private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("PEM certificate block", re.compile(r"-----BEGIN CERTIFICATE-----")),
    ("PEM CSR block", re.compile(r"-----BEGIN CERTIFICATE REQUEST-----")),
]

# This file legitimately contains the pattern *definitions* above as text,
# so it must not be scanned against itself.
SELF_PATH = Path(__file__).resolve()


def scannable_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def scan() -> list[str]:
    findings: list[str] = []

    for path in scannable_files():
        if path.resolve() == SELF_PATH:
            continue
        if not path.is_file():
            continue

        if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
            findings.append(f"{path.relative_to(REPO_ROOT)}: forbidden file extension present")
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for label, pattern in CONTENT_PATTERNS:
            if pattern.search(text):
                findings.append(f"{path.relative_to(REPO_ROOT)}: possible {label}")

    return findings


def main() -> int:
    try:
        findings = scan()
    except subprocess.CalledProcessError:
        print("check_secrets: unable to run 'git ls-files' (not a git repo?)", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("check_secrets: 'git' executable not found", file=sys.stderr)
        return 1

    if findings:
        print("Potential secrets found:")
        for finding in findings:
            print(f"  - {finding}")
        return 1

    print("check_secrets: no potential secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
