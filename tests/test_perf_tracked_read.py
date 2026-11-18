import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "perf" / "tracked-read.md"
SHA = re.compile(r"`[0-9a-f]{8,40}`")


def _results_row(label: str) -> list[str]:
    for line in DOC.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] == label:
            return cells[1:]
    raise AssertionError(f"no {label!r} row")


def test_each_window_names_the_commits_measured():
    before, after = _results_row("Commit measured")
    assert len(SHA.findall(before)) >= 2
    assert not before.startswith("`main`")
    assert SHA.search(after)


def test_doc_records_the_poller_heartbeat_version():
    text = DOC.read_text()
    assert "no commit field" not in text
    assert re.search(r"heartbeat[^\n]*`version=[0-9a-f]{12}`[^\n]*last seen", text)
