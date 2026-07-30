from __future__ import annotations

import os
import stat

from .container_staging_contracts import ContainerStagingError


def assert_private_regular(descriptor: int, kind: str) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise ContainerStagingError(kind)


def remove_directory_contents(
    directory_fd: int,
    *,
    open_directory_flags: int,
) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            os.chmod(name, 0o700, dir_fd=directory_fd, follow_symlinks=False)
            child_fd = os.open(
                name,
                open_directory_flags,
                dir_fd=directory_fd,
            )
            try:
                remove_directory_contents(
                    child_fd,
                    open_directory_flags=open_directory_flags,
                )
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


__all__ = ["assert_private_regular", "remove_directory_contents"]
