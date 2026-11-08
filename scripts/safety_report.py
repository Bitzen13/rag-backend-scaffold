"""Write the safety evidence for one test run as Markdown: one row per safety decision.

Usage: pytest --junitxml=test-results.xml
       python scripts/safety_report.py --junit test-results.xml >> "$GITHUB_STEP_SUMMARY"

Each decision in context/safety-considerations.md has a pytest marker (pyproject.toml). This
script asks pytest which tests carry each marker (collection only, nothing is re-run), looks up
how those tests did in the JUnit file the real run wrote, and runs the attack set once more to
print its table. In CI the output lands on the run's Summary page, so every commit carries its
own evidence. It never fails the build: the test step already does that.
"""

import argparse
import asyncio
import contextlib
import io
import logging
import os
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DECISIONS = (
    ("safety_output", "1. Remove images and links from answers"),
    ("safety_prompt", "2. Make the prompt impossible to forge"),
    ("safety_limits", "3. Limit what a single file can cost"),
    ("safety_deletion", "4. Don't let one bad run wipe the corpus"),
    ("safety_attacks", "5. Prove the defences work (attack set)"),
    ("safety_untrusted", "6. Treat everything from documents as untrusted"),
)
DOC = "context/safety-considerations.md"


@dataclass
class Tally:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    missing: int = 0  # carries the marker but has no result: the run did not reach it
    files: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return self.passed > 0 and self.failed == 0 and self.missing == 0


class _MarkerCollector:
    """A pytest plugin that records each collected test's markers."""

    def __init__(self) -> None:
        self.markers: dict[str, set[str]] = {}

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            self.markers[item.nodeid] = {m.name for m in item.iter_markers()}


def collect_markers() -> dict[str, set[str]]:
    collector = _MarkerCollector()
    with contextlib.redirect_stdout(io.StringIO()):
        pytest.main(
            ["--collect-only", "-q", "-p", "no:cacheprovider", str(REPO_ROOT / "tests")],
            plugins=[collector],
        )
    # pytest leaves logging pointed at streams it has since closed. This report prints Markdown
    # and needs no log lines, so logging is switched off rather than re-plumbed.
    logging.disable(logging.CRITICAL)
    return collector.markers


def _junit_key(nodeid: str) -> tuple[str, str]:
    """`tests/test_x.py::Class::test_y[a]` -> (`tests.test_x.Class`, `test_y[a]`)."""
    path, *parts = nodeid.split("::")
    module = path.removesuffix(".py").replace("/", ".")
    return ".".join([module, *parts[:-1]]), parts[-1]


def read_outcomes(junit: Path) -> dict[tuple[str, str], str]:
    outcomes: dict[tuple[str, str], str] = {}
    for case in ElementTree.parse(junit).iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        outcomes[(case.get("classname", ""), case.get("name", ""))] = outcome
    return outcomes


def tally(markers: dict[str, set[str]], outcomes: dict[tuple[str, str], str]) -> dict[str, Tally]:
    tallies = {marker: Tally() for marker, _ in DECISIONS}
    for nodeid, names in markers.items():
        outcome = outcomes.get(_junit_key(nodeid), "missing")
        for marker in names & tallies.keys():
            current = tallies[marker]
            setattr(current, outcome, getattr(current, outcome) + 1)
            current.files.add(nodeid.split("::")[0])
    return tallies


def decision_table(tallies: dict[str, Tally]) -> list[str]:
    lines = [
        "| Decision | Result | Passed | Failed | Skipped | Proven by |",
        "|---|---|---|---|---|---|",
    ]
    for marker, title in DECISIONS:
        t = tallies[marker]
        result = "✅ proven" if t.ok else "❌ **not proven**"
        files = ", ".join(f"`{f}`" for f in sorted(t.files)) or "no tests"
        failed = t.failed + t.missing
        lines.append(f"| {title} | {result} | {t.passed} | {failed} | {t.skipped} | {files} |")
    return lines


def attack_tables() -> list[str]:
    from evaluation.attacks import GOALS, format_run
    from tests.test_adversarial import NoOutputChecks, run_attack_set

    async def both() -> tuple[str, list[str]]:
        after = await run_attack_set()
        control = await run_attack_set(NoOutputChecks())
        counts = [
            f"- {goal}: {len(control.succeeded(goal))} of {len(control.of_goal(goal))} succeeded"
            for goal in GOALS
            if control.of_goal(goal)
        ]
        return format_run(after), counts

    after, control = asyncio.run(both())
    return [
        "### Attack set, against a fake model that obeys every instruction",
        "",
        after,
        "",
        "Canary attacks are expected to succeed here: only a real model can refuse an "
        "instruction to say something. Every other goal must be at 0.",
        "",
        "**Control, the same run with the output checks turned off** (shows the attacks are real):",
        "",
        *control,
    ]


def report(junit: Path) -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    # A run's Summary page does not resolve relative links, so in CI link to the doc as it was at
    # this exact commit.
    doc_link = f"{server}/{repository}/blob/{sha}/{DOC}" if sha and repository else DOC
    heading = f"## Safety evidence{f' for {sha[:7]}' if sha else ''}"
    lines = [
        heading,
        "",
        f"Each row is a decision in [`{DOC}`]({doc_link}), and the tests on this commit that "
        "prove it.",
        "",
    ]
    if not junit.is_file():
        lines.append(f"❌ No test results at `{junit}`: the test step did not finish.")
        return "\n".join(lines)
    lines += decision_table(tally(collect_markers(), read_outcomes(junit)))
    lines.append("")
    try:
        lines += attack_tables()
    except Exception as exc:  # the report must never be the thing that fails
        lines.append(f"❌ The attack set could not be run: {type(exc).__name__}: {exc}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, default=Path("test-results.xml"))
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # the ✅ / ❌ marks, on any platform
    print(report(args.junit))
