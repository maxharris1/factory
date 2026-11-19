from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

DEFAULT_FILE = Path(__file__).with_name("quarantine.txt")
ISSUE_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+")


def parse(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise pytest.UsageError(f"{path}: {e.strerror}") from None
    entries: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        where = f"{path}:{lineno}"
        if len(fields) != 2:
            raise pytest.UsageError(f"{where}: expected '<node id> <issue url>', got {line!r}")
        nodeid, url = fields
        if not ISSUE_URL.fullmatch(url):
            raise pytest.UsageError(
                f"{where}: {url!r} is not a GitHub issue link (https://github.com/<owner>/<repo>/issues/<n>)"
            )
        if nodeid in entries:
            raise pytest.UsageError(f"{where}: {nodeid} is already quarantined")
        entries[nodeid] = url
    return entries


def issue_for(entries: dict[str, str], nodeid: str) -> str | None:
    return entries.get(nodeid) or entries.get(nodeid.partition("[")[0])


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--quarantine-file",
        type=Path,
        default=DEFAULT_FILE,
        metavar="PATH",
        help="tests listed here run but cannot fail the run",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(Quarantine(parse(config.getoption("quarantine_file"))))


class Quarantine:
    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = entries
        self.outcomes: dict[str, str] = {}

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            url = issue_for(self.entries, item.nodeid)
            if url:
                # Prepended so it wins over the test's own xfail mark, which may be strict.
                mark = pytest.mark.xfail(reason=f"quarantined: {url}", strict=False)
                item.add_marker(mark, append=False)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if not issue_for(self.entries, report.nodeid):
            return
        if report.skipped and hasattr(report, "wasxfail"):
            outcome = "failed"
        elif report.skipped:
            outcome = "skipped"
        elif report.when == "call":
            outcome = "passed"
        else:
            return
        if self.outcomes.get(report.nodeid) != "failed":
            self.outcomes[report.nodeid] = outcome

    def pytest_terminal_summary(self, terminalreporter, config: pytest.Config) -> None:
        if hasattr(config, "workerinput") or not self.outcomes:
            return
        rows = [(nodeid, self.outcomes[nodeid], issue_for(self.entries, nodeid))
                for nodeid in sorted(self.outcomes)]
        terminalreporter.write_sep("=", "quarantined")
        for nodeid, outcome, url in rows:
            terminalreporter.write_line(f"{outcome.upper()} {nodeid} {url}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            table = ["### Quarantined tests", "", "| Test | Result | Issue |", "| --- | --- | --- |"]
            for nodeid, outcome, url in rows:
                cell = nodeid.replace("|", r"\|")
                table.append(f"| `{cell}` | {outcome} | {url} |")
            with open(summary, "a", encoding="utf-8") as out:
                out.write("\n".join(table) + "\n\n")


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else DEFAULT_FILE
    try:
        entries = parse(path)
    except pytest.UsageError as e:
        print(e, file=sys.stderr)
        return 1
    for url in dict.fromkeys(entries.values()):
        print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
