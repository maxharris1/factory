from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "9e2e31c6aab2391a38eb2aeb92ef99e3afea2b3847dd0e19491d6c9984b14c2f",
    "efficiency.SCRIPT": "5df478157cece5b5e4e54b8e15f5f66b44117274b2503037a8ad6357e73ac9d0",
    "TELEMETRY_CSS": "d411c22b93b0b403263b7f5c47b79cb083a294188188359631aea4758055e523",
    "__TELEMETRY__": "8033c22e84cd4c33eb101893f48a8fe741741672be24b635a986631ae8f1b087",
    "seeded/main": "b29c05e8b880a7226972f7f6744eb6873a77e133490ec2056f79d8fd48330269",
    "seeded/chrome": "c26f7b3a41c71644f96b33d8384494a9fe81dd78db216aed69a278b8655f71bc",
    "hostile/main": "b29c05e8b880a7226972f7f6744eb6873a77e133490ec2056f79d8fd48330269",
    "hostile/chrome": "e76657c6e62954fdaa26519a0dfe79b203e040cf187fbe23ae5bfb6b2921ae0e",
    "empty/main": "85e430ff37e5954f167eab295530626f0fb9e6762367e5886d5d92dfe7f23cb8",
    "empty/chrome": "0e87ae7051e3c1a551a9309768b9f3b189c3becfdd4d821a5b6fa4f4b6feb9d5",
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
