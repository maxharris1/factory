from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "0dba7be70efc83cea296eeee5c650725c3a567f63a995d1ce9a75b7cbf3d355d",
    "tickets.SCRIPT_TICKETS": "9b07bd9b677419c8a895ff16699cebc9e84b508715b72498f90d39fb4418911e",
    "ticket_detail.CSS": "0511cdbc17f3890f3c49637b1e568c5928d32bba6fcbdeb999303e0826444296",
    "ticket_detail.SCRIPT": "22bc4e4a4c4082ec0d0e35dd9f2987890ccf96bc558e9cf505def5c7843ad21c",
    "__BOARD__": "1af659d5f74268ee0c104803bd7f96bd3ae917f3962cd71b283f4acf7aab9557",
    "__FLOWMAP__": "2242fbc61152aea08c578bb6d06f45ca4c8603f4c784f4e851c9e8758db93773",
    "seeded/__FLOWMAP__": "2242fbc61152aea08c578bb6d06f45ca4c8603f4c784f4e851c9e8758db93773",
    "hostile/__FLOWMAP__": "2242fbc61152aea08c578bb6d06f45ca4c8603f4c784f4e851c9e8758db93773",
    "empty/__FLOWMAP__": "2242fbc61152aea08c578bb6d06f45ca4c8603f4c784f4e851c9e8758db93773",
    "seeded/main": "f1a3440bf7247948f4fa206b99885691faee66df90ec1c73940380889af2fcd6",
    "seeded/chrome": "0eb603610142b4c6d93f15c6076bb69b60dec58b6429c64fb6d7c7074d7ad83f",
    "hostile/main": "f1a3440bf7247948f4fa206b99885691faee66df90ec1c73940380889af2fcd6",
    "hostile/chrome": "85226d27bc97bf6944c88f6af626bf3495c9ad6a79c1b5ee9a2434a7559b435b",
    "empty/main": "0ec9c75f06e03378d33b54ae12ebc1a1541f84161dcebbe8ade6a35a0c0dd140",
    "empty/chrome": "78c6b2bfe6a8a3a39cdbe5dcbce2670b5eb730f502d2a3a8cc409d937d955ef7",
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
