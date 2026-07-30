from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


class IsolationStateAuthorityError(RuntimeError):
    """The state directory could not be kept outside workspace authority."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass
class IsolationStateAuthority:
    canonical_path: Path | None
    directory_fd: int
    identity: tuple[int, int] | None

    @property
    def forbidden_directory_identities(self) -> frozenset[tuple[int, int]]:
        return frozenset((self.identity,)) if self.identity is not None else frozenset()

    def close(self) -> None:
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1


def acquire_isolation_state_authority(
    state_dir: Path | None,
    *,
    workspace_roots: tuple[Path, ...],
) -> IsolationStateAuthority:
    if state_dir is None:
        return IsolationStateAuthority(None, -1, None)
    directory_fd = -1
    try:
        canonical = state_dir.resolve(strict=True)
        directory_fd = os.open(
            canonical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | os.O_DIRECTORY
            | os.O_NOFOLLOW,
        )
        opened = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise IsolationStateAuthorityError("isolation_state_authority_unavailable")
        identity = (opened.st_dev, opened.st_ino)
        current = os.stat(canonical, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise IsolationStateAuthorityError("isolation_state_authority_changed")
        roots = tuple(root.resolve(strict=True) for root in workspace_roots)
        if any(
            canonical == root
            or canonical.is_relative_to(root)
            or root.is_relative_to(canonical)
            for root in roots
        ):
            raise IsolationStateAuthorityError("isolation_state_authority_overlap")
        return IsolationStateAuthority(canonical, directory_fd, identity)
    except IsolationStateAuthorityError:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise IsolationStateAuthorityError(
            "isolation_state_authority_unavailable"
        ) from exc


__all__ = [
    "IsolationStateAuthority",
    "IsolationStateAuthorityError",
    "acquire_isolation_state_authority",
]
