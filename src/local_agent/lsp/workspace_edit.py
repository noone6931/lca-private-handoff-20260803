from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from local_agent.patch.anchored import PatchError, display_workspace_path, resolve_workspace_path

MAX_WORKSPACE_EDIT_FILES = 50
MAX_WORKSPACE_EDITS = 500
MAX_WORKSPACE_FILE_BYTES = 2 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_PREVIEW_BYTES = 96 * 1024


class WorkspaceEditError(RuntimeError):
    """Raised when an LSP WorkspaceEdit cannot be previewed safely."""


@dataclass(frozen=True)
class LspPosition:
    line: int
    character: int


@dataclass(frozen=True)
class LspRange:
    start: LspPosition
    end: LspPosition


@dataclass(frozen=True)
class WorkspaceEditFilePlan:
    path: Path
    before_bytes: bytes
    after_bytes: bytes
    before_sha256: str
    after_sha256: str
    edit_count: int
    unified_diff: str


@dataclass(frozen=True)
class WorkspaceEditPlan:
    files: tuple[WorkspaceEditFilePlan, ...]
    edit_count: int
    unified_diff: str
    digest: str

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(file.path for file in self.files)

    @property
    def stored_bytes(self) -> int:
        return sum(len(file.before_bytes) + len(file.after_bytes) for file in self.files) + len(
            self.unified_diff.encode("utf-8")
        )


WorkspaceEditPreview = WorkspaceEditPlan


@dataclass(frozen=True)
class _TextEdit:
    range: LspRange
    new_text: str


@dataclass(frozen=True)
class _OffsetEdit:
    start: int
    end: int
    new_text: str


@dataclass(frozen=True)
class _DecodedFile:
    path: Path
    raw_bytes: bytes
    text: str
    bom: bool
    line_ending: str
    byte_count: int


def utf16_character(text: str, character_index: int) -> int:
    if character_index < 0 or character_index > len(text):
        raise WorkspaceEditError("Character index is outside the target line.")
    return len(text[:character_index].encode("utf-16-le")) // 2


def exact_symbol_position(
    text: str,
    *,
    line: int,
    symbol: str,
    occurrence: int | None = None,
) -> tuple[LspPosition, int]:
    if line < 1:
        raise WorkspaceEditError("line must be 1-indexed and greater than zero.")
    if not symbol:
        raise WorkspaceEditError("symbol must be non-empty.")
    lines = text.splitlines()
    if line > len(lines):
        raise WorkspaceEditError(f"line {line} exceeds the file length ({len(lines)} lines).")
    target = lines[line - 1]
    starts: list[int] = []
    cursor = 0
    while True:
        found = target.find(symbol, cursor)
        if found < 0:
            break
        starts.append(found)
        cursor = found + max(1, len(symbol))
    if not starts:
        raise WorkspaceEditError(f"symbol does not occur on line {line}.")
    if occurrence is None:
        if len(starts) != 1:
            raise WorkspaceEditError(
                f"symbol occurs {len(starts)} times on line {line}; provide occurrence from 1 to {len(starts)}."
            )
        selected = 0
    else:
        if occurrence < 1 or occurrence > len(starts):
            raise WorkspaceEditError(
                f"occurrence {occurrence} is outside the available range 1-{len(starts)}."
            )
        selected = occurrence - 1
    return LspPosition(line=line - 1, character=utf16_character(target, starts[selected])), len(starts)


