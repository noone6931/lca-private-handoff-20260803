from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .client import LspClientError
from .client import get_client
from .client import render_diagnostics
from .client import render_locations
from .client import render_provider_header
from .client import render_symbols
from .config import external_lsp_enabled
from .config import root_for_path
from .config import servers_for_path
from .config import servers_for_workspace
from .config import strict_external_lsp


@dataclass(frozen=True)
class ExternalLspAttempt:
    content: str | None = None
    error: str | None = None
    tried: bool = False
    strict: bool = False


def external_symbols(
    root: Path,
    *,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    supported_files: Iterable[Path],
    query: str,
    max_results: int,
) -> ExternalLspAttempt:
    if not external_lsp_enabled():
        return ExternalLspAttempt()
    lsp_root = _lsp_root_for_path(workspace, allowed_roots, root)
    strict = strict_external_lsp()
    if root.is_file():
        server_match = _first_server_for_path(lsp_root, root)
        if server_match is None:
            return _unavailable(strict, f"No external LSP server configured for {root.suffix}.")
        server, server_root = server_match
        try:
            symbols = get_client(server, server_root).document_symbols(root)
        except (OSError, LspClientError) as exc:
            return _failed(strict, server.name, exc)
        if query:
            symbols = [symbol for symbol in symbols if query.lower() in symbol.name.lower()]
        if not symbols:
            return _unavailable(strict, f"External LSP found no symbols for query: {query or '<empty>'}.")
        return ExternalLspAttempt(
            content="\n".join([render_provider_header(server), *render_symbols(symbols, workspace, allowed_roots, max_results=max_results)]),
            tried=True,
            strict=strict,
        )
    if not query:
        return ExternalLspAttempt()
    servers = servers_for_workspace(lsp_root)
    for server in servers:
        try:
            symbols = get_client(server, lsp_root).workspace_symbols(query)
        except (OSError, LspClientError):
            continue
        symbols = [symbol for symbol in symbols if _path_is_under_any(symbol.path, (root,))]
        if symbols:
            return ExternalLspAttempt(
                content="\n".join([render_provider_header(server), *render_symbols(symbols, workspace, allowed_roots, max_results=max_results)]),
                tried=True,
                strict=strict,
            )
    # If workspace/symbol is unsupported or empty, try the first local file as a document-symbol query.
    for path in supported_files:
        server_match = _first_server_for_path(lsp_root, path)
        if server_match is None:
            continue
        server, server_root = server_match
        try:
            symbols = get_client(server, server_root).document_symbols(path)
        except (OSError, LspClientError):
            continue
        symbols = [symbol for symbol in symbols if query.lower() in symbol.name.lower()]
        if symbols:
            return ExternalLspAttempt(
                content="\n".join([render_provider_header(server), *render_symbols(symbols, workspace, allowed_roots, max_results=max_results)]),
                tried=True,
                strict=strict,
            )
    return _unavailable(strict, f"External LSP found no symbols for query: {query}.")


def external_definition(
    root: Path,
    *,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    supported_files: Iterable[Path],
    symbol: str,
    max_results: int,
) -> ExternalLspAttempt:
    return _external_locations(
        root,
        workspace=workspace,
        allowed_roots=allowed_roots,
        supported_files=supported_files,
        symbol=symbol,
        max_results=max_results,
        action="definition",
    )


def external_references(
    root: Path,
    *,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    supported_files: Iterable[Path],
    symbol: str,
    max_results: int,
) -> ExternalLspAttempt:
    return _external_locations(
        root,
        workspace=workspace,
        allowed_roots=allowed_roots,
        supported_files=supported_files,
        symbol=symbol,
        max_results=max_results,
        action="references",
    )


def external_diagnostics(
    root: Path,
    *,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    max_results: int,
) -> ExternalLspAttempt:
    if not external_lsp_enabled():
        return ExternalLspAttempt()
    strict = strict_external_lsp()
    if not root.is_file():
        return ExternalLspAttempt()
    lsp_root = _lsp_root_for_path(workspace, allowed_roots, root)
    server_match = _first_server_for_path(lsp_root, root)
    if server_match is None:
        return _unavailable(strict, f"No external LSP server configured for {root.suffix}.")
    server, server_root = server_match
    try:
        diagnostics = get_client(server, server_root).diagnostics(root)
    except (OSError, LspClientError) as exc:
        return _failed(strict, server.name, exc)
    return ExternalLspAttempt(
        content="\n".join([render_provider_header(server), *render_diagnostics(diagnostics, workspace, allowed_roots, max_results=max_results)]),
        tried=True,
        strict=strict,
    )


def _external_locations(
    root: Path,
    *,
    workspace: Path,
    allowed_roots: tuple[Path, ...],
    supported_files: Iterable[Path],
    symbol: str,
    max_results: int,
    action: str,
) -> ExternalLspAttempt:
    if not external_lsp_enabled():
        return ExternalLspAttempt()
    strict = strict_external_lsp()
    lsp_root = _lsp_root_for_path(workspace, allowed_roots, root)
    for path in supported_files:
        if not _file_contains_symbol(path, symbol):
            continue
        server_match = _first_server_for_path(lsp_root, path)
        if server_match is None:
            continue
        server, server_root = server_match
        try:
            client = get_client(server, server_root)
            locations = client.definition(path, symbol) if action == "definition" else client.references(path, symbol)
        except (OSError, LspClientError) as exc:
            return _failed(strict, server.name, exc)
        if locations:
            return ExternalLspAttempt(
                content="\n".join([render_provider_header(server), *render_locations(locations, workspace, allowed_roots, max_results=max_results)]),
                tried=True,
                strict=strict,
            )
    return _unavailable(strict, f"External LSP found no {action} for: {symbol}.")


def _first_server_for_path(workspace: Path, path: Path):
    servers = servers_for_path(workspace, path)
    for server in servers:
        server_root = root_for_path(workspace, path, server)
        if server_root is not None:
            return server, server_root
    return None


def _lsp_root_for_path(workspace: Path, allowed_roots: tuple[Path, ...], path: Path) -> Path:
    for root in (workspace, *allowed_roots):
        if _path_is_under_any(path, (root,)):
            return root
    return workspace


def _path_is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _file_contains_symbol(path: Path, symbol: str) -> bool:
    try:
        return symbol in path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False


def _unavailable(strict: bool, reason: str) -> ExternalLspAttempt:
    return ExternalLspAttempt(error=f"[lsp provider] external unavailable: {reason}", tried=True, strict=strict)


def _failed(strict: bool, server: str, exc: BaseException) -> ExternalLspAttempt:
    return ExternalLspAttempt(error=f"[lsp provider] external {server} failed: {type(exc).__name__}: {exc}", tried=True, strict=strict)
