from __future__ import annotations

import hashlib
import os
from pathlib import Path


def default_state_root() -> Path:
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / "local-coding-agent"
    return Path.home() / ".local" / "state" / "local-coding-agent"


def default_config_root() -> Path:
    raw_config_dir = os.environ.get("AGENT_CONFIG_DIR")
    if raw_config_dir:
        return Path(raw_config_dir).expanduser().resolve()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return (Path(xdg_config_home).expanduser() / "local-coding-agent").resolve()
    return (Path.home() / ".config" / "local-coding-agent").resolve()


def resolve_state_root(raw_state_dir: str | None, workspace: Path) -> Path:
    if raw_state_dir:
        state_root = Path(raw_state_dir).expanduser()
        if not state_root.is_absolute():
            state_root = workspace / state_root
        return state_root.resolve()
    return default_state_root().resolve()


def workspace_state_dir(state_root: Path, workspace: Path) -> Path:
    return state_root / "workspaces" / workspace_state_key(workspace)


def workspace_state_key(workspace: Path) -> str:
    resolved = workspace.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    label = _workspace_label(resolved)
    return f"{label}-{digest}"


def _workspace_label(workspace: Path) -> str:
    try:
        home = Path.home().resolve()
        source = str(workspace.relative_to(home))
    except ValueError:
        source = str(workspace)
    parts: list[str] = []
    previous_dash = False
    for char in source.lower():
        if char.isascii() and char.isalnum():
            parts.append(char)
            previous_dash = False
        elif not previous_dash:
            parts.append("-")
            previous_dash = True
    label = "".join(parts).strip("-")
    if not label:
        return "workspace"
    return label[:80].rstrip("-") or "workspace"