def build_workspace_edit_preview(
    value: Any,
    *,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    project_root: Path,
) -> WorkspaceEditPlan:
    edits_by_path = _parse_workspace_edit(
        value,
        workspace=workspace,
        allowed_roots=allowed_roots,
        project_root=project_root,
    )
    if len(edits_by_path) > MAX_WORKSPACE_EDIT_FILES:
        raise WorkspaceEditError(f"WorkspaceEdit exceeds the {MAX_WORKSPACE_EDIT_FILES}-file preview limit.")
    edit_count = sum(len(edits) for edits in edits_by_path.values())
    if edit_count == 0:
        raise WorkspaceEditError("LSP rename returned no text edits.")
    if edit_count > MAX_WORKSPACE_EDITS:
        raise WorkspaceEditError(f"WorkspaceEdit exceeds the {MAX_WORKSPACE_EDITS}-edit preview limit.")

    decoded: dict[Path, _DecodedFile] = {}
    offset_edits: dict[Path, tuple[_OffsetEdit, ...]] = {}
    total_bytes = 0
    for path, edits in edits_by_path.items():
        file = _read_file(path)
        total_bytes += file.byte_count
        if total_bytes > MAX_WORKSPACE_TOTAL_BYTES:
            raise WorkspaceEditError(
                f"WorkspaceEdit exceeds the {MAX_WORKSPACE_TOTAL_BYTES}-byte cumulative input limit."
            )
        decoded[path] = file
        offset_edits[path] = _validate_ranges(file, edits)

    file_plans: list[WorkspaceEditFilePlan] = []
    diff_parts: list[str] = []
    diff_bytes = 0
    ordered_paths = tuple(sorted(decoded, key=str))
    for path in ordered_paths:
        file = decoded[path]
        updated = _apply_edits(file.text, offset_edits[path], file.line_ending)
        after_bytes = (b"\xef\xbb\xbf" if file.bom else b"") + updated.encode("utf-8")
        label = display_workspace_path(workspace, path, allowed_roots)
        file_diff = "".join(
            difflib.unified_diff(
                file.text.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{label}",
                tofile=f"b/{label}",
            )
        )
        diff_bytes += len(file_diff.encode("utf-8"))
        if diff_bytes > MAX_WORKSPACE_PREVIEW_BYTES:
            raise WorkspaceEditError(
                f"WorkspaceEdit preview exceeds the {MAX_WORKSPACE_PREVIEW_BYTES}-byte output limit."
            )
        diff_parts.append(file_diff)
        file_plans.append(
            WorkspaceEditFilePlan(
                path=path,
                before_bytes=file.raw_bytes,
                after_bytes=after_bytes,
                before_sha256=hashlib.sha256(file.raw_bytes).hexdigest(),
                after_sha256=hashlib.sha256(after_bytes).hexdigest(),
                edit_count=len(offset_edits[path]),
                unified_diff=file_diff,
            )
        )
    unified_diff = "".join(diff_parts)
    if not unified_diff:
        raise WorkspaceEditError("LSP rename produced no textual change.")
    digest = _plan_digest(tuple(file_plans), edit_count)
    return WorkspaceEditPlan(files=tuple(file_plans), edit_count=edit_count, unified_diff=unified_diff, digest=digest)


