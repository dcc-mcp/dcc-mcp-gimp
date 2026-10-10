#!/usr/bin/env python3
"""Authenticated GIMP 3 bridge with bounded, main-thread host execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

_MAX_BOOTSTRAP_RECORD_BYTES = 64 * 1024
_MAX_BOOTSTRAP_RECORDS = 1024
_MAX_BOOTSTRAP_LOG_BYTES = 256 * 1024
_BOOTSTRAP_WRITE_LOCK = threading.Lock()
_BOOTSTRAP_RENAME_EXPECTED: Optional[tuple[int, int]] = None
_BOOTSTRAP_RENAME_HANDLE: Any = None
_BOOTSTRAP_POSIX_RENAME = os.rename
_BOOTSTRAP_POSIX_UNLINK = os.unlink
_BOOTSTRAP_POSIX_LINK = os.link


def _path_is_link_or_reparse(path: Path) -> bool:
    """Fail closed for symlinks and Windows reparse points (including junctions)."""
    try:
        if path.is_symlink():
            return True
        if os.name != "nt":
            return False
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if int(attributes) in (-1, 0xFFFFFFFF):
            return False
        return bool(int(attributes) & 0x400)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return True


def _bootstrap_path_safe(path: Path) -> bool:
    """Check the file and every existing parent without resolving links."""
    candidate = path
    while True:
        if _path_is_link_or_reparse(candidate):
            return False
        parent = candidate.parent
        if parent == candidate:
            return True
        candidate = parent


@contextmanager
def _windows_directory_lease(path: Path):
    """Hold a bootstrap parent without allowing pathname replacement."""
    if os.name != "nt":
        yield
        return
    import ctypes

    try:
        before = os.lstat(str(path))
        if _path_is_link_or_reparse(path):
            raise OSError("bootstrap parent is linked")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateFileW(
            str(path),
            0x80 | 0x20,
            0x1 | 0x2,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            raise OSError(ctypes.get_last_error(), "bootstrap parent lease failed")
        after = os.lstat(str(path))
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            kernel32.CloseHandle(handle)
            raise OSError("bootstrap parent changed identity")
    except (AttributeError, OSError, RuntimeError, ValueError):
        raise
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


@contextmanager
def _windows_object_handle(path: Path, *, access: int = 0x80):
    """Hold one bootstrap file with delete sharing disabled."""
    if os.name != "nt":
        yield
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateFileW(
        str(path),
        access,
        0x1 | 0x2,
        None,
        3,
        # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT:
        # inspect the named object itself and never traverse a swapped
        # junction/reparse point while holding the lease.
        0x02000000 | 0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), "bootstrap file handle failed")
    try:
        if _path_is_link_or_reparse(path):
            raise OSError("bootstrap file is linked")
        yield handle
    finally:
        kernel32.CloseHandle(handle)


def _bootstrap_object_identity(path: Path) -> tuple[int, int]:
    details = os.lstat(str(path))
    if not stat.S_ISREG(details.st_mode) or _path_is_link_or_reparse(path):
        raise OSError("bootstrap file is linked or not regular")
    return int(details.st_dev), int(details.st_ino)


def _windows_rename_by_handle(
    source: Path, destination: Path, *, expected_identity: Optional[tuple[int, int]] = None
) -> None:
    """Atomically rename a bootstrap file without replacing the destination."""
    import ctypes

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", ctypes.c_uint32),
            ("root_directory", ctypes.c_void_p),
            ("file_name_length", ctypes.c_uint32),
            ("file_name", ctypes.c_ubyte * 2),
        ]

    encoded = str(destination).encode("utf-16-le") + b"\x00\x00"
    header = _FileRenameInfo(0, None, len(encoded) - 2, (ctypes.c_ubyte * 2)())
    offset = _FileRenameInfo.file_name.offset
    payload = (ctypes.c_ubyte * (offset + len(encoded)))()
    ctypes.memmove(payload, ctypes.byref(header), ctypes.sizeof(header))
    ctypes.memmove(ctypes.addressof(payload) + offset, encoded, len(encoded))
    source_identity = _bootstrap_object_identity(source)
    bound_identity = expected_identity or _BOOTSTRAP_RENAME_EXPECTED
    if bound_identity is not None and source_identity != tuple(bound_identity):
        raise OSError("bootstrap temporary changed identity")

    def rename_with_handle(handle: Any) -> None:
        if _bootstrap_object_identity(source) != source_identity:
            raise OSError("bootstrap temporary changed identity")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetFileInformationByHandle.restype = ctypes.c_int
        if not kernel32.SetFileInformationByHandle(
            handle, 3, ctypes.byref(payload), ctypes.sizeof(payload)
        ):
            raise OSError(ctypes.get_last_error(), "bootstrap rename failed")

    bound_handle = _BOOTSTRAP_RENAME_HANDLE
    if bound_handle is not None:
        rename_with_handle(bound_handle)
    else:
        with _windows_object_handle(source, access=0x00010000 | 0x80) as handle:
            rename_with_handle(handle)


def _windows_create_new_file(path: Path, mode: int = 0o666) -> int:
    """Create a file with CREATE_NEW and OPEN_REPARSE_POINT semantics."""
    if os.name != "nt":
        raise OSError("Windows file creation is unavailable")
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        str(path),
        0x40000000,
        0x1 | 0x2,
        None,
        1,  # CREATE_NEW
        0x80 | 0x00200000,  # FILE_ATTRIBUTE_NORMAL | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), "bootstrap file create failed")
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_WRONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    if mode != 0o666:
        os.chmod(path, mode)
    return descriptor


def _windows_delete_handle(handle: Any) -> None:
    import ctypes

    class _FileDisposition(ctypes.Structure):
        _fields_ = [("delete_file", ctypes.c_ubyte)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.SetFileInformationByHandle.restype = ctypes.c_int
    if not kernel32.SetFileInformationByHandle(handle, 4, ctypes.byref(_FileDisposition(1)), 1):
        raise OSError(ctypes.get_last_error(), "bootstrap delete failed")


def _delete_bootstrap_owned(path: Path, expected_identity: tuple[int, int]) -> None:
    """Delete a Windows bootstrap temp through its identity-bound handle."""
    with _windows_object_handle(path, access=0x00010000 | 0x80) as handle:
        if _bootstrap_object_identity(path) != tuple(expected_identity)[:2]:
            raise OSError("bootstrap temporary changed identity")
        if os.name == "nt":
            _windows_delete_handle(handle)
        else:
            path.unlink()


def _bootstrap_error_path() -> Path:
    configured = os.environ.get("DCC_MCP_GIMP_BOOTSTRAP_ERRORS")
    if configured:
        return Path(configured).expanduser().absolute()
    return Path.home().joinpath(".dcc-mcp", "gimp-bootstrap-errors.jsonl").absolute()


def _capture_bootstrap_error(stage: str, error: BaseException) -> None:
    global _BOOTSTRAP_RENAME_EXPECTED, _BOOTSTRAP_RENAME_HANDLE
    path = _bootstrap_error_path()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "error_type": type(error).__name__,
        "message": str(error)[:_MAX_BOOTSTRAP_RECORD_BYTES],
    }

    def write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]

    # Keep AST-extracted smoke probes functional while production imports use
    # the original primitives captured before any caller can replace os APIs.
    posix_rename = globals().get("_BOOTSTRAP_POSIX_RENAME", os.rename)
    posix_unlink = globals().get("_BOOTSTRAP_POSIX_UNLINK", os.unlink)
    posix_link = globals().get("_BOOTSTRAP_POSIX_LINK", os.link)

    def unlink_bound(name: str, expected: tuple[int, int]) -> None:
        """Remove a descriptor-relative name without consuming a swap."""
        cleanup_name = ".%s.bootstrap-cleanup-%s" % (name, uuid.uuid4().hex)
        try:
            posix_rename(
                name,
                cleanup_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return
        moved = os.stat(cleanup_name, dir_fd=parent_descriptor, follow_symlinks=False)
        moved_identity = (int(moved.st_dev), int(moved.st_ino))
        if moved_identity != tuple(expected)[:2]:
            try:
                posix_link(
                    cleanup_name,
                    name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                pass
            raise OSError("bootstrap object changed identity")
        posix_unlink(cleanup_name, dir_fd=parent_descriptor)

    try:
        # Serialize all readers/writers in this process.  Without this lock,
        # concurrent startup failures can each observe the old size and append
        # past the bounded log cap.
        with _BOOTSTRAP_WRITE_LOCK:
            if not _bootstrap_path_safe(path):
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            if not _bootstrap_path_safe(path):
                return
            line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )

            def rotate(existing: bytes, current_size: int) -> bytes:
                lines = existing.splitlines(keepends=True)
                if current_size > _MAX_BOOTSTRAP_LOG_BYTES and b"\n" in existing:
                    lines = lines[1:]
                lines = lines[-(_MAX_BOOTSTRAP_RECORDS - 1) :]
                while (
                    lines
                    and sum(len(item) for item in lines) + len(line) > _MAX_BOOTSTRAP_LOG_BYTES
                ):
                    lines.pop(0)
                return b"".join(lines) + line

            if os.name == "nt":
                with _windows_directory_lease(path.parent):
                    if not _bootstrap_path_safe(path):
                        return
                    try:
                        details = os.lstat(str(path))
                    except FileNotFoundError:
                        details = None
                    if details is not None and (
                        not os.path.isfile(path) or _path_is_link_or_reparse(path)
                    ):
                        return
                    current_size = int(details.st_size) if details is not None else 0
                    if current_size + len(line) <= _MAX_BOOTSTRAP_LOG_BYTES:
                        if details is None:
                            descriptor = _windows_create_new_file(path)
                            created = os.fstat(descriptor)
                            created_identity = (int(created.st_dev), int(created.st_ino))
                            try:
                                try:
                                    write_all(descriptor, line)
                                    os.fsync(descriptor)
                                finally:
                                    os.close(descriptor)
                            except BaseException:
                                try:
                                    _delete_bootstrap_owned(path, created_identity)
                                except (FileNotFoundError, OSError):
                                    pass
                                raise
                        else:
                            before = (int(details.st_dev), int(details.st_ino))
                            with _windows_object_handle(path):
                                opened = os.lstat(str(path))
                                if (int(opened.st_dev), int(opened.st_ino)) != before:
                                    return
                                descriptor = os.open(
                                    str(path),
                                    os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0),
                                )
                                try:
                                    descriptor_details = os.fstat(descriptor)
                                    if (
                                        int(descriptor_details.st_dev),
                                        int(descriptor_details.st_ino),
                                    ) != before:
                                        return
                                    try:
                                        write_all(descriptor, line)
                                        os.fsync(descriptor)
                                    except BaseException:
                                        os.ftruncate(descriptor, current_size)
                                        os.fsync(descriptor)
                                        raise
                                finally:
                                    os.close(descriptor)
                                after = os.lstat(str(path))
                                if (int(after.st_dev), int(after.st_ino)) != before:
                                    return
                        return
                    with _windows_object_handle(path):
                        with path.open("rb") as stream:
                            stream.seek(max(0, current_size - _MAX_BOOTSTRAP_LOG_BYTES))
                            existing = stream.read(_MAX_BOOTSTRAP_LOG_BYTES)
                    payload = rotate(existing, current_size)
                    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
                    if not _bootstrap_path_safe(temporary):
                        return
                    temporary_descriptor = _windows_create_new_file(temporary)
                    temporary_details = os.fstat(temporary_descriptor)
                    temporary_identity = (
                        int(temporary_details.st_dev),
                        int(temporary_details.st_ino),
                    )
                    try:
                        try:
                            write_all(temporary_descriptor, payload)
                            os.fsync(temporary_descriptor)
                        finally:
                            os.close(temporary_descriptor)
                    except BaseException:
                        try:
                            _delete_bootstrap_owned(temporary, temporary_identity)
                        except (FileNotFoundError, OSError):
                            pass
                        raise
                    if _bootstrap_object_identity(temporary) != temporary_identity:
                        raise OSError("bootstrap temporary changed identity")
                    backup = path.with_name(
                        ".%s.bootstrap-backup-%s" % (path.name, uuid.uuid4().hex)
                    )
                    backup_identity = None
                    publication_succeeded = False
                    try:
                        if _bootstrap_path_safe(path):
                            existing_identity = (int(details.st_dev), int(details.st_ino))
                            _BOOTSTRAP_RENAME_EXPECTED = existing_identity
                            with _windows_object_handle(
                                path, access=0x00010000 | 0x80
                            ) as existing_handle:
                                _BOOTSTRAP_RENAME_HANDLE = existing_handle
                                _windows_rename_by_handle(
                                    path,
                                    backup,
                                    expected_identity=existing_identity,
                                )
                            _BOOTSTRAP_RENAME_HANDLE = None
                            _BOOTSTRAP_RENAME_EXPECTED = None
                            backup_identity = _bootstrap_object_identity(backup)
                            if backup_identity != existing_identity:
                                raise OSError("bootstrap log changed identity")
                            _BOOTSTRAP_RENAME_EXPECTED = temporary_identity
                            with _windows_object_handle(
                                temporary, access=0x00010000 | 0x80
                            ) as temporary_handle:
                                _BOOTSTRAP_RENAME_HANDLE = temporary_handle
                                _windows_rename_by_handle(
                                    temporary,
                                    path,
                                    expected_identity=temporary_identity,
                                )
                            _BOOTSTRAP_RENAME_HANDLE = None
                            _BOOTSTRAP_RENAME_EXPECTED = None
                            if _bootstrap_object_identity(path) != temporary_identity:
                                raise OSError("bootstrap temporary changed identity")
                            publication_succeeded = True
                    except BaseException:
                        _BOOTSTRAP_RENAME_HANDLE = None
                        _BOOTSTRAP_RENAME_EXPECTED = None
                        if backup_identity is not None:
                            try:
                                _bootstrap_object_identity(path)
                            except (FileNotFoundError, OSError):
                                try:
                                    _BOOTSTRAP_RENAME_EXPECTED = backup_identity
                                    with _windows_object_handle(
                                        backup, access=0x00010000 | 0x80
                                    ) as backup_handle:
                                        _BOOTSTRAP_RENAME_HANDLE = backup_handle
                                        _windows_rename_by_handle(
                                            backup,
                                            path,
                                            expected_identity=backup_identity,
                                        )
                                    _BOOTSTRAP_RENAME_HANDLE = None
                                    _BOOTSTRAP_RENAME_EXPECTED = None
                                    if _bootstrap_object_identity(path) != backup_identity:
                                        raise OSError("bootstrap rollback changed identity")
                                    backup_identity = None
                                except OSError:
                                    pass
                        raise
                    finally:
                        _BOOTSTRAP_RENAME_HANDLE = None
                        _BOOTSTRAP_RENAME_EXPECTED = None
                        if publication_succeeded and backup_identity is not None:
                            try:
                                _delete_bootstrap_owned(backup, backup_identity)
                            except (FileNotFoundError, OSError):
                                pass
                        try:
                            if _bootstrap_object_identity(temporary) == temporary_identity:
                                _delete_bootstrap_owned(temporary, temporary_identity)
                        except (FileNotFoundError, OSError):
                            pass
                return

            parent_before = os.lstat(str(path.parent))
            if not stat.S_ISDIR(parent_before.st_mode) or _path_is_link_or_reparse(path.parent):
                return
            parent_descriptor = os.open(
                str(path.parent),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            parent_opened = os.fstat(parent_descriptor)
            if (int(parent_opened.st_dev), int(parent_opened.st_ino)) != (
                int(parent_before.st_dev),
                int(parent_before.st_ino),
            ):
                return
            try:
                try:
                    details = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    details = None
                if details is not None and not stat.S_ISREG(details.st_mode):
                    return
                current_size = int(details.st_size) if details is not None else 0
                if current_size + len(line) <= _MAX_BOOTSTRAP_LOG_BYTES:
                    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                    if details is None:
                        flags |= os.O_CREAT | os.O_EXCL
                    descriptor = os.open(path.name, flags, 0o666, dir_fd=parent_descriptor)
                    try:
                        opened = os.fstat(descriptor)
                        if details is not None and (int(opened.st_dev), int(opened.st_ino)) != (
                            int(details.st_dev),
                            int(details.st_ino),
                        ):
                            return
                        try:
                            write_all(descriptor, line)
                            os.fsync(descriptor)
                        except BaseException:
                            if details is not None:
                                os.ftruncate(descriptor, int(details.st_size))
                                os.fsync(descriptor)
                            raise
                    finally:
                        os.close(descriptor)
                    return
                descriptor = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_descriptor,
                )
                try:
                    opened = os.fstat(descriptor)
                    if (int(opened.st_dev), int(opened.st_ino)) != (
                        int(details.st_dev),
                        int(details.st_ino),
                    ):
                        return
                    os.lseek(
                        descriptor, max(0, current_size - _MAX_BOOTSTRAP_LOG_BYTES), os.SEEK_SET
                    )
                    existing = os.read(descriptor, _MAX_BOOTSTRAP_LOG_BYTES)
                finally:
                    os.close(descriptor)
                payload = rotate(existing, current_size)
                # Stage the complete bounded payload before changing the log
                # name.  This keeps a short write or disk-full error from
                # truncating the previous diagnostics.  Move the old inode to
                # a private backup so a raced temporary can be preserved and
                # the previous log restored before the failure is swallowed.
                temporary_name = ".%s.%s.tmp" % (path.name, uuid.uuid4().hex)
                temporary_descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o666,
                    dir_fd=parent_descriptor,
                )
                temporary_opened = os.fstat(temporary_descriptor)
                temporary_identity = (
                    int(temporary_opened.st_dev),
                    int(temporary_opened.st_ino),
                )
                backup_name = ".%s.bootstrap-backup-%s" % (path.name, uuid.uuid4().hex)
                backup_identity = None
                committed = False
                publication_succeeded = False
                try:
                    try:
                        write_all(temporary_descriptor, payload)
                        os.fsync(temporary_descriptor)
                    except BaseException:
                        try:
                            unlink_bound(temporary_name, temporary_identity)
                        except OSError:
                            pass
                        raise
                finally:
                    os.close(temporary_descriptor)
                try:
                    posix_rename(
                        path.name,
                        backup_name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                    backup_details = os.stat(
                        backup_name, dir_fd=parent_descriptor, follow_symlinks=False
                    )
                    backup_identity = (
                        int(backup_details.st_dev),
                        int(backup_details.st_ino),
                    )
                    if backup_identity != (int(details.st_dev), int(details.st_ino)):
                        raise OSError("bootstrap log changed identity")
                    posix_link(
                        temporary_name,
                        path.name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    committed = True
                    published = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                    if (int(published.st_dev), int(published.st_ino)) != temporary_identity:
                        raise OSError("bootstrap temporary changed identity")
                    os.fsync(parent_descriptor)
                    publication_succeeded = True
                except BaseException:
                    if committed and temporary_identity is not None:
                        try:
                            published = os.stat(
                                path.name, dir_fd=parent_descriptor, follow_symlinks=False
                            )
                        except OSError:
                            published = None
                        if (
                            published is not None
                            and (int(published.st_dev), int(published.st_ino)) != temporary_identity
                        ):
                            foreign_name = ".%s.bootstrap-foreign-%s" % (
                                path.name,
                                uuid.uuid4().hex,
                            )
                            try:
                                posix_rename(
                                    path.name,
                                    foreign_name,
                                    src_dir_fd=parent_descriptor,
                                    dst_dir_fd=parent_descriptor,
                                )
                            except OSError:
                                pass
                    if backup_identity is not None:
                        try:
                            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                        except OSError:
                            try:
                                posix_link(
                                    backup_name,
                                    path.name,
                                    src_dir_fd=parent_descriptor,
                                    dst_dir_fd=parent_descriptor,
                                    follow_symlinks=False,
                                )
                                backup_current = os.stat(
                                    path.name,
                                    dir_fd=parent_descriptor,
                                    follow_symlinks=False,
                                )
                                if (
                                    int(backup_current.st_dev),
                                    int(backup_current.st_ino),
                                ) != backup_identity:
                                    raise OSError("bootstrap backup changed identity")
                                unlink_bound(backup_name, backup_identity)
                                backup_identity = None
                            except OSError:
                                pass
                    raise
                finally:
                    try:
                        if publication_succeeded and backup_identity is not None:
                            backup_current = os.stat(
                                backup_name, dir_fd=parent_descriptor, follow_symlinks=False
                            )
                            if (
                                int(backup_current.st_dev),
                                int(backup_current.st_ino),
                            ) == backup_identity:
                                unlink_bound(backup_name, backup_identity)
                    except OSError:
                        pass
                    try:
                        temporary_current = os.stat(
                            temporary_name, dir_fd=parent_descriptor, follow_symlinks=False
                        )
                    except OSError:
                        temporary_current = None
                    if temporary_current is not None and temporary_identity is not None:
                        if (
                            int(temporary_current.st_dev),
                            int(temporary_current.st_ino),
                        ) == temporary_identity:
                            try:
                                unlink_bound(temporary_name, temporary_identity)
                            except OSError:
                                pass
            finally:
                os.close(parent_descriptor)
    except (OSError, RuntimeError, ValueError, AttributeError):
        pass


@contextmanager
def capture_bootstrap_errors(stage: str):
    """Record and re-raise startup failures while GIMP loads the plug-in."""
    try:
        yield
    except BaseException as exc:
        _capture_bootstrap_error(stage, exc)
        raise


with capture_bootstrap_errors("gi-import"):
    import gi

    gi.require_version("Gegl", "0.4")
    gi.require_version("Gimp", "3.0")
    from gi.repository import Gegl, Gimp, Gio, GLib  # noqa: E402


VERSION = "0.6.0"  # x-release-please-version
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = int(os.environ.get("DCC_MCP_GIMP_BRIDGE_PORT", "3848"))
MAX_CONNECTIONS = 16
MAX_PENDING_COMMANDS = 32
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000
MAX_PREVIEW_SOURCE_PIXELS = 16_777_216
MAX_PREVIEW_LAYER_PIXELS = 134_217_728
MAX_PREVIEW_LAYER_NODES = 256
MAX_LAYER_NODES = 20_000
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMMAND_TIMEOUT_SECS = 1_800.0
MAX_PARASITE_BYTES = 16 * 1024 * 1024
OPEN_SUFFIXES = frozenset(
    {".xcf", ".ora", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".psd", ".exr"}
)
EXPORT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"})


class HostCommandError(RuntimeError):
    """A typed request violates the GIMP host or safety contract."""


class _PendingCommand:
    def __init__(self, method: str, params: Mapping[str, Any]) -> None:
        self.method = method
        self.params = dict(params)
        self.completed = threading.Event()
        self.lock = threading.Lock()
        self.started = False
        self.cancelled = False
        self.result: Any = None
        self.error: Optional[BaseException] = None


_pending_commands = threading.BoundedSemaphore(MAX_PENDING_COMMANDS)
_owned_displays: dict[int, Any] = {}
_bridge_token = ""


def _process_start_identity(pid: int) -> Optional[str]:
    """Capture the parent creation identity once so a reused PID cannot attest readiness."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return "windows-filetime:%d" % value
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform == "darwin":
        import ctypes

        class _ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            libproc.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            libproc.proc_pidinfo.restype = ctypes.c_int
            info = _ProcBsdInfo()
            size = ctypes.sizeof(info)
            returned = libproc.proc_pidinfo(int(pid), 3, 0, ctypes.byref(info), size)
        except (OSError, AttributeError):
            return None
        if returned != size or info.pbi_start_tvsec <= 0:
            return None
        return "darwin-timeval:%d:%d" % (info.pbi_start_tvsec, info.pbi_start_tvusec)
    try:
        process_stat = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8")
        closing = process_stat.rfind(") ")
        start_ticks = process_stat[closing + 2 :].split()[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (IndexError, OSError, ValueError):
        return None
    return "linux:%s:%s" % (boot_id, start_ticks)


_gimp_pid = os.getppid()
_gimp_start_identity = _process_start_identity(_gimp_pid)


def _split_roots(value: str) -> tuple[Path, ...]:
    return tuple(
        Path(item.strip()).expanduser().resolve()
        for item in value.split(os.pathsep)
        if item.strip()
    )


def _allowed_roots() -> tuple[Path, ...]:
    return _split_roots(os.environ.get("DCC_MCP_GIMP_ALLOWED_ROOTS", ""))


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    candidate = os.path.normcase(str(path))
    for root in roots:
        normalized_root = os.path.normcase(str(root))
        try:
            if os.path.commonpath((candidate, normalized_root)) == normalized_root:
                return True
        except ValueError:
            continue
    return False


def _safe_text(value: Any, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HostCommandError("%s must be a non-empty string" % label)
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise HostCommandError("%s is invalid or exceeds %d characters" % (label, maximum))
    return value.strip()


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostCommandError("%s must be an integer" % label)
    if value < minimum or value > maximum:
        raise HostCommandError("%s must be between %d and %d" % (label, minimum, maximum))
    return value


def _typed_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise HostCommandError("%s must be a boolean" % label)
    return value


def _bounded_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostCommandError("%s must be a number" % label)
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise HostCommandError("%s must be between %s and %s" % (label, minimum, maximum))
    return number


def _token_path() -> Path:
    configured = os.environ.get("DCC_MCP_GIMP_BRIDGE_TOKEN_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home().joinpath(".dcc-mcp", "gimp-bridge-token").resolve()


def _load_or_create_token() -> str:
    configured = os.environ.get("DCC_MCP_GIMP_BRIDGE_TOKEN", "")
    if configured:
        if len(configured) < 32:
            raise RuntimeError("DCC_MCP_GIMP_BRIDGE_TOKEN must contain at least 32 characters")
        return configured
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(token)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token
    except FileExistsError:
        for _attempt in range(10):
            try:
                existing = path.read_text(encoding="utf-8").strip()
            except OSError:
                existing = ""
            if len(existing) >= 32:
                return existing
            time.sleep(0.02)
    raise RuntimeError("GIMP bridge token file is missing, unreadable, or invalid")


def _input_path(value: Any) -> Path:
    roots = _allowed_roots()
    if not roots:
        raise HostCommandError("DCC_MCP_GIMP_ALLOWED_ROOTS is required for file access")
    path = Path(_safe_text(value, "path", 2_048)).expanduser().resolve()
    if not _within(path, roots):
        raise HostCommandError("Input path is outside DCC_MCP_GIMP_ALLOWED_ROOTS")
    if path.suffix.lower() not in OPEN_SUFFIXES:
        raise HostCommandError("Input file type is not supported by the adapter")
    if not path.is_file():
        raise HostCommandError("Input file does not exist")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise HostCommandError("Input file exceeds the configured size limit")
    return path


def _output_path(value: Any, suffixes: frozenset[str], overwrite: bool) -> Path:
    roots = _allowed_roots()
    if not roots:
        raise HostCommandError("DCC_MCP_GIMP_ALLOWED_ROOTS is required for file access")
    path = Path(_safe_text(value, "path", 2_048)).expanduser().resolve()
    if not _within(path, roots):
        raise HostCommandError("Output path is outside DCC_MCP_GIMP_ALLOWED_ROOTS")
    if path.suffix.lower() not in suffixes:
        raise HostCommandError("Output file type is not supported by the adapter")
    if not path.parent.is_dir():
        raise HostCommandError("Output parent directory does not exist")
    if path.exists() and not overwrite:
        raise HostCommandError("Output exists; set overwrite=true to replace it")
    if path.exists() and not path.is_file():
        raise HostCommandError("Output path is not a regular file")
    return path


def _preview_directory_identity(path: Path) -> tuple[int, int]:
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or _path_is_link_or_reparse(path):
        raise HostCommandError("Preview directory must be a regular directory")
    return int(details.st_dev), int(details.st_ino)


def _preview_dimensions(width: int, height: int, max_width: int, max_height: int):
    """Fit within an integer box, flooring the other axis and never upscaling."""
    if width <= max_width and height <= max_height:
        return width, height
    if max_width * height <= max_height * width:
        return max_width, max(1, height * max_width // width)
    return max(1, width * max_height // height), max_height


def _preview_admit_source(image: Any) -> tuple[int, int]:
    width = _bounded_int(image.get_width(), "source width", 1, 8192)
    height = _bounded_int(image.get_height(), "source height", 1, 8192)
    if width * height > MAX_PREVIEW_SOURCE_PIXELS:
        raise HostCommandError("Preview source exceeds 16777216 pixels")
    if (
        image.get_base_type() != Gimp.ImageBaseType.RGB
        or image.get_precision() != Gimp.Precision.U8_NON_LINEAR
    ):
        raise HostCommandError("Preview requires RGB/RGBA 8-bit non-linear precision")
    if image.get_channels() or image.get_paths() or image.get_floating_sel() is not None:
        raise HostCommandError(
            "Preview does not support saved channels, paths or floating selections"
        )
    _preview_parasite_admission(image)
    selection = image.get_selection()
    _preview_parasite_admission(selection)
    if selection.get_filters():
        raise HostCommandError("Preview does not support nonempty selection filters")
    stack = list(image.get_layers())
    nodes, pixels = 0, 0
    while stack:
        layer = stack.pop()
        nodes += 1
        if nodes > MAX_PREVIEW_LAYER_NODES:
            raise HostCommandError("Preview source exceeds 256 layer nodes")
        w = _bounded_int(layer.get_width(), "layer width", 1, 8192)
        h = _bounded_int(layer.get_height(), "layer height", 1, 8192)
        _preview_parasite_admission(layer)
        if layer.get_filters():
            raise HostCommandError("Preview does not support nonempty drawable filters")
        pixels += w * h
        mask = layer.get_mask()
        if mask is not None:
            _preview_parasite_admission(mask)
            if mask.get_filters():
                raise HostCommandError("Preview does not support nonempty mask filters")
            mask_width = _bounded_int(mask.get_width(), "mask width", 1, 8192)
            mask_height = _bounded_int(mask.get_height(), "mask height", 1, 8192)
            pixels += mask_width * mask_height
        if pixels > MAX_PREVIEW_LAYER_PIXELS:
            raise HostCommandError("Preview source exceeds aggregate layer pixel limit")
        if layer.is_group_layer():
            stack.extend(layer.get_children())
    return width, height


def _preview_parasite_admission(item: Any) -> None:
    if len(item.get_parasite_list()) > 256:
        raise HostCommandError("Preview does not support more than 256 parasites per native item")


def _preview_buffer_hash(drawable: Any, pixel_format: str, channels: int) -> str:
    """Hash native pixels in <=1 MiB strips; this never rescales or writes pixels."""
    width, height = int(drawable.get_width()), int(drawable.get_height())
    buffer = drawable.get_buffer()
    if buffer is None:
        raise HostCommandError("GIMP did not expose native pixels for source verification")
    digest = hashlib.sha256()
    rows_per_strip = max(1, min(64, 1024 * 1024 // (width * channels)))
    for y in range(0, height, rows_per_strip):
        rows = min(rows_per_strip, height - y)
        data = bytes(buffer.get(
            Gegl.Rectangle.new(0, y, width, rows), 1.0, pixel_format, Gegl.AbyssPolicy.NONE
        ))
        if len(data) != width * rows * channels:
            raise HostCommandError("GIMP source pixel readback has an unexpected byte length")
        digest.update(data)
    return digest.hexdigest()


def _preview_text_state(layer: Any) -> dict[str, Any]:
    size, unit = layer.get_font_size()
    return {
        "markup": layer.get_markup(), "font_size": size, "font_unit": unit.get_id(),
        "antialias": layer.get_antialias(), "hint_style": _enum_name(layer.get_hint_style()),
        "kerning": layer.get_kerning(), "language": layer.get_language(),
        "base_direction": _enum_name(layer.get_base_direction()),
        "justification": _enum_name(layer.get_justification()), "indent": layer.get_indent(),
        "line_spacing": layer.get_line_spacing(), "letter_spacing": layer.get_letter_spacing(),
    }


def _preview_state(image: Any) -> str:
    """Source fingerprint including hidden layer/mask bytes and editable text attributes."""
    metadata = image.get_metadata()
    serialized = metadata.serialize() if metadata is not None else ""
    report = _metadata_report(image)
    report["metadata_xml"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    layers = _walk_layers(image)
    pixels = []
    for row in layers:
        layer = _resolve_layer(image, row["layer_id"])
        mask = layer.get_mask()
        pixels.append({
            "layer_id": row["layer_id"],
            "rgba_sha256": _preview_buffer_hash(layer, "R'G'B'A u8", 4),
            "mask_sha256": _preview_buffer_hash(mask, "Y u8", 1) if mask is not None else None,
            "mask_parasites": _parasite_report(mask) if mask is not None else [],
            "mask_present": mask is not None,
            "mask_geometry": {
                "mask_id": int(mask.get_id()), "width": int(mask.get_width()),
                "height": int(mask.get_height()), "offsets": list(mask.get_offsets())[1:],
            } if mask is not None else None,
            "mask_apply": layer.get_apply_mask() if mask is not None else None,
            "mask_show": layer.get_show_mask() if mask is not None else None,
            "mask_edit": layer.get_edit_mask() if mask is not None else None,
            "blend_space": _enum_name(layer.get_blend_space()),
            "composite_space": _enum_name(layer.get_composite_space()),
            "composite_mode": _enum_name(layer.get_composite_mode()),
            "text_attributes": _preview_text_state(layer) if layer.is_text_layer() else None,
        })
    selection = image.get_selection()
    state = {
        "image": _image_info(image), "layers": layers, "metadata": report, "pixels": pixels,
        "selection_sha256": _preview_buffer_hash(selection, "Y u8", 1),
        "selection_parasites": _parasite_report(selection),
        "selected_channels": [item.get_id() for item in image.get_selected_channels()],
        "selected_paths": [item.get_id() for item in image.get_selected_paths()],
        "image_order": [item.get_id() for item in Gimp.get_images()],
        "components": [{
            "channel": _enum_name(channel),
            "visible": image.get_component_visible(channel),
            "active": image.get_component_active(channel),
        } for channel in (Gimp.ChannelType.RED, Gimp.ChannelType.GREEN,
                          Gimp.ChannelType.BLUE, Gimp.ChannelType.ALPHA)],
        "effective_icc_sha256": _effective_icc_identity(image)["sha256"],
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()


MAX_ICC_PROFILE_BYTES = 16 * 1024 * 1024


def _read_gchar_bytes(raw: Any, label: str, limit: int) -> bytes:
    """Convert a PyGObject gchar array to bytes, checking the size limit first.

    GIMP exposes gchar arrays as signed integers. Reject the payload on length
    before materializing it, so an oversized native read never gets copied.
    """
    try:
        length = len(raw)
    except BaseException:
        raise HostCommandError("GIMP returned unusable %s" % label) from None
    if length > limit:
        raise HostCommandError("%s exceeds the %d byte inspection limit" % (label, limit))
    octets = bytearray()
    try:
        for value in raw:
            # GIMP's gchar array is exposed as signed integers by PyGObject.
            # Preserve valid byte values without coercing invalid types or domains.
            if type(value) is not int or not -128 <= value <= 255:
                raise HostCommandError("GIMP returned %s data outside the byte domain" % label)
            octets.append(value & 0xff)
    except HostCommandError:
        raise
    except BaseException:
        raise HostCommandError("GIMP returned unusable %s" % label) from None
    return bytes(octets)


def _read_icc_bytes(profile: Any, label: str) -> bytes:
    """Read one GIMP color profile's ICC payload as bytes, or raise a typed error."""
    if profile is None:
        raise HostCommandError("GIMP returned no %s for this image" % label)
    try:
        raw = profile.get_icc_profile()
    except BaseException:
        raise HostCommandError("GIMP failed to read the %s" % label) from None
    if raw is None:
        raise HostCommandError("GIMP returned no bytes for the %s" % label)
    data = _read_gchar_bytes(raw, label, MAX_ICC_PROFILE_BYTES)
    if not data:
        raise HostCommandError("GIMP returned an empty %s" % label)
    return data


def _effective_icc_identity(image: Any) -> dict[str, Any]:
    """Identify the native effective ICC payload by bytes and digest, never by label.

    GIMP falls back to a built-in profile when the image stores none, so an absent
    ICC parasite is not an invalid image and `get_effective_color_profile()` is the
    only getter that always answers.
    """
    try:
        profile = image.get_effective_color_profile()
    except BaseException:
        raise HostCommandError("GIMP failed to read the effective color profile") from None
    data = _read_icc_bytes(profile, "effective color profile")
    stored = None
    try:
        stored = image.get_color_profile()
    except BaseException:
        raise HostCommandError("GIMP failed to read the stored color profile") from None
    return {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "explicitly_stored": stored is not None,
        "label": str(profile.get_label()),
    }


def _export_preview(image: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _export_preview_checked(image, params)
    except HostCommandError:
        raise
    except BaseException:
        raise HostCommandError("Preview export failed; inspect source and destination") from None


def _export_preview_checked(image: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Export only a native duplicate; publish after native cleanup and state checks."""
    if set(params) - {"image_id", "path", "max_width", "max_height", "overwrite"}:
        raise HostCommandError("Unknown export_preview argument")
    max_width = _bounded_int(params.get("max_width"), "max_width", 1, 2048)
    max_height = _bounded_int(params.get("max_height"), "max_height", 1, 2048)
    overwrite = params.get("overwrite", False)
    if type(overwrite) is not bool:
        raise HostCommandError("overwrite must be a boolean")
    requested = Path(_safe_text(params.get("path"), "path", 2048)).expanduser().absolute()
    if not _bootstrap_path_safe(requested):
        raise HostCommandError("Preview output path must not contain links or reparse points")
    path = _output_path(str(requested), frozenset({".png"}), overwrite)
    width, height = _preview_admit_source(image)
    output_width, output_height = _preview_dimensions(width, height, max_width, max_height)
    # Exporters write to a private sibling, never the destination. The atomic
    # no-replace hard link below also refuses a target created during export.
    temporary = None
    pushed = False
    native_error = False
    cleanup_error = False
    staging = None
    published = False
    parent_fd = stage_fd = None
    leases = ExitStack()
    try:
        before = _preview_state(image)
        interpolation = Gimp.context_get_interpolation()
        parent_identity = _preview_directory_identity(path.parent)
        leases.enter_context(_windows_directory_lease(path.parent))
        if os.name == "posix":
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened = os.fstat(parent_fd)
            if (opened.st_dev, opened.st_ino) != parent_identity:
                raise HostCommandError("Preview parent changed before staging")
        staging = Path(tempfile.mkdtemp(prefix=".dcc-preview-", dir=str(path.parent)))
        staging_identity = _preview_directory_identity(staging)
        leases.enter_context(_windows_directory_lease(staging))
        if parent_fd is not None:
            stage_fd = os.open(
                staging.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            opened = os.fstat(stage_fd)
            if (opened.st_dev, opened.st_ino) != staging_identity:
                raise HostCommandError("Preview staging changed identity")
        stage_path = staging / "preview.png"
        try:
            if not Gimp.context_push():
                raise HostCommandError("GIMP could not preserve the preview context")
            pushed = True
            if not Gimp.context_set_interpolation(Gimp.InterpolationType.NOHALO):
                raise HostCommandError("GIMP could not select NoHalo interpolation")
            if Gimp.context_get_interpolation() != Gimp.InterpolationType.NOHALO:
                raise HostCommandError("GIMP did not activate NoHalo interpolation")
            temporary = image.duplicate()
            if temporary is None or temporary.get_id() == image.get_id():
                temporary = None
                raise HostCommandError("GIMP did not create an isolated preview image")
            if (output_width, output_height) != (width, height) and not temporary.scale(
                output_width, output_height
            ):
                raise HostCommandError("GIMP could not scale the preview image")
            if (temporary.get_width(), temporary.get_height()) != (output_width, output_height):
                raise HostCommandError("GIMP preview dimensions differ from the bounded request")
            if not Gimp.file_save(
                Gimp.RunMode.NONINTERACTIVE, temporary, Gio.File.new_for_path(str(stage_path)), None
            ):
                raise HostCommandError("GIMP could not export the preview PNG")
            if (
                not _bootstrap_path_safe(stage_path)
                or not stage_path.is_file()
                or not 0 < stage_path.stat().st_size <= 32 * 1024 * 1024
            ):
                raise HostCommandError("GIMP did not produce a bounded regular preview PNG")
            with stage_path.open("rb") as stream:
                header = stream.read(33)
            if (
                len(header) != 33
                or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
                or int.from_bytes(header[16:20], "big") != output_width
                or int.from_bytes(header[20:24], "big") != output_height
            ):
                raise HostCommandError("GIMP preview PNG header did not match the request")
            digest = _file_digest(stage_path)
        except BaseException:
            native_error = True
        finally:
            if temporary is not None:
                try:
                    if not temporary.delete():
                        cleanup_error = True
                except BaseException:
                    cleanup_error = True
            if pushed:
                try:
                    if not Gimp.context_pop():
                        cleanup_error = True
                except BaseException:
                    cleanup_error = True
            try:
                if Gimp.context_get_interpolation() != interpolation:
                    # Best effort restoration after a failed context pop. Still fail
                    # the request because the rest of the context is not attested.
                    Gimp.context_set_interpolation(interpolation)
                    cleanup_error = True
            except BaseException:
                cleanup_error = True
        if cleanup_error:
            raise HostCommandError("Preview cleanup failed; no output was published")
        if native_error:
            raise HostCommandError("Native preview export failed; no output was published")
        _preview_admit_source(image)
        if _preview_state(image) != before:
            raise HostCommandError("Preview source state changed; no output was published")
        if (
            not _bootstrap_path_safe(path)
            or _preview_directory_identity(path.parent) != parent_identity
        ):
            raise HostCommandError("Preview output parent changed; no output was published")
        # Revalidate the destination type/allowlist after the native exporter.
        _output_path(str(path), frozenset({".png"}), overwrite)
        if (not _bootstrap_path_safe(staging)
                or _preview_directory_identity(staging) != staging_identity):
            raise HostCommandError("Preview staging changed before publication")
        if parent_fd is not None:
            # Anchor publication to admitted directories; a concurrent parent-path
            # replacement cannot redirect the atomic operation to another directory.
            if overwrite:
                os.replace("preview.png", path.name, src_dir_fd=stage_fd, dst_dir_fd=parent_fd)
            else:
                os.link("preview.png", path.name, src_dir_fd=stage_fd,
                        dst_dir_fd=parent_fd, follow_symlinks=False)
        elif overwrite:
            os.replace(stage_path, path)
        else:
            os.link(stage_path, path)
        published = True
        return {
            **digest,
            "path": str(path),
            "source_image_id": image.get_id(),
            "source_dimensions": [width, height],
            "output_dimensions": [output_width, output_height],
            "interpolation": "nohalo",
            "upscaled": False,
            "source_state_sha256": before,
            "source_image_mutated": False,
            "temporary_image_deleted": True,
            "context_restored": True,
            "published": True,
        }
    except HostCommandError:
        raise
    except BaseException:
        # Native/GI/filesystem exceptions can include paths or metadata. They are
        # deliberately not copied into the remote error envelope.
        raise HostCommandError(
            "Preview export failed; inspect the destination before retrying"
        ) from None
    finally:
        try:
            if staging is not None:
                if stage_fd is not None:
                    try:
                        os.unlink("preview.png", dir_fd=stage_fd)
                    except FileNotFoundError:
                        pass
                    os.close(stage_fd)
                    stage_fd = None
                    # Only remove our unchanged directory; never follow a swap.
                    if _preview_directory_identity(staging) != staging_identity:
                        raise OSError("Staging identity changed")
                    os.rmdir(staging.name, dir_fd=parent_fd)
                else:
                    stage_path = staging / "preview.png"
                    if stage_path.exists() or stage_path.is_symlink():
                        stage_path.unlink()
                    # Windows directory lease must close before rmdir.
                    leases.close()
                    staging.rmdir()
        except OSError:
            if published:
                raise HostCommandError(
                    "Preview output was published, but staging cleanup failed; "
                    "inspect the destination before any retry"
                ) from None
            raise HostCommandError(
                "Preview staging cleanup failed before publication; destination was not changed"
            ) from None
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            leases.close()


def _file_digest(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _enum_name(value: Any) -> str:
    for attribute in ("value_nick", "value_name"):
        name = getattr(value, attribute, None)
        if name:
            return str(name)
    return str(value)


def _safe_image_path(image: Any) -> tuple[Optional[str], bool]:
    file_object = image.get_file() or image.get_imported_file() or image.get_exported_file()
    if file_object is None:
        return None, False
    raw = file_object.get_path()
    if not raw:
        return None, False
    path = Path(raw).expanduser().resolve()
    allowed = bool(_allowed_roots()) and _within(path, _allowed_roots())
    return (str(path) if allowed else path.name), allowed


def _image_info(image: Any) -> dict[str, Any]:
    path, path_allowed = _safe_image_path(image)
    selected = list(image.get_selected_layers())
    return {
        "image_id": int(image.get_id()),
        "name": str(image.get_name()),
        "width": int(image.get_width()),
        "height": int(image.get_height()),
        "base_type": _enum_name(image.get_base_type()),
        "precision": _enum_name(image.get_precision()),
        "dirty": bool(image.is_dirty()),
        "color_profile": image.get_effective_color_profile().get_label(),
        "typed_color_encoding": "sRGB IEC 61966-2-1 / straight alpha",
        "file_name": path,
        "file_path_allowed": path_allowed,
        "selected_layer_ids": [int(layer.get_id()) for layer in selected],
        "bridge_owned_display": int(image.get_id()) in _owned_displays,
    }


def _resolve_image(value: Any) -> Any:
    image_id = _bounded_int(value, "image_id", 1, 2_147_483_647)
    for image in Gimp.get_images():
        if int(image.get_id()) == image_id:
            return image
    raise HostCommandError("GIMP image was not found in this instance")


def _walk_layers(image: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    stack: list[tuple[Any, Optional[int], int]] = [
        (layer, None, 0) for layer in reversed(list(image.get_layers()))
    ]
    while stack:
        layer, parent_id, depth = stack.pop()
        if len(result) >= MAX_LAYER_NODES:
            raise HostCommandError("Layer tree exceeds the configured node limit")
        layer_id = int(layer.get_id())
        children = list(layer.get_children()) if layer.is_group_layer() else []
        result.append(
            {
                "layer_id": layer_id,
                "parent_id": parent_id,
                "depth": depth,
                "name": str(layer.get_name()),
                "visible": bool(layer.get_visible()),
                "locked": bool(layer.get_lock_content()),
                "opacity": float(layer.get_opacity()),
                "is_group": bool(layer.is_group_layer()),
                "is_text": bool(layer.is_text_layer()),
                "text": str(layer.get_text()) if layer.is_text_layer() else None,
                "text_font": layer.get_font().get_name() if layer.is_text_layer() else None,
                "text_color_linear_rgba": (
                    list(layer.get_color().get_rgba()) if layer.is_text_layer() else None
                ),
                "child_count": len(children),
                "mode": _enum_name(layer.get_mode()),
                "width": int(layer.get_width()),
                "height": int(layer.get_height()),
                "offsets": list(layer.get_offsets())[1:],
            }
        )
        stack.extend((child, layer_id, depth + 1) for child in reversed(children))
    return result


def _resolve_layer(image: Any, value: Any) -> Any:
    layer_id = _bounded_int(value, "layer_id", 1, 2_147_483_647)
    stack = list(image.get_layers())
    visited = 0
    while stack:
        layer = stack.pop()
        visited += 1
        if visited > MAX_LAYER_NODES:
            break
        if int(layer.get_id()) == layer_id:
            return layer
        if layer.is_group_layer():
            stack.extend(layer.get_children())
    raise HostCommandError("GIMP layer was not found in this image")


def _color(value: Any) -> Any:
    if not isinstance(value, list) or len(value) not in {3, 4}:
        raise HostCommandError("color must be [red, green, blue] or [red, green, blue, alpha]")
    channels = [_bounded_int(channel, "color channel", 0, 255) for channel in value]
    if len(channels) == 3:
        channels.append(255)
    # The typed channel contract is encoded sRGB, not linear-light RGBA.
    # GEGL's hex parser converts sRGB RGB channels while preserving straight alpha.
    return Gegl.Color.new("#" + "".join("%02x" % channel for channel in channels))


def _fill_color(layer: Any, color_value: Any) -> None:
    # Foreground fills discard foreground alpha in GIMP. The native GEGL drawable
    # buffer preserves the requested straight alpha without changing layer opacity.
    color = _color(color_value)
    width, height = int(layer.get_width()), int(layer.get_height())
    buffer = layer.get_buffer()
    if buffer is None:
        raise HostCommandError("GIMP failed to expose the paintable layer buffer")
    buffer.set_color(Gegl.Rectangle.new(0, 0, width, height), color)
    buffer.flush()
    if not layer.update(0, 0, width, height):
        raise HostCommandError("GIMP failed to update the filled layer")

def _parasite_report(item: Any) -> list[dict[str, Any]]:
    result = []
    for name in list(item.get_parasite_list())[:256]:
        parasite = item.get_parasite(name)
        data = _read_gchar_bytes(
            parasite.get_data(), "parasite data", MAX_PARASITE_BYTES
        )
        result.append(
            {
                "name": name,
                "flags": parasite.get_flags(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "text": data[:16384].decode("utf-8", errors="replace"),
                "truncated": len(data) > 16384,
            }
        )
    return result

def _metadata_report(image: Any) -> dict[str, Any]:
    metadata = image.get_metadata()
    serialized = metadata.serialize() if metadata is not None else ""
    layers = _walk_layers(image)
    return {
        "image_parasites": _parasite_report(image),
        "layer_parasites": [
            {
                "name": layer["name"],
                "parasites": _parasite_report(_resolve_layer(image, layer["layer_id"])),
            }
            for layer in layers
        ],
        "metadata_xml": serialized[:65536],
        "metadata_xml_truncated": len(serialized) > 65536,
    }

def _layer_info(image: Any, layer: Any) -> dict[str, Any]:
    return next(item for item in _walk_layers(image) if item["layer_id"] == layer.get_id())

def _parent_layer(image: Any, value: Any) -> Any:
    if value is None:
        return None
    parent = _resolve_layer(image, value)
    if not parent.is_group_layer():
        raise HostCommandError("parent_id must identify a layer group in this image")
    return parent

def _layer_position(value: Any) -> int:
    return _bounded_int(value, "position", 0, MAX_LAYER_NODES)

def _cleanup_new_layer(image: Any, layer: Any, inserted: bool, original: BaseException) -> None:
    """Release only the item allocated by a failed group or text command."""
    try:
        if inserted:
            cleaned = image.remove_layer(layer)
            operation = "remove_layer"
        else:
            cleaned = layer.delete()
            operation = "delete"
        if not cleaned:
            raise HostCommandError("%s returned false" % operation)
    except BaseException as cleanup:
        raise HostCommandError(
            "New layer setup failed (%s: %s); cleanup also failed (%s: %s); "
            "inspect the image before retrying"
            % (type(original).__name__, original, type(cleanup).__name__, cleanup)
        ) from original


def _push_shape_context(image: Any, saved_selection: Any) -> bool:
    """Release an unused mask if context setup fails before any painting."""
    try:
        if not Gimp.context_push():
            raise HostCommandError("GIMP failed to save the user context; shape was not painted")
    except BaseException as original:
        try:
            removed = image.remove_channel(saved_selection)
            if not removed:
                raise HostCommandError("remove_channel returned false")
        except BaseException as cleanup:
            raise HostCommandError(
                "Context setup failed (%s: %s); cleanup of unused saved selection channel %d "
                "also failed (%s: %s); shape was not painted"
                % (
                    type(original).__name__, original, int(saved_selection.get_id()),
                    type(cleanup).__name__, cleanup,
                )
            ) from original
        raise
    return True


def _paint_shape(image: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    layer = _resolve_layer(image, params.get("layer_id"))
    if layer.is_group_layer() or layer.get_lock_content():
        raise HostCommandError("paint_shape requires an unlocked paintable layer")
    shape = params.get("shape")
    if shape not in {"polygon", "ellipse", "rectangle"}:
        raise HostCommandError("shape must be polygon, ellipse, or rectangle")
    color = _color(params.get("color"))
    feather = _bounded_float(params.get("feather", 0), "feather", 0, 256)
    if shape == "polygon":
        points = params.get("points")
        if not isinstance(points, list) or not 3 <= len(points) <= 256:
            raise HostCommandError("polygon requires 3 to 256 points")
        coordinates = []
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise HostCommandError("each point must be [x, y]")
            coordinates.extend(_bounded_float(v, "coordinate", -32768, 32768) for v in point)
    else:
        bounds = params.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4:
            raise HostCommandError("bounds must be [x, y, width, height]")
        coordinates = [
            _bounded_float(bounds[0], "x", -32768, 32768),
            _bounded_float(bounds[1], "y", -32768, 32768),
            _bounded_float(bounds[2], "width", 0.01, 32768),
            _bounded_float(bounds[3], "height", 0.01, 32768),
        ]
    saved_selection = Gimp.Selection.save(image)
    if saved_selection is None:
        raise HostCommandError("GIMP failed to save the selection; shape was not painted")
    pushed = False
    painting_error = None
    try:
        if not _push_shape_context(image, saved_selection):
            raise HostCommandError("GIMP failed to save the user context; shape was not painted")
        pushed = True
        channels = params["color"]
        alpha = channels[3] if len(channels) == 4 else 255
        if not all(
            (
                Gimp.context_set_opacity(alpha / 255.0 * 100.0),
                Gimp.context_set_antialias(True),
                Gimp.context_set_feather(feather > 0),
                Gimp.context_set_feather_radius(feather, feather),
            )
        ):
            raise HostCommandError("GIMP failed to configure shape painting")
        if shape == "polygon":
            ok = image.select_polygon(Gimp.ChannelOps.REPLACE, coordinates)
        elif shape == "ellipse":
            ok = image.select_ellipse(Gimp.ChannelOps.REPLACE, *coordinates)
        else:
            ok = image.select_rectangle(Gimp.ChannelOps.REPLACE, *coordinates)
        if not ok or not Gimp.context_set_foreground(color):
            raise HostCommandError("GIMP failed to select the shape or set its color")
        if not layer.edit_fill(Gimp.FillType.FOREGROUND):
            raise HostCommandError("GIMP failed to paint the shape")
    except BaseException as exc:
        painting_error = exc
        raise
    finally:
        cleanup_errors = []
        try:
            if pushed:
                # select_item applies the active selection context. Reusing the
                # shape's feathering would irreversibly blur the saved mask.
                recovery_id = int(saved_selection.get_id())
                message = (
                    "GIMP could not restore the original selection; saved channel %d "
                    "is retained for recovery; painting may have occurred" % recovery_id
                )
                try:
                    exact_context = (
                        Gimp.context_set_feather(False),
                        Gimp.context_set_antialias(False),
                    )
                    if not all(exact_context):
                        raise HostCommandError(message)
                    if not image.select_item(Gimp.ChannelOps.REPLACE, saved_selection):
                        raise HostCommandError(message)
                except BaseException as exc:
                    if isinstance(exc, HostCommandError):
                        raise
                    raise HostCommandError(message) from exc
                # Never remove the only recovery mask before confirmed restore.
                if not image.remove_channel(saved_selection):
                    raise HostCommandError(
                        "Selection restored, but GIMP could not remove saved recovery channel %d"
                        % recovery_id
                    )
        except BaseException as exc:
            cleanup_errors.append(("Selection cleanup", exc))
        try:
            if pushed and not Gimp.context_pop():
                raise HostCommandError("GIMP failed to restore the original user context")
        except BaseException as exc:
            cleanup_errors.append(("Context cleanup", exc))
        if cleanup_errors:
            if painting_error is None and len(cleanup_errors) == 1:
                raise cleanup_errors[0][1]
            errors = []
            if painting_error is not None:
                errors.append(
                    "Shape operation failed (%s: %s)"
                    % (type(painting_error).__name__, painting_error)
                )
            errors.extend(
                "%s failed (%s: %s)" % (stage, type(error).__name__, error)
                for stage, error in cleanup_errors
            )
            raise HostCommandError("; ".join(errors)) from (
                painting_error if painting_error is not None else cleanup_errors[0][1]
            )
    Gimp.displays_flush()
    return {"layer_id": int(layer.get_id()), "shape": shape, "painted": True}


def _new_layer(image: Any, name: str, color_value: Any) -> Any:
    layer = Gimp.Layer.new(
        image,
        name,
        int(image.get_width()),
        int(image.get_height()),
        Gimp.ImageType.RGBA_IMAGE,
        100.0,
        image.get_default_new_layer_mode(),
    )
    if layer is None:
        raise HostCommandError("GIMP failed to create the layer")
    if color_value is None:
        if not layer.fill(Gimp.FillType.TRANSPARENT):
            raise HostCommandError("GIMP failed to initialize the layer")
    else:
        _fill_color(layer, color_value)
    if not image.insert_layer(layer, None, 0):
        layer.delete()
        raise HostCommandError("GIMP failed to insert the layer")
    if not image.set_selected_layers([layer]):
        raise HostCommandError("GIMP failed to select the new layer")
    return layer


def _execute_command(method: str, params: Mapping[str, Any]) -> Any:
    if method in {"gimp.get_status", "gimp.ping"}:
        return {
            "ready": True,
            "gimp_version": str(Gimp.version()),
            "adapter_version": VERSION,
            "bridge_host": BRIDGE_HOST,
            "bridge_port": BRIDGE_PORT,
            "authenticated": True,
            "gimp_pid": _gimp_pid,
            "gimp_start_identity": _gimp_start_identity,
            "plugin_pid": os.getpid(),
            "plugin_module_path": str(Path(__file__).resolve()),
            "main_thread_id": threading.get_ident(),
            "allowed_roots": [str(root) for root in _allowed_roots()],
            "command_count": 25,
            "arbitrary_script_input": False,
        }
    if method == "gimp.list_fonts":
        limit = _bounded_int(params.get("limit", 64), "limit", 1, 256)
        query = params.get("query", "")
        if not isinstance(query, str) or len(query) > 128:
            raise HostCommandError("font query must be a string of at most 128 characters")
        names = sorted(str(font.get_name()) for font in Gimp.fonts_get_list(None))
        matches = [name for name in names if query.casefold() in name.casefold()]
        return {
            "fonts": matches[:limit],
            "match_count": len(matches),
            "truncated": len(matches) > limit,
        }
    if method == "gimp.list_images":
        return [_image_info(image) for image in Gimp.get_images()]
    if method == "gimp.get_active_image":
        images = list(Gimp.get_images())
        return _image_info(images[0]) if images else None
    if method == "gimp.create_image":
        width = _bounded_int(params.get("width"), "width", 1, 16_384)
        height = _bounded_int(params.get("height"), "height", 1, 16_384)
        if width * height > MAX_IMAGE_PIXELS:
            raise HostCommandError("Image exceeds the configured pixel limit")
        name = _safe_text(params.get("name", "Untitled"), "name", 256)
        image = Gimp.Image.new(width, height, Gimp.ImageBaseType.RGB)
        if image is None:
            raise HostCommandError("GIMP failed to create the image")
        layer = _new_layer(image, name, params.get("background_color"))
        display = Gimp.Display.new(image)
        if display is None:
            image.delete()
            raise HostCommandError("GIMP failed to display the image")
        _owned_displays[int(image.get_id())] = display
        Gimp.displays_flush()
        return {**_image_info(image), "created_layer_id": int(layer.get_id())}
    if method == "gimp.open_image":
        path = _input_path(params.get("path"))
        image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(str(path)))
        if image is None:
            raise HostCommandError("GIMP failed to open the image")
        display = Gimp.Display.new(image)
        if display is None:
            image.delete()
            raise HostCommandError("GIMP failed to display the image")
        _owned_displays[int(image.get_id())] = display
        Gimp.displays_flush()
        return _image_info(image)

    image = _resolve_image(params.get("image_id"))
    if method in {"gimp.inspect_image", "gimp.list_layers"}:
        layers = _walk_layers(image)
        if method == "gimp.list_layers":
            return layers
        result = {**_image_info(image), "layer_count": len(layers), "layers": layers}
        if params.get("include_metadata", False):
            result["metadata"] = _metadata_report(image)
        if _typed_bool(params.get("include_icc_identity", False), "include_icc_identity"):
            result["effective_icc"] = _effective_icc_identity(image)
        return result
    if method == "gimp.export_preview":
        return _export_preview(image, params)
    if method == "gimp.export_layer":
        layer = _resolve_layer(image, params.get("layer_id"))
        if layer.is_group_layer():
            raise HostCommandError("export_layer requires one non-group drawable")
        if layer.get_mask() is not None:
            raise HostCommandError("export_layer does not support masked layers")
        path = _output_path(
            params.get("path"), frozenset({".png"}), bool(params.get("overwrite", False))
        )
        source_info = _layer_info(image, layer)
        width, height = int(image.get_width()), int(image.get_height())
        isolated = Gimp.Image.new_with_precision(
            width, height, image.get_base_type(), image.get_precision()
        )
        if isolated is None:
            raise HostCommandError("GIMP failed to create the temporary layer export image")
        try:
            if not isolated.set_color_profile(image.get_effective_color_profile()):
                raise HostCommandError("GIMP failed to copy the export color profile")
            copied = Gimp.Layer.new_from_drawable(layer, isolated)
            if copied is None or not isolated.insert_layer(copied, None, 0):
                raise HostCommandError("GIMP failed to copy the selected layer for export")
            offsets = list(layer.get_offsets())[1:]
            if not copied.set_offsets(*offsets) or not copied.set_visible(True):
                raise HostCommandError("GIMP failed to preserve export placement")
            if not Gimp.file_save(
                Gimp.RunMode.NONINTERACTIVE, isolated, Gio.File.new_for_path(str(path)), None
            ):
                raise HostCommandError("GIMP failed to export the isolated layer PNG")
            if not path.is_file() or path.stat().st_size <= 0:
                raise HostCommandError("GIMP produced no layer PNG")
            return {
                **_file_digest(path),
                "source_layer": source_info,
                "export_canvas": [width, height],
                "precision": _enum_name(isolated.get_precision()),
                "color_profile": isolated.get_effective_color_profile().get_label(),
                "source_image_mutated": False,
            }
        finally:
            isolated.delete()
    if method == "gimp.create_group":
        name = _safe_text(params.get("name"), "name", 256)
        parent = _parent_layer(image, params.get("parent_id"))
        position = _layer_position(params.get("position", 0))
        group = Gimp.GroupLayer.new(image, name)
        if group is None:
            raise HostCommandError("GIMP failed to create the layer group")
        try:
            if not image.insert_layer(group, parent, position):
                raise HostCommandError("GIMP failed to create the layer group")
        except BaseException as original:
            _cleanup_new_layer(image, group, False, original)
            raise
        Gimp.displays_flush()
        return _layer_info(image, group)
    if method == "gimp.import_layer":
        path = _input_path(params.get("path"))
        parent = _parent_layer(image, params.get("parent_id"))
        position = _layer_position(params.get("position", 0))
        name = _safe_text(params.get("name", path.stem), "name", 256)
        layer = Gimp.file_load_layer(
            Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(str(path))
        )
        if layer is None:
            raise HostCommandError("GIMP failed to import the image as a layer")
        if layer.get_width() * layer.get_height() > MAX_IMAGE_PIXELS:
            layer.delete()
            raise HostCommandError("Imported layer exceeds the pixel limit")
        if not layer.set_name(name) or not image.insert_layer(layer, parent, position):
            layer.delete()
            raise HostCommandError("GIMP failed to insert the imported layer")
        Gimp.displays_flush()
        return {**_layer_info(image, layer), "source": _file_digest(path)}
    if method == "gimp.place_layer":
        layer = _resolve_layer(image, params.get("layer_id"))
        parent = _parent_layer(image, params.get("parent_id"))
        position = _layer_position(params.get("position", 0))
        if parent is not None and parent.get_id() == layer.get_id():
            raise HostCommandError("A group cannot parent itself")
        x = _bounded_int(params.get("x", 0), "x", -32768, 32768)
        y = _bounded_int(params.get("y", 0), "y", -32768, 32768)
        if not image.reorder_item(layer, parent, position):
            raise HostCommandError("GIMP rejected the layer hierarchy placement")
        if not layer.set_offsets(x, y):
            raise HostCommandError("GIMP failed to set layer offsets")
        Gimp.displays_flush()
        return _layer_info(image, layer)
    if method == "gimp.set_layer_mode":
        layer = _resolve_layer(image, params.get("layer_id"))
        modes = {
            "normal": Gimp.LayerMode.NORMAL,
            "multiply": Gimp.LayerMode.MULTIPLY,
            "screen": Gimp.LayerMode.SCREEN,
            "overlay": Gimp.LayerMode.OVERLAY,
            "soft-light": Gimp.LayerMode.SOFTLIGHT,
            "pass-through": Gimp.LayerMode.PASS_THROUGH,
        }
        mode = params.get("mode")
        if mode not in modes or (mode == "pass-through" and not layer.is_group_layer()):
            raise HostCommandError("Unsupported layer mode or pass-through on a non-group")
        if not layer.set_mode(modes[mode]):
            raise HostCommandError("GIMP failed to set layer mode")
        Gimp.displays_flush()
        return _layer_info(image, layer)
    if method == "gimp.paint_shape":
        return _paint_shape(image, params)
    if method == "gimp.create_text":
        text = _safe_text(params.get("text"), "text", 4096)
        name = _safe_text(params.get("name"), "name", 256)
        font_name = _safe_text(params.get("font", "DejaVu Sans"), "font", 256)
        size = _bounded_float(params.get("size", 48), "size", 1, 2048)
        x = _bounded_int(params.get("x", 0), "x", -32768, 32768)
        y = _bounded_int(params.get("y", 0), "y", -32768, 32768)
        parent = _parent_layer(image, params.get("parent_id"))
        color = _color(params.get("color", [0, 0, 0]))
        font = Gimp.Font.get_by_name(font_name)
        if font is None:
            raise HostCommandError("The requested font is unavailable in GIMP")
        layer = Gimp.TextLayer.new(image, text, font, size, Gimp.Unit.pixel())
        if layer is None:
            raise HostCommandError("GIMP failed to create editable text")
        inserted = False
        try:
            if layer.get_width() * layer.get_height() > MAX_IMAGE_PIXELS:
                raise HostCommandError("Native text layer exceeds the pixel limit")
            if not layer.set_name(name):
                raise HostCommandError("GIMP failed to initialize editable text")
            if not image.insert_layer(layer, parent, 0):
                raise HostCommandError("GIMP failed to place editable text")
            inserted = True
            if not layer.set_offsets(x, y):
                raise HostCommandError("GIMP failed to place editable text")
            if not layer.set_color(color):
                raise HostCommandError("GIMP failed to set editable text color")
        except BaseException as original:
            _cleanup_new_layer(image, layer, inserted, original)
            raise
        Gimp.displays_flush()
        return {**_layer_info(image, layer), "text": text, "font": font_name, "font_size": size}
    if method == "gimp.save_image":
        path = _output_path(
            params.get("path"), frozenset({".xcf"}), bool(params.get("overwrite", False))
        )
        if not Gimp.file_save(
            Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(str(path)), None
        ):
            raise HostCommandError("GIMP failed to save the XCF image")
        image.clean_all()
        if not path.is_file() or path.stat().st_size <= 0:
            raise HostCommandError("GIMP reported success but produced no XCF artifact")
        return _file_digest(path)
    if method == "gimp.export_image":
        path = _output_path(
            params.get("path"), EXPORT_SUFFIXES, bool(params.get("overwrite", False))
        )
        if not Gimp.file_save(
            Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(str(path)), None
        ):
            raise HostCommandError("GIMP failed to export the image")
        if not path.is_file() or path.stat().st_size <= 0:
            raise HostCommandError("GIMP reported success but produced no export artifact")
        return _file_digest(path)
    if method == "gimp.create_layer":
        layer = _new_layer(
            image,
            _safe_text(params.get("name"), "name", 256),
            params.get("color"),
        )
        Gimp.displays_flush()
        return next(item for item in _walk_layers(image) if item["layer_id"] == layer.get_id())
    if method == "gimp.fill_layer":
        layer = _resolve_layer(image, params.get("layer_id"))
        if layer.is_group_layer():
            raise HostCommandError("fill_layer requires a paintable layer")
        _fill_color(layer, params.get("color"))
        Gimp.displays_flush()
        return {"layer_id": int(layer.get_id()), "filled": True}
    if method == "gimp.set_layer_properties":
        layer = _resolve_layer(image, params.get("layer_id"))
        changed = []
        if "name" in params:
            if not layer.set_name(_safe_text(params["name"], "name", 256)):
                raise HostCommandError("GIMP failed to rename the layer")
            changed.append("name")
        if "visible" in params:
            if not layer.set_visible(bool(params["visible"])):
                raise HostCommandError("GIMP failed to change layer visibility")
            changed.append("visible")
        if "locked" in params:
            if not layer.set_lock_content(bool(params["locked"])):
                raise HostCommandError("GIMP failed to change the layer lock")
            changed.append("locked")
        if "opacity" in params:
            opacity = _bounded_float(params["opacity"], "opacity", 0.0, 100.0)
            if not layer.set_opacity(opacity):
                raise HostCommandError("GIMP failed to change layer opacity")
            changed.append("opacity")
        if not changed:
            raise HostCommandError("At least one layer property must be provided")
        Gimp.displays_flush()
        return {"layer_id": int(layer.get_id()), "changed": changed}
    if method == "gimp.set_active_layer":
        layer = _resolve_layer(image, params.get("layer_id"))
        if not image.set_selected_layers([layer]):
            raise HostCommandError("GIMP failed to select the layer")
        Gimp.displays_flush()
        return {"layer_id": int(layer.get_id()), "active": True}
    if method == "gimp.delete_layer":
        layer = _resolve_layer(image, params.get("layer_id"))
        if len(_walk_layers(image)) <= 1:
            raise HostCommandError("The last remaining layer cannot be deleted")
        layer_id = int(layer.get_id())
        if not image.remove_layer(layer):
            raise HostCommandError("GIMP failed to delete the layer")
        Gimp.displays_flush()
        return {"layer_id": layer_id, "deleted": True}
    if method == "gimp.flatten_image":
        if params.get("confirm") is not True:
            raise HostCommandError("flatten_image requires confirm=true")
        layer = image.flatten()
        if layer is None:
            raise HostCommandError("GIMP failed to flatten the image")
        Gimp.displays_flush()
        return {**_image_info(image), "flattened": True, "layer_id": int(layer.get_id())}
    if method == "gimp.close_image":
        image_id = int(image.get_id())
        display = _owned_displays.get(image_id)
        if display is None:
            raise HostCommandError("Only images opened by this bridge may be closed")
        dirty = bool(image.is_dirty())
        if dirty and params.get("discard_changes") is not True:
            raise HostCommandError("Image has unsaved changes; set discard_changes=true to close")
        if not display.delete():
            raise HostCommandError("GIMP failed to close the bridge-owned display")
        _owned_displays.pop(image_id, None)
        return {"image_id": image_id, "closed": True, "discarded_changes": dirty}
    raise HostCommandError("Unsupported GIMP bridge method")


def _command_timeout(params: Mapping[str, Any]) -> float:
    return _bounded_float(
        params.get("timeout_secs", 120.0), "timeout_secs", 1.0, MAX_COMMAND_TIMEOUT_SECS
    )


def _dispatch(method: str, params: Mapping[str, Any]) -> Any:
    if not _pending_commands.acquire(blocking=False):
        raise HostCommandError("GIMP main-thread command queue is full")
    pending = _PendingCommand(method, params)

    def run_on_main() -> bool:
        try:
            with pending.lock:
                if pending.cancelled:
                    return GLib.SOURCE_REMOVE
                pending.started = True
            pending.result = _execute_command(pending.method, pending.params)
        except BaseException as exc:
            pending.error = exc
        finally:
            pending.completed.set()
            _pending_commands.release()
        return GLib.SOURCE_REMOVE

    try:
        GLib.idle_add(run_on_main)
    except BaseException:
        _pending_commands.release()
        raise
    if not pending.completed.wait(_command_timeout(params)):
        with pending.lock:
            if not pending.started:
                pending.cancelled = True
                raise HostCommandError(
                    "GIMP main thread did not start the command before timeout; request cancelled"
                )
        raise HostCommandError(
            "GIMP command exceeded timeout after host execution began; host outcome is unknown"
        )
    if pending.error is not None:
        raise pending.error
    return pending.result


def _error_response(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(5.0)
        request_id: Any = None
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                response = _error_response(
                    None, "request_too_large", "Request exceeds the size limit"
                )
            else:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise HostCommandError("Request must be a JSON object")
                request_id = request.get("id")
                method = request.get("method")
                params = request.get("params", {})
                token = request.get("token", "")
                if not isinstance(token, str) or not hmac.compare_digest(token, _bridge_token):
                    response = _error_response(
                        request_id, "unauthorized", "Bridge authentication failed"
                    )
                elif not isinstance(method, str) or not method.startswith("gimp."):
                    response = _error_response(
                        request_id, "invalid_method", "Method must use gimp.*"
                    )
                elif not isinstance(params, dict):
                    response = _error_response(
                        request_id, "invalid_params", "params must be an object"
                    )
                else:
                    try:
                        value = _dispatch(method, params)
                        response = {"jsonrpc": "2.0", "id": request_id, "result": value}
                    except HostCommandError as exc:
                        response = _error_response(request_id, "host_command_error", str(exc))
                    except Exception:
                        response = _error_response(
                            request_id, "bridge_error", "GIMP host command failed"
                        )
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = _error_response(
                request_id, "invalid_json", "Request is not valid UTF-8 JSON"
            )
        except (OSError, HostCommandError) as exc:
            response = _error_response(request_id, "bridge_error", str(exc))
        encoded = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = (
                json.dumps(
                    _error_response(
                        request_id, "response_too_large", "Response exceeds the size limit"
                    ),
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        try:
            self.connection.sendall(encoded)
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, server_address: tuple[str, int], handler_class: type[_Handler]) -> None:
        super().__init__(server_address, handler_class)
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


class DccMcpGimp(Gimp.PlugIn):
    def do_query_procedures(self) -> list[str]:
        return ["python-fu-dcc-mcp-gimp-bridge"]

    def do_create_procedure(self, name: str) -> Any:
        procedure = Gimp.Procedure.new(
            self, name, Gimp.PDBProcType.PERSISTENT, self._run, self
        )
        procedure.set_documentation(
            "Start the DCC-MCP GIMP bridge",
            "Starts an authenticated loopback bridge for dcc-mcp-gimp.",
            "dcc-mcp-gimp",
        )
        procedure.set_attribution("loonghao", "dcc-mcp", "2026")
        return procedure

    @staticmethod
    def _run(procedure: Any, config: Any, plugin: Any) -> Any:
        with capture_bootstrap_errors("bridge-startup"):
            return DccMcpGimp._run_bridge(procedure, config, plugin)

    @staticmethod
    def _run_bridge(procedure: Any, config: Any, plugin: Any) -> Any:
        del config
        global _bridge_token
        _bridge_token = _load_or_create_token()
        server = _Server((BRIDGE_HOST, BRIDGE_PORT), _Handler)
        threading.Thread(
            target=server.serve_forever, name="dcc-mcp-gimp-bridge", daemon=True
        ).start()
        procedure.persistent_ready()
        plugin.persistent_enable()
        try:
            GLib.MainLoop().run()
        finally:
            server.shutdown()
            server.server_close()
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)


Gimp.main(DccMcpGimp.__gtype__, sys.argv)
