from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "762a0788079508265018ff8b8e0544dcd15c5d79d438793f7e6174229410937a",
    "efficiency.SCRIPT": "eea1c24079540f0e15d564883f8d656ec166760c9281c116852d10e76b094a36",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "2da26301d0862980ebee0d08b8250a2caa63fab7ab228fefa24c56ba7f0d9351",
    "seeded/chrome": "08ef35bff09ffaafcc0ffc50b70448d2e5daf2382f291537a934e2ac6d2116fa",
    "hostile/main": "2da26301d0862980ebee0d08b8250a2caa63fab7ab228fefa24c56ba7f0d9351",
    "hostile/chrome": "5fb72c9efeb8bf4f7e8aa2b3ad6bb77dfe4af459fa5e5aa3e92994d1a6b3f123",
    "empty/main": "144ee7728324301e2b449e1bc72c99c3c312f88f88c9f9a08b55c6515ede68a1",
    "empty/chrome": "6801cace7fec4800fb20f65d65b03bb564f65dcf69b3710a0319190df89e4a17",
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
