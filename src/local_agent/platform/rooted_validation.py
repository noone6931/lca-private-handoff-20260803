from __future__ import annotations

import os

from .rooted_contracts import RootedFileError


def validate_relative_parts(parts: tuple[str, ...]) -> None:
    if not parts:
        raise RootedFileError("invalid_relative_path")
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or "/" in part
            or "\0" in part
            or "\n" in part
            or "\r" in part
        ):
            raise RootedFileError("invalid_relative_path")
        try:
            part.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise RootedFileError("invalid_relative_path") from exc


def validate_mutation_sides(
    before_content: bytes | None,
    after_content: bytes | None,
    before_mode: int | None,
    after_mode: int | None,
) -> None:
    if before_content is None and after_content is None:
        raise RootedFileError("empty_mutation")
    if before_content == after_content:
        raise RootedFileError("empty_mutation")
    for content, mode in (
        (before_content, before_mode),
        (after_content, after_mode),
    ):
        if (content is None) != (mode is None):
            raise RootedFileError("invalid_mutation_state")
        if mode is not None and not 0 <= mode <= 0o777:
            raise RootedFileError("invalid_mutation_state")
    if (
        before_content is not None
        and after_content is not None
        and before_mode != after_mode
    ):
        raise RootedFileError("mode_change_not_supported")


def file_snapshot(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def directory_snapshot(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def identity(file_stat: os.stat_result) -> tuple[int, int]:
    return (file_stat.st_dev, file_stat.st_ino)


def assert_expected_root_identity(
    file_stat: os.stat_result,
    expected_identity: tuple[int, int] | None,
) -> None:
    if expected_identity is not None and identity(file_stat) != tuple(
        expected_identity
    ):
        raise RootedFileError("root_identity_changed")


__all__ = [
    "assert_expected_root_identity",
    "directory_snapshot",
    "file_snapshot",
    "identity",
    "validate_mutation_sides",
    "validate_relative_parts",
]
