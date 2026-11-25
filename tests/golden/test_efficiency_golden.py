from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency, routing
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "159fdfbcc55b166f35d0fc8a141641a773e92ee2a5fae1224a6ae0d777336120",
    "efficiency.SCRIPT": "997f9c391299a200bb1580ccdfe68aa42a3911f30e2e0daba8c8b0d91abc6388",
    "routing.CSS": "e6c7ee7099be006cebb80d2c965a9c60d3aa1d429ecf9a9af5dc9f797c39c137",
    "routing.SCRIPT": "a9d2c2f0bcda434daea5b4c0c186a0e21e6523307d9f19a6baf919a7e6bdcc21",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "7026fe18daced5a6eab6af6f0eba7dd2729534bb5bfb8e48461391326ebdaf64",
    "seeded/chrome": "ccb9be0d9d4f40ee41934e726653a2993d591fcb4cd3f36f6a383e4b3ab25b63",
    "hostile/main": "7026fe18daced5a6eab6af6f0eba7dd2729534bb5bfb8e48461391326ebdaf64",
    "hostile/chrome": "e4b93534cf5ff192300e63892ee6b28cd904de6c35171842c9d24a1ec760214a",
    "empty/main": "f9629ee2bbfd175876a2ef6c21a503a6f9b4433a44f138930b2fd4770bb8ad74",
    "empty/chrome": "7518e72c6e3c2c9f11e64df1a93752a5cd36dd3d5f847f02e3cbc7b07a105a0e",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "efficiency.CSS": efficiency.CSS,
        "efficiency.SCRIPT": efficiency.SCRIPT,
        "routing.CSS": routing.CSS,
        "routing.SCRIPT": routing.SCRIPT,
        "TELEMETRY_CSS": TELEMETRY_CSS,
        "__TELEMETRY__": substitutions(None)["__TELEMETRY__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
