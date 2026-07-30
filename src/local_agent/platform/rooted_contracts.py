from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RootedFileError(RuntimeError):
    """A stable fail-closed result for a rooted file operation."""

    def __init__(self, kind: str, *, workspace_changed: bool = False) -> None:
        super().__init__(kind)
        self.kind = kind
        self.workspace_changed = workspace_changed


@dataclass(frozen=True)
class RootedTextSnapshot:
    lexical_path: Path
    canonical_path: Path
    identity: tuple[int, int]
    text: str


@dataclass(frozen=True)
class RootedDirectoryListing:
    lexical_path: Path
    canonical_path: Path
    identity: tuple[int, int]
    names: tuple[str, ...]


@dataclass(frozen=True)
class RootedAppendResult:
    lexical_path: Path
    canonical_path: Path
    identity: tuple[int, int]
    bytes_written: int


@dataclass(frozen=True)
class RootedRegularSnapshot:
    relative_parts: tuple[str, ...]
    content: bytes | None
    mode: int | None
    identity: tuple[int, int] | None

    @property
    def exists(self) -> bool:
        return self.content is not None


@dataclass(frozen=True)
class RootedMutationResult:
    relative_parts: tuple[str, ...]
    workspace_changed: bool


__all__ = [
    "RootedAppendResult",
    "RootedDirectoryListing",
    "RootedFileError",
    "RootedMutationResult",
    "RootedRegularSnapshot",
    "RootedTextSnapshot",
]
