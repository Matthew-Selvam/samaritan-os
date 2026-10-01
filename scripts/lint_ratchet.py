#!/usr/bin/env python3
"""
lint_ratchet.py — fail on *new* lint findings, not on inherited ones.
==========================================================================
Why this exists
---------------
`ruff check` over backend/ currently reports a few hundred findings, almost all
of them in files owned by other workstreams (backend/agents/**,
backend/connectors/**, backend/api/**, backend/*.py). A plain "CI must be green"
rule would mean either (a) blocking every PR until all of that is cleaned up,
or (b) pretending the lint job does not exist. Both are bad.

So CI uses a ratchet instead:

  1. `make lint-ratchet` runs ruff and compares the current finding count
     against infra/lint-baseline.json.
  2. Exceeding the baseline  → FAIL. A PR must not make lint debt worse.
  3. Coming in under it     → PASS, and the count is printed so the baseline
     can be lowered in the same PR (see "How to lower the baseline").

A finding's identity is (rule_code, relative_path, normalised_code_line). The
line number is stripped because an unrelated edit one line above an existing
finding would otherwise look like a brand-new violation and block a PR that
actually reduced debt.

Usage
-----
  python3 scripts/lint_ratchet.py                 # check against the baseline
  python3 scripts/lint_ratchet.py --write         # rewrite the baseline
  python3 scripts/lint_ratchet.py --print-current # dump the current findings

Exit codes: 0 = within budget, 1 = over budget, 2 = tooling error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
BASELINE = REPO_ROOT / "infra" / "lint-baseline.json"
RUFF_CONFIG = REPO_ROOT / "infra" / "ruff.toml"

# Line numbers are stripped, so all remaining whitespace runs collapse to one
# space. That makes the key stable against reindentation and reflowing.
_WS = re.compile(r"\s+")


def _rel(path: str) -> str:
    """Normalise a ruff-reported path to a repo-relative POSIX path.

    ruff reports paths relative to the directory it was invoked from, so this is
    already relative to REPO_ROOT. An absolute path is made relative defensively
    so the script still works if someone runs ruff from elsewhere.

    Args:
        path: Path as printed by ruff.

    Returns:
        Repo-relative path string using forward slashes.
    """
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.relative_to(REPO_ROOT)
        except ValueError:
            pass
    return p.as_posix()


def current_findings() -> list[tuple[str, str, str]]:
    """Run ruff and return normalised findings as (rule, path, line) tuples.

    Returns:
        Sorted list of finding identities. An empty list means a clean tree.

    Raises:
        SystemExit: if ruff is missing or exits with a status other than
            0 (clean) or 1 (findings present).
    """
    try:
        proc = subprocess.run(
            [
                "ruff", "check",
                "--config", str(RUFF_CONFIG),
                "--output-format", "concise",
                "--no-cache",
                str(BACKEND),
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit(
            "ruff is not installed. Run `make install-backend`, or "
            "`pip install ruff==0.11.2`."
        ) from None

    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"ruff failed with status {proc.returncode}")

    findings: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        # concise format: path:line:col: CODE message
        match = re.match(r"^(?P<path>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+(?P<code>[A-Z]+\d+)\s", line)
        if not match:
            continue
        # The trailing part is "<code> <message>"; normalise whitespace so the
        # identity survives reindentation and reflowing of the source line.
        detail = match.group(0).split(":", 3)[-1]
        findings.append(
            (
                match.group("code"),
                _rel(match.group("path")),
                _WS.sub(" ", detail).strip(),
            )
        )
    return sorted(findings)


def load_baseline() -> dict:
    """Read the baseline file, tolerating absence."""
    if not BASELINE.exists():
        return {"version": 1, "total": 0, "by_rule": {}, "note": "no baseline recorded yet"}
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{BASELINE} is not valid JSON: {exc}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true",
                        help="rewrite the baseline to the current findings")
    parser.add_argument("--print-current", action="store_true",
                        help="print the current findings and exit")
    args = parser.parse_args(argv)

    findings = current_findings()

    if args.print_current:
        for rule, path, line in findings:
            print(f"{rule:8s} {path}  {line}")
        print(f"\n{len(findings)} finding(s)")
        return 0

    counts = Counter(rule for rule, _, _ in findings)
    total = len(findings)

    if args.write:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(
                {
                    "version": 1,
                    "total": total,
                    "by_rule": dict(sorted(counts.items())),
                    "note": (
                        "Auto-generated by scripts/lint_ratchet.py --write. "
                        "CI fails when the current finding count exceeds `total`. "
                        "Lower this number in the same PR that reduces lint debt."
                    ),
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        print(f"baseline written: {total} finding(s) -> {BASELINE.relative_to(REPO_ROOT)}")
        return 0

    baseline = load_baseline()
    allowed = int(baseline.get("total", 0))

    if total <= allowed:
        remaining = allowed - total
        print(f"lint ratchet OK: {total} finding(s), budget {allowed}"
              + (f" ({remaining} slots of headroom)" if remaining else " (at budget — lower the baseline)"))
        if total < allowed:
            print("  -> run `python3 scripts/lint_ratchet.py --write` to record the lower count")
        return 0

    over = total - allowed
    print(f"lint ratchet FAILED: {total} finding(s) exceeds the budget of {allowed} (+{over}).", file=sys.stderr)
    print("", file=sys.stderr)
    new_counts = {rule: n for rule, n in counts.items()
                  if n > int(baseline.get("by_rule", {}).get(rule, 0))}
    if new_counts:
        print("Rules that grew past their recorded budget:", file=sys.stderr)
        for rule, n in sorted(new_counts.items(), key=lambda kv: -kv[1]):
            was = int(baseline.get("by_rule", {}).get(rule, 0))
            print(f"  {rule:8s} {was} -> {n}", file=sys.stderr)
    else:
        print("No single rule grew — the increase is spread across existing rules.", file=sys.stderr)
    print("", file=sys.stderr)
    print("Fix the new findings, or raise the baseline with --write if they are", file=sys.stderr)
    print("inherited from code you did not touch.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
