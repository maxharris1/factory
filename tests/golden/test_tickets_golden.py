from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "866aac1a613e948a74f41e2445f640cadf894cc587daae6d32cdffedd26d46f5",
    "tickets.SCRIPT_TICKETS": "bc7837d90f7870f527e29a34d204460c92c185e6fc54a74aa63e62761ae0ed89",
    "ticket_detail.CSS": "da37e77fbd8ab2d98c0a4bb0f507928b81a8d437688d9ef38f35a6517293d143",
    "ticket_detail.SCRIPT": "a6b429229374219c10e8244ae524ee57ca4562ed7898c80763c982fcc992715d",
    "__BOARD__": "a95cdf573c85b101daa3f685be6323ee93936c5c9a779bcc75c3c11dcade93c6",
    "__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "hostile/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "empty/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/main": "d0ffbee91cf1102c2d2e03dd1c9659d3189b4ca53b0b0c862cb3cf75a7483402",
    "seeded/chrome": "25243b2b23181bb500bb0ae75b458563a3776cbf60b71dd8a22b43fc11aa09d6",
    "hostile/main": "d0ffbee91cf1102c2d2e03dd1c9659d3189b4ca53b0b0c862cb3cf75a7483402",
    "hostile/chrome": "6dd91374a98a4768353983eae7bf8a94e47b1cbd49b4c407ae20f48df672f03f",
    "empty/main": "6ff12ac4ae1e291d65a71d84d65d55a0c11b290d85b40d6dbff115c655f842c9",
    "empty/chrome": "2e576ea7d76b1582b5aed1b9bc4e6792f74a8379f9d21f33ec84ddca545478a4",
}


def digests(root: Path) -> dict[str, str]:
    assets = {
        "tickets.CSS": tickets.CSS,
        "tickets.SCRIPT_TICKETS": tickets.SCRIPT_TICKETS,
        "ticket_detail.CSS": ticket_detail.CSS,
        "ticket_detail.SCRIPT": ticket_detail.SCRIPT,
        "__BOARD__": substitutions(None)["__BOARD__"],
        "__FLOWMAP__": substitutions(load_chart())["__FLOWMAP__"],
    }
    for case, snapshot in snapshots(root / "charts").items():
        assets[f"{case}/__FLOWMAP__"] = substitutions(snapshot.get("statechart"))["__FLOWMAP__"]
    return page_digests(NAME, assets, root / "pages")


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