def _parse_workspace_edit(
    value: Any,
    *,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    project_root: Path,
) -> dict[Path, list[_TextEdit]]:
    if not isinstance(value, dict):
        raise WorkspaceEditError("LSP rename did not return a WorkspaceEdit object.")
    has_changes = "changes" in value
    has_document_changes = "documentChanges" in value
    if has_changes == has_document_changes:
        raise WorkspaceEditError("WorkspaceEdit must contain exactly one of changes or documentChanges.")
    unknown = set(value) - {"changes", "documentChanges", "changeAnnotations"}
    if unknown or "changeAnnotations" in value:
        raise WorkspaceEditError("WorkspaceEdit contains unsupported fields or annotated edits.")

    parsed: dict[Path, list[_TextEdit]] = {}
    if has_changes:
        changes = value["changes"]
        if not isinstance(changes, dict):
            raise WorkspaceEditError("WorkspaceEdit.changes must be an object.")
        if len(changes) > MAX_WORKSPACE_EDIT_FILES:
            raise WorkspaceEditError(f"WorkspaceEdit exceeds the {MAX_WORKSPACE_EDIT_FILES}-file preview limit.")
        edit_count = 0
        for uri, raw_edits in changes.items():
            path = _path_from_uri(
                uri,
                workspace=workspace,
                allowed_roots=allowed_roots,
                project_root=project_root,
            )
            if path in parsed:
                raise WorkspaceEditError("WorkspaceEdit contains duplicate canonical target files.")
            edits = _parse_text_edits(raw_edits)
            edit_count += len(edits)
            if edit_count > MAX_WORKSPACE_EDITS:
                raise WorkspaceEditError(f"WorkspaceEdit exceeds the {MAX_WORKSPACE_EDITS}-edit preview limit.")
            parsed[path] = edits
        return parsed

    document_changes = value["documentChanges"]
    if not isinstance(document_changes, list):
        raise WorkspaceEditError("WorkspaceEdit.documentChanges must be an array.")
    if len(document_changes) > MAX_WORKSPACE_EDITS:
        raise WorkspaceEditError(f"WorkspaceEdit exceeds the {MAX_WORKSPACE_EDITS}-entry preview limit.")
    edit_count = 0
    for item in document_changes:
        if not isinstance(item, dict) or set(item) != {"textDocument", "edits"}:
            raise WorkspaceEditError("Only textDocument edit entries are supported in documentChanges.")
        document = item["textDocument"]
        if not isinstance(document, dict) or set(document) - {"uri", "version"} or "uri" not in document:
            raise WorkspaceEditError("documentChanges contains an invalid textDocument identifier.")
        version = document.get("version")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
            raise WorkspaceEditError("textDocument.version must be an integer or null.")
        path = _path_from_uri(
            document["uri"],
            workspace=workspace,
            allowed_roots=allowed_roots,
            project_root=project_root,
        )
        if path in parsed:
            raise WorkspaceEditError("WorkspaceEdit contains duplicate canonical target files.")
        edits = _parse_text_edits(item["edits"])
        edit_count += len(edits)
        if edit_count > MAX_WORKSPACE_EDITS:
            raise WorkspaceEditError(f"WorkspaceEdit exceeds the {MAX_WORKSPACE_EDITS}-edit preview limit.")
        parsed[path] = edits
        if len(parsed) > MAX_WORKSPACE_EDIT_FILES:
            raise WorkspaceEditError(f"WorkspaceEdit exceeds the {MAX_WORKSPACE_EDIT_FILES}-file preview limit.")
    return parsed


def _parse_text_edits(value: Any) -> list[_TextEdit]:
    if not isinstance(value, list):
        raise WorkspaceEditError("Text edits must be an array.")
    if len(value) > MAX_WORKSPACE_EDITS:
        raise WorkspaceEditError(f"WorkspaceEdit exceeds the {MAX_WORKSPACE_EDITS}-edit preview limit.")
    result: list[_TextEdit] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"range", "newText"}:
            raise WorkspaceEditError("Only plain TextEdit objects are supported.")
        if not isinstance(item["newText"], str):
            raise WorkspaceEditError("TextEdit.newText must be a string.")
        result.append(_TextEdit(range=_parse_range(item["range"]), new_text=item["newText"]))
    return result


def _parse_range(value: Any) -> LspRange:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise WorkspaceEditError("TextEdit.range must contain start and end positions.")
    return LspRange(start=_parse_position(value["start"]), end=_parse_position(value["end"]))


def _parse_position(value: Any) -> LspPosition:
    if not isinstance(value, dict) or set(value) != {"line", "character"}:
        raise WorkspaceEditError("LSP positions must contain line and character.")
    line = value["line"]
    character = value["character"]
    if isinstance(line, bool) or isinstance(character, bool) or not isinstance(line, int) or not isinstance(character, int):
        raise WorkspaceEditError("LSP line and character values must be integers.")
    if line < 0 or character < 0:
        raise WorkspaceEditError("LSP line and character values cannot be negative.")
    return LspPosition(line=line, character=character)


