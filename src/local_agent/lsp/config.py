from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path


JDTLS_METADATA_CONTAINMENT_OPTION = "-Djava.import.generatesMetadataFilesAtProjectRoot=false"


@dataclass(frozen=True)
class LspProcessEnvironment:
    append: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LspServerConfig:
    name: str
    command: tuple[str, ...]
    file_types: tuple[str, ...]
    root_markers: tuple[str, ...]
    language_id: str
    process_environment: LspProcessEnvironment = LspProcessEnvironment()

    def __post_init__(self) -> None:
        containment = ("JAVA_TOOL_OPTIONS", JDTLS_METADATA_CONTAINMENT_OPTION)
        append = self.process_environment.append
        if self.name == "jdtls" and (not append or append[-1] != containment):
            object.__setattr__(
                self,
                "process_environment",
                LspProcessEnvironment(append=(*append, containment)),
            )


@dataclass(frozen=True)
class LspServerIdentity:
    name: str
    command: tuple[str, ...]
    file_types: tuple[str, ...]
    root_markers: tuple[str, ...]
    language_id: str
    process_environment: tuple[tuple[str, str], ...]
    fingerprint: str


def server_identity(server: LspServerConfig) -> LspServerIdentity:
    payload = {
        "name": server.name,
        "command": list(server.command),
        "file_types": list(server.file_types),
        "root_markers": list(server.root_markers),
        "language_id": server.language_id,
        "process_environment": [list(item) for item in server.process_environment.append],
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return LspServerIdentity(
        name=server.name,
        command=server.command,
        file_types=server.file_types,
        root_markers=server.root_markers,
        language_id=server.language_id,
        process_environment=server.process_environment.append,
        fingerprint=fingerprint,
    )


LSP_MODE_ENV = "AGENT_LSP_MODE"

DEFAULT_SERVER_CONFIGS = (
    LspServerConfig(
        name="jdtls",
        command=("jdtls",),
        file_types=(".java",),
        root_markers=("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", ".project"),
        language_id="java",
    ),
    LspServerConfig(
        name="typescript-language-server",
        command=("typescript-language-server", "--stdio"),
        file_types=(".js", ".jsx", ".ts", ".tsx"),
        root_markers=("package.json", "tsconfig.json", "jsconfig.json"),
        language_id="typescript",
    ),
    LspServerConfig(
        name="vue-language-server",
        command=("vue-language-server", "--stdio"),
        file_types=(".vue",),
        root_markers=("package.json", "vue.config.js", "vue.config.ts", "vite.config.js", "vite.config.ts", "nuxt.config.js", "nuxt.config.ts"),
        language_id="vue",
    ),
)


def external_lsp_enabled() -> bool:
    mode = os.environ.get(LSP_MODE_ENV, "auto").strip().lower()
    return mode not in {"0", "false", "no", "off", "light", "fallback"}


def strict_external_lsp() -> bool:
    return os.environ.get(LSP_MODE_ENV, "auto").strip().lower() in {"external", "strict"}


def servers_for_path(workspace: Path, path: Path) -> list[LspServerConfig]:
    return [
        server
        for server in resolved_server_configs(workspace)
        if path.suffix in server.file_types and root_for_path(workspace, path, server) is not None
    ]


def servers_for_workspace(workspace: Path) -> list[LspServerConfig]:
    return [
        server
        for server in resolved_server_configs(workspace)
        if _has_root_marker(workspace, server.root_markers)
    ]


def root_for_path(workspace: Path, path: Path, server: LspServerConfig) -> Path | None:
    current = path if path.is_dir() else path.parent
    workspace = workspace.resolve()
    try:
        current = current.resolve()
    except OSError:
        return None
    while True:
        if _has_root_marker(current, server.root_markers):
            return current
        if current == workspace:
            return None
        try:
            current.relative_to(workspace)
        except ValueError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def resolved_server_configs(workspace: Path) -> list[LspServerConfig]:
    configs: list[LspServerConfig] = []
    for server in DEFAULT_SERVER_CONFIGS:
        override = _command_override(server.name)
        command = override or server.command
        resolved = _resolve_command(
            workspace,
            command,
            allow_workspace_executable=bool(override and Path(command[0]).is_absolute()),
        )
        if resolved is None:
            continue
        configs.append(
            LspServerConfig(
                name=server.name,
                command=resolved,
                file_types=server.file_types,
                root_markers=server.root_markers,
                language_id=server.language_id,
                process_environment=server.process_environment,
            )
        )
    return configs


def _command_override(server_name: str) -> tuple[str, ...] | None:
    env_name = {
        "jdtls": "AGENT_LSP_JDTLS_COMMAND",
        "typescript-language-server": "AGENT_LSP_TYPESCRIPT_COMMAND",
        "vue-language-server": "AGENT_LSP_VUE_COMMAND",
    }.get(server_name)
    if not env_name:
        return None
    raw = os.environ.get(env_name)
    if not raw:
        return None
    parts = tuple(shlex.split(raw))
    return parts or None


def _resolve_command(
    workspace: Path,
    command: tuple[str, ...],
    *,
    allow_workspace_executable: bool = False,
) -> tuple[str, ...] | None:
    if not command:
        return None
    executable = command[0]
    if os.path.isabs(executable):
        resolved = _canonical_executable(Path(executable))
    else:
        found = shutil.which(executable)
        resolved = _canonical_executable(Path(found)) if found else None
    if resolved is None:
        return None
    if not allow_workspace_executable and _is_within_workspace(resolved, workspace):
        return None
    return (str(resolved), *command[1:])


def _canonical_executable(path: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_file() else None


def _is_within_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False
    return True


def _has_root_marker(workspace: Path, markers: tuple[str, ...]) -> bool:
    return any((workspace / marker).exists() for marker in markers)
