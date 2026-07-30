from __future__ import annotations

import os
import stat
from pathlib import Path

from .rooted_contracts import RootedFileError
from .rooted_validation import assert_expected_root_identity


def creation_parent(
    canonical_root: Path,
    lexical_parent: Path,
) -> tuple[Path, tuple[str, ...]]:
    missing: list[str] = []
    candidate = lexical_parent
    while True:
        try:
            candidate.lstat()
        except FileNotFoundError:
            if candidate == canonical_root:
                raise RootedFileError("root_missing")
            missing.insert(0, candidate.name)
            candidate = candidate.parent
            continue
        except OSError as exc:
            raise RootedFileError("path_inspection_failed") from exc
        try:
            ancestor = candidate.resolve(strict=True)
            ancestor.relative_to(canonical_root)
        except FileNotFoundError as exc:
            raise RootedFileError("dangling_symlink") from exc
        except RuntimeError as exc:
            raise RootedFileError("symlink_loop") from exc
        except (OSError, ValueError) as exc:
            raise RootedFileError("outside_root") from exc
        if not ancestor.is_dir():
            raise RootedFileError("not_directory")
        return ancestor, tuple(missing)


def canonical_root(
    root: Path,
    expected_identity: tuple[int, int] | None,
) -> Path:
    try:
        inspected = root.expanduser().lstat()
        assert_expected_root_identity(inspected, expected_identity)
        canonical = root.expanduser().resolve(strict=True)
        if canonical != root:
            raise RootedFileError("root_not_canonical")
        resolved = canonical.lstat()
        if not stat.S_ISDIR(resolved.st_mode):
            raise RootedFileError("root_not_directory")
        assert_expected_root_identity(resolved, expected_identity)
        return canonical
    except RootedFileError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("invalid_root") from exc


def lexical_under_root(canonical_root: Path, lexical_path: Path) -> Path:
    try:
        lexical = Path(os.path.abspath(lexical_path))
        lexical.relative_to(canonical_root)
        if lexical.name in {"", ".", ".."}:
            raise ValueError
        return lexical
    except (OSError, RuntimeError, ValueError) as exc:
        raise RootedFileError("outside_root") from exc


def resolve_existing(canonical_root: Path, lexical: Path) -> Path:
    try:
        canonical = lexical.resolve(strict=True)
        canonical.relative_to(canonical_root)
        return canonical
    except FileNotFoundError as exc:
        try:
            lexical.lstat()
        except FileNotFoundError:
            creation_parent(canonical_root, lexical.parent)
            raise RootedFileError("not_found") from exc
        except OSError as inspection_error:
            raise RootedFileError("path_inspection_failed") from inspection_error
        raise RootedFileError("dangling_symlink") from exc
    except RuntimeError as exc:
        raise RootedFileError("symlink_loop") from exc
    except (OSError, ValueError) as exc:
        raise RootedFileError("outside_root") from exc


__all__ = [
    "canonical_root",
    "creation_parent",
    "lexical_under_root",
    "resolve_existing",
]
