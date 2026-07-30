from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from pathlib import Path

from .container_staging_contracts import ContainerStagingError
from .container_staging_contracts import ContainerStagingContainerBinding
from .container_staging_contracts import ContainerStagingJournalRecord
from .container_staging_contracts import ContainerStagingWorkspaceBinding
from .container_staging_contracts import STAGING_JOURNAL_SCHEMA
from .container_staging_contracts import staging_record_transition_is_valid
from .container_types import ContainerDirectoryPathIdentity
from .container_types import ContainerFileIdentity
from .container_types import ContainerRootIdentity
from .container_types import validate_attempt_id


STAGING_JOURNAL_DIRECTORY_NAME = ".lca-staging-journal"
STAGING_LOCK_NAME = ".lca-staging.lock"
STAGING_JOURNAL_LIMIT_BYTES = 64 * 1024
STAGING_ATTEMPT_NAME = re.compile(r"[0-9a-f]{32}")
STAGING_JOURNAL_NAME = re.compile(r"(?P<attempt>[0-9a-f]{32})\.json")
OPEN_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)


def open_or_create_staging_journal(root_fd: int) -> int:
    try:
        os.mkdir(STAGING_JOURNAL_DIRECTORY_NAME, mode=0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    descriptor = os.open(
        STAGING_JOURNAL_DIRECTORY_NAME,
        OPEN_DIRECTORY_FLAGS,
        dir_fd=root_fd,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise ContainerStagingError("staging_journal_authority_invalid")
    return descriptor


def write_staging_record(
    journal_fd: int,
    record: ContainerStagingJournalRecord,
    *,
    expected_identity: ContainerFileIdentity | None,
    authority_is_current: Callable[[], bool],
) -> ContainerFileIdentity:
    if not authority_is_current():
        raise ContainerStagingError("staging_authority_changed")
    payload = (
        json.dumps(
            record.payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > STAGING_JOURNAL_LIMIT_BYTES:
        raise ContainerStagingError("staging_journal_budget_exceeded")
    name = _journal_name(record.attempt_id)
    temporary = f".{record.attempt_id}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        if expected_identity is None:
            try:
                os.stat(name, dir_fd=journal_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ContainerStagingError("staging_journal_exists")
        else:
            observed, identity = read_staging_record(
                journal_fd,
                record.attempt_id,
                expected_identity=expected_identity,
            )
            if (
                not staging_record_transition_is_valid(observed, record)
                or identity != expected_identity
            ):
                raise ContainerStagingError(
                    "staging_journal_transition_invalid"
                )
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=journal_fd,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if expected_identity is None:
            os.link(
                temporary,
                name,
                src_dir_fd=journal_fd,
                dst_dir_fd=journal_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=journal_fd)
        else:
            os.replace(
                temporary,
                name,
                src_dir_fd=journal_fd,
                dst_dir_fd=journal_fd,
            )
        os.fsync(journal_fd)
        observed, identity = read_staging_record(
            journal_fd,
            record.attempt_id,
        )
        if observed != record:
            raise ContainerStagingError(
                "staging_journal_verification_failed"
            )
        return identity
    except ContainerStagingError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ContainerStagingError("staging_journal_write_failed") from exc
    finally:
        _close_fds(descriptor)
        try:
            os.unlink(temporary, dir_fd=journal_fd)
        except OSError:
            pass


def read_staging_record(
    journal_fd: int,
    attempt_id: str,
    *,
    expected_identity: ContainerFileIdentity | None = None,
) -> tuple[ContainerStagingJournalRecord, ContainerFileIdentity]:
    name = _journal_name(attempt_id)
    descriptor = -1
    try:
        inspected = os.stat(name, dir_fd=journal_fd, follow_symlinks=False)
        _assert_journal_metadata(inspected)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=journal_fd,
        )
        opened = os.fstat(descriptor)
        _assert_journal_metadata(opened)
        identity = ContainerFileIdentity.from_stat(opened)
        if (
            ContainerFileIdentity.from_stat(inspected) != identity
            or expected_identity is not None
            and identity != expected_identity
        ):
            raise ContainerStagingError("staging_journal_changed")
        if opened.st_size > STAGING_JOURNAL_LIMIT_BYTES:
            raise ContainerStagingError("staging_journal_budget_exceeded")
        payload = _read_exact(descriptor, opened.st_size)
        if ContainerFileIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise ContainerStagingError("staging_journal_changed")
        entry = os.stat(name, dir_fd=journal_fd, follow_symlinks=False)
        if ContainerFileIdentity.from_stat(entry) != identity:
            raise ContainerStagingError("staging_journal_changed")
        value = json.loads(payload.decode("utf-8", errors="strict"))
        record = _parse_record(value)
        if record.attempt_id != attempt_id:
            raise ContainerStagingError(
                "staging_journal_correlation_invalid"
            )
        return record, identity
    except ContainerStagingError:
        raise
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise ContainerStagingError("staging_journal_invalid") from exc
    finally:
        _close_fds(descriptor)


def scan_staging_records(
    journal_fd: int,
) -> dict[
    str,
    tuple[ContainerStagingJournalRecord, ContainerFileIdentity],
]:
    try:
        names = tuple(os.listdir(journal_fd))
    except OSError as exc:
        raise ContainerStagingError("staging_recovery_scan_failed") from exc
    records: dict[
        str,
        tuple[ContainerStagingJournalRecord, ContainerFileIdentity],
    ] = {}
    for name in names:
        match = STAGING_JOURNAL_NAME.fullmatch(name)
        if match is None:
            raise ContainerStagingError("staging_recovery_journal_invalid")
        attempt_id = match.group("attempt")
        record, identity = read_staging_record(journal_fd, attempt_id)
        if record.attempt_id in records:
            raise ContainerStagingError(
                "staging_recovery_journal_duplicate"
            )
        records[record.attempt_id] = (record, identity)
    return records


def remove_staging_record(
    journal_fd: int,
    record: ContainerStagingJournalRecord,
    identity: ContainerFileIdentity,
) -> None:
    observed, current = read_staging_record(
        journal_fd,
        record.attempt_id,
        expected_identity=identity,
    )
    if observed != record or current != identity:
        raise ContainerStagingError("staging_journal_changed")
    os.unlink(_journal_name(record.attempt_id), dir_fd=journal_fd)
    os.fsync(journal_fd)


def _parse_record(value: object) -> ContainerStagingJournalRecord:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "attempt_id",
        "state",
        "authority_root",
        "authority_identity",
        "authority_path_identity",
        "attempt_identity",
        "attempt_path_identity",
        "workspace_roots_revision",
        "workspace_roots",
        "container",
    }:
        raise ValueError("staging journal fields are invalid")
    if value["schema"] != STAGING_JOURNAL_SCHEMA:
        raise ValueError("staging journal schema is invalid")
    roots = value["workspace_roots"]
    if not isinstance(roots, list):
        raise ValueError("staging journal roots are invalid")
    return ContainerStagingJournalRecord(
        attempt_id=_text(value["attempt_id"]),
        state=_text(value["state"]),
        authority_root=Path(_text(value["authority_root"])),
        authority_identity=_parse_root_identity(value["authority_identity"]),
        authority_path_identity=_parse_directory_identity(
            value["authority_path_identity"]
        ),
        attempt_identity=_parse_optional_root_identity(
            value["attempt_identity"]
        ),
        attempt_path_identity=_parse_optional_directory_identity(
            value["attempt_path_identity"]
        ),
        workspace_roots_revision=_integer(value["workspace_roots_revision"]),
        workspace_roots=tuple(_parse_workspace_binding(item) for item in roots),
        container=_parse_optional_container_binding(value["container"]),
    )


def _parse_workspace_binding(
    value: object,
) -> ContainerStagingWorkspaceBinding:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "device",
        "inode",
        "manifest_sha256",
    }:
        raise ValueError("staging journal root fields are invalid")
    return ContainerStagingWorkspaceBinding(
        Path(_text(value["path"])),
        _integer(value["device"]),
        _integer(value["inode"]),
        _text(value["manifest_sha256"]),
    )


def _parse_root_identity(value: object) -> ContainerRootIdentity:
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "mode",
    }:
        raise ValueError("staging root identity fields are invalid")
    return ContainerRootIdentity(
        _integer(value["device"]),
        _integer(value["inode"]),
        _integer(value["mode"]),
    )


def _parse_optional_root_identity(
    value: object,
) -> ContainerRootIdentity | None:
    return None if value is None else _parse_root_identity(value)


def _parse_directory_identity(
    value: object,
) -> ContainerDirectoryPathIdentity:
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "mode",
        "changed_ns",
    }:
        raise ValueError("staging path identity fields are invalid")
    return ContainerDirectoryPathIdentity(
        _integer(value["device"]),
        _integer(value["inode"]),
        _integer(value["mode"]),
        _integer(value["changed_ns"]),
    )


