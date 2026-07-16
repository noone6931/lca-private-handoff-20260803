from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.patch.anchored import apply_anchored_patch
from local_agent.patch.anchored import display_workspace_path
from local_agent.patch.anchored import hash_text
from local_agent.patch.anchored import PatchError
from local_agent.patch.anchored import PatchResult
from local_agent.patch.anchored import resolve_workspace_path

from .base import Tool, ToolContext, ToolResult, VisionInspectionUnavailableError, tool_state_dir

MAX_READ_BYTES = 256 * 1024
MAX_READ_LINES = 400
MAX_READ_LINE_COLUMNS = 768
MAX_INSPECT_IMAGE_BYTES = 8 * 1024 * 1024


def file_tools() -> list[Tool]:
    return [
        Tool(
            name="read_file",
            description=(
                "Read a text file inside the workspace or an explicitly allowed directory. "
                "Returns the pure hash tag to pass to apply_patch and numbered lines."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=read_file,
        ),
        Tool(
            name="inspect_image",
            description=(
                "Inspect a PNG, JPEG, GIF, or WEBP image inside the workspace or an explicitly allowed directory. "
                "It returns a text observation with path/hash metadata."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "question": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=inspect_image,
        ),
        Tool(
            name="apply_patch",
            description=(
                "Apply a safe anchored patch to a previously read workspace or allowed-directory file. "
                "Use mode=replace to replace the anchored lines, insert_before to insert before them, "
                "or insert_after to insert after them. old_text must be an exact, uniquely located line anchor. "
                "Set dry_run=true to preview the diff without changing the file."
            ),
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "tag": {"type": "string"},
                    "mode": {"type": "string", "enum": ["replace", "insert_before", "insert_after"]},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["path", "tag", "start_line", "end_line", "old_text", "new_text"],
                "additionalProperties": False,
            },
            handler=patch_file,
        ),
        Tool(
            name="rollback_patch",
            description=(
                "Roll back a patch previously applied by apply_patch in this session. "
                "If patch_id is omitted, rolls back the latest unapplied rollback candidate. "
                "The target file must still match the recorded after tag."
            ),
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "patch_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=rollback_patch,
        ),
        Tool(
            name="write_file",
            description=(
                "Create a new text file inside the workspace or an explicitly allowed directory. "
                "Refuses to overwrite existing files; use apply_patch for existing files. "
                "Set dry_run=true to preview the new-file diff without writing."
            ),
            tier="write",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=write_file,
        ),
    ]


def read_file(args: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        path = resolve_workspace_path(context.workspace, args["path"], context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(
            f"File not found: {args['path']}",
            is_error=True,
            metadata={
                "negative_evidence_type": "exact_path_missing",
                "path": str(args["path"]),
                "complete": True,
            },
        )
    if not path.is_file():
        return ToolResult(
            f"Path is a directory, not a readable file: {args['path']}. Use list_files or glob_files for discovery.",
            is_error=True,
            metadata={
                "path": str(args["path"]),
                "complete": True,
            },
        )
    file_size = path.stat().st_size
    # Detect image content from a bounded header before applying the text-file
    # limit. Images may be inspectable up to the separate vision-read limit.
    with path.open("rb") as handle:
        header = handle.read(16)
    image_mime = detect_image_mime(header)
    if image_mime is not None:
        rel = display_workspace_path(context.workspace, path, context.allowed_dirs)
        inspectable = file_size <= MAX_INSPECT_IMAGE_BYTES
        metadata: dict[str, Any] = {
            "image_metadata": True,
            "mime_type": image_mime,
            "size_bytes": file_size,
            "path": rel,
            "inspect_image_available": inspectable,
            "sha256_computed": inspectable,
        }
        if inspectable:
            metadata["sha256"] = _sha256_file(path)
        inspection_hint = (
            "Use inspect_image with this path to obtain a visual observation."
            if inspectable
            else f"inspect_image is unavailable because the file exceeds its {MAX_INSPECT_IMAGE_BYTES} byte limit."
        )
        return ToolResult(
            f"Image file metadata: {rel} ({image_mime}, {file_size} bytes). {inspection_hint}",
            metadata=metadata,
        )
    if file_size > MAX_READ_BYTES:
        return ToolResult(
            f"File too large to read safely: {args['path']} is {file_size} bytes, limit is {MAX_READ_BYTES} bytes.",
            is_error=True,
        )
    raw = path.read_bytes()
    if b"\x00" in raw:
        return ToolResult(f"Refusing to read likely binary file: {args['path']}", is_error=True)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return ToolResult(f"File is not valid UTF-8 text: {args['path']}: {exc}", is_error=True)

    start_line = int(args.get("start_line") or 1)
    if start_line < 1:
        return ToolResult("start_line must be >= 1.", is_error=True)
    end_line = args.get("end_line")
    max_line = int(end_line) if end_line else start_line + MAX_READ_LINES - 1
    if max_line < start_line:
        return ToolResult("end_line must be >= start_line.", is_error=True)
    if max_line - start_line + 1 > MAX_READ_LINES:
        max_line = start_line + MAX_READ_LINES - 1

    lines = text.splitlines()
    rel = display_workspace_path(context.workspace, path, context.allowed_dirs)
    tag = hash_text(text)
    rendered = [f"[{rel}#{tag}]", f"tag: {tag}"]
    truncated_line_numbers: list[int] = []
    for index, line in enumerate(lines, start=1):
        if start_line <= index <= max_line:
            display_line = line
            if len(display_line) > MAX_READ_LINE_COLUMNS:
                display_line = display_line[:MAX_READ_LINE_COLUMNS].rstrip() + "..."
                truncated_line_numbers.append(index)
            rendered.append(f"{index}:{display_line}")
    if max_line < len(lines):
        rendered.append(
            f"... more lines exist after line {max_line}; continue with start_line/end_line only if needed for the task."
        )
    if truncated_line_numbers:
        preview = ", ".join(str(number) for number in truncated_line_numbers[:8])
        suffix = "..." if len(truncated_line_numbers) > 8 else ""
        rendered.append(
            f"... {len(truncated_line_numbers)} line(s) truncated to {MAX_READ_LINE_COLUMNS} characters "
            f"(lines {preview}{suffix}); use search_code or a narrower source artifact for targeted evidence."
        )
    rendered = "\n".join(rendered)
    return ToolResult(
        rendered,
        metadata={
            "line_truncated": bool(truncated_line_numbers),
            "truncated_line_count": len(truncated_line_numbers),
            "column_limit": MAX_READ_LINE_COLUMNS,
        },
    )


def inspect_image(args: dict[str, Any], context: ToolContext) -> ToolResult:
    """Send one approved image read to a configured vision capability."""

    try:
        path = resolve_workspace_path(context.workspace, args["path"], context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists() or not path.is_file():
        return ToolResult(f"Image file not found: {args['path']}", is_error=True)
    size = path.stat().st_size
    if size > MAX_INSPECT_IMAGE_BYTES:
        return ToolResult(
            f"Image is too large to inspect safely: {args['path']} is {size} bytes, limit is {MAX_INSPECT_IMAGE_BYTES} bytes.",
            is_error=True,
            metadata={"image_inspection_unavailable": True, "reason": "too_large", "size_bytes": size},
        )
    raw = path.read_bytes()
    mime_type = detect_image_mime(raw)
    if mime_type is None:
        return ToolResult(
            "inspect_image supports PNG, JPEG, GIF, and WEBP files detected from file content.",
            is_error=True,
            metadata={"image_inspection_unavailable": True, "reason": "unsupported_mime"},
        )
    if context.vision_inspector is None:
        return ToolResult(
            "Image inspection is unavailable: configure AI_VISION_MODEL with an explicit vision-capable model.",
            is_error=True,
            metadata={"image_inspection_unavailable": True, "mime_type": mime_type},
        )
    question = str(args.get("question") or "Describe relevant visible content and constraints without inventing unseen details.")
    try:
        observation = context.vision_inspector(path, mime_type, raw, question)
    except VisionInspectionUnavailableError as exc:
        return ToolResult(
            f"Image inspection is unavailable: {exc}",
            is_error=True,
            metadata={"image_inspection_unavailable": True, "mime_type": mime_type},
        )
    except Exception as exc:  # noqa: BLE001 - provider failures remain tool observations.
        return ToolResult(f"inspect_image unavailable: {type(exc).__name__}: {exc}", is_error=True)
    parsed_observation = _parse_vision_observation_contract(observation)
    if parsed_observation is None:
        return ToolResult(
            "inspect_image unavailable: vision model did not return the required structured direct-observation JSON.",
            is_error=True,
            metadata={"image_inspection_unavailable": True, "reason": "invalid_vision_contract", "mime_type": mime_type},
        )
    direct_observations, uncertainty_count, inference_count = parsed_observation
    rel = display_workspace_path(context.workspace, path, context.allowed_dirs)
    digest = hashlib.sha256(raw).hexdigest()
    rendered = [f"[model-generated visual observation: {rel}#{digest[:16]}]", "Direct visual observations:"]
    rendered.extend(f"- {item}" for item in direct_observations)
    if uncertainty_count or inference_count:
        rendered.append(
            f"[vision caveats/inferences separated from evidence: uncertainties={uncertainty_count}, inferences={inference_count}]"
        )
    return ToolResult(
        "\n".join(rendered),
        metadata={
            "image_observation": True,
            "observation_origin": "vision_model",
            "observation_reliability": "model_declared_visible_observations",
            "vision_contract": "structured_direct_observations",
            "vision_uncertainty_items": uncertainty_count,
            "vision_inference_items": inference_count,
            "vision_inferences_separated": bool(uncertainty_count or inference_count),
            "mime_type": mime_type,
            "size_bytes": size,
            "path": rel,
            "sha256": digest,
        },
    )


def _parse_vision_observation_contract(value: str) -> tuple[tuple[str, ...], int, int] | None:
    """Extract direct observations from the strict vision JSON contract."""

    try:
        payload = json.loads(_strip_json_fence(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"observations", "uncertainties", "inferences"}:
        return None
    observations = _required_string_list(payload.get("observations"))
    uncertainties = _required_string_list(payload.get("uncertainties"))
    inferences = _required_string_list(payload.get("inferences"))
    if observations is None or uncertainties is None or inferences is None:
        return None
    if not observations:
        return None
    return observations, len(uncertainties), len(inferences)


def _strip_json_fence(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


def _required_string_list(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return tuple(" ".join(item.split()) for item in value if item.strip())


def detect_image_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def patch_file(args: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        path = resolve_workspace_path(context.workspace, args["path"], context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(f"Target file does not exist: {args['path']}", is_error=True)
    if not args.get("dry_run") and context.patch_preview_checker is not None:
        denial_reason = context.patch_preview_checker(args, path)
        if denial_reason:
            return ToolResult(denial_reason, is_error=True)
    if not args.get("dry_run") and context.patch_relevance_checker is not None:
        denial_reason = context.patch_relevance_checker(str(args["path"]), path)
        if denial_reason:
            return ToolResult(denial_reason, is_error=True)
    before_text = path.read_bytes().decode("utf-8")
    before_tag = hash_text(before_text)
    tag, interpreted_from = _normalize_patch_tag(args["tag"])
    result = apply_anchored_patch(
        workspace=context.workspace,
        path=args["path"],
        tag=tag,
        start_line=int(args["start_line"]),
        end_line=int(args["end_line"]),
        old_text=args["old_text"],
        new_text=args["new_text"],
        mode=args.get("mode") or "replace",
        dry_run=bool(args.get("dry_run")),
        allowed_roots=context.allowed_dirs,
    )
    tag_note = _interpreted_tag_note(interpreted_from, tag)
    range_note = _patch_range_note(args, result)
    range_metadata = _patch_range_metadata(args, result)
    if args.get("dry_run"):
        return ToolResult(
            f"{tag_note}{range_note}Patch preview only. File not changed. "
            f"New tag after apply would be: {result.new_tag}\n\n{result.diff}",
            metadata=range_metadata,
        )
    patch_id = _record_patch(
        context=context,
        path=display_workspace_path(context.workspace, path, context.allowed_dirs),
        before_text=before_text,
        before_tag=before_tag,
        after_tag=result.new_tag,
        diff=result.diff,
    )
    return ToolResult(
        f"{tag_note}{range_note}Applied patch. Patch id: {patch_id}. New tag: {result.new_tag}\n\n{result.diff}",
        metadata={
            "changed_path": display_workspace_path(context.workspace, path, context.allowed_dirs),
            **range_metadata,
        },
    )


def _patch_range_note(args: dict[str, Any], result: PatchResult) -> str:
    authored = (int(args["start_line"]), int(args["end_line"]))
    effective = (result.effective_start_line, result.effective_end_line)
    if authored == effective:
        return ""
    return (
        "Recovered exact unique old_text anchor: "
        f"authored range {authored[0]}-{authored[1]} -> effective range {effective[0]}-{effective[1]}.\n\n"
    )


def _patch_range_metadata(args: dict[str, Any], result: PatchResult) -> dict[str, Any]:
    authored = [int(args["start_line"]), int(args["end_line"])]
    effective = [result.effective_start_line, result.effective_end_line]
    return {
        "authored_range": authored,
        "effective_range": effective,
        "range_recovered": authored != effective,
    }


def rollback_patch(args: dict[str, Any], context: ToolContext) -> ToolResult:
    records = _load_patch_records(context)
    patch_id = args.get("patch_id")
    record = _find_rollback_record(records, patch_id)
    if record is None:
        if patch_id:
            return ToolResult(f"Patch record not found or already rolled back: {patch_id}", is_error=True)
        return ToolResult("No unapplied patch record found for this session.", is_error=True)

    try:
        path = resolve_workspace_path(context.workspace, str(record["path"]), context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(f"Target file does not exist: {record['path']}", is_error=True)
    current_text = path.read_bytes().decode("utf-8")
    current_tag = hash_text(current_text)
    after_tag = str(record["after_tag"])
    if current_tag != after_tag:
        return ToolResult(
            f"Refusing rollback for {record['id']}: expected current tag {after_tag}, got {current_tag}. "
            "The file changed after the patch; inspect git_diff and roll back manually.",
            is_error=True,
        )

    before_exists = bool(record.get("before_exists", True))
    if not before_exists:
        path.unlink()
        _record_rollback(context, str(record["id"]))
        diff = "".join(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                [],
                fromfile=f"a/{record['path']}",
                tofile=f"b/{record['path']}",
            )
        )
        return ToolResult(
            f"Rolled back patch {record['id']}. Deleted created file.\n\n{diff}",
            metadata={"changed_path": str(record["path"]), "rollback_of": str(record["id"])},
        )

    before_text = str(record["before_text"])
    path.write_bytes(before_text.encode("utf-8"))
    _record_rollback(context, str(record["id"]))
    diff = "".join(
        difflib.unified_diff(
            current_text.splitlines(keepends=True),
            before_text.splitlines(keepends=True),
            fromfile=f"a/{record['path']}",
            tofile=f"b/{record['path']}",
        )
    )
    return ToolResult(
        f"Rolled back patch {record['id']}. Restored tag: {record['before_tag']}\n\n{diff}",
        metadata={"changed_path": str(record["path"]), "rollback_of": str(record["id"])},
    )


def _normalize_patch_tag(value: object) -> tuple[str, str | None]:
    raw = str(value).strip()
    cleaned = raw.strip("`").strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1].strip()
    if "#" not in cleaned:
        return raw, None
    candidate = cleaned.rsplit("#", 1)[1].strip().strip("]`")
    if not candidate:
        return raw, None
    return candidate, raw


def _interpreted_tag_note(original: str | None, tag: str) -> str:
    if original is None:
        return ""
    return f"Interpreted tag {original!r} as hash {tag!r}. Pass only the pure hash tag next time.\n\n"


def _new_file_diff(path: str, content: str) -> str:
    return "".join(
        difflib.unified_diff(
            [],
            content.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{path}",
        )
    )


def write_file(args: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        path = resolve_workspace_path(context.workspace, args["path"], context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if path.exists():
        return ToolResult(
            f"Refusing to overwrite existing file with write_file: {args['path']}. Use apply_patch instead.",
            is_error=True,
        )
    display_path = display_workspace_path(context.workspace, path, context.allowed_dirs)
    content = str(args["content"])
    diff = _new_file_diff(display_path, content)
    after_tag = hash_text(content)
    if args.get("dry_run"):
        return ToolResult(
            f"New file preview only. File not changed. New tag after write would be: {after_tag}\n\n{diff}"
        )
    Path(path.parent).mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    patch_id = _record_patch(
        context=context,
        path=display_path,
        before_text="",
        before_tag=hash_text(""),
        after_tag=after_tag,
        diff=diff,
        before_exists=False,
    )
    return ToolResult(
        f"Wrote {display_path}. Patch id: {patch_id}. New tag: {after_tag}\n\n{diff}",
        metadata={"changed_path": display_path},
    )


def _record_patch(
    *,
    context: ToolContext,
    path: str,
    before_text: str,
    before_tag: str,
    after_tag: str,
    diff: str,
    before_exists: bool = True,
) -> str:
    patch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    record = {
        "event": "apply",
        "id": patch_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "before_tag": before_tag,
        "after_tag": after_tag,
        "before_text": before_text,
        "before_exists": before_exists,
        "diff": diff,
    }
    _append_patch_record(context, record)
    return patch_id


def _record_rollback(context: ToolContext, patch_id: str) -> None:
    _append_patch_record(
        context,
        {
            "event": "rollback",
            "patch_id": patch_id,
            "time": datetime.now(timezone.utc).isoformat(),
        },
    )


def _append_patch_record(context: ToolContext, record: dict[str, Any]) -> None:
    path = _patch_log_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_patch_records(context: ToolContext) -> list[dict[str, Any]]:
    path = _patch_log_path(context)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def session_patch_records(context: ToolContext) -> list[dict[str, Any]]:
    return _load_patch_records(context)


def _find_rollback_record(records: list[dict[str, Any]], patch_id: str | None) -> dict[str, Any] | None:
    rolled_back = {
        str(record.get("patch_id"))
        for record in records
        if record.get("event") == "rollback" and record.get("patch_id")
    }
    applied = [
        record
        for record in records
        if record.get("event") == "apply"
        and isinstance(record.get("id"), str)
        and isinstance(record.get("path"), str)
        and isinstance(record.get("before_text"), str)
        and isinstance(record.get("before_tag"), str)
        and isinstance(record.get("after_tag"), str)
        and record["id"] not in rolled_back
    ]
    if patch_id:
        for record in reversed(applied):
            if record["id"] == patch_id:
                return record
        return None
    return applied[-1] if applied else None


def _patch_log_path(context: ToolContext) -> Path:
    session_id = context.session_id or "default"
    return tool_state_dir(context) / "patches" / f"{session_id}.jsonl"
