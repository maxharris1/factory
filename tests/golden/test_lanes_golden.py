from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "a31da0da4311ec406567663b97415272e650a5ce1e3e30bb8d4f38923096f4e9",
    "lanes.SCRIPT": "0710eebb30054de6c73ebcb196cee22bb610b80c98ce4c929e2d91fd738930b9",
    "__LANES__": "5b84435192910a2ae4df47f385717dd58485121b59cc158496e3a5e6109c25f6",
    "seeded/main": "4a850ef7322d86a958e7ddc056e0c7050f363192b13bb78b27e9ba23ff06c527",
    "seeded/chrome": "0f6507c5052d1a73968aec9e5444e4b6302cca161fcdbb3a09c75187482e95aa",
    "hostile/main": "4a850ef7322d86a958e7ddc056e0c7050f363192b13bb78b27e9ba23ff06c527",
    "hostile/chrome": "1ce019db3d6d20dbbde6bdb713507be18b3321163788faa4402a125c98ad28f7",
    "empty/main": "8af5420398211711d92ef6a83ab262a43e71257cbb0545e73fd713dd5f0d87cf",
    "empty/chrome": "d23f23faebf48e37b2841bb639c678d998bee55e8e886b922b411cb29ef90c12",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "lanes.CSS": lanes.CSS,
        "lanes.SCRIPT": lanes.SCRIPT,
        "__LANES__": substitutions(None)["__LANES__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