def _parse_optional_directory_identity(
    value: object,
) -> ContainerDirectoryPathIdentity | None:
    return None if value is None else _parse_directory_identity(value)


def _parse_optional_container_binding(
    value: object,
) -> ContainerStagingContainerBinding | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "instance_name",
        "prep_instance_name",
        "volume_names",
        "runtime_image",
        "executable",
        "executable_sha256",
        "socket_path",
        "socket_identity",
        "client_config_directory",
        "client_config_identity",
        "gate_image_reference",
        "gate_image_digest",
    }:
        raise ValueError("staging container binding fields are invalid")
    volume_names = value["volume_names"]
    if not isinstance(volume_names, list):
        raise ValueError("staging volume bindings are invalid")
    return ContainerStagingContainerBinding(
        instance_name=_text(value["instance_name"]),
        prep_instance_name=_text(value["prep_instance_name"]),
        volume_names=tuple(_text(item) for item in volume_names),
        runtime_image=_text(value["runtime_image"]),
        executable=Path(_text(value["executable"])),
        executable_sha256=_text(value["executable_sha256"]),
        socket_path=Path(_text(value["socket_path"])),
        socket_identity=_parse_file_identity(value["socket_identity"]),
        client_config_directory=Path(
            _text(value["client_config_directory"])
        ),
        client_config_identity=_parse_file_identity(
            value["client_config_identity"]
        ),
        gate_image_reference=_text(value["gate_image_reference"]),
        gate_image_digest=_text(value["gate_image_digest"]),
    )


