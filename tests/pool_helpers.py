"""Claim a job the way `orch wait` does since F2-02: through the device pool."""

from __future__ import annotations

from pathlib import Path

from lib import job_token
from lib.device_pool import DevicePool
from lib.machines_yml import load_machines_yml
from lib.queue import Job, SQLiteJobQueue

ROOT = Path(__file__).resolve().parents[1]
CLAIM_PROOF = "test-claim-proof"
CLAIM_HASH = job_token.digest(CLAIM_PROOF)


def pool_claim(queue: SQLiteJobQueue, recipient: str, machines: Path | None = None) -> Job:
    """Claim the oldest queued job for recipient with a slot from machines.yml."""
    config, _ = load_machines_yml(machines or ROOT / "machines.yml", allow_placeholders=True)
    assert config is not None
    claimed = DevicePool(queue, config).claim(recipient)
    assert claimed is not None
    return claimed[0]


TARGET_MACHINES_YML = """\
orch_host: pc
devices:
  - name: pc
    tailscale_name: pc-wsl-bots.tail68624b.ts.net
    slot_max: 10
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot}
    workdir_root: /home/{bot}/factory-work
    ssh_alias: pc-wsl-{bot}
    factory_root: /home/{bot}/factory
  - name: box
    tailscale_name: grokbot-factory-box.tail68624b.ts.net
    slot_max: 6
    ssh_user: box
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot}
    workdir_root: /home/box/factory-work
    ssh_alias: box-{bot}
    factory_root: /home/box/factory
  - name: mac
    enabled: false
    tailscale_name: maxs-macbook-pro.tail68624b.ts.net
    slot_max: 2
    harnesses: {claude: claude-bot}
    workdir_root: /Users/{bot}/factory-work
    ssh_alias: mac-{bot}
    factory_root: /Users/{bot}/factory
"""


def write_target_machines(directory: Path) -> Path:
    """The target machines.yml shape: orch on pc, box a remote device."""
    path = directory / "machines.yml"
    path.write_text(TARGET_MACHINES_YML, encoding="utf-8")
    return path
