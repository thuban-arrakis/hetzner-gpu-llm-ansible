#!/usr/bin/env python3
"""Register the remote demo runner without a token in arguments or output."""

import argparse
import json
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path

BASE = Path("/tmp/llm-actions-runner")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def safe_directory(path, private=True):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path")
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        shared_temp = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or (info.st_mode & 0o022 and not shared_temp)):
            raise ValueError("directory ancestor metadata")
    if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("private directory metadata")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository)
            or any(part in (".", "..") for part in args.repository.split("/"))):
        raise ValueError("repository")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.name) or os.getuid() == 0:
        raise ValueError("runner identity")
    account = pwd.getpwuid(os.getuid())
    if (account.pw_name != "llm-runner" or account.pw_dir != str(BASE / "home")
            or os.environ.get("HOME") != account.pw_dir):
        raise ValueError("runner account home")
    safe_directory(BASE, private=False)
    base_info = BASE.lstat()
    if not ((base_info.st_uid == 0 and base_info.st_gid == account.pw_gid
             and stat.S_IMODE(base_info.st_mode) == 0o750)
            or (base_info.st_uid == os.getuid() and stat.S_IMODE(base_info.st_mode) == 0o700)):
        raise ValueError("runner base metadata")
    for name in ("runner", "protected", "home", "tmp", "cache", "work"):
        safe_directory(BASE / name)
    runner = BASE / "runner"
    for name in (".runner", ".credentials", ".credentials_rsaparams"):
        if os.path.lexists(runner / name):
            raise ValueError("runner already configured; inspect registration before retrying")
    script = runner / "config.sh"
    script_info = script.lstat()
    if (not stat.S_ISREG(script_info.st_mode) or script_info.st_uid != os.getuid()
            or script_info.st_nlink != 1 or script_info.st_mode & 0o022
            or not script_info.st_mode & stat.S_IXUSR):
        raise ValueError("runner configuration script metadata")
    source = BASE / "protected/registration-token.json"
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise ValueError("credential path")
    before = source.lstat()
    parent = source.parent.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
            or not 0 < before.st_size <= 4096 or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError("credential metadata")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_ino, opened.st_dev, opened.st_mode, opened.st_uid, opened.st_nlink, opened.st_size) != (
                before.st_ino, before.st_dev, before.st_mode, before.st_uid, before.st_nlink, before.st_size):
            raise ValueError("credential changed")
        data = json.loads(stream.read(4097), object_pairs_hook=unique_object)
        after = os.fstat(stream.fileno())
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ValueError("credential changed")
    token = data.get("token") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or set(data) != {"token"} or not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9._~-]{16,1024}", token)):
        raise ValueError("credential schema")
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
        "HOME": os.environ["HOME"], "TMPDIR": str(BASE / "tmp"),
        "XDG_CACHE_HOME": str(BASE / "cache"), "DOTNET_CLI_HOME": str(BASE / "home"),
        "ACTIONS_RUNNER_INPUT_TOKEN": token,
    }
    previous_umask = os.umask(0o077)
    try:
        result = subprocess.run(
            [str(script), "--unattended", "--url",
             f"https://github.com/{args.repository}", "--name", args.name,
             "--labels", "llm-deploy", "--work", str(BASE / "work")],
            cwd=runner, env=env, capture_output=True, check=False, timeout=180,
        )
    finally:
        env.pop("ACTIONS_RUNNER_INPUT_TOKEN", None)
        os.umask(previous_umask)
    if result.returncode:
        raise RuntimeError("runner registration failed; inspect remote registration before retrying")
    for name in (".runner", ".credentials", ".credentials_rsaparams"):
        path = runner / name
        credential_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(credential_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError("runner credential metadata")
            os.fchmod(credential_fd, 0o600)
        finally:
            os.close(credential_fd)
    current = source.lstat()
    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
        raise ValueError("credential changed")
    source.unlink()
    print("Remote repository runner registered; temporary registration token removed.")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except Exception:
        raise SystemExit("Runner registration did not complete; credential and command output suppressed.") from None
