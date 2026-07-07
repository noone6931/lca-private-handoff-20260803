from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

PATCH_MODES = {"replace", "insert_before", "insert_after"}


class PatchError(RuntimeError):
    """Raised when an anchored patch cannot be applied safely."""


@dataclass(frozen=True)
class PatchResult:
    diff: str
    new_tag: str


def hash_text(text: str) -> str:
    normalized = _normalize_to_lf(_strip_bom(text)[1])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]


def format_tagged_read(path: Path, workspace: Path, text: str, start_line: int = 1) -> str:
    rel = path.relative_to(workspace)
    tag = hash_text(text)
    lines = text.splitlines()
    rendered = [f"[{rel}#{tag}]"]
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
) -> PatchResult:
    if mode not in PATCH_MODES:
        raise PatchError(f"Invalid patch mode: {mode}. Use one of: {', '.join(sorted(PATCH_MODES))}.")
    target = resolve_workspace_path(workspace, path)
    if not target.exists():
        raise PatchError(f"Target file does not exist: {path}")
    raw_original = target.read_bytes().decode("utf-8")
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
        raise PatchError("old_text does not match the selected line range. Re-read the file.")

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
        target.write_bytes(persisted.encode("utf-8"))
    return PatchResult(diff=diff, new_tag=hash_text(persisted))


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


def resolve_workspace_path(workspace: Path, raw_path: str) -> Path:
    candidate = (workspace / raw_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise PatchError(f"Path escapes workspace: {raw_path}") from exc
    return candidate


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
