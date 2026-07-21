from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .transaction import ExistingTextFileChange, apply_existing_text_transaction

PATCH_MODES = {"replace", "insert_before", "insert_after"}


class PatchError(RuntimeError):
    """Raised when an anchored patch cannot be applied safely."""

    def __init__(self, message: str, *, transaction_result: object | None = None) -> None:
        super().__init__(message)
        self.transaction_result = transaction_result


@dataclass(frozen=True)
class PatchResult:
    diff: str
    new_tag: str
    effective_start_line: int
    effective_end_line: int
    before_bytes: bytes
    after_bytes: bytes


def hash_text(text: str) -> str:
    normalized = _normalize_to_lf(_strip_bom(text)[1])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]


def format_tagged_read(
    path: Path,
    workspace: Path,
    text: str,
    start_line: int = 1,
    allowed_roots: tuple[Path, ...] = (),
) -> str:
    rel = display_workspace_path(workspace, path, allowed_roots)
    tag = hash_text(text)
    lines = text.splitlines()
    rendered = [f"[{rel}#{tag}]", f"tag: {tag}"]
    for index, line in enumerate(lines, start=1):
        if index >= start_line:
            rendered.append(f"{index}:{line}")
    return "\n".join(rendered)


def apply_anchored_patch(
    *,
    workspace: Path,
    path: str,
    tag: str,
    start_line: int,
    end_line: int,
    old_text: str,
    new_text: str,
    mode: str = "replace",
    dry_run: bool = False,
    allowed_roots: tuple[Path, ...] = (),
) -> PatchResult:
    if mode not in PATCH_MODES:
        raise PatchError(f"Invalid patch mode: {mode}. Use one of: {', '.join(sorted(PATCH_MODES))}.")
    target = resolve_workspace_path(workspace, path, allowed_roots)
    if not target.exists():
        raise PatchError(f"Target file does not exist: {path}")
    raw_original_bytes = target.read_bytes()
    raw_original = raw_original_bytes.decode("utf-8")
    bom, original = _strip_bom(raw_original)
    line_ending = _detect_line_ending(original)
    normalized_original = _normalize_to_lf(original)
    current_tag = hash_text(raw_original)
    if current_tag != tag:
        raise PatchError(
            f"File changed since it was read: expected tag {tag}, current tag {current_tag}. Re-read the file."
        )
    if start_line < 1 or end_line < start_line:
        raise PatchError("Invalid line range.")

    lines = normalized_original.splitlines(keepends=True)
    if end_line > len(lines):
        raise PatchError(f"Line range exceeds file length: {start_line}-{end_line}.")
    old_slice = "".join(lines[start_line - 1 : end_line])
    normalized_old = _normalize_to_match_existing(old_text, old_slice)
    if old_slice != normalized_old:
        start_line, end_line = _recover_exact_unique_anchor(lines, old_text)
        old_slice = "".join(lines[start_line - 1 : end_line])
        normalized_old = _normalize_to_match_existing(old_text, old_slice)
        if old_slice != normalized_old:
            raise PatchError("Exact anchor recovery did not preserve old_text. Re-read the file.")

    new_lines = _apply_patch_mode(
        lines=lines,
        start_line=start_line,
        end_line=end_line,
        old_slice=old_slice,
        new_text=new_text,
        mode=mode,
    )
    updated = "".join(new_lines)
    diff = "".join(
        difflib.unified_diff(
            normalized_original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    persisted = bom + _restore_line_endings(updated, line_ending)
    if not dry_run:
        transaction = apply_existing_text_transaction(
            (ExistingTextFileChange.create(target, raw_original_bytes, persisted.encode("utf-8")),)
        )
        if transaction.status != "committed":
            suffix = " Workspace content may have changed." if transaction.workspace_changed else ""
            raise PatchError(
                f"Anchored patch transaction failed: {transaction.error_kind or transaction.status}.{suffix}",
                transaction_result=transaction,
            )
    return PatchResult(
        diff=diff,
        new_tag=hash_text(persisted),
        effective_start_line=start_line,
        effective_end_line=end_line,
        before_bytes=raw_original_bytes,
        after_bytes=persisted.encode("utf-8"),
    )


def _apply_patch_mode(
    *,
    lines: list[str],
    start_line: int,
    end_line: int,
    old_slice: str,
    new_text: str,
    mode: str,
) -> list[str]:
    if mode == "replace":
        replacement = _normalize_to_match_existing(new_text, old_slice)
        return lines[: start_line - 1] + replacement.splitlines(keepends=True) + lines[end_line:]

    insertion = _normalize_insert_text(new_text)
    insertion_lines = insertion.splitlines(keepends=True)
    if mode == "insert_before":
        return lines[: start_line - 1] + insertion_lines + lines[start_line - 1 :]
    if mode == "insert_after":
        prefix = lines[:end_line]
        suffix = lines[end_line:]
        if prefix and not prefix[-1].endswith("\n"):
            prefix = [*prefix[:-1], prefix[-1] + "\n"]
        return prefix + insertion_lines + suffix
    raise PatchError(f"Invalid patch mode: {mode}.")


def resolve_workspace_path(
    workspace: Path,
    raw_path: str,
    allowed_roots: tuple[Path, ...] = (),
) -> Path:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        candidate = candidate.resolve()
    else:
        candidate = (workspace / candidate).resolve()
    if _is_under_any_root(candidate, _normalized_roots(workspace, allowed_roots)):
        return candidate
    raise PatchError(_path_escape_message(workspace, raw_path, candidate, allowed_roots))


def display_workspace_path(
    workspace: Path,
    path: Path,
    allowed_roots: tuple[Path, ...] = (),
) -> str:
    resolved = path.resolve()
    workspace = workspace.resolve()
    try:
        return str(resolved.relative_to(workspace))
    except ValueError:
        pass
    for root in _normalized_roots(workspace, allowed_roots)[1:]:
        try:
            resolved.relative_to(root)
            return str(resolved)
        except ValueError:
            continue
    return str(resolved)


def _normalized_roots(workspace: Path, allowed_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots = [workspace.resolve()]
    for root in allowed_roots:
        resolved = root.expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _path_escape_message(
    workspace: Path,
    raw_path: str,
    candidate: Path,
    allowed_roots: tuple[Path, ...],
) -> str:
    roots = _normalized_roots(workspace, allowed_roots)
    lines = [
        f"Path escapes workspace and allowed directories: {raw_path}",
        f"Resolved path: {candidate}",
        "Workspace roots:",
        f"- Primary workspace (--cwd): {roots[0]}",
    ]
    if len(roots) > 1:
        lines.append("- Additional allowed directories; use these exact absolute paths for external docs/specs/code:")
        lines.extend(f"  - {root}" for root in roots[1:])
    if _is_parent_of(candidate, roots[0]):
        lines.append(
            "The requested path is a parent of the primary workspace. "
            "Use '.' for the primary workspace, or use the exact primary workspace path above."
        )
    else:
        lines.append("Use a relative path inside the primary workspace, or one of the exact allowed roots above.")
    return "\n".join(lines)


def _is_parent_of(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return parent != child
    except ValueError:
        return False


def _is_under_any_root(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _normalize_to_match_existing(text: str, existing: str) -> str:
    normalized = _normalize_to_lf(text)
    if existing == "\n" and normalized == "":
        return "\n"
    if existing.endswith("\n") and normalized and not normalized.endswith("\n"):
        normalized += "\n"
    if not existing.endswith("\n") and normalized.endswith("\n"):
        normalized = normalized[:-1]
    return normalized


def _normalize_insert_text(text: str) -> str:
    normalized = _normalize_to_lf(text)
    if normalized == "":
        raise PatchError("insert_before/insert_after require non-empty new_text.")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _recover_exact_unique_anchor(lines: list[str], old_text: str) -> tuple[int, int]:
    expected = _normalized_anchor_lines(old_text)
    existing = tuple(_line_content(line) for line in lines)
    width = len(expected)
    matches = [
        (index + 1, index + width)
        for index in range(len(existing) - width + 1)
        if existing[index : index + width] == expected
    ]
    if not matches:
        raise PatchError(
            "old_text does not match the authored range and no complete exact anchor exists in the tagged file. "
            "Re-read the file."
        )
    if len(matches) != 1:
        raise PatchError(
            "old_text does not match the authored range and occurs in multiple exact locations in the tagged file. "
            "Refusing ambiguous recovery; use the precise range from a fresh read."
        )
    return matches[0]


def _normalized_anchor_lines(text: str) -> tuple[str, ...]:
    normalized = _normalize_to_lf(text)
    if normalized == "":
        return ("",)
    return tuple(normalized.splitlines())


def _line_content(line: str) -> str:
    return line[:-1] if line.endswith("\n") else line


def _detect_line_ending(text: str) -> str:
    crlf_index = text.find("\r\n")
    lf_index = text.find("\n")
    if lf_index == -1:
        return "\n"
    if crlf_index == -1:
        return "\n"
    return "\r\n" if crlf_index < lf_index else "\n"


def _normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_line_endings(text: str, line_ending: str) -> str:
    return text.replace("\n", "\r\n") if line_ending == "\r\n" else text


def _strip_bom(text: str) -> tuple[str, str]:
    if text.startswith("\ufeff"):
        return "\ufeff", text[1:]
    return "", text
