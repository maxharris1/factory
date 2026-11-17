from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "2432d51bbb001fc4a892b4c21146fe1904cb0717c8daffef5334b00f3abcc0b0",
    "tickets.SCRIPT_TICKETS": "65c4eadb63677f53077c2fa065d25035f89efd98abee7775421df74fd7dd3359",
    "ticket_detail.CSS": "8c6af76772cf24486e8009cb75a344e1dd85f7acc1abc0231d494625ab4411c4",
    "ticket_detail.SCRIPT": "e6253dc3886088079b5c2014aa4631f04de9d02e4d1f021b85d3acb3dbd0485a",
    "__BOARD__": "988b51ce050bf3873af75787819c57711326377eaed5a138918d033658a9ef31",
    "__FLOWMAP__": "2242fbc61152aea08c578bb6d06f45ca4c8603f4c784f4e851c9e8758db93773",
    "seeded/__FLOWMAP__": "2242fbc61152aea08c578bb6d06f45ca4c8603f4c784f4e851c9e8758db93773",
    "hostile/__FLOWMAP__": "2242fbc61152aea08c578bb6d06f45ca4c8603f4c784f4e851c9e8758db93773",
    "empty/__FLOWMAP__": "2242fbc61152aea08c578bb6d06f45ca4c8603f4c784f4e851c9e8758db93773",
    "seeded/main": "8ce9c9bd7774c51d6eb475eb03017e1a638ef3bd3734252ddfd2f8a152da9a70",
    "seeded/chrome": "25243b2b23181bb500bb0ae75b458563a3776cbf60b71dd8a22b43fc11aa09d6",
    "hostile/main": "8ce9c9bd7774c51d6eb475eb03017e1a638ef3bd3734252ddfd2f8a152da9a70",
    "hostile/chrome": "6dd91374a98a4768353983eae7bf8a94e47b1cbd49b4c407ae20f48df672f03f",
    "empty/main": "370600786fd2ae62b641bd7805b85471c05aed8b9765ea3b83b52659feb5366b",
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
