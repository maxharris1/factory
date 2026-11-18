from pathlib import Path

import pytest

from lib import cli
from lib.orch_yml import QaStorage, load_orch_yml

ROOT = Path(__file__).resolve().parents[1]

ORCH = """\
poll_seconds: 30
projects:
  - repo: owner/repo
trusted_logins:
  maxharris1: [grok-bot]
"""

ENTRY = """\
projects:
  - repo: owner/repo
    qa_storage:
      bucket: factory-qa-owner
      region: us-east-1
      access_key_env: QA_S3_KEY_ID
      secret_key_env: QA_S3_SECRET
      evidence_root: /srv/factory/qa-evidence
"""

QA = """\
qa:
  enabled: true
  runners: [claude-cloud]
  profile: none
  claude_cloud:
    environment: qa-env
  lane:
    boot: bash boot.sh
    base_url: http://127.0.0.1:8080
"""


@pytest.fixture(autouse=True)
def overlay_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY", raising=False)


def qa_factory_md(tmp_path: Path) -> Path:
    text = (ROOT / "examples/cleffy/factory.md").read_text(encoding="utf-8")
    text = text.replace("skills: []\n", "skills: []\n" + QA, 1)
    path = tmp_path / "factory.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_orch(tmp_path: Path, local: str | None = None) -> Path:
    orch = tmp_path / "orch.yml"
    orch.write_text(ORCH, encoding="utf-8")
    if local is not None:
        (tmp_path / "orch.local.yml").write_text(local, encoding="utf-8")
    return orch


def test_qa_enabled_with_no_storage_entry_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    orch = write_orch(tmp_path)
    factory = qa_factory_md(tmp_path)

    status = cli._check_config(["--orch", str(orch), "--factory", f"owner/repo={factory}"])

    assert status == 1
    err = capsys.readouterr().err
    assert "owner/repo has no qa_storage entry" in err
    assert "orch.local.yml" in err


def test_qa_enabled_on_an_unpaired_path_asks_for_the_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    orch = write_orch(tmp_path, ENTRY)
    factory = qa_factory_md(tmp_path)

    status = cli._check_config(["--orch", str(orch), "--factory", str(factory)])

    assert status == 1
    assert "--factory owner/name=PATH" in capsys.readouterr().err


def test_qa_enabled_with_a_storage_entry_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    orch = write_orch(tmp_path, ENTRY)
    factory = qa_factory_md(tmp_path)

    status = cli._check_config(["--orch", str(orch), "--factory", f"owner/repo={factory}"])

    out = capsys.readouterr()
    assert status == 0, out.err
    assert out.out.splitlines()[-1] == "ok"


def test_a_paired_path_without_qa_needs_no_storage_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    orch = write_orch(tmp_path)
    factory = ROOT / "examples/cleffy/factory.md"

    status = cli._check_config(["--orch", str(orch), "--factory", f"owner/repo={factory}"])

    assert status == 0, capsys.readouterr().err


def test_a_storage_entry_parses_from_the_local_overlay(tmp_path: Path) -> None:
    config, findings = load_orch_yml(write_orch(tmp_path, ENTRY))

    assert findings == []
    assert config is not None
    assert config.projects[0].qa_storage == QaStorage(
        bucket="factory-qa-owner",
        region="us-east-1",
        endpoint=None,
        access_key_env="QA_S3_KEY_ID",
        secret_key_env="QA_S3_SECRET",
        evidence_root=Path("/srv/factory/qa-evidence"),
    )


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("      access_key_env: qa-key\n", "qa_storage.access_key_env must be an environment variable name"),
        ("      evidence_root: relative/dir\n", "qa_storage.evidence_root must be an absolute path"),
        ("      endpoint: ftp://example.com\n", "qa_storage.endpoint must be an https URL"),
        ("      bucket: Bad_Bucket\n", "qa_storage.bucket must be an S3 bucket name"),
        ("      color: blue\n", "unknown field 'projects[0].qa_storage.color'"),
    ],
)
def test_a_bad_storage_entry_is_refused(tmp_path: Path, line: str, message: str) -> None:
    key = line.split(":")[0]
    lines = [item for item in ENTRY.splitlines(keepends=True) if not item.startswith(key + ":")]
    config, findings = load_orch_yml(write_orch(tmp_path, "".join(lines) + line))

    assert config is None
    assert message in [finding.message for finding in findings]


def test_a_missing_storage_field_is_refused(tmp_path: Path) -> None:
    local = ENTRY.replace("      secret_key_env: QA_S3_SECRET\n", "")

    config, findings = load_orch_yml(write_orch(tmp_path, local))

    assert config is None
    assert "missing required field 'projects[0].qa_storage.secret_key_env'" in [
        finding.message for finding in findings
    ]


def test_budget_and_floor_parse_as_gib(tmp_path: Path) -> None:
    local = ENTRY + "      evidence_budget_gib: 40\n      min_free_gib: 5\n"

    config, findings = load_orch_yml(write_orch(tmp_path, local))

    assert findings == []
    storage = config.projects[0].qa_storage
    assert (storage.evidence_budget_gib, storage.min_free_gib) == (40, 5)


def test_budget_and_floor_default_off(tmp_path: Path) -> None:
    config, _findings = load_orch_yml(write_orch(tmp_path, ENTRY))

    storage = config.projects[0].qa_storage
    assert (storage.evidence_budget_gib, storage.min_free_gib) == (None, None)


@pytest.mark.parametrize("key", ["evidence_budget_gib", "min_free_gib"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", '"5"', "true"])
def test_a_bad_budget_or_floor_is_refused(tmp_path: Path, key: str, value: str) -> None:
    config, findings = load_orch_yml(write_orch(tmp_path, ENTRY + f"      {key}: {value}\n"))

    assert config is None
    assert f"qa_storage.{key} must be a positive integer (GiB)" in [finding.message for finding in findings]


def test_an_unknown_shared_account_runner_fails_check_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    orch = write_orch(tmp_path, ENTRY)
    orch.write_text(ORCH + "qa:\n  shares_usage_with:\n    gpu-cloud: claude\n", encoding="utf-8")
    factory = qa_factory_md(tmp_path)

    status = cli.main(["check-config", "--orch", str(orch), "--factory", f"owner/repo={factory}"])

    assert status != 0
    assert "qa.shares_usage_with has unknown runner 'gpu-cloud'" in capsys.readouterr().err
