from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


MAX_SESSION_ROOTS = 16


class WorkspaceContextError(ValueError):
    """Raised when a session workspace-root change would broaden access unsafely."""


@dataclass
class WorkspaceContext:
    """Mutable session roots while the configured primary workspace stays fixed."""

    primary: Path
    configured_roots: tuple[Path, ...] = ()
    session_roots: tuple[Path, ...] = ()
    revision: int = 0
    _configured: tuple[Path, ...] = field(init=False, repr=False)
    _session: list[Path] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.primary = _canonical_directory(self.primary, label="primary workspace")
        self._configured = _normalize_configured_roots(self.primary, self.configured_roots)
        self._session = []
        initial_roots = tuple(self.session_roots)
        self.session_roots = ()
        for root in initial_roots:
            try:
                self.add_session_root(str(root))
            except WorkspaceContextError:
                continue
        self.revision = max(0, int(self.revision))

    @property
    def additional_roots(self) -> tuple[Path, ...]:
        return (*self._configured, *self._session)

    @property
    def all_roots(self) -> tuple[Path, ...]:
        return (self.primary, *self.additional_roots)

    @property
    def configured(self) -> tuple[Path, ...]:
        return self._configured

    @property
    def session(self) -> tuple[Path, ...]:
        return tuple(self._session)

    def add_session_root(self, raw_path: str) -> tuple[Path, bool]:
        candidate = self._resolve_root(raw_path, require_exists=True)
        known = self.all_roots
        for root in known:
            if candidate == root or _is_within(candidate, root):
                return root, False
            if _is_within(root, candidate):
                raise WorkspaceContextError(
                    f"Refusing workspace root {candidate}: it contains existing allowed root {root} and would broaden access."
                )
        if len(self._session) >= MAX_SESSION_ROOTS:
            raise WorkspaceContextError(f"A session can add at most {MAX_SESSION_ROOTS} workspace roots.")
        self._session.append(candidate)
        self.session_roots = tuple(self._session)
        self.revision += 1
        return candidate, True

    def remove_session_root(self, raw_path: str) -> tuple[Path, bool]:
        candidate = self._resolve_root(raw_path, require_exists=False)
        if candidate == self.primary:
            raise WorkspaceContextError("The primary workspace cannot be removed. Use /move in T-128B instead.")
        if candidate in self._configured:
            raise WorkspaceContextError("Configured roots can only be changed through startup configuration.")
        if candidate not in self._session:
            raise WorkspaceContextError(f"Not a session workspace root: {candidate}")
        self._session.remove(candidate)
        self.session_roots = tuple(self._session)
        self.revision += 1
        return candidate, True

    def reset_session_roots(self) -> bool:
        if not self._session:
            return False
        self._session.clear()
        self.session_roots = ()
        self.revision += 1
        return True

    def restore_session_roots(self, paths: tuple[str, ...], revision: int) -> tuple[Path, ...]:
        self._session.clear()
        self.session_roots = ()
        missing: list[Path] = []
        for raw_path in paths:
            candidate = Path(raw_path).expanduser()
            if not candidate.exists() or not candidate.is_dir():
                missing.append(candidate)
                continue
            try:
                self.add_session_root(str(candidate))
            except WorkspaceContextError:
                continue
        self.revision = max(0, int(revision))
        return tuple(missing)

    def snapshot(self, *, operation: str, path: Path | None = None, changed: bool = True) -> dict[str, object]:
        return {
            "operation": operation,
            "path": str(path) if path is not None else None,
            "changed": changed,
            "primary": str(self.primary),
            "configured_roots": [str(root) for root in self._configured],
            "session_roots": [str(root) for root in self._session],
            "revision": self.revision,
        }

    def summary(self) -> str:
        lines = [f"Workspace roots (revision {self.revision}):", f"- primary: {self.primary}"]
        if self._configured:
            lines.extend(f"- configured: {root}" for root in self._configured)
        else:
            lines.append("- configured: none")
        if self._session:
            lines.extend(f"- session: {root}" for root in self._session)
        else:
            lines.append("- session: none")
        return "\n".join(lines)

    def _resolve_root(self, raw_path: str, *, require_exists: bool) -> Path:
        value = (raw_path or "").strip()
        if not value:
            raise WorkspaceContextError("Workspace root path is required.")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.primary / path
        if path == Path(path.anchor):
            raise WorkspaceContextError("Refusing to use the filesystem root as a workspace root.")
        if require_exists:
            return _canonical_directory(path, label="workspace root")
        return path.resolve()


def _normalize_configured_roots(primary: Path, roots: tuple[Path, ...]) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for raw_root in roots:
        root = _canonical_directory(raw_root, label="configured root")
        if root == primary or _is_within(root, primary):
            continue
        if any(root == known or _is_within(root, known) for known in normalized):
            continue
        if any(_is_within(known, root) for known in normalized):
            raise WorkspaceContextError(
                f"Configured root {root} contains an existing configured root and would broaden access."
            )
        normalized.append(root)
    return tuple(normalized)


def _canonical_directory(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise WorkspaceContextError(f"{label.capitalize()} must be an existing directory: {resolved}")
    if resolved == Path(resolved.anchor):
        raise WorkspaceContextError(f"Refusing to use the filesystem root as {label}.")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = ["MAX_SESSION_ROOTS", "WorkspaceContext", "WorkspaceContextError"]
