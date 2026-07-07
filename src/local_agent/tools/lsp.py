from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from local_agent.patch.anchored import PatchError, resolve_workspace_path

from .base import Tool, ToolContext, ToolResult
from .search import SKIPPED_DIRS

MAX_LSP_FILES = 300
MAX_LSP_FILE_BYTES = 768 * 1024
MAX_RESULT_LINE_CHARS = 240

SUPPORTED_SUFFIXES = {".py", ".java", ".js", ".jsx", ".ts", ".tsx", ".vue"}
JS_TS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".vue"}
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

    def render(self, workspace: Path) -> str:
        rel = self.path.relative_to(workspace)
        scoped = f"{self.container}.{self.name}" if self.container else self.name
        return f"{rel}:{self.line}:{self.column + 1}: {self.kind} {scoped}"


def lsp_tools() -> list[Tool]:
    languages = "Python, Java, JavaScript, TypeScript, and Vue"
    return [
        Tool(
            name="lsp_symbols",
            description=(
                f"List lightweight symbols for {languages} files under a workspace path. "
                "Use this for code navigation before broad text search."
            ),
            tier="read",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
            handler=lsp_symbols,
        ),
        Tool(
            name="lsp_definition",
            description=(
                f"Find lightweight definitions for {languages} symbols. "
                "Returns workspace-relative file, line, column, and symbol kind."
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
                "This is local find-references without starting an external LSP server."
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
                f"Run lightweight diagnostics for {languages} files under a workspace path. "
                "Python uses compile(); other languages use delimiter checks, not a full compiler."
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
    ]


def lsp_symbols(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _resolve_lsp_root(args, context)
    if isinstance(root, ToolResult):
        return root
    query = str(args.get("query") or "").strip()
    max_results = _max_results(args, default=80, upper=200)
    results: list[str] = []
    for record in _iter_symbol_records(root, context.workspace):
        rendered = record.render(context.workspace)
        if query and query.lower() not in rendered.lower():
            continue
        results.append(rendered)
        if len(results) >= max_results:
            results.append(f"... truncated after {max_results} symbols")
            break
    return ToolResult("\n".join(results) if results else "No supported code symbols found.")


def lsp_definition(args: dict[str, Any], context: ToolContext) -> ToolResult:
    symbol = _clean_symbol(args["symbol"])
    if symbol is None:
        return ToolResult("symbol must be a valid identifier, optionally dotted.", is_error=True)
    root = _resolve_lsp_root(args, context)
    if isinstance(root, ToolResult):
        return root
    max_results = _max_results(args, default=40, upper=100)
    matches: list[str] = []
    for record in _iter_symbol_records(root, context.workspace):
        if record.name == symbol or (record.container and f"{record.container}.{record.name}" == symbol):
            matches.append(record.render(context.workspace))
            if len(matches) >= max_results:
                matches.append(f"... truncated after {max_results} definitions")
                break
    return ToolResult("\n".join(matches) if matches else f"No definition found for: {symbol}")


def lsp_references(args: dict[str, Any], context: ToolContext) -> ToolResult:
    symbol = _clean_symbol(args["symbol"])
    if symbol is None:
        return ToolResult("symbol must be a valid identifier, optionally dotted.", is_error=True)
    root = _resolve_lsp_root(args, context)
    if isinstance(root, ToolResult):
        return root
    max_results = _max_results(args, default=80, upper=200)
    pattern = re.compile(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])")
    results: list[str] = []
    for path in _iter_supported_files(root):
        text = _read_text(path)
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if not match:
                continue
            rel = path.relative_to(context.workspace)
            snippet = line.strip()
            if len(snippet) > MAX_RESULT_LINE_CHARS:
                snippet = snippet[: MAX_RESULT_LINE_CHARS - 14] + "...<truncated>"
            results.append(f"{rel}:{line_number}:{match.start() + 1}: {snippet}")
            if len(results) >= max_results:
                results.append(f"... truncated after {max_results} references")
                return ToolResult("\n".join(results))
    return ToolResult("\n".join(results) if results else f"No references found for: {symbol}")


def lsp_diagnostics(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _resolve_lsp_root(args, context)
    if isinstance(root, ToolResult):
        return root
    max_results = _max_results(args, default=80, upper=200)
    diagnostics: list[str] = []
    for path in _iter_supported_files(root):
        text = _read_text(path)
        if text is None:
            continue
        if path.suffix == ".py":
            diagnostic = _python_diagnostic(path, context.workspace, text)
        else:
            diagnostic = _delimiter_diagnostic(path, context.workspace, text)
        if diagnostic:
            diagnostics.append(diagnostic)
            if len(diagnostics) >= max_results:
                diagnostics.append(f"... truncated after {max_results} diagnostics")
                break
    return ToolResult("\n".join(diagnostics) if diagnostics else "No lightweight diagnostics.")


def _resolve_lsp_root(args: dict[str, Any], context: ToolContext) -> Path | ToolResult:
    raw_path = args.get("path") or "."
    try:
        path = resolve_workspace_path(context.workspace, raw_path)
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


def _iter_symbol_records(root: Path, workspace: Path) -> Iterable[SymbolRecord]:
    for path in _iter_supported_files(root):
        text = _read_text(path)
        if text is None:
            continue
        if path.suffix == ".py":
            yield from _python_symbol_records(path, workspace, text)
        elif path.suffix == ".java":
            yield from _java_symbol_records(path, text)
        elif path.suffix in JS_TS_SUFFIXES:
            yield from _js_ts_vue_symbol_records(path, text)


def _python_symbol_records(path: Path, workspace: Path, text: str) -> Iterable[SymbolRecord]:
    try:
        tree = ast.parse(text, filename=str(path.relative_to(workspace)))
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


def _python_diagnostic(path: Path, workspace: Path, text: str) -> str | None:
    try:
        compile(text, str(path.relative_to(workspace)), "exec")
    except SyntaxError as exc:
        rel = path.relative_to(workspace)
        line = exc.lineno or 1
        column = exc.offset or 1
        return f"{rel}:{line}:{column}: SyntaxError: {exc.msg}"
    return None


def _delimiter_diagnostic(path: Path, workspace: Path, text: str) -> str | None:
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
                rel = path.relative_to(workspace)
                return f"{rel}:{line}:{column}: DelimiterError: unmatched '{char}'"
            stack.pop()
    if stack:
        opener, opener_line, opener_column = stack[-1]
        rel = path.relative_to(workspace)
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