def _path_from_uri(
    value: Any,
    *,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    project_root: Path,
) -> Path:
    if not isinstance(value, str):
        raise WorkspaceEditError("WorkspaceEdit URI must be a string.")
    parsed = urlparse(value)
    if (
        parsed.scheme != "file"
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
    ):
        raise WorkspaceEditError("Only local file:// WorkspaceEdit URIs are supported.")
    decoded_path = unquote(parsed.path)
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded_path):
        raise WorkspaceEditError("WorkspaceEdit target path contains unsupported control characters.")
    candidate = Path(decoded_path)
    try:
        resolved = resolve_workspace_path(workspace, str(candidate), allowed_roots)
    except (PatchError, OSError, ValueError) as exc:
        raise WorkspaceEditError("WorkspaceEdit targets a path outside the authorized roots.") from exc
    project = project_root.expanduser().resolve()
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise WorkspaceEditError("WorkspaceEdit targets a path outside the selected LSP project root.") from exc
    if not resolved.exists() or not resolved.is_file():
        raise WorkspaceEditError("WorkspaceEdit target must be an existing regular file.")
    return resolved


def _read_file(path: Path) -> _DecodedFile:
    raw = path.read_bytes()
    if len(raw) > MAX_WORKSPACE_FILE_BYTES:
        raise WorkspaceEditError(f"WorkspaceEdit target exceeds the {MAX_WORKSPACE_FILE_BYTES}-byte file limit.")
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceEditError("WorkspaceEdit target is not valid UTF-8 text.") from exc
    line_ending = _line_ending(text)
    return _DecodedFile(path=path, raw_bytes=raw, text=text, bom=bom, line_ending=line_ending, byte_count=len(raw))


def _plan_digest(files: tuple[WorkspaceEditFilePlan, ...], edit_count: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"workspace-edit-v1:{edit_count}\n".encode("ascii"))
    for file in files:
        digest.update(str(file.path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.before_sha256.encode("ascii"))
        digest.update(file.after_sha256.encode("ascii"))
        digest.update(str(file.edit_count).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _line_ending(text: str) -> str:
    has_crlf = "\r\n" in text
    without_crlf = text.replace("\r\n", "")
    if "\r" in without_crlf or (has_crlf and "\n" in without_crlf):
        raise WorkspaceEditError("WorkspaceEdit preview does not support mixed or bare-CR line endings.")
    return "\r\n" if has_crlf else "\n"


def _validate_ranges(file: _DecodedFile, edits: list[_TextEdit]) -> tuple[_OffsetEdit, ...]:
    offsets = [
        _OffsetEdit(
            start=_position_offset(file.text, edit.range.start),
            end=_position_offset(file.text, edit.range.end),
            new_text=edit.new_text,
        )
        for edit in edits
    ]
    for edit in offsets:
        if edit.start > edit.end:
            raise WorkspaceEditError("TextEdit range start must not follow its end.")
    ordered = sorted(offsets, key=lambda edit: (edit.start, edit.end))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start < previous.end or (current.start == previous.start and current.end == previous.end):
            raise WorkspaceEditError("WorkspaceEdit contains overlapping or duplicate edit ranges.")
    return tuple(ordered)


def _position_offset(text: str, position: LspPosition) -> int:
    line_starts = [0]
    line_ends: list[int] = []
    for index, char in enumerate(text):
        if char == "\n":
            line_ends.append(index - 1 if index > 0 and text[index - 1] == "\r" else index)
            line_starts.append(index + 1)
    line_ends.append(len(text))
    if position.line >= len(line_starts):
        raise WorkspaceEditError("TextEdit line is outside the target file.")
    start = line_starts[position.line]
    end = line_ends[position.line]
    line = text[start:end]
    units = 0
    for index, char in enumerate(line):
        if units == position.character:
            return start + index
        width = len(char.encode("utf-16-le")) // 2
        if units < position.character < units + width:
            raise WorkspaceEditError("TextEdit character splits a UTF-16 surrogate pair.")
        units += width
    if units == position.character:
        return end
    raise WorkspaceEditError("TextEdit character is outside the target line.")


def _apply_edits(text: str, edits: tuple[_OffsetEdit, ...], line_ending: str) -> str:
    updated = text
    for edit in reversed(edits):
        replacement = edit.new_text.replace("\r\n", "\n").replace("\r", "\n")
        if line_ending == "\r\n":
            replacement = replacement.replace("\n", "\r\n")
        updated = updated[: edit.start] + replacement + updated[edit.end :]
    return updated
