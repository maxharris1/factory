from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "20e435f7bc1dd966e3364fc84bd8f8a5c43ac84f609f2c098f2d60207bacc43e",
    "tickets.SCRIPT_TICKETS": "6323dd808d2985aeb6b2d6cde07250dc6bfa0d86300f6e14534fdf34a6afaec8",
    "ticket_detail.CSS": "8cd6e218f91c4d13aea734e716a877fbf2287222a67190a6f480340374c2725d",
    "ticket_detail.SCRIPT": "f7fe539841d49aca9332ec4542504cddd53df371b329d193f16ba8227ab64496",
    "__BOARD__": "18b21db47d9ddf72eff3caa915c12ee0cffde0834e62ca0477318d9629503780",
    "__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "hostile/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "empty/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/main": "1792a822b40fa96b2774e3372e9866c428d4a999ee1df7ff49b54d86be748939",
    "seeded/chrome": "f7fbdb4b81985537d0b9b806d2a07da4b102fe1755f187c40e31a8066858318f",
    "hostile/main": "1792a822b40fa96b2774e3372e9866c428d4a999ee1df7ff49b54d86be748939",
    "hostile/chrome": "9d4afdf83cd1a61d6c1dfe9d932176bc1f16b189b825af695a60942964822a81",
    "empty/main": "4131688da3fe5d52c2613ae4bee45372b9a7a3380b4675438669c8924436953a",
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
