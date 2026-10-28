#!/usr/bin/env python3
"""Validate the runner-owned inventory reference without printing its contents."""

import os
import stat
from pathlib import Path


def validate_inventory(path: str, workspace: str) -> Path:
    candidate = Path(path)
    if not path or not candidate.is_absolute():
        raise ValueError("LLM_INVENTORY_FILE must be an absolute path")
    if candidate.suffix not in (".yml", ".yaml"):
        raise ValueError("Inventory must be a static YAML file")
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise ValueError("Inventory path must not contain symlinks")
    info = candidate.lstat()
    if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
        raise ValueError("Inventory must be a regular file, not a symlink")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Inventory must be owned by the runner and have mode 0600 or stricter")
    if stat.S_IMODE(info.st_mode) & 0o111:
        raise ValueError("Executable dynamic inventories are not accepted")
    resolved = candidate.resolve()
    if resolved.is_relative_to(Path(workspace).resolve()):
        raise ValueError("Deployment inventory must be outside the checked-out repository")
    return resolved


if __name__ == "__main__":
    validate_inventory(os.environ.get("LLM_INVENTORY_FILE", ""), os.environ["GITHUB_WORKSPACE"])
    print("Protected controller inventory metadata verified.")
