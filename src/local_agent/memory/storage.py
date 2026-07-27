"""Canonical project-memory names, ordering, reads, and append ownership."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..platform.rooted_files import RootedAppendResult
from ..platform.rooted_files import RootedFileError
from ..platform.rooted_files import RootedTextSnapshot
from ..platform.rooted_files import append_rooted_utf8
from ..platform.rooted_files import list_rooted_directory
from ..platform.rooted_files import read_rooted_utf8


PROJECT_MEMORY_NAMES = ("project", "decisions", "conventions", "learned")


class ProjectMemoryStoreError(RuntimeError):
    """Typed project-memory containment or I/O failure."""

    def __init__(self, kind: str, *, workspace_changed: bool = False) -> None:
        super().__init__(kind)
        self.kind = kind
        self.workspace_changed = workspace_changed


@dataclass(frozen=True)
class ProjectMemoryDocument:
    name: str
    lexical_path: Path
    canonical_path: Path
    identity: tuple[int, int]
    text: str


@dataclass(frozen=True)
class ProjectMemoryAppend:
    name: str
    lexical_path: Path
    canonical_path: Path
    identity: tuple[int, int]
    bytes_written: int


class ProjectMemoryStore:
    """The only owner of project-memory names, ordering, and mutation."""

    def __init__(self, workspace: Path) -> None:
        try:
            self.workspace = workspace.expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProjectMemoryStoreError("invalid_workspace") from exc
        if self.workspace != workspace or not self.workspace.is_dir():
            raise ProjectMemoryStoreError("invalid_workspace")
        self.memory_dir = self.workspace / ".local-agent" / "memory"

    def read(self, name: str, *, allow_custom: bool = True) -> ProjectMemoryDocument | None:
        cleaned = _validated_memory_name(name, allow_custom=allow_custom)
        if cleaned is None:
            raise ProjectMemoryStoreError("invalid_memory_name")
        return self._read_file(cleaned, f"{cleaned}.md")

    def _read_file(self, name: str, file_name: str) -> ProjectMemoryDocument | None:
        try:
            snapshot = read_rooted_utf8(self.workspace, self.memory_dir / file_name)
        except RootedFileError as exc:
            if exc.kind == "not_found":
                return None
            raise _store_error(exc) from exc
        return _document(name, snapshot)

    def startup_documents(self) -> tuple[ProjectMemoryDocument, ...]:
        sources = [(name, f"{name}.md") for name in PROJECT_MEMORY_NAMES]
        sources.extend(
            (file_name[:-3], file_name)
            for file_name in self._extra_file_names()
        )
        documents: list[ProjectMemoryDocument] = []
        seen: set[tuple[int, int]] = set()
        for name, file_name in sources:
            try:
                document = self._read_file(name, file_name)
            except ProjectMemoryStoreError:
                continue
            if document is None or document.identity in seen:
                continue
            seen.add(document.identity)
            documents.append(document)
        return tuple(documents)

    def append(
        self,
        name: str,
        text: str,
        *,
        allow_custom: bool = False,
    ) -> ProjectMemoryAppend:
        cleaned = _validated_memory_name(name, allow_custom=allow_custom)
        if cleaned is None:
            raise ProjectMemoryStoreError("invalid_memory_name")
        try:
            result = append_rooted_utf8(self.workspace, self._path(cleaned), text)
        except RootedFileError as exc:
            raise _store_error(exc) from exc
        return _append_result(cleaned, result)

    def _extra_file_names(self) -> tuple[str, ...]:
        try:
            listing = list_rooted_directory(self.workspace, self.memory_dir)
        except RootedFileError as exc:
            if exc.kind == "not_found":
                return ()
            return ()
        priority_files = {f"{name}.md" for name in PROJECT_MEMORY_NAMES}
        names = [
            name
            for name in listing.names
            if name.endswith(".md")
            and name not in priority_files
            and Path(name).name == name
            and name not in {"", ".", ".."}
        ]
        return tuple(sorted(names, key=str.casefold))

    def _path(self, name: str) -> Path:
        return self.memory_dir / f"{name}.md"


def _validated_memory_name(name: str, *, allow_custom: bool) -> str | None:
    cleaned = str(name).strip()
    if allow_custom:
        if not cleaned or cleaned in {".", ".."}:
            return None
        if not all(char.isalnum() or char in {"_", "-"} for char in cleaned):
            return None
        return cleaned
    return cleaned if cleaned in PROJECT_MEMORY_NAMES else None


def _document(name: str, snapshot: RootedTextSnapshot) -> ProjectMemoryDocument:
    return ProjectMemoryDocument(
        name=name,
        lexical_path=snapshot.lexical_path,
        canonical_path=snapshot.canonical_path,
        identity=snapshot.identity,
        text=snapshot.text,
    )


def _append_result(name: str, result: RootedAppendResult) -> ProjectMemoryAppend:
    return ProjectMemoryAppend(
        name=name,
        lexical_path=result.lexical_path,
        canonical_path=result.canonical_path,
        identity=result.identity,
        bytes_written=result.bytes_written,
    )


def _store_error(error: RootedFileError) -> ProjectMemoryStoreError:
    return ProjectMemoryStoreError(
        error.kind,
        workspace_changed=error.workspace_changed,
    )
