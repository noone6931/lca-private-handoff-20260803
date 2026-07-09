from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from local_agent.patch.anchored import PatchError
from local_agent.patch.anchored import display_workspace_path
from local_agent.patch.anchored import resolve_workspace_path
from local_agent.lsp.client import LspClientError
from local_agent.lsp.client import get_client
from local_agent.lsp.config import external_lsp_enabled
from local_agent.lsp.config import root_for_path
from local_agent.lsp.config import resolved_server_configs
from local_agent.lsp.config import servers_for_path
from local_agent.lsp.config import strict_external_lsp
from local_agent.lsp.external import external_definition
from local_agent.lsp.external import external_diagnostics
from local_agent.lsp.external import external_references
from local_agent.lsp.external import external_symbols

from .base import Tool, ToolContext, ToolResult
from .search import SKIPPED_DIRS

MAX_LSP_FILES = 300
MAX_LSP_FILE_BYTES = 768 * 1024
MAX_RESULT_LINE_CHARS = 240

SUPPORTED_SUFFIXES = {".py", ".java", ".js", ".jsx", ".ts", ".tsx", ".vue"}
JS_TS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".vue"}
BEST_EFFORT_SUFFIXES = SUPPORTED_SUFFIXES - {".py"}
BEST_EFFORT_NOTICE = (
    "[lsp confidence] Python uses AST parsing. Java/JavaScript/TypeScript/Vue use lightweight "
    "regex and delimiter fallback, so results are best-effort and may miss cases."
)
IDENTIFIER_RE = r"[$A-Za-z_][\w$]*"
SYMBOL_RE = re.compile(rf"^{IDENTIFIER_RE}(?:\.{IDENTIFIER_RE})*$")

JAVA_TYPE_RE = re.compile(
    rf"\b(class|interface|enum|record)\s+({IDENTIFIER_RE})\b"
)
JAVA_METHOD_RE = re.compile(
    rf"^\s*(?:(?:public|protected|private|static|final|abstract|synchronized|native|strictfp)\s+)*"
    rf"(?:<[^>{{}};]+>\s*)?"
    rf"(?:[\w$<>\[\],.?]+\s+)+({IDENTIFIER_RE})\s*\([^;{{}}]*\)\s*(?:throws [^{{]+)?\s*(?:\{{|$)"
)

JS_CLASS_RE = re.compile(rf"\b(?:export\s+default\s+|export\s+)?class\s+({IDENTIFIER_RE})\b")
JS_FUNCTION_RE = re.compile(rf"\b(?:export\s+)?(?:async\s+)?function\s+({IDENTIFIER_RE})\s*\(")
JS_ARROW_FUNCTION_RE = re.compile(
    rf"\b(?:export\s+)?(?:const|let|var)\s+({IDENTIFIER_RE})\s*=\s*(?:async\s*)?(?:\([^)]*\)|{IDENTIFIER_RE})\s*=>"
)
JS_VARIABLE_RE = re.compile(rf"\b(?:export\s+)?(?:const|let|var)\s+({IDENTIFIER_RE})\b")
TS_TYPE_RE = re.compile(rf"\b(?:export\s+)?(?:interface|type)\s+({IDENTIFIER_RE})\b")
JS_METHOD_RE = re.compile(rf"^\s*(?:async\s+)?({IDENTIFIER_RE})\s*\([^)]*\)\s*(?::[^{{]+)?\s*\{{")
VUE_NAME_RE = re.compile(r"""name\s*:\s*['"]([^'"]+)['"]""")

CONTROL_WORDS = {
    "catch",
    "do",
    "for",
    "if",
    "return",
    "switch",
    "while",
}


@dataclass(frozen=True)
class SymbolRecord:
    path: Path
    name: str
    kind: str
    line: int
    column: int
    container: str | None = None

    def render(self, workspace: Path, allowed_roots: tuple[Path, ...] = ()) -> str:
        rel = display_workspace_path(workspace, self.path, allowed_roots)
        scoped = f"{self.container}.{self.name}" if self.container else self.name
        return f"{rel}:{self.line}:{self.column + 1}: {self.kind} {scoped}"


