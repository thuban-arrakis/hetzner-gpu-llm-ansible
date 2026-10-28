#!/usr/bin/env python3
"""Generate HTTPS client credentials directly into protected local files."""

import argparse
import ipaddress
import os
import re
import secrets
import stat
from pathlib import Path

import bcrypt


def create_credentials(directory, address, username):
    destination = Path(directory)
    workspace = Path(__file__).resolve().parents[1]
    if not destination.is_absolute() or ".." in destination.parts:
        raise ValueError("Use an absolute output directory without parent traversal")
    if destination.is_relative_to(workspace):
        raise ValueError("Credential directory must be outside the repository")
    parsed_address = ipaddress.ip_address(address)
    if parsed_address.version != 4 or not parsed_address.is_global:
        raise ValueError("Use the server's public IPv4 address")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", username):
        raise ValueError("Use a simple lowercase API username")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in destination.parts[1:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            shared_sticky = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
            if metadata.st_uid not in (0, os.getuid()) or (metadata.st_mode & 0o022 and not shared_sticky):
                raise ValueError("Output parents have unsafe ownership or write permissions")
        # Refuse to overwrite existing material, including symlinks.
        os.mkdir(destination.name, mode=0o700, dir_fd=descriptor)
        child = os.open(destination.name, flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("Output directory must be owned by the controller with mode 0700")
        password = secrets.token_urlsafe(32)
        password_hash = bcrypt.hashpw(password.encode("ascii"), bcrypt.gensalt(rounds=12)).decode("ascii")
        files = {
            "password.bcrypt": password_hash + "\n",
            "client.curl": (
                f'url = "https://{address}/api/chat"\n'
                f'user = "{username}:{password}"\n'
                'proto = "=https"\n'
                'proto-redir = "=https"\n'
                'max-redirs = 0\n'
                'no-insecure\nno-location\nno-location-trusted\n'
                'connect-timeout = 10\n'
                'max-time = 310\n'
                'fail\nsilent\nshow-error\n'
            ),
        }
        for name, content in files.items():
            file_descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=descriptor,
            )
            with os.fdopen(file_descriptor, "w", encoding="ascii") as output:
                output.write(content)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--username", default="llm")
    arguments = parser.parse_args()
    try:
        create_credentials(arguments.directory, arguments.address, arguments.username)
    except (OSError, ValueError):
        parser.exit(1, "Credential creation failed; use a new protected directory outside the repository and valid public IPv4 address.\n")
    print("Created protected password.bcrypt and client.curl files; no credential values were displayed.")
