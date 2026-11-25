from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from lib import cli
from lib.factory_md import EXAMPLE_PATHS, QaRunner, parse_factory_md
from lib.load import split_front_matter

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_FACTORY_MDS = [path for path in EXAMPLE_PATHS if path.startswith("examples/")]
QA_EXAMPLES = ("examples/aut0/factory.md", "examples/cleffy/factory.md", "examples/tracy/factory.md")


def test_every_project_example_is_covered() -> None:
    shipped = sorted(str(path.relative_to(ROOT)) for path in ROOT.glob("examples/*/factory.md"))
    assert sorted(EXAMPLE_FACTORY_MDS) == shipped
    assert set(QA_EXAMPLES) <= set(shipped)


@pytest.mark.parametrize("relative", EXAMPLE_FACTORY_MDS)
def test_example_passes_check_config(relative: str, capsys: pytest.CaptureFixture[str]) -> None:
    status = cli._check_config(
        ["--orch", str(ROOT / "config/orch.example.yml"), "--factory", str(ROOT / relative)]
    )

    output = capsys.readouterr()
    assert status == 0, output.err
    assert output.err == ""


@pytest.mark.parametrize("relative", QA_EXAMPLES)
def test_example_qa_stanza_is_local_docker_with_visual_hold(relative: str) -> None:
    raw, body = split_front_matter((ROOT / relative).read_text(encoding="utf-8"))
    front = yaml.safe_load(raw)
    qa = front["qa"]
    assert qa["runners"] == ["local-docker"]
    assert qa["visual_hold"] is True
    assert "## QA" in body

    enabled = yaml.safe_dump({**front, "qa": {**qa, "enabled": True}}, sort_keys=False)
    config, findings = parse_factory_md(f"---\n{enabled}---\n{body}", strict=True)

    assert findings == []
    assert config is not None and config.qa is not None
    assert config.qa.runners == (QaRunner("local-docker", qa["local_docker"]["environment"], None),)
    assert config.qa.visual_hold is True


QA_DOCS = (*QA_EXAMPLES, "docs/AUT0-CLEANUP.md")


def _qa_section(relative: str) -> str:
    text = (ROOT / relative).read_text(encoding="utf-8")
    return text.split("\n## QA\n", 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize("relative", QA_DOCS)
def test_qa_section_says_the_profile_provisions_nothing(relative: str) -> None:
    section = _qa_section(relative)
    assert re.search(r"profile[^.]*\bloads\b", section) is None
    assert "only labels the result comment's `env=` field" in section
    assert "starts no service for any profile" in section
    assert re.search(r"`lane\.boot` and `lane\.base_url` are placeholders", section)


def test_spec_profile_row_claims_no_provisioning() -> None:
    spec = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
    row = next(line for line in spec.splitlines() if line.startswith("  | `qa.profile` |"))
    assert "loads" not in row
    assert "starts no service" in row
