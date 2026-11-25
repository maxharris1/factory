from __future__ import annotations

import ast
import re
import subprocess
import urllib.request
from pathlib import Path

import pytest
import yaml

import lib.qa_runners.fake
import lib.qa_smoke
from lib import cli, github_actions, qa_runners, qa_storage
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


SMOKE_ORCH = """\
poll_seconds: 30
projects:
  - repo: owner/repo
{storage}trusted_logins:
  maxharris1: [grok-bot]
"""

SMOKE_STORAGE = """\
    qa_storage:
      bucket: factory-qa-owner
      region: us-east-1
      access_key_env: QA_S3_KEY_ID
      secret_key_env: QA_S3_SECRET
      evidence_root: {root}
"""


def smoke_orch(tmp_path: Path, root: Path | None) -> Path:
    storage = "" if root is None else SMOKE_STORAGE.format(root=root)
    orch = tmp_path / "orch.yml"
    orch.write_text(SMOKE_ORCH.format(storage=storage), encoding="utf-8")
    return orch


def smoke(orch: Path, repo: str = "owner/repo", runner: str = "fake") -> int:
    return cli.main(["qa", "smoke", "--repo", repo, "--runner", runner, "--orch", str(orch)])


def _tree(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_mode) for p in root.rglob("*"))


def test_qa_smoke_passes_with_a_fake_runner_and_leaves_the_evidence_root_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "kept.txt").write_text("operator file\n")
    before = (_tree(root), root.stat().st_mode)

    status = smoke(smoke_orch(tmp_path, root))

    out = capsys.readouterr().out.splitlines()
    assert status == 0, out
    assert [line.split(":")[0] for line in out] == [
        "ok project", "ok runner", "ok evidence root", "ok evidence write",
    ]
    assert out[1] == "ok runner: fake fake-1.0"
    assert (_tree(root), root.stat().st_mode) == before
    assert (root / "kept.txt").read_text() == "operator file\n"


def test_qa_smoke_writes_where_the_dashboard_reads_and_touches_no_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("orch qa smoke reached GitHub or the network")

    for owner, name in (
        (subprocess, "run"), (subprocess, "Popen"), (urllib.request, "urlopen"), (qa_storage, "urlopen"),
        (github_actions.GitHubReviewActions, "post_issue_comment"),
        (github_actions.GitHubReviewActions, "post_issue_comment_once"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    root = tmp_path / "evidence"
    root.mkdir()

    status = smoke(smoke_orch(tmp_path, root))

    out = capsys.readouterr().out.splitlines()
    assert status == 0, out
    assert re.fullmatch(
        rf"ok evidence write: {re.escape(str(root))}/owner/repo/smoke/smoke-\d{{14}}-\d+/lane-1/result\.json",
        out[3],
    )
    assert list(root.iterdir()) == []
    tree = ast.parse(Path(lib.qa_smoke.__file__).read_text(encoding="utf-8"))
    imported = [
        f"{getattr(node, 'module', '')}.{alias.name}"
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert imported and not any("github" in name for name in imported)


def test_qa_smoke_fails_cleanly_on_a_missing_evidence_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "missing"

    status = smoke(smoke_orch(tmp_path, root))

    output = capsys.readouterr()
    assert status == 1
    assert f"fail evidence root: {root} does not exist" in output.out.splitlines()
    assert "fail evidence write: skipped (evidence root failed)" in output.out.splitlines()
    assert not root.exists()
    assert "Traceback" not in output.err


def test_qa_smoke_without_qa_storage_fails_project_and_skips_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = smoke(smoke_orch(tmp_path, None))

    out = capsys.readouterr().out.splitlines()
    assert status == 1
    assert out[0].startswith("fail project: ")
    assert "qa_storage" in out[0]
    assert out[1] == "ok runner: fake fake-1.0"
    assert out[2:] == [
        "fail evidence root: skipped (project failed)",
        "fail evidence write: skipped (evidence root failed)",
    ]


def test_qa_smoke_reports_a_runner_outage_and_an_unknown_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    down = lib.qa_runners.fake.FakeRunner()
    down.ping_error = qa_runners.RunnerOutage("docker daemon unavailable")
    monkeypatch.setitem(qa_runners._REGISTRY, "fake-down", lambda: down)
    root = tmp_path / "evidence"
    root.mkdir()
    orch = smoke_orch(tmp_path, root)

    assert smoke(orch, runner="fake-down") == 1
    assert "fail runner: docker daemon unavailable" in capsys.readouterr().out.splitlines()

    assert smoke(orch, runner="nope") == 1
    assert "fail runner: no QA runner named 'nope'" in capsys.readouterr().out.splitlines()


def test_qa_smoke_matches_the_repo_case_insensitively_and_refuses_an_unknown_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    orch = smoke_orch(tmp_path, root)

    assert smoke(orch, repo="Owner/Repo") == 0
    capsys.readouterr()
    assert smoke(orch, repo="owner/other") == 1
    assert capsys.readouterr().out.startswith("fail project: owner/other is not in ")


def test_qa_smoke_argument_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["qa", "smoke", "--runner", "fake"]) == 2
    assert cli.main(["qa", "launch"]) == 2
    assert cli.main(["qa", "smoke", "--repo", "owner/repo", "--runner", "fake",
                     "--orch", str(tmp_path / "absent.yml")]) == 1
    assert "orch qa smoke: " in capsys.readouterr().err
