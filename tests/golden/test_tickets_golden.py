from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "381084a5f2be20182876d9794216aaa9cbc407e662e2db03d5ce202d9cb8d821",
    "tickets.SCRIPT_TICKETS": "0eef082ad3f056652424d9669dd99c2fe165fd175dfb3258c47df4cbe5bea5c7",
    "ticket_detail.CSS": "983bc4533c83a77797187c1b508327834bc168b41487b07e988a326248c39197",
    "ticket_detail.SCRIPT": "355922aeb9b17139a3b38542c280d4de4a544d3afb7601f69abfcdc5610871b0",
    "__BOARD__": "611cc9d340cdc999b2086c8a76dad3a3170502fa3d7cfa41d439fa1f9eddf1b3",
    "__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "seeded/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "hostile/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "empty/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "seeded/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "seeded/chrome": "972f0acb0cca6b6cdeb1b32db67b983e0a9b5749d3a6eab0125396afe7e5978e",
    "hostile/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "hostile/chrome": "d95cea09428464cbb3bf64a264f5f75987df8ef82796e04ed0ed1ab1fb0c8040",
    "empty/main": "e36d217e80efb19b357b57a0949ab9f065931c323ae9ab40c99774ec7aa34f38",
    "empty/chrome": "21a18a3977ce3719d4e7f1d74f4d7182415256067fd28788c6b88b775cd65da3",
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
