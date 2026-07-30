from __future__ import annotations

import ctypes
import errno
import os
import sys


_RENAME_NOREPLACE = 0x00000001
_RENAME_EXCHANGE = 0x00000002
_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004
_LIBC = ctypes.CDLL(None, use_errno=True)


def atomic_exchange_at(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
) -> None:
    _rename_at(
        source_directory_fd,
        source_name,
        destination_directory_fd,
        destination_name,
        darwin_flags=_RENAME_SWAP,
        linux_flags=_RENAME_EXCHANGE,
    )


def atomic_noreplace_at(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
) -> None:
    _rename_at(
        source_directory_fd,
        source_name,
        destination_directory_fd,
        destination_name,
        darwin_flags=_RENAME_EXCL,
        linux_flags=_RENAME_NOREPLACE,
    )


def atomic_rename_supported() -> bool:
    return (
        sys.platform == "darwin"
        and hasattr(_LIBC, "renameatx_np")
        or sys.platform.startswith("linux")
        and hasattr(_LIBC, "renameat2")
    )


def _rename_at(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
    *,
    darwin_flags: int,
    linux_flags: int,
) -> None:
    source = _name_bytes(source_name)
    destination = _name_bytes(destination_name)
    if sys.platform == "darwin" and hasattr(_LIBC, "renameatx_np"):
        function = _LIBC.renameatx_np
        flags = darwin_flags
    elif sys.platform.startswith("linux") and hasattr(_LIBC, "renameat2"):
        function = _LIBC.renameat2
        flags = linux_flags
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic rooted rename flags are unavailable",
        )
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = function(
        source_directory_fd,
        source,
        destination_directory_fd,
        destination,
        flags,
    )
    if result != 0:
        observed = ctypes.get_errno()
        raise OSError(
            observed or errno.EIO,
            os.strerror(observed or errno.EIO),
        )


def _name_bytes(name: str) -> bytes:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\0" in name
    ):
        raise ValueError("atomic rooted rename requires one path component")
    return os.fsencode(name)


__all__ = [
    "atomic_exchange_at",
    "atomic_noreplace_at",
    "atomic_rename_supported",
]
