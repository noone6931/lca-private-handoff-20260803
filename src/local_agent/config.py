from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when the agent cannot be configured safely."""


DEFAULT_MAX_STEPS = 0
DEFAULT_BUDGET_SECONDS = 600
DEFAULT_CONTEXT_CHAR_BUDGET = 60000
DEFAULT_CONTEXT_RECENT_MESSAGES = 40
TOOL_APPROVAL_POLICIES = {"allow", "prompt", "deny"}


@dataclass(frozen=True)
class AgentConfig:
    provider: str
    api_base_url: str
    api_key: str
    model: str
    workspace: Path
    max_steps: int = DEFAULT_MAX_STEPS
    budget_seconds: int | None = DEFAULT_BUDGET_SECONDS
    request_timeout: int = 120
    approval_mode: str = "ask"
    auto_approve_tools: tuple[str, ...] = ()
    tool_approval: dict[str, str] | None = None
    context_char_budget: int = DEFAULT_CONTEXT_CHAR_BUDGET
    context_recent_messages: int = DEFAULT_CONTEXT_RECENT_MESSAGES


def load_config(
    *,
    config_path: str | None,
    cwd: str | None,
    provider: str | None,
    api_base_url: str | None,
    api_key: str | None,
    model: str | None,
    max_steps: int | None,
    budget_seconds: int | None,
    approval_mode: str | None,
    auto_approve_tools: object | None = None,
    tool_approval: object | None = None,
    context_char_budget: int | None = None,
    context_recent_messages: int | None = None,
) -> AgentConfig:
    file_config = _load_json_config(config_path)
    workspace = Path(cwd or file_config.get("workspace") or os.getcwd()).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        raise ConfigError(f"Workspace does not exist or is not a directory: {workspace}")
    _load_dotenv(workspace / ".env")

    resolved_provider = _resolve_provider(provider or file_config.get("provider"))
    provider_defaults = _provider_defaults(resolved_provider)
    resolved_api_base_url = (
        api_base_url
        or file_config.get("api_base_url")
        or os.environ.get("AI_API_BASE_URL")
        or provider_defaults.get("api_base_url")
        or ""
    ).rstrip("/")
    resolved_api_key = _resolve_api_key(api_key, file_config, resolved_provider)
    resolved_model = (
        model
        or file_config.get("model")
        or os.environ.get("AI_MODEL")
        or provider_defaults.get("model")
        or ""
    )
    resolved_approval_mode = (
        approval_mode
        or file_config.get("approval_mode")
        or os.environ.get("AGENT_APPROVAL_MODE")
        or "ask"
    )
    raw_max_steps = max_steps if max_steps is not None else file_config.get("max_steps")
    resolved_max_steps = _non_negative_int(
        "max_steps",
        raw_max_steps if raw_max_steps is not None else DEFAULT_MAX_STEPS,
    )
    if budget_seconds is not None:
        raw_budget_seconds = budget_seconds
    elif "budget_seconds" in file_config:
        raw_budget_seconds = file_config.get("budget_seconds")
    elif os.environ.get("AGENT_BUDGET_SECONDS") is not None:
        raw_budget_seconds = os.environ.get("AGENT_BUDGET_SECONDS")
    else:
        raw_budget_seconds = DEFAULT_BUDGET_SECONDS
    resolved_budget_seconds = _optional_budget_seconds("budget_seconds", raw_budget_seconds)
    raw_request_timeout = file_config.get("request_timeout")
    resolved_request_timeout = _positive_int(
        "request_timeout",
        raw_request_timeout if raw_request_timeout is not None else 120,
    )
    raw_auto_approve_tools = (
        auto_approve_tools
        if auto_approve_tools is not None
        else file_config.get("auto_approve_tools", os.environ.get("AGENT_AUTO_APPROVE_TOOLS"))
    )
    resolved_auto_approve_tools = _tool_name_tuple("auto_approve_tools", raw_auto_approve_tools)
    raw_tool_approval = (
        tool_approval
        if tool_approval is not None
        else file_config.get("tool_approval", os.environ.get("AGENT_TOOL_APPROVAL"))
    )
    resolved_tool_approval = _tool_approval_map("tool_approval", raw_tool_approval)
    for tool in resolved_auto_approve_tools:
        resolved_tool_approval.setdefault(tool, "allow")
    raw_context_char_budget = (
        context_char_budget
        if context_char_budget is not None
        else file_config.get("context_char_budget", os.environ.get("AGENT_CONTEXT_CHAR_BUDGET"))
    )
    resolved_context_char_budget = _non_negative_int(
        "context_char_budget",
        raw_context_char_budget if raw_context_char_budget is not None else DEFAULT_CONTEXT_CHAR_BUDGET,
    )
    raw_context_recent_messages = (
        context_recent_messages
        if context_recent_messages is not None
        else file_config.get("context_recent_messages", os.environ.get("AGENT_CONTEXT_RECENT_MESSAGES"))
    )
    resolved_context_recent_messages = _positive_int(
        "context_recent_messages",
        raw_context_recent_messages
        if raw_context_recent_messages is not None
        else DEFAULT_CONTEXT_RECENT_MESSAGES,
    )

    if not resolved_api_base_url:
        raise ConfigError("Missing AI_API_BASE_URL.")
    if not resolved_api_key:
        raise ConfigError("Missing AI_API_KEY.")
    if not resolved_model:
        raise ConfigError("Missing AI_MODEL.")
    if resolved_approval_mode not in {"ask", "auto-read", "yolo"}:
        raise ConfigError("approval_mode must be one of: ask, auto-read, yolo.")

    return AgentConfig(
        provider=resolved_provider,
        api_base_url=resolved_api_base_url,
        api_key=resolved_api_key,
        model=resolved_model,
        workspace=workspace,
        max_steps=resolved_max_steps,
        budget_seconds=resolved_budget_seconds,
        request_timeout=resolved_request_timeout,
        approval_mode=resolved_approval_mode,
        auto_approve_tools=resolved_auto_approve_tools,
        tool_approval=resolved_tool_approval,
        context_char_budget=resolved_context_char_budget,
        context_recent_messages=resolved_context_recent_messages,
    )


def _load_json_config(config_path: str | None) -> dict:
    if not config_path:
        return {}
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ConfigError("Config file must contain a JSON object.")
    return data


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].strip()
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if not key or not all(char.isalnum() or char == "_" for char in key):
                continue
            os.environ.setdefault(key, _strip_env_quotes(value.strip()))


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _resolve_provider(raw_provider: str | None) -> str:
    if raw_provider:
        provider = raw_provider.strip().lower()
    elif os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("BAILIAN_API_KEY"):
        provider = "bailian"
    else:
        provider = "openai-compatible"
    aliases = {
        "aliyun": "bailian",
        "alibaba": "bailian",
        "dashscope": "bailian",
        "bailian-cn": "bailian",
        "dashscope-cn": "bailian",
        "dashscope-intl": "bailian-intl",
        "bailian-international": "bailian-intl",
    }
    provider = aliases.get(provider, provider)
    if provider not in {"openai-compatible", "bailian", "bailian-intl"}:
        raise ConfigError("provider must be one of: openai-compatible, bailian, bailian-intl.")
    return provider


def _provider_defaults(provider: str) -> dict[str, str]:
    if provider == "bailian":
        return {
            "api_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
        }
    if provider == "bailian-intl":
        return {
            "api_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
        }
    return {}


def _resolve_api_key(api_key: str | None, file_config: dict, provider: str) -> str:
    if api_key:
        return api_key
    if file_config.get("api_key"):
        return file_config["api_key"]
    if provider.startswith("bailian"):
        return (
            os.environ.get("DASHSCOPE_API_KEY")
            or os.environ.get("BAILIAN_API_KEY")
            or os.environ.get("AI_API_KEY")
            or ""
        )
    return os.environ.get("AI_API_KEY") or ""


def _positive_int(name: str, value: object) -> int:
    try:
        resolved = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if resolved < 1:
        raise ConfigError(f"{name} must be >= 1.")
    return resolved


def _non_negative_int(name: str, value: object) -> int:
    try:
        resolved = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if resolved < 0:
        raise ConfigError(f"{name} must be >= 0.")
    return resolved


def _optional_budget_seconds(name: str, value: object) -> int | None:
    if value is None or value == "":
        return None
    resolved = _non_negative_int(name, value)
    if resolved == 0:
        return None
    return resolved


def _tool_name_tuple(name: str, value: object) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        items = []
        for item in value:
            if not isinstance(item, str):
                raise ConfigError(f"{name} entries must be strings.")
            items.append(item.strip())
    else:
        raise ConfigError(f"{name} must be a comma-separated string or a list of strings.")

    tools = tuple(item for item in items if item)
    for tool in tools:
        _validate_tool_name(name, tool)
    return tools


def _tool_approval_map(name: str, value: object) -> dict[str, str]:
    if value is None or value == "":
        return {}
    raw_items: dict[str, object] = {}
    if isinstance(value, str):
        for item in value.split(","):
            part = item.strip()
            if not part:
                continue
            if "=" not in part:
                raise ConfigError(f"{name} entries must use tool=allow|prompt|deny.")
            tool, policy = part.split("=", 1)
            raw_items[tool.strip()] = policy.strip()
    elif isinstance(value, dict):
        raw_items = {str(tool).strip(): policy for tool, policy in value.items()}
    else:
        raise ConfigError(f"{name} must be a comma-separated tool=policy string or an object.")

    approvals: dict[str, str] = {}
    for tool, raw_policy in raw_items.items():
        _validate_tool_name(name, tool)
        if not isinstance(raw_policy, str):
            raise ConfigError(f"{name}.{tool} must be a string policy.")
        policy = raw_policy.strip().lower()
        if policy not in TOOL_APPROVAL_POLICIES:
            raise ConfigError(f"{name}.{tool} must be one of: allow, prompt, deny.")
        approvals[tool] = policy
    return approvals


def _validate_tool_name(name: str, tool: str) -> None:
    if not tool or not all(char.isalnum() or char == "_" for char in tool):
        raise ConfigError(f"{name} contains invalid tool name: {tool}")
