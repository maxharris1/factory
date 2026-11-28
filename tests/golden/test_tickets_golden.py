from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "381084a5f2be20182876d9794216aaa9cbc407e662e2db03d5ce202d9cb8d821",
    "tickets.SCRIPT_TICKETS": "0eef082ad3f056652424d9669dd99c2fe165fd175dfb3258c47df4cbe5bea5c7",
    "ticket_detail.CSS": "2d92ffacf1b6e95a557bc5db6ff603f9abac0ebbec6d91e5d11db227e14368fb",
    "ticket_detail.SCRIPT": "60feb8846872be5f932813dde0034b2a1940a5f4103c41c35b1cce8012de2c21",
    "__BOARD__": "611cc9d340cdc999b2086c8a76dad3a3170502fa3d7cfa41d439fa1f9eddf1b3",
    "__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "seeded/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "hostile/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "empty/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "seeded/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "seeded/chrome": "7b44af7c757587575376ae673d6a9dc3730f016474de3b3b39406a943337ac03",
    "hostile/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "hostile/chrome": "bf9e629c7a21f3c53353ca4f5b999b890a42d8f54c4e886bc8492baa6db7bfc8",
    "empty/main": "e36d217e80efb19b357b57a0949ab9f065931c323ae9ab40c99774ec7aa34f38",
    "empty/chrome": "9325fb0ce6026ce6ea005e1224286e3f387fad7e8f7ea69cb3ae6606f37036f7",
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
