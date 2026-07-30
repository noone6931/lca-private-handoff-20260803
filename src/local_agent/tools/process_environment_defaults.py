from types import MappingProxyType

from ..execution.environment import PROVIDER_CREDENTIAL_ENVIRONMENT_KEYS

NONINTERACTIVE_ENVIRONMENT_DEFAULTS = MappingProxyType(
    {
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "MANPAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
    }
)


__all__ = [
    "NONINTERACTIVE_ENVIRONMENT_DEFAULTS",
    "PROVIDER_CREDENTIAL_ENVIRONMENT_KEYS",
]