def lsp_tools() -> list[Tool]:
    languages = "Python, Java, JavaScript, TypeScript, and Vue"
    symbol_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "additionalProperties": False,
    }
    return [
        Tool(
            name="lsp_symbols",
            description=(
                f"List symbols for {languages} files under the workspace or an explicitly allowed path. "
                "Uses an external language server when auto-detected/configured, otherwise lightweight fallback. "
                "Use this for code navigation before broad text search."
            ),
            tier="read",
            input_schema=symbol_schema,
            handler=lsp_symbols,
        ),
        Tool(
            name="lsp_workspace_symbols",
            description=(
                "Compatibility alias for lsp_symbols. "
                f"List workspace symbols for {languages} files under the workspace or an explicitly allowed path."
            ),
            tier="read",
            input_schema=symbol_schema,
            handler=lsp_symbols,
        ),
        Tool(
            name="lsp_document_symbols",
            description=(
                "Compatibility alias for lsp_symbols. "
                f"List symbols for a specific {languages} file or directory path."
            ),
            tier="read",
            input_schema=symbol_schema,
            handler=lsp_symbols,
        ),
        Tool(
            name="lsp_definition",
            description=(
                f"Find definitions for {languages} symbols using external LSP when available, otherwise lightweight fallback. "
                "Returns workspace-relative or absolute allowed-directory file, line, column, and symbol kind."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["symbol"],
                "additionalProperties": False,
            },
            handler=lsp_definition,
        ),
        Tool(
            name="lsp_references",
            description=(
                f"Find text references to an identifier in {languages} files under a workspace path. "
                "Uses external LSP when available, otherwise local text fallback."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["symbol"],
                "additionalProperties": False,
            },
            handler=lsp_references,
        ),
        Tool(
            name="lsp_diagnostics",
            description=(
                f"Run diagnostics for {languages} files under a workspace path. "
                "Uses external LSP when available; fallback uses Python compile() and delimiter checks."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
            handler=lsp_diagnostics,
        ),
        Tool(
            name="lsp_status",
            description=(
                "Show external language-server availability and fallback status. "
                "External servers are used when configured/auto-detected; otherwise tools use lightweight fallback. "
                "Set probe=true to start the matching server and inspect Java project import/source path health."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "probe": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            handler=lsp_status,
        ),
    ]


def lsp_symbols(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _resolve_lsp_root(args, context)
    if isinstance(root, ToolResult):
        return root
    query = str(args.get("query") or "").strip()
    max_results = _max_results(args, default=80, upper=200)
    external = external_symbols(
        root,
        workspace=context.workspace,
        allowed_roots=context.allowed_dirs,
        supported_files=_iter_supported_files(root),
        query=query,
        max_results=max_results,
    )
    if external.content:
        return ToolResult(external.content)
    results: list[str] = []
    matched_suffixes: set[str] = set()
    for record in _iter_symbol_records(root, context.workspace, context.allowed_dirs):
        rendered = record.render(context.workspace, context.allowed_dirs)
        if query and query.lower() not in rendered.lower():
            continue
        results.append(rendered)
        matched_suffixes.add(record.path.suffix)
        if len(results) >= max_results:
            results.append(f"... truncated after {max_results} symbols")
            break
    if not results:
        if external.strict and external.error:
            return ToolResult(external.error, is_error=True)
        return ToolResult("No supported code symbols found.", useless=True)
    return ToolResult(_render_lsp_results(results, matched_suffixes, external=external))


def lsp_definition(args: dict[str, Any], context: ToolContext) -> ToolResult:
    symbol = _clean_symbol(args["symbol"])
    if symbol is None:
        return ToolResult("symbol must be a valid identifier, optionally dotted.", is_error=True)
    root = _resolve_lsp_root(args, context)
    if isinstance(root, ToolResult):
        return root
    max_results = _max_results(args, default=40, upper=100)
    external = external_definition(
        root,
        workspace=context.workspace,
        allowed_roots=context.allowed_dirs,
        supported_files=_iter_supported_files(root),
        symbol=symbol,
        max_results=max_results,
    )
    if external.content:
        return ToolResult(external.content)
    matches: list[str] = []
    matched_suffixes: set[str] = set()
    for record in _iter_symbol_records(root, context.workspace, context.allowed_dirs):
        if record.name == symbol or (record.container and f"{record.container}.{record.name}" == symbol):
            matches.append(record.render(context.workspace, context.allowed_dirs))
            matched_suffixes.add(record.path.suffix)
            if len(matches) >= max_results:
                matches.append(f"... truncated after {max_results} definitions")
                break
    if not matches:
        if external.strict and external.error:
            return ToolResult(external.error, is_error=True)
        return ToolResult(f"No definition found for: {symbol}", useless=True)
    return ToolResult(_render_lsp_results(matches, matched_suffixes, external=external))


def lsp_references(args: dict[str, Any], context: ToolContext) -> ToolResult:
    symbol = _clean_symbol(args["symbol"])
    if symbol is None:
        return ToolResult("symbol must be a valid identifier, optionally dotted.", is_error=True)
    root = _resolve_lsp_root(args, context)
    if isinstance(root, ToolResult):
        return root
    max_results = _max_results(args, default=80, upper=200)
    external = external_references(
        root,
        workspace=context.workspace,
        allowed_roots=context.allowed_dirs,
        supported_files=_iter_supported_files(root),
        symbol=symbol,
        max_results=max_results,
    )
    if external.content:
        return ToolResult(external.content)
    pattern = re.compile(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])")
    results: list[str] = []
    matched_suffixes: set[str] = set()
    for path in _iter_supported_files(root):
        text = _read_text(path)
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if not match:
                continue
            rel = display_workspace_path(context.workspace, path, context.allowed_dirs)
            snippet = line.strip()
            if len(snippet) > MAX_RESULT_LINE_CHARS:
                snippet = snippet[: MAX_RESULT_LINE_CHARS - 14] + "...<truncated>"
            results.append(f"{rel}:{line_number}:{match.start() + 1}: {snippet}")
            matched_suffixes.add(path.suffix)
            if len(results) >= max_results:
                results.append(f"... truncated after {max_results} references")
                return ToolResult(_render_lsp_results(results, matched_suffixes, external=external))
    if not results:
        if external.strict and external.error:
            return ToolResult(external.error, is_error=True)
        return ToolResult(f"No references found for: {symbol}", useless=True)
    return ToolResult(_render_lsp_results(results, matched_suffixes, external=external))


def lsp_diagnostics(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _resolve_lsp_root(args, context)
    if isinstance(root, ToolResult):
        return root
    max_results = _max_results(args, default=80, upper=200)
    external = external_diagnostics(
        root,
        workspace=context.workspace,
        allowed_roots=context.allowed_dirs,
        max_results=max_results,
    )
    if external.content:
        return ToolResult(external.content)
    diagnostics: list[str] = []
    matched_suffixes: set[str] = set()
    for path in _iter_supported_files(root):
        text = _read_text(path)
        if text is None:
            continue
        if path.suffix == ".py":
            diagnostic = _python_diagnostic(path, context.workspace, context.allowed_dirs, text)
        else:
            diagnostic = _delimiter_diagnostic(path, context.workspace, context.allowed_dirs, text)
        if diagnostic:
            diagnostics.append(diagnostic)
            matched_suffixes.add(path.suffix)
            if len(diagnostics) >= max_results:
                diagnostics.append(f"... truncated after {max_results} diagnostics")
                break
    if not diagnostics:
        if external.strict and external.error:
            return ToolResult(external.error, is_error=True)
        return ToolResult("No lightweight diagnostics.", useless=True)
    return ToolResult(_render_lsp_results(diagnostics, matched_suffixes, external=external))


def lsp_status(args: dict[str, Any], context: ToolContext) -> ToolResult:
    if not external_lsp_enabled():
        return ToolResult("External LSP: disabled by AGENT_LSP_MODE; lightweight fallback is active.")
    raw_path = args.get("path") or "."
    try:
        status_root = resolve_workspace_path(context.workspace, raw_path, context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not status_root.exists():
        return ToolResult(f"Path not found: {raw_path}", is_error=True)
    servers = resolved_server_configs(context.workspace)
    mode = "strict external" if strict_external_lsp() else "auto external with lightweight fallback"
    lines = [f"External LSP mode: {mode}"]
    if not servers:
        lines.append("No external LSP server commands found; lightweight fallback is active.")
        lines.append(
            "Configure AGENT_LSP_JDTLS_COMMAND, AGENT_LSP_TYPESCRIPT_COMMAND, "
            "or AGENT_LSP_VUE_COMMAND, or install commands on PATH."
        )
        return ToolResult("\n".join(lines))
    lines.append("Available external LSP servers:")
    for server in servers:
        markers = ", ".join(server.root_markers)
        file_types = ", ".join(server.file_types)
        command = " ".join(server.command)
        lines.append(f"- {server.name}: {file_types}; command={command}; root markers={markers}")
    lines.append(
        "Tools use an external server only when root markers match; otherwise they fall back to lightweight static navigation."
    )
    if args.get("probe"):
        lines.extend(_render_lsp_probe_status(status_root, context))
    else:
        lines.append("Set probe=true to inspect external project health; this may start language-server processes.")
    return ToolResult("\n".join(lines))


def _resolve_lsp_root(args: dict[str, Any], context: ToolContext) -> Path | ToolResult:
    raw_path = args.get("path") or "."
    try:
        path = resolve_workspace_path(context.workspace, raw_path, context.allowed_dirs)
    except PatchError as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.exists():
        return ToolResult(f"Path not found: {raw_path}", is_error=True)
    if path.is_file() and path.suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        return ToolResult(f"Unsupported file for lightweight LSP: {raw_path}. Supported suffixes: {supported}.")
    return path


def _max_results(args: dict[str, Any], *, default: int, upper: int) -> int:
    return min(max(int(args.get("max_results") or default), 1), upper)


def _clean_symbol(symbol: str) -> str | None:
    cleaned = symbol.strip()
    return cleaned if SYMBOL_RE.fullmatch(cleaned) else None


def _render_lsp_results(results: list[str], suffixes: set[str], *, external: Any | None = None) -> str:
    lines: list[str] = []
    if getattr(external, "error", None):
        lines.append(str(external.error))
        lines.append("[lsp fallback] using lightweight static navigation because the external server returned no usable result.")
    if suffixes.intersection(BEST_EFFORT_SUFFIXES):
        lines.append(BEST_EFFORT_NOTICE)
    lines.extend(results)
    return "\n".join(lines)


def _render_lsp_probe_status(root: Path, context: ToolContext) -> list[str]:
    lines = ["", "[lsp probe]"]
    java_file = _first_java_file(root)
    if java_file is None:
        lines.append("No Java files found under the requested path; Java project health probe skipped.")
        return lines
    lsp_root = _lsp_probe_root_for_path(context.workspace, context.allowed_dirs, java_file)
    matches = servers_for_path(lsp_root, java_file)
    jdtls_matches = [server for server in matches if server.name == "jdtls"]
    if not jdtls_matches:
        lines.append("No matching jdtls server/root marker found for the Java probe file.")
        lines.append(f"Probe file: {display_workspace_path(context.workspace, java_file, context.allowed_dirs)}")
        return lines
    for server in jdtls_matches:
        server_root = root_for_path(lsp_root, java_file, server)
        if server_root is None:
            lines.append(f"- {server.name}: root marker not found for probe file.")
            continue
        rel_probe = display_workspace_path(context.workspace, java_file, context.allowed_dirs)
        rel_root = display_workspace_path(context.workspace, server_root, context.allowed_dirs)
        lines.append(f"- {server.name}: probe file={rel_probe}; server root={rel_root}")
        try:
            client = get_client(server, server_root)
            projects = client.execute_command("java.project.getAll", [], timeout=15)
            source_paths = client.execute_command("java.project.listSourcePaths", [], timeout=15)
        except (OSError, LspClientError) as exc:
            lines.append(f"  project health: unavailable ({type(exc).__name__}: {exc})")
            continue
        lines.extend(_render_java_project_probe(projects, source_paths))
    return lines


def _first_java_file(root: Path) -> Path | None:
    if root.is_file():
        return root if root.suffix == ".java" else None
    for path in _iter_supported_files(root):
        if path.suffix == ".java":
            return path
    return None


def _lsp_probe_root_for_path(workspace: Path, allowed_roots: tuple[Path, ...], path: Path) -> Path:
    resolved = path.resolve()
    for root in (workspace, *allowed_roots):
        try:
            resolved.relative_to(root.resolve())
            return root
        except ValueError:
            continue
    return workspace


def _render_java_project_probe(projects: Any, source_paths: Any) -> list[str]:
    lines: list[str] = []
    project_count = len(projects) if isinstance(projects, list) else 0
    source_entries = _java_source_path_entries(source_paths)
    source_count = len(source_entries)
    lines.append(f"  java.project.getAll: {project_count} project(s)")
    lines.append(f"  java.project.listSourcePaths: {source_count} source path(s)")
    for entry in source_entries[:8]:
        lines.append(f"    - {entry}")
    if len(source_entries) > 8:
        lines.append(f"    ... truncated after 8 of {len(source_entries)} source paths")
    if project_count == 0 or source_count == 0:
        lines.append(
            "  project health: incomplete. jdtls started, but Java project import/source paths are empty; "
            "check Maven/Gradle parent POMs, private repositories, and local dependency cache."
        )
    else:
        lines.append("  project health: jdtls has imported Java project metadata.")
    return lines


def _java_source_path_entries(source_paths: Any) -> list[str]:
    raw = source_paths.get("data") if isinstance(source_paths, dict) else source_paths
    if not isinstance(raw, list):
        return []
    entries: list[str] = []
    for item in raw:
        if isinstance(item, str):
            entries.append(item)
        elif isinstance(item, dict):
            value = item.get("path") or item.get("sourcePath") or item.get("uri") or item.get("name")
            if value:
                entries.append(str(value))
    return entries


def _iter_symbol_records(root: Path, workspace: Path, allowed_roots: tuple[Path, ...]) -> Iterable[SymbolRecord]:
    for path in _iter_supported_files(root):
        text = _read_text(path)
        if text is None:
            continue
        if path.suffix == ".py":
            yield from _python_symbol_records(path, workspace, allowed_roots, text)
        elif path.suffix == ".java":
            yield from _java_symbol_records(path, text)
        elif path.suffix in JS_TS_SUFFIXES:
            yield from _js_ts_vue_symbol_records(path, text)


def _python_symbol_records(
    path: Path,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    text: str,
) -> Iterable[SymbolRecord]:
    try:
        tree = ast.parse(text, filename=display_workspace_path(workspace, path, allowed_roots))
    except SyntaxError:
        return []
    visitor = _PythonSymbolVisitor(path)
    visitor.visit(tree)
    return visitor.records


def _java_symbol_records(path: Path, text: str) -> Iterable[SymbolRecord]:
    records: list[SymbolRecord] = []
    current_type: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if _looks_like_comment(stripped):
            continue
        type_match = JAVA_TYPE_RE.search(line)
        if type_match:
            current_type = type_match.group(2)
            records.append(
                SymbolRecord(
                    path=path,
                    name=current_type,
                    kind=type_match.group(1),
                    line=line_number,
                    column=type_match.start(2),
                )
            )
            continue
        method_match = JAVA_METHOD_RE.search(line)
        if method_match:
            name = method_match.group(1)
            if name not in CONTROL_WORDS:
                records.append(
                    SymbolRecord(
                        path=path,
                        name=name,
                        kind="method",
                        line=line_number,
                        column=method_match.start(1),
                        container=current_type,
                    )
                )
    return records


def _js_ts_vue_symbol_records(path: Path, text: str) -> Iterable[SymbolRecord]:
    records: list[SymbolRecord] = []
    current_class: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if _looks_like_comment(stripped):
            continue
        vue_name = VUE_NAME_RE.search(line) if path.suffix == ".vue" else None
        if vue_name:
            records.append(
                SymbolRecord(
                    path=path,
                    name=vue_name.group(1),
                    kind="vue_component",
                    line=line_number,
                    column=vue_name.start(1),
                )
            )
        class_match = JS_CLASS_RE.search(line)
        if class_match:
            current_class = class_match.group(1)
            records.append(
                SymbolRecord(
                    path=path,
                    name=current_class,
                    kind="class",
                    line=line_number,
                    column=class_match.start(1),
                )
            )
            continue
        type_match = TS_TYPE_RE.search(line)
        if type_match:
            keyword = "interface" if "interface" in line[: type_match.start(1)] else "type"
            records.append(
                SymbolRecord(
                    path=path,
                    name=type_match.group(1),
                    kind=keyword,
                    line=line_number,
                    column=type_match.start(1),
                )
            )
            continue
        function_match = JS_FUNCTION_RE.search(line) or JS_ARROW_FUNCTION_RE.search(line)
        if function_match:
            records.append(
                SymbolRecord(
                    path=path,
                    name=function_match.group(1),
                    kind="function",
                    line=line_number,
                    column=function_match.start(1),
                )
            )
            continue
        method_match = JS_METHOD_RE.search(line)
        if method_match and method_match.group(1) not in CONTROL_WORDS:
            records.append(
                SymbolRecord(
                    path=path,
                    name=method_match.group(1),
                    kind="method",
                    line=line_number,
                    column=method_match.start(1),
                    container=current_class,
                )
            )
            continue
        variable_match = JS_VARIABLE_RE.search(line)
        if variable_match:
            records.append(
                SymbolRecord(
                    path=path,
                    name=variable_match.group(1),
                    kind="variable",
                    line=line_number,
                    column=variable_match.start(1),
                )
            )
    return records


def _iter_supported_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        if root.suffix in SUPPORTED_SUFFIXES and _safe_file_size(root):
            yield root
        return
    yielded = 0
    for path in _walk_supported_files(root):
        if not _safe_file_size(path):
            continue
        yield path
        yielded += 1
        if yielded >= MAX_LSP_FILES:
            return


def _walk_supported_files(root: Path) -> Iterable[Path]:
    for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.is_dir():
            if child.name in SKIPPED_DIRS:
                continue
            yield from _walk_supported_files(child)
        elif child.is_file() and child.suffix in SUPPORTED_SUFFIXES:
            yield child


def _safe_file_size(path: Path) -> bool:
    try:
        return path.stat().st_size <= MAX_LSP_FILE_BYTES
    except OSError:
        return False


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _python_diagnostic(path: Path, workspace: Path, allowed_roots: tuple[Path, ...], text: str) -> str | None:
    try:
        compile(text, display_workspace_path(workspace, path, allowed_roots), "exec")
    except SyntaxError as exc:
        rel = display_workspace_path(workspace, path, allowed_roots)
        line = exc.lineno or 1
        column = exc.offset or 1
        return f"{rel}:{line}:{column}: SyntaxError: {exc.msg}"
    return None


def _delimiter_diagnostic(path: Path, workspace: Path, allowed_roots: tuple[Path, ...], text: str) -> str | None:
    sanitized = _strip_strings_and_comments(text)
    stack: list[tuple[str, int, int]] = []
    pairs = {"(": ")", "{": "}", "[": "]"}
    closers = {")": "(", "}": "{", "]": "["}
    line = 1
    column = 0
    for char in sanitized:
        if char == "\n":
            line += 1
            column = 0
            continue
        column += 1
        if char in pairs:
            stack.append((char, line, column))
        elif char in closers:
            if not stack or stack[-1][0] != closers[char]:
                rel = display_workspace_path(workspace, path, allowed_roots)
                return f"{rel}:{line}:{column}: DelimiterError: unmatched '{char}'"
            stack.pop()
    if stack:
        opener, opener_line, opener_column = stack[-1]
        rel = display_workspace_path(workspace, path, allowed_roots)
        return f"{rel}:{opener_line}:{opener_column}: DelimiterError: missing closing '{pairs[opener]}'"
    return None


def _strip_strings_and_comments(text: str) -> str:
    result: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                state = "line_comment"
                result.extend("  ")
                index += 2
                continue
            if char == "/" and nxt == "*":
                state = "block_comment"
                result.extend("  ")
                index += 2
                continue
            if char in {"'", '"', "`"}:
                state = "string"
                quote = char
                result.append(" ")
                index += 1
                continue
            result.append(char)
            index += 1
            continue
        if state == "line_comment":
            result.append("\n" if char == "\n" else " ")
            if char == "\n":
                state = "code"
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and nxt == "/":
                result.extend("  ")
                state = "code"
                index += 2
                continue
            result.append("\n" if char == "\n" else " ")
            index += 1
            continue
        if state == "string":
            if char == "\\":
                result.append(" ")
                if nxt:
                    result.append("\n" if nxt == "\n" else " ")
                    index += 2
                    continue
            result.append("\n" if char == "\n" else " ")
            if char == quote:
                state = "code"
            index += 1
    return "".join(result)


def _looks_like_comment(stripped_line: str) -> bool:
    return stripped_line.startswith(("#", "//", "*", "/*", "<!--"))


class _PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self, path: Path):
        self.path = path
        self.records: list[SymbolRecord] = []
        self._scope: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self._add(node.name, "class", node)
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._add(node.name, "method" if self._scope else "function", node)
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._add(node.name, "async_method" if self._scope else "async_function", node)
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            self._add(alias.asname or alias.name.split(".")[0], "import", node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        for alias in node.names:
            self._add(alias.asname or alias.name, "import", node)

    def visit_Assign(self, node: ast.Assign) -> Any:
        if len(self._scope) > 1:
            return self.generic_visit(node)
        for target in node.targets:
            for name in _assignment_names(target):
                self._add(name, "variable", node)
        self.generic_visit(node)

    def _add(self, name: str, kind: str, node: ast.AST) -> None:
        if not name or not name.isidentifier():
            return
        self.records.append(
            SymbolRecord(
                path=self.path,
                name=name,
                kind=kind,
                line=getattr(node, "lineno", 1),
                column=getattr(node, "col_offset", 0),
                container=".".join(self._scope) if self._scope else None,
            )
        )


def _assignment_names(target: ast.AST) -> Iterable[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            yield from _assignment_names(item)
