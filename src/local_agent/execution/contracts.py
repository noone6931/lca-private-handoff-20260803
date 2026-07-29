from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


IsolationMode = Literal["off", "preferred", "required"]
IsolationProfile = Literal["read-only", "workspace-write", "danger-full-access"]
IsolationBackendName = Literal["auto", "container", "linux-native", "local"]
IsolationAvailability = Literal["available", "unavailable", "unsupported"]
NetworkPolicy = Literal["deny", "allow"]

ISOLATION_MODES = frozenset({"off", "preferred", "required"})
ISOLATION_PROFILES = frozenset({"read-only", "workspace-write", "danger-full-access"})
ISOLATION_BACKENDS = frozenset({"auto", "container", "linux-native", "local"})
ISOLATION_AVAILABILITIES = frozenset({"available", "unavailable", "unsupported"})
NETWORK_POLICIES = frozenset({"deny", "allow"})


@dataclass(frozen=True)
class IsolationRequest:
    mode: IsolationMode
    profile: IsolationProfile
    backend: IsolationBackendName
    network_policy: NetworkPolicy
    workspace: Path
    readable_roots: tuple[Path, ...] = ()
    writable_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        _require_member("mode", self.mode, ISOLATION_MODES)
        _require_member("profile", self.profile, ISOLATION_PROFILES)
        _require_member("backend", self.backend, ISOLATION_BACKENDS)
        _require_member("network_policy", self.network_policy, NETWORK_POLICIES)
        _require_absolute("workspace", self.workspace)
        _require_absolute_roots("readable_roots", self.readable_roots)
        _require_absolute_roots("writable_roots", self.writable_roots)
        if self.profile == "read-only" and self.writable_roots:
            raise ValueError("read-only isolation cannot declare writable roots")
        if self.profile == "danger-full-access" and self.mode == "required":
            raise ValueError("danger-full-access cannot satisfy required isolation")
        if self.backend == "local" and self.mode == "required":
            raise ValueError("local backend cannot satisfy required isolation")

    @property
    def requires_applied_proof(self) -> bool:
        return self.mode == "required"


@dataclass(frozen=True)
class IsolationBackendCapability:
    backend: str
    availability: IsolationAvailability
    reason_code: str
    supported_profiles: frozenset[str]
    supported_network_policies: frozenset[str]
    enforces_isolation: bool

    def __post_init__(self) -> None:
        if not self.backend.strip():
            raise ValueError("backend must not be empty")
        _require_member("availability", self.availability, ISOLATION_AVAILABILITIES)
        if not self.reason_code.strip():
            raise ValueError("reason_code must not be empty")
        if not self.supported_profiles.issubset(ISOLATION_PROFILES):
            raise ValueError("supported_profiles contains an unknown profile")
        if not self.supported_network_policies.issubset(NETWORK_POLICIES):
            raise ValueError("supported_network_policies contains an unknown policy")
        if self.availability != "available" and self.enforces_isolation:
            raise ValueError("an unavailable backend cannot enforce isolation")
        if self.backend == "local" and self.enforces_isolation:
            raise ValueError("local backend must remain explicitly unsandboxed")

    def supports(self, request: IsolationRequest) -> bool:
        return (
            self.availability == "available"
            and request.profile in self.supported_profiles
            and request.network_policy in self.supported_network_policies
            and (self.enforces_isolation or request.mode != "required")
        )


@dataclass(frozen=True)
class AppliedIsolationProof:
    backend: str
    backend_instance_id: str
    profile: IsolationProfile
    network_policy: NetworkPolicy
    workspace: Path
    readable_roots: tuple[Path, ...]
    writable_roots: tuple[Path, ...]
    image_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.backend.strip() or self.backend == "local":
            raise ValueError("applied proof requires an enforcing backend")
        if not self.backend_instance_id.strip():
            raise ValueError("backend_instance_id must not be empty")
        _require_member("profile", self.profile, ISOLATION_PROFILES)
        _require_member("network_policy", self.network_policy, NETWORK_POLICIES)
        if self.profile == "danger-full-access":
            raise ValueError("danger-full-access is not an isolation proof")
        _require_absolute("workspace", self.workspace)
        _require_absolute_roots("readable_roots", self.readable_roots)
        _require_absolute_roots("writable_roots", self.writable_roots)

    def event_payload(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "backend_instance_id": self.backend_instance_id,
            "profile": self.profile,
            "network_policy": self.network_policy,
            "workspace": str(self.workspace),
            "readable_roots": [str(path) for path in self.readable_roots],
            "writable_roots": [str(path) for path in self.writable_roots],
            "image_digest": self.image_digest,
            "applied": True,
        }


class IsolationBackend(Protocol):
    @property
    def name(self) -> str:
        """Return the stable backend identifier."""

    def capability(self) -> IsolationBackendCapability:
        """Return a typed capability snapshot without starting user code."""


def _require_member(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")


def _require_absolute(name: str, path: Path) -> None:
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")


def _require_absolute_roots(name: str, roots: tuple[Path, ...]) -> None:
    if len(set(roots)) != len(roots):
        raise ValueError(f"{name} must not contain duplicates")
    for path in roots:
        _require_absolute(name, path)