def _parse_file_identity(value: object) -> ContainerFileIdentity:
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "mode",
        "size",
        "modified_ns",
        "changed_ns",
    }:
        raise ValueError("staging file identity fields are invalid")
    return ContainerFileIdentity(
        _integer(value["device"]),
        _integer(value["inode"]),
        _integer(value["mode"]),
        _integer(value["size"]),
        _integer(value["modified_ns"]),
        _integer(value["changed_ns"]),
    )


def _assert_journal_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size < 1
    ):
        raise ContainerStagingError("staging_journal_invalid")


def _journal_name(attempt_id: str) -> str:
    validate_attempt_id(attempt_id)
    return f"{attempt_id}.json"


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise OSError("short staging journal read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short staging journal write")
        offset += written


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("staging journal text is invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("staging journal integer is invalid")
    return value


def _close_fds(*descriptors: int) -> None:
    for descriptor in descriptors:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


__all__ = [
    "OPEN_DIRECTORY_FLAGS",
    "STAGING_ATTEMPT_NAME",
    "STAGING_JOURNAL_DIRECTORY_NAME",
    "STAGING_LOCK_NAME",
    "open_or_create_staging_journal",
    "read_staging_record",
    "remove_staging_record",
    "scan_staging_records",
    "write_staging_record",
]
