from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "fc5bfed384b2fc937588f348286a6bfc80a9ea72341111cc0df76ff545565b3c",
    "efficiency.SCRIPT": "765a09e41a76dcac556954da29b78a89511f1311b2bef98b105d0f9e994a999f",
    "TELEMETRY_CSS": "d411c22b93b0b403263b7f5c47b79cb083a294188188359631aea4758055e523",
    "__TELEMETRY__": "46282b20d23c15d3b06e6aaee9f34d3305a8f3285c53f1913fb0933df19d7f9f",
    "seeded/main": "30ef102e2a8a354abe4812ad8b6ffb1c0711dd6ffa2942796f5bd3be275600e6",
    "hostile/main": "30ef102e2a8a354abe4812ad8b6ffb1c0711dd6ffa2942796f5bd3be275600e6",
    "empty/main": "5b7a908edc00d44b70fd121a7bb925b4cac66a443d3d2ef75fb8ff8c141a0202",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "efficiency.CSS": efficiency.CSS,
        "efficiency.SCRIPT": efficiency.SCRIPT,
        "TELEMETRY_CSS": TELEMETRY_CSS,
        "__TELEMETRY__": substitutions(None)["__TELEMETRY__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
