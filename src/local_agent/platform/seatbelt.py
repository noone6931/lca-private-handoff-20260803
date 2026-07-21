from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence


SEATBELT_EXECUTABLE = Path("/usr/bin/sandbox-exec")
SEATBELT_BACKEND = "seatbelt"
SEATBELT_MODE = "workspace-write-network-deny"
SeatbeltErrorKind = Literal[
    "unsupported_platform",
    "backend_unavailable",
    "invalid_root",
    "launch_failed",
]

_PROFILE = """(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix*)
(allow ipc-sysv*)
(allow file-read*)
{write_rules}
"""


class SeatbeltPreparationError(RuntimeError):
    def __init__(self, kind: SeatbeltErrorKind, message: str, *, platform: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.platform = platform or _platform_label()

    def metadata(self) -> dict[str, object]:
        return _sandbox_metadata(sandboxed=False, error_kind=self.kind, platform=self.platform)


@dataclass(frozen=True)
class PreparedSeatbeltCommand:
    argv: tuple[str, ...]
    writable_roots: tuple[Path, ...]
    shell: bool = False

    def applied_metadata(self) -> dict[str, object]:
        return _sandbox_metadata(sandboxed=True, platform="macos")

    def launch_failed_metadata(self) -> dict[str, object]:
        return _sandbox_metadata(sandboxed=False, error_kind="launch_failed", platform="macos")


def trusted_runtime_temp_root() -> Path:
    """Resolve the process-level platform temp before any child overrides exist."""

    return _canonical_write_root(Path(tempfile.gettempdir()))


def prepare_seatbelt_command(
    command: str | Sequence[str],
    *,
    shell: bool,
    writable_roots: Sequence[Path],
) -> PreparedSeatbeltCommand:
    if sys.platform != "darwin":
        raise SeatbeltPreparationError(
            "unsupported_platform",
            "Seatbelt sandboxing is supported only on macOS.",
            platform=sys.platform,
        )
    if not SEATBELT_EXECUTABLE.is_file() or not os.access(SEATBELT_EXECUTABLE, os.X_OK):
        raise SeatbeltPreparationError(
            "backend_unavailable",
            "The trusted macOS Seatbelt backend is unavailable.",
        )

    roots = _canonical_write_roots(writable_roots)
    profile, definitions = _profile_and_definitions(roots)
    if shell:
        if not isinstance(command, str) or not command:
            raise SeatbeltPreparationError("invalid_root", "Seatbelt shell command must be non-empty.")
        child_argv = ("/bin/sh", "-c", command)
    else:
        if isinstance(command, str):
            raise SeatbeltPreparationError("invalid_root", "Seatbelt argv command must be structured.")
        child_argv = tuple(command)
        if not child_argv or any(not isinstance(value, str) or not value for value in child_argv):
            raise SeatbeltPreparationError("invalid_root", "Seatbelt argv command must be non-empty.")

    return PreparedSeatbeltCommand(
        argv=(str(SEATBELT_EXECUTABLE), "-p", profile, *definitions, "--", *child_argv),
        writable_roots=roots,
    )


def _canonical_write_roots(raw_roots: Sequence[Path]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for raw_root in raw_roots:
        root = _canonical_write_root(raw_root)
        if root not in roots:
            roots.append(root)
    if not roots:
        raise SeatbeltPreparationError("invalid_root", "Seatbelt requires at least one writable root.")
    return tuple(roots)


def _canonical_write_root(raw_root: Path) -> Path:
    try:
        if not raw_root.is_absolute():
            raise ValueError
        root = raw_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SeatbeltPreparationError("invalid_root", "Seatbelt writable root is invalid.") from exc
    if root == Path(root.anchor) or not root.is_dir():
        raise SeatbeltPreparationError("invalid_root", "Seatbelt writable root is invalid.")
    return root


def _profile_and_definitions(roots: tuple[Path, ...]) -> tuple[str, tuple[str, ...]]:
    parameters = tuple(f"LCA_WRITE_ROOT_{index}" for index in range(len(roots)))
    write_filters = [f'(subpath (param "{name}"))' for name in parameters]
    write_filters.extend(
        (
            '(literal (param "LCA_DEV_NULL"))',
            '(subpath (param "LCA_DEV_FD"))',
        )
    )
    write_rules = "(allow file-write*\n  " + "\n  ".join(write_filters) + ")"
    definitions: list[str] = []
    for name, root in zip(parameters, roots):
        definitions.extend(("-D", f"{name}={root}"))
    definitions.extend(("-D", "LCA_DEV_NULL=/dev/null", "-D", "LCA_DEV_FD=/dev/fd"))
    return _PROFILE.format(write_rules=write_rules), tuple(definitions)


def _platform_label() -> str:
    return "macos" if sys.platform == "darwin" else sys.platform


def _sandbox_metadata(
    *,
    sandboxed: bool,
    error_kind: SeatbeltErrorKind | None = None,
    platform: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "sandboxed": sandboxed,
        "sandbox_requested": True,
        "sandbox_platform": platform or _platform_label(),
        "sandbox_backend": SEATBELT_BACKEND,
        "sandbox_mode": SEATBELT_MODE,
    }
    if error_kind is not None:
        metadata["sandbox_error_kind"] = error_kind
    return metadata
