#!/usr/bin/env python3
"""Create a short-lived GitHub runner token directly in a protected file."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path


def valid_repository(value):
    return (re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
            and all(part not in (".", "..") for part in value.split("/")))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def protected_parent(target):
    """Pin every directory by descriptor; never follow a symlink."""
    if not target.is_absolute() or ".." in target.parts:
        raise ValueError("output path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in target.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            shared_temp = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not shared_temp):
                raise ValueError("output ancestor metadata")
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("output parent metadata")
        return fd
    except BaseException:
        os.close(fd)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not valid_repository(args.repository):
        raise ValueError("repository")
    target = args.output
    # Preserve the caller's existing gh authentication context, but not proxy,
    # TLS override, tracing, pager, or arbitrary GitHub endpoint overrides.
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "GH_CONFIG_DIR", "XDG_CONFIG_HOME",
               "GH_TOKEN", "GITHUB_TOKEN")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1",
               GH_NO_EXTENSION_UPDATE_NOTIFIER="1")
    executable = shutil.which("gh", path=env.get("PATH", os.defpath))
    if not executable:
        raise ValueError("GitHub CLI unavailable")
    directory = protected_parent(target)
    created = None
    completed = False
    try:
        # Reserve the protected destination before the token-creating API call.
        fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "w") as destination:
            created = os.fstat(destination.fileno())
            if (created.st_uid != os.getuid() or created.st_nlink != 1
                    or stat.S_IMODE(created.st_mode) != 0o600):
                raise ValueError("output metadata")
            result = subprocess.run(
                [executable, "api", "--hostname", "github.com", "--method", "POST",
                 f"repos/{args.repository}/actions/runners/registration-token"],
                env=env, capture_output=True, check=False, timeout=60,
            )
            if result.returncode:
                raise RuntimeError("registration request")
            if len(result.stdout) > 16384:
                raise ValueError("registration response size")
            data = json.loads(result.stdout, object_pairs_hook=unique_object)
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{16,1024}", token):
                raise ValueError("registration response")
            json.dump({"token": token}, destination)
            destination.flush()
            os.fsync(destination.fileno())
            completed = True
    finally:
        if created is not None and not completed:
            current = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                os.unlink(target.name, dir_fd=directory)
        os.close(directory)
    print("Short-lived registration token saved to the protected file; value suppressed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Runner token preparation failed; no credential or response was displayed.") from None
