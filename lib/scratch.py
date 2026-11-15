from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def remove(path: Path) -> str | None:
    try:
        _grant_owner_rwx(path)
        shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not remove {path}: {exc}"
    return None


def _grant_owner_rwx(top: Path) -> None:
    if top.is_symlink() or not top.is_dir():
        return
    top.chmod(stat.S_IRWXU)
    for root, directories, _ in os.walk(top):
        for name in directories:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRWXU)
