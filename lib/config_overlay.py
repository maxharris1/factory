"""Host-local overlays for orch.yml and machines.yml (issue #107).

A gitignored `<stem>.local.yml` beside the base file (orch.local.yml next
to orch.yml, machines.local.yml next to machines.yml) holds the values one
host sets by hand: a Linear project id, trusted_linear_users, the webhook's
public_url, a device's slot_max or enabled. It is deep-merged into the
parsed base before validation, so the checkout itself stays clean and
`git pull` never conflicts:

* mappings merge key by key, recursively;
* top-level `projects` merge by `repo` and `devices` by `name`: an entry
  whose key matches overrides that entry's fields, any other is appended;
* every other list, and every scalar, replaces the base value.

The overlay sits beside the base file rather than in a user's home: the
poller and each bot's waiter run as different users and must read the
same config. ORCH_NO_LOCAL_OVERLAY=1 turns overlays off (the test suite
sets it). `overlay_keys` names what an overlay sets, never its values, for
`orch check-config`.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from lib.load import Finding, load_yaml

DISABLE_ENV = "ORCH_NO_LOCAL_OVERLAY"
KEYED_LISTS = {"projects": "repo", "devices": "name"}
# Mappings keyed by identities (logins, Linear user ids): named as a whole.
OPAQUE = frozenset({"trusted_logins", "trusted_linear_users"})


class OverlayError(ValueError):
    """An overlay whose shape cannot be merged."""


def overlay_path(path: Path) -> Path:
    """orch.yml -> orch.local.yml, in the same directory."""
    path = Path(path)
    return path.with_name(f"{path.stem}.local.yml")


def overlay_enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "") in ("", "0")


def read_overlay(path: Path) -> tuple[dict | None, list[Finding]]:
    """The parsed overlay for base file `path`; (None, []) when there is none."""
    local = overlay_path(path)
    if not overlay_enabled() or not local.is_file():
        return None, []
    try:
        data = load_yaml(local.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return None, [Finding("error", f"{local.name}: cannot read: {type(exc).__name__}")]
    except yaml.YAMLError as exc:
        text = " ".join(line.strip() for line in str(exc).splitlines() if line.strip())
        return None, [Finding("error", f"{local.name}: invalid YAML: {text or 'invalid YAML'}")]
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return None, [Finding("error", f"{local.name}: an overlay must be a mapping")]
    try:
        _check_keyed(data)
    except OverlayError as exc:
        return None, [Finding("error", f"{local.name}: {exc}")]
    return data, []


def apply_overlay(path: Path, data: dict) -> tuple[dict | None, list[Finding]]:
    """`data` (the parsed base of `path`) with its overlay merged in.

    (None, findings) when the overlay is malformed; the findings name it."""
    overlay, findings = read_overlay(path)
    if findings:
        return None, findings
    if overlay is None:
        return data, []
    return merge(data, overlay), []


def merge(base: dict, overlay: dict) -> dict:
    """A new mapping: `overlay` deep-merged into `base` (neither is changed)."""
    _check_keyed(overlay)
    return _merge_mapping(base, overlay, top=True)


def overlay_keys(overlay: dict) -> list[str]:
    """The dotted names an overlay sets, e.g. `linear_webhook.public_url`
    or `projects[owner/name].linear_project_id`. Names only: never a value,
    and never the logins or user ids that key the trusted_* mappings."""
    names: list[str] = []
    for key, value in overlay.items():
        field = KEYED_LISTS.get(key)
        if field is not None and isinstance(value, list):
            for item in value:
                label = f"{key}[{item[field]}]"
                rest = {k: v for k, v in item.items() if k != field}
                names.extend(_dotted(label, rest) if rest else [label])
        elif key in OPAQUE:
            names.append(str(key))
        else:
            names.extend(_dotted(str(key), value))
    return names


def describe(path: Path) -> str | None:
    """`overlay <file> sets: <names>` for check-config, or None without one."""
    overlay, findings = read_overlay(path)
    if overlay is None or findings:
        return None
    keys = overlay_keys(overlay)
    return f"overlay {overlay_path(path).name} sets: {', '.join(keys) if keys else 'nothing'}"


def _dotted(prefix: str, value: object) -> list[str]:
    if isinstance(value, dict) and value:
        names: list[str] = []
        for key, inner in value.items():
            names.extend(_dotted(f"{prefix}.{key}", inner))
        return names
    return [prefix]


def _check_keyed(overlay: dict) -> None:
    for key, field in KEYED_LISTS.items():
        if key not in overlay:
            continue
        value = overlay[key]
        if not isinstance(value, list):
            raise OverlayError(f"{key} must be a list of entries keyed by {field}")
        for index, item in enumerate(value):
            if not isinstance(item, dict) or not isinstance(item.get(field), str):
                raise OverlayError(f"{key}[{index}] must be a mapping with a {field}")


def _merge_mapping(base: dict, overlay: dict, *, top: bool = False) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        held = merged.get(key)
        if top and key in KEYED_LISTS and isinstance(held, list):
            merged[key] = _merge_keyed(held, value, KEYED_LISTS[key])
        elif isinstance(held, dict) and isinstance(value, dict):
            merged[key] = _merge_mapping(held, value)
        else:
            merged[key] = value
    return merged


def _merge_keyed(base: list, overlay: list, field: str) -> list:
    merged = list(base)
    for item in overlay:
        index = next(
            (i for i, entry in enumerate(merged)
             if isinstance(entry, dict) and entry.get(field) == item[field]),
            None,
        )
        if index is None:
            merged.append(item)
        else:
            merged[index] = _merge_mapping(merged[index], item)
    return merged
