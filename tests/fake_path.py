"""A PATH dir that mirrors real bin dirs minus a few names, for tests that hide a tool."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


def link_path_without(bare: Path, directories: Iterable[str | os.PathLike[str]],
                      skip: set[str]) -> Path:
    """lexists, not exists: a dangling link (as /bin and /usr/bin may both list) fails exists()."""
    bare.mkdir(exist_ok=True)
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            link = bare / name
            if name not in skip and not os.path.lexists(link):
                link.symlink_to(os.path.join(directory, name))
    return bare
