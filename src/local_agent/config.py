from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when the agent cannot be configured safely."""


DEFAULT_MAX_STEPS = 20


@dataclass(frozen=True)
class AgentConfig:
    provider: str
    api_base_url: str
    api_key: str
    model: str
    workspace: Path
    max_steps: int = DEFAULT_MAX_STEPS
    request_timeout: int = 120
    approval_mode: str = "ask"


def load_config(
    *,
    config_path: str | None,
    cwd: str | None,
    provider: str | None,
    api_base_url: str | None,
    api_key: str | None,
    model: str | None,
    max_steps: int | None,
    approval_mode: str | None,
) -> AgentConfig:
    file_config = _load_json_config(config_path)
    workspace = Path(cwd or file_config.get("workspace") or os.getcwd()).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        raise ConfigError(f"Workspace does not exist or is not a directory: {workspace}")

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
    resolved_max_steps = _positive_int(
        "max_steps",
        raw_max_steps if raw_max_steps is not None else DEFAULT_MAX_STEPS,
    )
    raw_request_timeout = file_config.get("request_timeout")
    resolved_request_timeout = _positive_int(
        "request_timeout",
        raw_request_timeout if raw_request_timeout is not None else 120,
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
        request_timeout=resolved_request_timeout,
        approval_mode=resolved_approval_mode,
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
