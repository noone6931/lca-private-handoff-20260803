from __future__ import annotations


PROVIDER_CREDENTIAL_ENVIRONMENT_KEYS = frozenset(
    {
        "AI_API_KEY",
        "BAILIAN_API_KEY",
        "DASHSCOPE_API_KEY",
    }
)
_PROVIDER_CREDENTIAL_ENVIRONMENT_KEY_FOLDS = frozenset(
    key.casefold() for key in PROVIDER_CREDENTIAL_ENVIRONMENT_KEYS
)


def is_provider_credential_environment_key(key: str) -> bool:
    return key.casefold() in _PROVIDER_CREDENTIAL_ENVIRONMENT_KEY_FOLDS


__all__ = [
    "PROVIDER_CREDENTIAL_ENVIRONMENT_KEYS",
    "is_provider_credential_environment_key",
]
