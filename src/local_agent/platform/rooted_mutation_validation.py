from __future__ import annotations

import os
import stat
from collections.abc import Callable

from .rooted_contracts import RootedFileError
from .rooted_validation import file_snapshot
from .rooted_validation import identity


def assert_open_regular_unchanged(
    descriptor: int,
    parent_fd: int,
    name: str,
    opened: os.stat_result,
    before_content: bytes,
    before_mode: int | None,
    read_exact: Callable[[int, int], bytes],
) -> None:
    current = os.fstat(descriptor)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or file_snapshot(current) != file_snapshot(opened)
        or stat.S_IMODE(current.st_mode) != before_mode
    ):
        raise RootedFileError("stale_before_image")
    content = read_exact(descriptor, current.st_size)
    completed = os.fstat(descriptor)
    if file_snapshot(completed) != file_snapshot(current) or content != before_content:
        raise RootedFileError("stale_before_image")
    path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if file_snapshot(path) != file_snapshot(completed):
        raise RootedFileError("path_identity_changed")


def assert_displaced_regular_matches_before(
    descriptor: int,
    parent_fd: int,
    name: str,
    opened: os.stat_result,
    before_content: bytes,
    before_mode: int | None,
    read_exact: Callable[[int, int], bytes],
) -> None:
    current = os.fstat(descriptor)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or identity(current) != identity(opened)
        or current.st_size != opened.st_size
        or current.st_mtime_ns != opened.st_mtime_ns
        or stat.S_IMODE(current.st_mode) != before_mode
    ):
        raise RootedFileError("stale_before_image")
    content = read_exact(descriptor, current.st_size)
    completed = os.fstat(descriptor)
    if file_snapshot(completed) != file_snapshot(current) or content != before_content:
        raise RootedFileError("stale_before_image")
    path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if file_snapshot(path) != file_snapshot(completed):
        raise RootedFileError("path_identity_changed")


def assert_same_regular(
    inspected: os.stat_result,
    opened: os.stat_result,
) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise RootedFileError("not_regular")
    if identity(opened) != identity(inspected):
        raise RootedFileError("file_identity_changed")


__all__ = [
    "assert_displaced_regular_matches_before",
    "assert_open_regular_unchanged",
    "assert_same_regular",
]
