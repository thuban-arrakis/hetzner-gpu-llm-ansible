#!/usr/bin/env python3
"""Create a host-local Grafana signing key without emitting its value."""

import os
from pathlib import Path
import secrets
import stat


KEY_PATH = Path("/var/lib/llm-monitoring/grafana-secret-key")


def main():
    try:
        parent = KEY_PATH.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
            return 1
        try:
            descriptor = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        except FileExistsError:
            current = KEY_PATH.lstat()
            valid = (
                stat.S_ISREG(current.st_mode)
                and current.st_uid == 472
                and current.st_gid == 472
                and stat.S_IMODE(current.st_mode) == 0o400
                and current.st_nlink == 1
                and current.st_size == 65
            )
            return 0 if valid else 1
        with os.fdopen(descriptor, "wb") as output:
            output.write((secrets.token_hex(32) + "\n").encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
            os.fchown(output.fileno(), 472, 472)
            os.fchmod(output.fileno(), 0o400)
        return 2
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
