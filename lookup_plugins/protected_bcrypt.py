"""Read a controller-owned bcrypt hash without exposing its contents in errors."""

import os
import re
import stat
from pathlib import Path

from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase


def read_bcrypt(path, workspace):
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Hash reference must be an absolute path without parent traversal")
    if candidate.is_relative_to(Path(workspace).resolve()):
        raise ValueError("Hash file must be outside the repository")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in candidate.parts[1:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            directory_info = os.fstat(descriptor)
            shared_sticky = directory_info.st_uid == 0 and directory_info.st_mode & stat.S_ISVTX
            if directory_info.st_uid not in (0, os.getuid()) or (
                directory_info.st_mode & 0o022 and not shared_sticky
            ):
                raise ValueError("Hash parent directories have unsafe ownership or write permissions")
        parent_info = os.fstat(descriptor)
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise ValueError("Hash file must be inside a controller-owned private directory")
        file_descriptor = os.open(
            candidate.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Hash must be a regular file")
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o177:
                raise ValueError("Hash file must be controller-owned, non-executable and owner-only")
            value = os.read(file_descriptor, 128)
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)
    # Require a bcrypt cost of 12-16; reject multiline snippets or Caddy syntax.
    if not re.fullmatch(rb"\$2[aby]\$(?:1[2-6])\$[./A-Za-z0-9]{53}\n?", value):
        raise ValueError("Hash file must contain exactly one approved bcrypt hash")
    return value.decode("ascii").strip()


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        try:
            return [read_bcrypt(term, Path(__file__).resolve().parents[1]) for term in terms]
        except (OSError, ValueError, TypeError) as error:
            raise AnsibleError("Protected bcrypt file validation failed; check its reference and permissions") from error
