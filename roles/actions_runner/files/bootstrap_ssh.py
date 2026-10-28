#!/usr/bin/env python3
"""Prepare a dedicated loopback SSH identity without returning key material."""

import base64
import json
import os
from pathlib import Path
import pwd
import stat
import struct
import subprocess
import sys
import tempfile


BASE = Path('/tmp/llm-actions-runner')
COMMENT = 'llm-actions-runner-loopback'


def metadata(path, uid, mode, *, directory=False, optional=False):
    try:
        value = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise ValueError('Required path missing') from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(value.st_mode) or value.st_uid != uid or stat.S_IMODE(value.st_mode) != mode:
        raise ValueError('Unsafe path metadata')
    if not directory and value.st_nlink != 1:
        raise ValueError('Unexpected hard link')
    return True


def public_key(path, uid, mode):
    metadata(path, uid, mode)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode:
            raise ValueError('Unsafe public key metadata')
        data = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(data) > 4096 or len(data.splitlines()) != 1:
        raise ValueError('Invalid public key length')
    fields = data.strip().split()
    if len(fields) < 2 or fields[0] != b'ssh-ed25519':
        raise ValueError('Ed25519 public key required')
    raw = base64.b64decode(fields[1], validate=True)
    if len(raw) != 51 or raw[:4] != struct.pack('!I', 11) or raw[4:15] != b'ssh-ed25519' or raw[15:19] != struct.pack('!I', 32):
        raise ValueError('Invalid Ed25519 public key encoding')
    return fields[1]


def read_regular(path, uid, *, optional=False):
    if not metadata(path, uid, 0o600, optional=optional):
        return b''
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Unsafe protected file metadata')
        data = os.read(descriptor, 1048577)
    finally:
        os.close(descriptor)
    if len(data) > 1048576:
        raise ValueError('Protected file too large')
    return data


def replace_protected(path, content, uid, gid):
    exists = path.exists()
    if exists and read_regular(path, uid) == content:
        return False
    metadata(path, uid, 0o600, optional=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.runner-ssh-', dir=path.parent)
    try:
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def bootstrap():
    if os.getuid() != 0:
        raise ValueError('Root bootstrap required')
    account = pwd.getpwnam('llm-runner')
    if account.pw_uid == 0:
        raise ValueError('Unprivileged runner required')
    metadata(BASE, 0, 0o750, directory=True)
    protected = BASE / 'protected'
    metadata(protected, account.pw_uid, 0o700, directory=True)
    private = protected / 'deploy_ed25519'
    public = protected / 'deploy_ed25519.pub'
    private_exists = metadata(private, account.pw_uid, 0o600, optional=True)
    public_exists = metadata(public, account.pw_uid, 0o600, optional=True)
    for name in ('known_hosts', 'password.bcrypt'):
        metadata(protected / name, account.pw_uid, 0o600, optional=True)
    if private_exists != public_exists:
        raise ValueError('Incomplete SSH identity requires explicit recovery')
    changed = False
    if not private_exists:
        result = subprocess.run(
            ['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', COMMENT, '-f', str(private)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            user=account.pw_uid, group=account.pw_gid, extra_groups=[], umask=0o077,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError('SSH identity generation failed')
        os.chmod(private, 0o600)
        os.chmod(public, 0o600)
        changed = True
    metadata(private, account.pw_uid, 0o600)
    encoded = public_key(public, account.pw_uid, 0o600)
    root_ssh = Path('/root/.ssh')
    if not root_ssh.exists():
        root_ssh.mkdir(mode=0o700)
        changed = True
    metadata(root_ssh, 0, 0o700, directory=True)
    authorized = root_ssh / 'authorized_keys'
    previous = read_regular(authorized, 0, optional=True)
    restricted = b'from="127.0.0.1",restrict ssh-ed25519 ' + encoded + b' ' + COMMENT.encode() + b'\n'
    # Preserve unrelated keys. Normalize every occurrence of this dedicated
    # key into one restricted entry, including a prior unrestricted entry.
    lines = [line for line in previous.splitlines(keepends=True)
             if line.lstrip().startswith(b'#') or encoded not in line.split()]
    contents = b''.join(lines)
    if contents and not contents.endswith(b'\n'):
        contents += b'\n'
    changed |= replace_protected(authorized, contents + restricted, 0, 0)
    host_key = public_key(Path('/etc/ssh/ssh_host_ed25519_key.pub'), 0, 0o644)
    known_hosts = b'127.0.0.1 ssh-ed25519 ' + host_key + b'\n'
    changed |= replace_protected(protected / 'known_hosts', known_hosts, account.pw_uid, account.pw_gid)
    return {'changed': changed}


if __name__ == '__main__':
    try:
        print(json.dumps(bootstrap()))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('runner_ssh_bootstrap_failed', file=sys.stderr)
        raise SystemExit(1) from None
