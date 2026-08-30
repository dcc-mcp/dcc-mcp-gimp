"""Receipt-owned transactional file operations for the GIMP Install SOP."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from dcc_mcp_core.install_lifecycle import inspect_install_root, safe_remove_tree

from .__version__ import __version__
from .install_contract import (
    _PLUGIN_NAME,
    EXIT_ACQUIRE,
    EXIT_INSTALL,
    EXIT_PREFLIGHT,
    EXIT_REQUIRES_RESTART,
    RECEIPT_RELATIVE_PATH,
    SCHEMA_VERSION,
    InstallFailure,
    _version_tuple,
)
from .install_host import default_plugin_dir

_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_ENTRIES = 4096
_MAX_MANIFEST_DEPTH = 64
_DEFAULT_SAFE_REMOVE_TREE = safe_remove_tree
_DEFAULT_COPYTREE = shutil.copytree
# Keep the interpreter's original POSIX primitives before any lifecycle test
# or caller can replace attributes on the shared ``os`` module.  All callers
# still pass held no-follow directory descriptors and revalidate identities;
# this prevents an injectable pathname wrapper from running between those
# checks and the syscall.
_POSIX_RENAME = os.rename
_POSIX_UNLINK = os.unlink
_POSIX_UNLINK_SAFE = os.unlink
_POSIX_LINK = os.link
# Path-returning test seams cannot carry an inode identity by themselves.  Keep
# the identity captured at the side-effect boundary so a caller that swaps the
# returned pathname before publication fails closed instead of consuming the
# replacement object.
_BOUND_STAGE_IDENTITIES: dict[str, tuple[int, int, int, int, int, int]] = {}
_BOUND_TRANSACTION_IDENTITIES: dict[str, tuple[int, int, int, int, int, int]] = {}


def _source_file() -> Path:
    bundled = Path(__file__).resolve().parent / "gimp_plugin" / ("%s.py" % _PLUGIN_NAME)
    if bundled.is_file():
        return bundled
    source = (
        Path(__file__).resolve().parents[2] / "bridge" / "gimp-plugin" / ("%s.py" % _PLUGIN_NAME)
    )
    if source.is_file():
        return source
    raise FileNotFoundError("Bundled GIMP plug-in not found: %s" % bundled)


def install(destination: Optional[Path] = None) -> Path:
    """Retain the legacy unreceipted copy helper for API compatibility."""
    root = (destination or default_plugin_dir()).expanduser().absolute()
    return _replace_plugin(root)


def _plugin_version(script: Path) -> Optional[str]:
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    except (OSError, SyntaxError, UnicodeError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    return None


def _is_link(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        if path.is_symlink() or bool(is_junction and is_junction()):
            return True
        if os.name != "nt":
            return False
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except AttributeError:
        return False
    except (OSError, RuntimeError, ValueError):
        return True
    # GetFileAttributesW returns INVALID_FILE_ATTRIBUTES (-1) for a missing
    # path.  ctypes may expose that sentinel as either signed or unsigned.
    if int(attributes) in (-1, 0xFFFFFFFF):
        return False
    return bool(int(attributes) & 0x400)


def _file_record(
    path: Path,
    root: Path,
    *,
    parent_descriptor: Optional[int] = None,
) -> dict[str, Any]:
    descriptor = None
    owns_parent_descriptor = parent_descriptor is None
    object_lease = None
    directory_lease = None
    try:
        if parent_descriptor is None:
            before = os.lstat(str(path))
        else:
            before = os.lstat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or (parent_descriptor is None and _is_link(path)):
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Managed GIMP file is missing or linked"
            )
        size = int(before.st_size)
        if size <= 0:
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file is empty")
        if size > _MAX_SNAPSHOT_BYTES:
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file is too large")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt":
            directory_lease = _windows_directory_lease(path.parent, "receipt")
            directory_lease.__enter__()
            object_lease = _windows_object_handle(path, "receipt", access=0x80)
            object_lease.__enter__()
            descriptor = os.open(str(path), flags)
        else:
            if parent_descriptor is None:
                parent_descriptor = _open_absolute_dir_nofollow(path.parent)
                owns_parent_descriptor = True
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (int(opened.st_dev), int(opened.st_ino)) != (
            int(before.st_dev),
            int(before.st_ino),
        ) or not stat.S_ISREG(opened.st_mode):
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file changed identity")
        contents = os.read(descriptor, _MAX_SNAPSHOT_BYTES + 1)
        if len(contents) != size:
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file changed identity")
        after = os.fstat(descriptor)
        if (int(after.st_dev), int(after.st_ino)) != (int(before.st_dev), int(before.st_ino)):
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file changed identity")
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file is unavailable") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if parent_descriptor is not None and owns_parent_descriptor:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass
        if object_lease is not None:
            object_lease.__exit__(None, None, None)
        if directory_lease is not None:
            directory_lease.__exit__(None, None, None)
    if not contents:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file is empty")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size": len(contents),
    }


def _owned_root_manifest(
    root: Path, *, max_total_bytes: Optional[int] = None
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    if not root.is_dir() or _is_link(root):
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", "Managed GIMP plug-in root is missing or linked"
        )
    root_identity = _directory_identity(root, "receipt")
    directories: list[str] = []
    # ``os.walk`` yields child names and then later revisits those names by
    # pathname.  Pin every directory inode at the listing boundary so a
    # nested directory replacement cannot turn the next batch into an
    # operator-owned tree while the root itself remains unchanged.
    directory_identities: dict[str, tuple[int, int]] = {"": root_identity}
    files: list[dict[str, Any]] = []
    links: list[str] = []
    total_bytes = 0
    for current, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
        # ``os.walk`` receives a pathname, not an inode.  Revalidate the
        # selected root before consuming each batch so a root swap cannot turn
        # this manifest into an operator-owned external tree.
        if _directory_identity(root, "receipt") != root_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Managed GIMP plug-in root changed identity"
            )
        current_path = Path(current)
        try:
            current_depth = len(current_path.relative_to(root).parts)
        except ValueError as exc:
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Managed manifest escaped its root"
            ) from exc
        current_relative = "" if current_path == root else current_path.relative_to(root).as_posix()
        expected_current_identity = directory_identities.get(current_relative)
        if expected_current_identity is None:
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed manifest directory is unowned")
        if _directory_identity(current_path, "receipt") != expected_current_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Managed manifest directory changed identity"
            )
        if current_depth > _MAX_MANIFEST_DEPTH:
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed manifest is too deep")
        traversable: list[str] = []
        for name in sorted(dirnames):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if len(directories) + len(files) + len(links) >= _MAX_MANIFEST_ENTRIES:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "receipt", "Managed manifest has too many entries"
                )
            if len(Path(relative).parts) > _MAX_MANIFEST_DEPTH:
                raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed manifest is too deep")
            if _is_link(path):
                links.append(relative)
            else:
                directory_identities[relative] = _directory_identity(path, "receipt")
                directories.append(relative)
                traversable.append(name)
        dirnames[:] = traversable
        for name in sorted(filenames):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if len(directories) + len(files) + len(links) >= _MAX_MANIFEST_ENTRIES:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "receipt", "Managed manifest has too many entries"
                )
            if len(Path(relative).parts) > _MAX_MANIFEST_DEPTH:
                raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed manifest is too deep")
            if _is_link(path):
                links.append(relative)
            else:
                try:
                    details = os.lstat(str(path))
                except (OSError, RuntimeError, ValueError) as exc:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "receipt", "Managed GIMP file is unavailable"
                    ) from exc
                if not stat.S_ISREG(details.st_mode):
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "receipt", "Managed GIMP file is not regular"
                    )
                file_size = int(details.st_size)
                if max_total_bytes is not None and total_bytes + file_size > max_total_bytes:
                    raise InstallFailure(EXIT_INSTALL, "recovery", "Managed recovery is too large")
                total_bytes += file_size
                files.append(_file_record(path, root))
        # The current directory may be replaced after its initial identity
        # check but before the file batch is consumed.  Recheck it at the
        # batch boundary so no bytes from a pathname-swapped tree become
        # receipt-owned metadata.
        if _directory_identity(current_path, "receipt") != expected_current_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Managed manifest directory changed identity"
            )
    for relative, expected_identity in directory_identities.items():
        selected = root if not relative else root / relative
        if _directory_identity(selected, "receipt") != expected_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "receipt",
                "Managed manifest directory changed identity",
            )
    return sorted(directories), sorted(files, key=lambda item: item["path"]), sorted(links)


def _files_manifest(root: Path) -> list[dict[str, Any]]:
    """Compatibility projection of the typed owned manifest."""
    return _owned_root_manifest(root)[1]


def _manifest_digest(files: list[dict[str, Any]]) -> str:
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt_path() -> Path:
    return Path.home() / RECEIPT_RELATIVE_PATH


def _assert_receipt_path_safe(path: Path) -> None:
    """Reject receipt files or parent directories that are links/reparse points."""
    try:
        candidate = path
        while True:
            if _is_link(candidate):
                raise InstallFailure(
                    EXIT_PREFLIGHT,
                    "receipt",
                    "Install receipt path contains a link or reparse point",
                )
            parent = candidate.parent
            if parent == candidate:
                break
            candidate = parent
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, InstallFailure):
            raise
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", "Install receipt path is unavailable"
        ) from exc


def _assert_path_components_safe(path: Path, stage: str) -> None:
    try:
        candidate = path
        while True:
            if candidate.exists() and _is_link(candidate):
                raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed path is linked")
            parent = candidate.parent
            if parent == candidate:
                break
            candidate = parent
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed path is unavailable") from exc


@contextlib.contextmanager
def _windows_directory_lease(path: Path, stage: str) -> Any:
    """Hold a non-delete-sharing directory handle while mutating its children."""
    if os.name != "nt":
        yield
        return
    _assert_path_components_safe(path, stage)
    handle = None
    try:
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
        # FILE_READ_ATTRIBUTES | FILE_TRAVERSE, sharing read/write but not
        # delete prevents a concurrent root rename/reparse swap.
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
            error = ctypes.get_last_error()
            raise OSError(error, "CreateFileW directory lease failed", str(path))
    except InstallFailure:
        raise
    except (OSError, AttributeError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, stage, "Windows physical directory lease unavailable"
        ) from exc
    try:
        yield
    finally:
        if handle not in (None,):
            try:
                kernel32.CloseHandle(handle)
            except (OSError, AttributeError):
                pass


@contextlib.contextmanager
def _windows_object_handle(path: Path, stage: str, *, access: int) -> Any:
    """Open one Windows object without following a reparse point."""
    if os.name != "nt":
        yield None
        return
    handle = None
    try:
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
            0x02000000 | 0x00200000,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            error = ctypes.get_last_error()
            raise OSError(error, "CreateFileW object handle failed", str(path))
    except InstallFailure:
        raise
    except (OSError, AttributeError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Windows object handle unavailable") from exc
    try:
        yield handle
    finally:
        if handle not in (None,):
            try:
                kernel32.CloseHandle(handle)
            except (OSError, AttributeError):
                pass


def _windows_set_file_information(handle: Any, info_class: int, payload: Any) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.SetFileInformationByHandle.restype = ctypes.c_int
    size = ctypes.sizeof(payload)
    if not kernel32.SetFileInformationByHandle(handle, info_class, ctypes.byref(payload), size):
        error = ctypes.get_last_error()
        raise OSError(error, "SetFileInformationByHandle failed")


def _object_identity(path: Path, stage: str) -> tuple[int, int, int, int, int, int]:
    try:
        details = os.lstat(str(path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed path is unavailable") from exc
    if _is_link(path):
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed path is linked")
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
        stat.S_IMODE(details.st_mode),
    )


def _windows_rename_by_handle(
    source: Path,
    destination: Path,
    *,
    replace: bool = False,
    _bound_handle: Any = None,
) -> None:
    """Rename a child through its held handle, preventing pathname swaps."""
    import ctypes

    source_before = _object_identity(source, "install")
    handle_context = (
        contextlib.nullcontext(_bound_handle)
        if _bound_handle is not None
        else _windows_object_handle(source, "install", access=0x00010000 | 0x80)
    )
    with handle_context as source_handle:
        if _object_identity(source, "install") != source_before:
            raise InstallFailure(EXIT_PREFLIGHT, "install", "Managed path changed identity")
        _assert_path_components_safe(destination, "install")
        if not replace:
            try:
                os.lstat(str(destination))
            except FileNotFoundError:
                pass
            else:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "install", "Managed rename destination already exists"
                )

        class _FileRenameHeader(ctypes.Structure):
            _fields_ = [
                ("replace_if_exists", ctypes.c_uint32),
                ("root_directory", ctypes.c_void_p),
                ("file_name_length", ctypes.c_uint32),
                # FILE_RENAME_INFO has a flexible WCHAR[1] member.  Keeping
                # one WCHAR in the ctypes layout is important: the filename
                # starts at offset 20 on 64-bit Windows, not at sizeof the
                # padded header (24 bytes).
                ("file_name", ctypes.c_ubyte * 2),
            ]

        encoded_name = str(destination).encode("utf-16-le") + b"\x00\x00"
        header = _FileRenameHeader(
            1 if replace else 0,
            None,
            len(encoded_name) - 2,
            (ctypes.c_ubyte * 2)(),
        )
        filename_offset = _FileRenameHeader.file_name.offset
        payload = (ctypes.c_ubyte * (filename_offset + len(encoded_name)))()
        ctypes.memmove(payload, ctypes.byref(header), ctypes.sizeof(header))
        ctypes.memmove(
            ctypes.addressof(payload) + filename_offset,
            encoded_name,
            len(encoded_name),
        )
        _windows_set_file_information(source_handle, 3, payload)


def _windows_delete_handle(handle: Any) -> None:
    import ctypes

    class _FileDisposition(ctypes.Structure):
        _fields_ = [("delete_file", ctypes.c_ubyte)]

    _windows_set_file_information(handle, 4, _FileDisposition(1))


def _private_tree_identities(
    path: Path, stage: str
) -> dict[str, tuple[int, int, int, int, int, int]]:
    """Capture every physical entry in a private transaction tree."""
    identities = {"": _object_identity(path, stage)}
    try:
        for current, dirnames, filenames in os.walk(str(path), topdown=True, followlinks=False):
            current_path = Path(current)
            for name in sorted(dirnames + filenames):
                child = current_path / name
                relative = child.relative_to(path).as_posix()
                identities[relative] = _object_identity(child, stage)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_INSTALL, stage, "Managed cleanup enumeration failed") from exc
    return identities


def _assert_private_tree_identities(
    path: Path,
    expected: Mapping[str, tuple[int, int, int, int, int, int]],
    stage: str,
) -> None:
    actual = _private_tree_identities(path, stage)
    if actual.keys() != expected.keys() or any(
        actual[key][:2] != tuple(value)[:2] for key, value in expected.items()
    ):
        raise InstallFailure(EXIT_INSTALL, stage, "Private transaction entry changed identity")


def _windows_remove_tree(
    path: Path,
    stage: str = "cleanup",
    *,
    expected: Optional[Mapping[str, tuple[int, int, int, int, int, int]]] = None,
    owner_root: Optional[Path] = None,
) -> None:
    """Recursively remove one tree with per-entry no-follow handles."""
    if os.name != "nt":
        raise InstallFailure(EXIT_INSTALL, stage, "Windows cleanup is unavailable")
    if owner_root is None:
        owner_root = path
    root_expected = expected.get("", _object_identity(owner_root, stage)) if expected else None
    if root_expected is not None:
        relative_root = path.relative_to(owner_root).as_posix() if path != owner_root else ""
        key = relative_root
        if expected is not None and key not in expected:
            raise InstallFailure(EXIT_INSTALL, stage, "Private transaction entry is unowned")
        expected_identity = expected.get(key, root_expected) if expected else root_expected
    else:
        expected_identity = _object_identity(path, stage)
    with _windows_object_handle(path, stage, access=0x00010000 | 0x80 | 0x1) as handle:
        if _object_identity(path, stage)[:2] != tuple(expected_identity)[:2]:
            raise InstallFailure(EXIT_INSTALL, stage, "Managed path changed identity")
        details = os.lstat(str(path))
        if not stat.S_ISDIR(details.st_mode):
            _windows_delete_handle(handle)
            return
        try:
            entries = list(os.scandir(str(path)))
        except (OSError, RuntimeError, ValueError) as exc:
            raise InstallFailure(EXIT_INSTALL, stage, "Managed cleanup enumeration failed") from exc
        for entry in entries:
            child = Path(path) / entry.name
            child_relative = child.relative_to(owner_root).as_posix()
            if expected is not None and child_relative not in expected:
                raise InstallFailure(EXIT_INSTALL, stage, "Private transaction entry is unowned")
            child_expected = (
                expected[child_relative] if expected is not None else _object_identity(child, stage)
            )
            child_details = os.lstat(str(child))
            if stat.S_ISDIR(child_details.st_mode):
                _windows_remove_tree(
                    child,
                    stage,
                    expected=expected,
                    owner_root=owner_root,
                )
            else:
                with _windows_object_handle(
                    child,
                    stage,
                    access=0x00010000 | 0x80,
                ) as child_handle:
                    if _object_identity(child, stage)[:2] != tuple(child_expected)[:2]:
                        raise InstallFailure(EXIT_INSTALL, stage, "Managed path changed identity")
                    _windows_delete_handle(child_handle)
        _windows_delete_handle(handle)


def _directory_identity(path: Path, stage: str) -> tuple[int, int]:
    try:
        details = os.lstat(str(path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed directory is unavailable") from exc
    if not stat.S_ISDIR(details.st_mode) or _is_link(path):
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed directory is linked")
    return int(details.st_dev), int(details.st_ino)


def _receipt_parent_identity(path: Path) -> tuple[int, int]:
    return _directory_identity(path.parent, "receipt")


def _assert_receipt_parent_identity(path: Path, expected: tuple[int, int]) -> None:
    if _receipt_parent_identity(path) != expected:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt parent changed identity")


def _receipt_file_identity(
    path: Path, stage: str = "receipt"
) -> tuple[int, int, int, int, int, int]:
    """Capture one regular receipt/temporary file without following links."""
    try:
        details = os.lstat(str(path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file is unavailable") from exc
    if not stat.S_ISREG(details.st_mode) or _is_link(path):
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file is linked or not regular")
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
        stat.S_IMODE(details.st_mode),
    )


def _assert_receipt_file_identity(
    path: Path, expected: tuple[int, ...], stage: str = "receipt"
) -> None:
    actual = _receipt_file_identity(path, stage)
    if len(expected) == 2:
        matches = actual[:2] == tuple(expected)
    else:
        matches = actual == tuple(expected)
    if not matches:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file changed identity")


def _assert_receipt_file_identity_strict(
    path: Path, expected: tuple[int, ...], stage: str = "receipt"
) -> None:
    """Recheck a receipt inode without going through the injectable seam."""
    actual = _receipt_file_identity(path, stage)
    if (actual[:2] if len(expected) == 2 else actual) != tuple(expected):
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file changed identity")


def _assert_receipt_name_identity(
    path: Path, expected: tuple[int, ...], stage: str = "receipt"
) -> None:
    """Re-check the pathname itself after descriptor checks and race hooks."""
    actual = _receipt_file_identity(path, stage)
    if len(expected) == 2:
        matches = actual[:2] == tuple(expected)
    else:
        matches = actual == tuple(expected)
    if not matches:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file changed identity")


def _receipt_descriptor_identity(
    descriptor: int, name: str, stage: str = "receipt"
) -> tuple[int, int, int, int, int, int]:
    try:
        details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file is unavailable") from exc
    if not stat.S_ISREG(details.st_mode):
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file is linked or not regular")
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
        stat.S_IMODE(details.st_mode),
    )


def _assert_receipt_descriptor_identity(
    descriptor: int, name: str, expected: tuple[int, ...], stage: str = "receipt"
) -> None:
    actual = _receipt_descriptor_identity(descriptor, name, stage)
    if len(expected) == 2:
        matches = actual[:2] == tuple(expected)
    else:
        matches = actual == tuple(expected)
    if not matches:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file changed identity")


def _path_present_no_follow(path: Path) -> bool:
    try:
        os.lstat(str(path))
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed file is unavailable") from exc
    return True


def _read_bytes_no_follow(path: Path, *, max_bytes: Optional[int] = None) -> bytes:
    """Read one regular file while binding the read to its original identity."""
    _assert_receipt_path_safe(path)
    lease = None
    lease_entered = False
    object_lease = None
    object_entered = False
    parent_identity = _receipt_parent_identity(path)
    try:
        before = os.lstat(str(path))
        if not stat.S_ISREG(before.st_mode) or _is_link(path):
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed file is missing or linked")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_descriptor = None
        if os.name == "nt":
            lease = _windows_directory_lease(path.parent, "receipt")
            lease.__enter__()
            lease_entered = True
            _assert_receipt_parent_identity(path, parent_identity)
            _assert_receipt_path_safe(path)
            object_lease = _windows_object_handle(path, "receipt", access=0x80)
            object_lease.__enter__()
            object_entered = True
            if _object_identity(path, "receipt") != (
                int(before.st_dev),
                int(before.st_ino),
                int(before.st_size),
                int(before.st_mtime_ns),
                int(before.st_ctime_ns),
                stat.S_IMODE(before.st_mode),
            ):
                raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed file changed identity")
            descriptor = os.open(str(path), flags)
        else:
            parent_descriptor = _open_absolute_dir_nofollow(path.parent)
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        try:
            opened = os.fstat(descriptor)
            if (int(opened.st_dev), int(opened.st_ino)) != (
                int(before.st_dev),
                int(before.st_ino),
            ) or not stat.S_ISREG(opened.st_mode):
                raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed file changed identity")
            chunks: list[bytes] = []
            remaining = max_bytes
            while True:
                size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining + 1)
                chunk = os.read(descriptor, size)
                if not chunk:
                    break
                chunks.append(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed file is unbounded")
            return b"".join(chunks)
        finally:
            try:
                os.close(descriptor)
            finally:
                if parent_descriptor is not None:
                    os.close(parent_descriptor)
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed file is unavailable") from exc
    finally:
        if object_entered and object_lease is not None:
            object_lease.__exit__(None, None, None)
        if lease_entered and lease is not None:
            lease.__exit__(None, None, None)


def _write_bytes_no_follow(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    """Create a new regular file without following a swapped symlink."""
    descriptor = None
    file_descriptor = None
    lease = None
    lease_entered = False
    object_lease = None
    object_entered = False
    try:
        _assert_path_components_safe(path.parent, "uninstall")
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_path_components_safe(path.parent, "uninstall")
        parent_identity = _directory_identity(path.parent, "uninstall")
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_INSTALL, "uninstall", "Managed restore parent is unavailable"
        ) from exc
    try:
        if os.name == "nt":
            _assert_path_components_safe(path.parent, "uninstall")
            lease = _windows_directory_lease(path.parent, "uninstall")
            lease.__enter__()
            lease_entered = True
            if _directory_identity(path.parent, "uninstall") != parent_identity:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "uninstall", "Managed directory changed identity"
                )
            _assert_path_components_safe(path.parent, "uninstall")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            file_descriptor = os.open(str(path), flags, 0o666)
            parent_descriptor = None
        else:
            parent_descriptor = _open_absolute_dir_nofollow(path.parent, create=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            file_descriptor = os.open(path.name, flags, 0o666, dir_fd=parent_descriptor)
        view = memoryview(data)
        while view:
            written = os.write(file_descriptor, view)
            view = view[written:]
        if mode is not None:
            if os.name == "nt":
                expected = _file_identity(path)
                object_lease = _windows_object_handle(path, "uninstall", access=0x80 | 0x20)
                object_lease.__enter__()
                object_entered = True
                if _file_identity(path) != expected:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "uninstall", "Managed file changed identity"
                    )
                _chmod_no_follow(path, mode)
            else:
                os.chmod(path.name, mode, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileExistsError as exc:
        raise InstallFailure(EXIT_INSTALL, "uninstall", "Managed file changed identity") from exc
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_INSTALL, "uninstall", "Managed file could not be restored"
        ) from exc
    finally:
        if object_entered and object_lease is not None:
            object_lease.__exit__(None, None, None)
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if "parent_descriptor" in locals() and parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass
        if lease_entered and lease is not None:
            lease.__exit__(None, None, None)


def _open_absolute_dir_nofollow(path: Path, *, create: bool = False) -> int:
    """Open an absolute directory by components without following links."""
    if os.name == "nt":
        raise OSError("directory descriptors are unavailable on Windows")
    candidate = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate.anchor or "/", flags)
    try:
        for component in candidate.parts[1:]:
            if component in {"", "."}:
                continue
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o755, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_owned_root_fd(root: Path, expected_identity: Optional[tuple[int, int]]) -> int:
    descriptor = _open_absolute_dir_nofollow(root)
    details = os.fstat(descriptor)
    identity = (int(details.st_dev), int(details.st_ino))
    if expected_identity is not None and identity != expected_identity:
        os.close(descriptor)
        raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path changed identity")
    return descriptor


def _open_relative_dir_nofollow(
    root: Path,
    expected_identity: Optional[tuple[int, int]],
    relative: str,
    *,
    create: bool = False,
) -> int:
    if os.name == "nt":
        return _open_absolute_dir_nofollow(root / relative, create=create)
    parts = Path(relative).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise InstallFailure(EXIT_INSTALL, "profile", "Managed relative path is invalid")
    descriptor = _open_owned_root_fd(root, expected_identity)
    opened: list[int] = []
    try:
        parent = descriptor
        for component in parts:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o755, dir_fd=parent)
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            opened.append(child)
            parent = child
        for child in opened[:-1]:
            os.close(child)
        if opened:
            os.close(descriptor)
            return opened[-1]
        return descriptor
    except BaseException:
        for child in reversed(opened):
            try:
                os.close(child)
            except OSError:
                pass
        os.close(descriptor)
        raise


def _replace_path_owned(
    root: Path,
    expected_identity: Optional[tuple[int, int]],
    source: Path,
    destination: Path,
    expected_file_identities: Optional[Mapping[str, tuple[int, ...]]] = None,
    expected_source_identity: Optional[tuple[int, ...]] = None,
) -> None:
    """Rename only while the selected profile still owns the pathname."""
    # Tests and callers may intentionally replace the primitive to inject a
    # failure.  Keep that seam, while the default path uses a held directory
    # descriptor so a root pathname swap cannot redirect the rename.
    if _replace_path is not _DEFAULT_REPLACE_PATH:
        if expected_file_identities:
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "install",
                "Managed child identity cannot be bound through a replaced rename primitive",
            )
        with _windows_directory_lease(root, "install"):
            with _windows_directory_lease(destination.parent, "install"):
                _assert_physical_root(root, expected_identity)
                _assert_path_components_safe(source, "install")
                _assert_path_components_safe(destination, "install")
                if expected_source_identity is not None:
                    if (
                        _object_identity(source, "install")[:2]
                        != tuple(expected_source_identity)[:2]
                    ):
                        raise InstallFailure(
                            EXIT_PREFLIGHT, "install", "Managed stage changed identity"
                        )
                _replace_path(source, destination)
                _assert_physical_root(root, expected_identity)
        return
    if os.name == "nt":
        with _windows_directory_lease(root, "install"):
            with _windows_directory_lease(destination.parent, "install"):
                _assert_physical_root(root, expected_identity)
                _assert_path_components_safe(source, "install")
                _assert_path_components_safe(destination, "install")
                if expected_source_identity is not None:
                    if (
                        _object_identity(source, "install")[:2]
                        != tuple(expected_source_identity)[:2]
                    ):
                        raise InstallFailure(
                            EXIT_PREFLIGHT, "install", "Managed stage changed identity"
                        )
                if expected_file_identities:
                    _assert_owned_file_identities(source, expected_file_identities, "install")
                _windows_rename_by_handle(source, destination, replace=False)
                _assert_physical_root(root, expected_identity)
        return
    source_descriptor = None
    destination_descriptor = None
    try:
        try:
            source_relative = source.relative_to(root)
        except ValueError:
            source_relative = None
        try:
            destination_relative = destination.relative_to(root)
        except ValueError:
            destination_relative = None
        if source_relative is None and destination_relative is None:
            raise InstallFailure(EXIT_INSTALL, "install", "Managed rename escaped the profile root")

        def open_parent(path: Path, relative: Optional[Path]) -> int:
            if relative is None:
                _assert_path_components_safe(path.parent, "install")
                return _open_absolute_dir_nofollow(path.parent)
            parent_parts = relative.parts[:-1]
            parent_relative = Path(*parent_parts).as_posix() if parent_parts else ""
            return _open_relative_dir_nofollow(
                root, expected_identity, parent_relative, create=False
            )

        source_descriptor = open_parent(source, source_relative)
        destination_descriptor = open_parent(destination, destination_relative)
        source_name = source_relative.name if source_relative is not None else source.name
        destination_name = (
            destination_relative.name if destination_relative is not None else destination.name
        )
        if expected_file_identities:
            _assert_owned_file_identities(source, expected_file_identities, "install")
        if expected_source_identity is not None:
            if _object_identity(source, "install")[:2] != tuple(expected_source_identity)[:2]:
                raise InstallFailure(EXIT_PREFLIGHT, "install", "Managed stage changed identity")
        for attempt in range(6):
            try:
                os.replace(
                    source_name,
                    destination_name,
                    src_dir_fd=source_descriptor,
                    dst_dir_fd=destination_descriptor,
                )
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2**attempt))
        _assert_physical_root(root, expected_identity)
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_INSTALL, "install", "Managed profile rename failed") from exc
    finally:
        try:
            if source_descriptor is not None:
                os.close(source_descriptor)
            if destination_descriptor is not None and destination_descriptor != source_descriptor:
                os.close(destination_descriptor)
        except OSError:
            pass


def _chmod_no_follow(path: Path, mode: int) -> None:
    """Change mode without traversing a swapped symlink where supported."""
    try:
        os.chmod(str(path), mode, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        if _is_link(path):
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Managed file changed to a link"
            ) from None
        os.chmod(str(path), mode)


def _write_bytes_atomic(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    """Replace bytes without following a swapped receipt symlink."""
    _assert_receipt_path_safe(path)
    if _replace_path is not _DEFAULT_REPLACE_PATH or os.name == "nt":
        _write_bytes_atomic_legacy(path, data, mode)
        return
    _write_atomic_at_parent(path, data, mode)


def _write_bytes_atomic_legacy(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    parent_identity = None
    created_temp = False
    published = False
    temporary_identity: Optional[tuple[int, ...]] = None
    try:
        temporary.parent.mkdir(parents=True, exist_ok=True)
        _assert_receipt_path_safe(path)
        parent_identity = _receipt_parent_identity(path)
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_INSTALL, "receipt", "Install receipt parent is unavailable"
        ) from exc
    with _windows_directory_lease(temporary.parent, "receipt"):
        try:
            _assert_receipt_path_safe(path)
            _assert_receipt_parent_identity(path, parent_identity)
            with temporary.open("xb") as stream:
                stream.write(data)
            created_temp = True
            if mode is not None:
                _chmod_no_follow(temporary, mode)
            temporary_identity = _receipt_file_identity(temporary, "receipt")
            _assert_receipt_file_identity(temporary, temporary_identity, "receipt")
            if os.name == "nt" and _replace_path is _DEFAULT_REPLACE_PATH:
                _windows_publish_receipt(temporary, path, temporary_identity, mode)
            else:
                _replace_path(temporary, path)
            _assert_receipt_file_identity(path, temporary_identity[:2], "receipt")
            _assert_receipt_parent_identity(path, parent_identity)
            if mode is not None:
                _chmod_no_follow(path, mode)
            published = True
        finally:
            if created_temp and not published:
                try:
                    _unlink_receipt_owned(temporary, expected_identity=temporary_identity)
                except InstallFailure:
                    pass


def _windows_publish_receipt(
    temporary: Path,
    destination: Path,
    temporary_identity: tuple[int, ...],
    mode: Optional[int],
) -> None:
    """Publish a staged receipt without replacing a raced final name."""
    backup: Optional[Path] = None
    backup_identity: Optional[tuple[int, ...]] = None
    published = False
    with _windows_directory_lease(destination.parent, "receipt"):
        _assert_receipt_path_safe(destination)
        try:
            existing = _receipt_file_identity(destination, "receipt")
        except InstallFailure:
            if _path_present_no_follow(destination):
                raise
            existing = None
        if existing is not None:
            backup = destination.with_name(
                ".%s.receipt-backup-%s" % (destination.name, uuid.uuid4().hex)
            )
            with _windows_object_handle(destination, "receipt", access=0x00010000 | 0x80) as handle:
                _windows_rename_by_handle(destination, backup, replace=False, _bound_handle=handle)
            backup_identity = _receipt_file_identity(backup, "receipt")
        try:
            with _windows_object_handle(temporary, "receipt", access=0x00010000 | 0x80) as handle:
                _windows_rename_by_handle(
                    temporary,
                    destination,
                    replace=False,
                    _bound_handle=handle,
                )
            _assert_receipt_file_identity(destination, temporary_identity[:2], "receipt")
            if mode is not None:
                _chmod_no_follow(destination, mode)
            published = True
        except BaseException:
            if backup is not None and backup_identity is not None:
                try:
                    os.lstat(str(destination))
                except FileNotFoundError:
                    try:
                        with _windows_object_handle(
                            backup, "receipt", access=0x00010000 | 0x80
                        ) as handle:
                            _windows_rename_by_handle(
                                backup,
                                destination,
                                replace=False,
                                _bound_handle=handle,
                            )
                        backup = None
                    except (InstallFailure, OSError):
                        pass
            raise
        finally:
            if published and backup is not None:
                try:
                    _unlink_receipt_owned(backup, expected_identity=backup_identity)
                except InstallFailure:
                    pass


def _write_atomic_at_parent(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    """Publish a complete payload through a held no-follow parent.

    The payload is written and fsynced in a private descriptor first.  The
    destination name is changed only after that descriptor is complete, so a
    short write, disk-full error, or interrupted staging operation leaves the
    previous receipt untouched.  The destination identity is checked again
    after the descriptor-relative rename; a raced replacement is preserved in
    a private recovery name rather than being deleted or treated as owned.
    """
    parent_descriptor = None
    temporary_path: Optional[Path] = None
    temporary_descriptor = None
    created_temp = False
    backup_name: Optional[str] = None
    backup_identity: Optional[tuple[int, int]] = None
    expected_parent_identity: Optional[tuple[int, int]] = None
    try:
        if path.parent.exists():
            expected_parent_identity = _directory_identity(path.parent, "receipt")
        parent_descriptor = _open_absolute_dir_nofollow(path.parent, create=True)
        if expected_parent_identity is not None:
            parent_details = os.fstat(parent_descriptor)
            if (int(parent_details.st_dev), int(parent_details.st_ino)) != expected_parent_identity:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "receipt", "Install receipt parent changed identity"
                )
        with _posix_directory_lock(parent_descriptor):
            temporary_path = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
            temporary_descriptor = os.open(
                temporary_path.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o666,
                dir_fd=parent_descriptor,
            )
            created_temp = True
            temporary_opened = os.fstat(temporary_descriptor)
            temporary_identity = (
                int(temporary_opened.st_dev),
                int(temporary_opened.st_ino),
            )
            view = memoryview(data)
            while view:
                written = os.write(temporary_descriptor, view)
                if written <= 0:
                    raise OSError("short write while staging receipt")
                view = view[written:]
            if mode is not None:
                os.fchmod(temporary_descriptor, mode)
            os.fsync(temporary_descriptor)

            try:
                existing = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise InstallFailure(
                    EXIT_PREFLIGHT, "receipt", "Install receipt file is linked or not regular"
                )
            if existing is not None:
                existing_identity = (int(existing.st_dev), int(existing.st_ino))
                backup_name = ".%s.receipt-backup-%s" % (path.name, uuid.uuid4().hex)
                _POSIX_RENAME(
                    path.name,
                    backup_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                backup_details = os.stat(
                    backup_name, dir_fd=parent_descriptor, follow_symlinks=False
                )
                backup_identity = (int(backup_details.st_dev), int(backup_details.st_ino))
                if backup_identity != existing_identity:
                    _POSIX_RENAME(
                        backup_name,
                        path.name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                    backup_name = None
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "receipt", "Managed receipt file changed identity"
                    )

            committed = False
            publication_succeeded = False
            try:
                _POSIX_LINK(
                    temporary_path.name,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                committed = True
                published = _receipt_descriptor_identity(parent_descriptor, path.name, "receipt")
                if published[:2] != temporary_identity:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "receipt", "Managed receipt file changed identity"
                    )
                if mode is not None and stat.S_IMODE(published[5]) != stat.S_IMODE(mode):
                    raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed receipt mode changed")
                os.fsync(parent_descriptor)
                publication_succeeded = True
            except BaseException:
                # If a raced temporary name was promoted, move the resulting
                # foreign object aside before restoring the previous receipt.
                if committed:
                    try:
                        current = _receipt_descriptor_identity(
                            parent_descriptor, path.name, "receipt"
                        )
                    except InstallFailure:
                        current = None
                    if current is not None and current[:2] != temporary_identity:
                        foreign_name = ".%s.receipt-foreign-%s" % (path.name, uuid.uuid4().hex)
                        try:
                            _posix_rename_at(
                                path.name,
                                foreign_name,
                                parent_descriptor,
                                parent_descriptor,
                            )
                        except OSError:
                            pass
                if committed:
                    try:
                        _unlink_posix_name(
                            parent_descriptor,
                            temporary_path.name,
                            temporary_identity,
                            "receipt",
                        )
                    except InstallFailure:
                        pass
                if backup_name is not None and backup_identity is not None:
                    try:
                        current_destination = os.stat(
                            path.name, dir_fd=parent_descriptor, follow_symlinks=False
                        )
                    except OSError:
                        current_destination = None
                    if current_destination is None:
                        if _restore_posix_name(
                            parent_descriptor,
                            backup_name,
                            path.name,
                            backup_identity,
                            "receipt",
                        ):
                            backup_name = None
                raise
            finally:
                if publication_succeeded and backup_name is not None:
                    try:
                        _unlink_posix_name(
                            parent_descriptor, backup_name, backup_identity, "receipt"
                        )
                    except InstallFailure:
                        pass
                    backup_name = None
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_INSTALL, "receipt", "Install receipt could not be written"
        ) from exc
    finally:
        if temporary_descriptor is not None:
            try:
                os.close(temporary_descriptor)
            except OSError:
                pass
        if created_temp and temporary_path is not None:
            try:
                if parent_descriptor is not None:
                    _unlink_posix_name(
                        parent_descriptor,
                        temporary_path.name,
                        temporary_identity,
                        "receipt",
                    )
            except InstallFailure:
                # A replaced temporary is intentionally preserved for
                # recovery rather than risking deletion of an unowned file.
                pass
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass


def _read_receipt() -> Optional[dict[str, Any]]:
    path = _receipt_path()
    _assert_receipt_path_safe(path)
    try:
        data = _read_bytes_no_follow(path, max_bytes=_MAX_RECEIPT_BYTES)
    except InstallFailure as exc:
        if not _path_present_no_follow(path):
            return None
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt is unreadable") from exc
    if not data:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt is unreadable")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt is unreadable") from exc
    if not isinstance(payload, dict):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("dcc_type") != "gimp":
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt is unsupported")
    return payload


def _posix_rename_at(
    source_name: str, destination_name: str, source_descriptor: int, destination_descriptor: int
) -> None:
    """Rename descriptor-relative names without an injectable pathname seam.

    Python's ``os.rename`` is intentionally not used for identity-sensitive
    lifecycle commits.  Calling libc directly keeps the parent descriptors
    held by the transaction and prevents a test hook (or a replaced Python
    attribute) from substituting the final child after its identity check.
    """
    if os.name == "nt":
        raise OSError("POSIX descriptor rename is unavailable")
    _POSIX_RENAME(
        source_name,
        destination_name,
        src_dir_fd=source_descriptor,
        dst_dir_fd=destination_descriptor,
    )


def _unlink_posix_name(
    descriptor: int,
    name: str,
    expected_identity: Optional[tuple[int, int]],
    stage: str,
) -> None:
    """Remove a descriptor-relative name without consuming a raced object.

    POSIX has no inode-conditional ``unlinkat``.  Move the selected name to a
    fresh private tombstone first, then validate the moved inode.  If the
    pathname was replaced before the move, restore that object with a
    no-replace hard link (or leave it in the private tombstone) and fail
    closed.  Only the identity captured by this transaction is ever eligible
    for cleanup.
    """
    if expected_identity is not None:
        actual = _receipt_descriptor_identity(descriptor, name, stage)
        if actual[:2] != tuple(expected_identity)[:2]:
            raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file changed identity")
    cleanup_name = ".%s.receipt-cleanup-%s" % (name, uuid.uuid4().hex)
    try:
        _posix_rename_at(name, cleanup_name, descriptor, descriptor)
    except FileNotFoundError:
        return
    moved = _receipt_descriptor_identity(descriptor, cleanup_name, stage)
    if expected_identity is not None and moved[:2] != tuple(expected_identity)[:2]:
        _restore_posix_name(descriptor, cleanup_name, name, moved[:2], stage)
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed receipt file changed identity")
    # The cleanup name is private and still identity-bound.  The captured
    # unlink primitive is used only after the rename transaction above; a
    # raced original pathname can therefore never be consumed here.
    _POSIX_UNLINK_SAFE(cleanup_name, dir_fd=descriptor)


def _restore_posix_name(
    descriptor: int,
    source_name: str,
    destination_name: str,
    expected_identity: tuple[int, int],
    stage: str,
) -> bool:
    """Restore a moved object without replacing a newly-created destination."""
    try:
        _assert_receipt_descriptor_identity(descriptor, source_name, expected_identity, stage)
    except InstallFailure:
        return False
    try:
        _POSIX_LINK(
            source_name,
            destination_name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
    except OSError:
        # A foreign creator owns the destination.  Leave the source object in
        # its private name so it is never consumed by recovery cleanup.
        return False
    try:
        _assert_receipt_descriptor_identity(descriptor, destination_name, expected_identity, stage)
    except InstallFailure:
        return False
    try:
        _unlink_posix_name(descriptor, source_name, expected_identity, stage)
    except InstallFailure:
        pass
    return True


def _restore_failed_receipt_move(
    source_descriptor: int,
    source_name: str,
    tombstone_name: Optional[str],
    tombstone_identity: Optional[tuple[int, int]],
    destination_descriptor: int,
    destination_name: str,
    destination_identity: Optional[tuple[int, int]],
) -> None:
    """Undo a failed receipt copy without consuming a raced foreign object."""
    if destination_identity is not None:
        try:
            _unlink_posix_name(
                destination_descriptor,
                destination_name,
                destination_identity,
                "uninstall",
            )
        except (InstallFailure, OSError):
            pass
    if tombstone_name is None or tombstone_identity is None:
        return
    try:
        _assert_receipt_descriptor_identity(
            source_descriptor,
            tombstone_name,
            tombstone_identity,
            "uninstall",
        )
    except InstallFailure:
        return
    try:
        os.stat(source_name, dir_fd=source_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        try:
            _POSIX_LINK(
                tombstone_name,
                source_name,
                src_dir_fd=source_descriptor,
                dst_dir_fd=source_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            return
        try:
            _unlink_posix_name(
                source_descriptor,
                tombstone_name,
                tombstone_identity,
                "uninstall",
            )
        except InstallFailure:
            pass
    except OSError:
        return


def _replace_path(source: Path, destination: Path) -> None:
    """Rename a staged path, tolerating short-lived Windows filesystem handles."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (2**attempt))


_DEFAULT_REPLACE_PATH = _replace_path


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _assert_receipt_path_safe(path)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if _replace_path is not _DEFAULT_REPLACE_PATH or os.name == "nt":
        temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
        parent_identity = None
        created_temp = False
        published = False
        temporary_identity: Optional[tuple[int, ...]] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _assert_receipt_path_safe(path)
            parent_identity = _receipt_parent_identity(path)
        except InstallFailure:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise InstallFailure(
                EXIT_INSTALL, "receipt", "Install receipt parent is unavailable"
            ) from exc
        with _windows_directory_lease(path.parent, "receipt"):
            try:
                _assert_receipt_path_safe(path)
                _assert_receipt_parent_identity(path, parent_identity)
                with temporary.open("xb") as stream:
                    stream.write(encoded)
                created_temp = True
                temporary_identity = _receipt_file_identity(temporary, "receipt")
                _assert_receipt_file_identity(temporary, temporary_identity, "receipt")
                if os.name == "nt" and _replace_path is _DEFAULT_REPLACE_PATH:
                    _windows_publish_receipt(temporary, path, temporary_identity, None)
                else:
                    _replace_path(temporary, path)
                _assert_receipt_file_identity(path, temporary_identity[:2], "receipt")
                _assert_receipt_parent_identity(path, parent_identity)
                published = True
            finally:
                if created_temp and not published:
                    try:
                        _unlink_receipt_owned(temporary, expected_identity=temporary_identity)
                    except InstallFailure:
                        pass
        return
    _write_atomic_at_parent(path, encoded)


def _replace_receipt_owned(
    source: Path,
    destination: Path,
    root: Path,
    expected_identity: Optional[tuple[int, int]],
) -> None:
    """Move the receipt using a held no-follow source parent and root check."""
    source_identity = _receipt_file_identity(source, "uninstall")
    if _replace_path is not _DEFAULT_REPLACE_PATH or os.name == "nt":
        source_parent_identity = _receipt_parent_identity(source)
        destination_parent_identity = _directory_identity(destination.parent, "uninstall")
        with _windows_directory_lease(source.parent, "receipt"):
            with _windows_directory_lease(destination.parent, "uninstall"):
                _assert_receipt_path_safe(source)
                _assert_receipt_parent_identity(source, source_parent_identity)
                _assert_receipt_file_identity(source, source_identity, "uninstall")
                _assert_receipt_name_identity(source, source_identity, "uninstall")
                _assert_receipt_file_identity(source, source_identity, "uninstall")
                _assert_receipt_file_identity_strict(source, source_identity, "uninstall")
                if (
                    _directory_identity(destination.parent, "uninstall")
                    != destination_parent_identity
                ):
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "uninstall", "Receipt destination parent changed identity"
                    )
                _assert_path_components_safe(destination, "uninstall")
                _assert_physical_root(root, expected_identity)
                _assert_receipt_path_safe(source)
                if os.name == "nt" and _replace_path is _DEFAULT_REPLACE_PATH:
                    with _windows_object_handle(
                        source, "uninstall", access=0x00010000 | 0x80
                    ) as source_handle:
                        _assert_receipt_file_identity_strict(source, source_identity, "uninstall")
                        _windows_rename_by_handle(
                            source,
                            destination,
                            replace=False,
                            _bound_handle=source_handle,
                        )
                else:
                    _replace_path(source, destination)
                _assert_receipt_file_identity(destination, source_identity[:2], "uninstall")
                _assert_physical_root(root, expected_identity)
        return
    source_descriptor = None
    destination_descriptor = None
    source_file_descriptor = None
    destination_file_descriptor = None
    tombstone_name: Optional[str] = None
    tombstone_identity: Optional[tuple[int, int]] = None
    destination_identity: Optional[tuple[int, int]] = None
    try:
        _assert_receipt_path_safe(source)
        expected_source_parent = _directory_identity(source.parent, "uninstall")
        expected_destination_parent = _directory_identity(destination.parent, "uninstall")
        source_descriptor = _open_absolute_dir_nofollow(source.parent)
        source_details = os.fstat(source_descriptor)
        if (int(source_details.st_dev), int(source_details.st_ino)) != expected_source_parent:
            raise InstallFailure(
                EXIT_PREFLIGHT, "uninstall", "Receipt source parent changed identity"
            )
        try:
            relative_parent = destination.parent.relative_to(root).as_posix()
        except ValueError:
            _assert_path_components_safe(destination.parent, "uninstall")
            destination_descriptor = _open_absolute_dir_nofollow(destination.parent, create=True)
        else:
            destination_descriptor = _open_relative_dir_nofollow(
                root, expected_identity, relative_parent, create=True
            )
        destination_details = os.fstat(destination_descriptor)
        if (
            int(destination_details.st_dev),
            int(destination_details.st_ino),
        ) != expected_destination_parent:
            raise InstallFailure(
                EXIT_PREFLIGHT, "uninstall", "Receipt destination parent changed identity"
            )
        with _posix_directory_locks(source_descriptor, destination_descriptor):
            _assert_receipt_descriptor_identity(
                source_descriptor, source.name, source_identity, "uninstall"
            )
            _assert_receipt_file_identity(source, source_identity, "uninstall")
            _assert_receipt_name_identity(source, source_identity, "uninstall")
            _assert_receipt_file_identity(source, source_identity, "uninstall")
            _assert_receipt_file_identity_strict(source, source_identity, "uninstall")
            # First move the selected name to a private tombstone in the same
            # source directory.  If a final-name race substitutes an
            # unowned file, the post-move identity check restores that file to
            # its original name and fails closed; it is never promoted to the
            # destination or deleted.
            tombstone_name = ".%s.receipt-moved-%s" % (source.name, uuid.uuid4().hex)
            _posix_rename_at(
                source.name,
                tombstone_name,
                source_descriptor,
                source_descriptor,
            )
            moved_identity = _receipt_descriptor_identity(
                source_descriptor, tombstone_name, "uninstall"
            )
            if moved_identity[:2] != source_identity[:2]:
                _restore_posix_name(
                    source_descriptor,
                    tombstone_name,
                    source.name,
                    moved_identity[:2],
                    "uninstall",
                )
                raise InstallFailure(
                    EXIT_PREFLIGHT, "uninstall", "Managed receipt file changed identity"
                )
            tombstone_identity = moved_identity[:2]
            source_file_descriptor = os.open(
                tombstone_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_descriptor,
            )
            opened_source = os.fstat(source_file_descriptor)
            if (int(opened_source.st_dev), int(opened_source.st_ino)) != source_identity[:2]:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "uninstall", "Managed receipt file changed identity"
                )
            destination_file_descriptor = os.open(
                destination.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                stat.S_IMODE(opened_source.st_mode),
                dir_fd=destination_descriptor,
            )
            destination_opened = os.fstat(destination_file_descriptor)
            destination_identity = (
                int(destination_opened.st_dev),
                int(destination_opened.st_ino),
            )
            try:
                while True:
                    chunk = os.read(source_file_descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_file_descriptor, view)
                        if written <= 0:
                            raise OSError("short write while moving receipt")
                        view = view[written:]
                os.fchmod(destination_file_descriptor, stat.S_IMODE(opened_source.st_mode))
                published = os.fstat(destination_file_descriptor)
            finally:
                os.close(destination_file_descriptor)
                destination_file_descriptor = None
            published_identity = (int(published.st_dev), int(published.st_ino))
            destination_identity = published_identity
            if published_identity == source_identity[:2]:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "uninstall", "Receipt destination identity was reused"
                )
            _assert_receipt_descriptor_identity(
                destination_descriptor, destination.name, published_identity, "uninstall"
            )
            _unlink_posix_name(source_descriptor, tombstone_name, moved_identity[:2], "uninstall")
            tombstone_name = None
            tombstone_identity = None
        _assert_physical_root(root, expected_identity)
    except BaseException as exc:
        if source_descriptor is not None and destination_descriptor is not None:
            _restore_failed_receipt_move(
                source_descriptor,
                source.name,
                tombstone_name,
                tombstone_identity,
                destination_descriptor,
                destination.name,
                destination_identity,
            )
        if isinstance(exc, InstallFailure):
            raise
        if isinstance(exc, (OSError, RuntimeError, ValueError)):
            raise InstallFailure(EXIT_INSTALL, "uninstall", "Install receipt move failed") from exc
        raise
    finally:
        if destination_file_descriptor is not None:
            try:
                os.close(destination_file_descriptor)
            except OSError:
                pass
        if source_file_descriptor is not None:
            try:
                os.close(source_file_descriptor)
            except OSError:
                pass
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)


def _unlink_receipt_owned(
    path: Path, *, expected_identity: Optional[tuple[int, ...]] = None
) -> None:
    """Unlink one receipt name relative to its no-follow parent handle."""
    if expected_identity is None:
        try:
            expected_identity = _receipt_file_identity(path, "receipt")
        except InstallFailure:
            if not _path_present_no_follow(path):
                return
            raise
    if os.name == "nt":
        parent_identity = _receipt_parent_identity(path)
        with _windows_directory_lease(path.parent, "receipt"):
            _assert_receipt_path_safe(path)
            _assert_receipt_parent_identity(path, parent_identity)
            _assert_receipt_file_identity(path, expected_identity, "receipt")
            _assert_receipt_name_identity(path, expected_identity, "receipt")
            _assert_receipt_file_identity(path, expected_identity, "receipt")
            with _windows_object_handle(path, "receipt", access=0x00010000 | 0x80) as handle:
                _assert_receipt_file_identity_strict(path, expected_identity, "receipt")
                _windows_delete_handle(handle)
        return
    descriptor = None
    try:
        _assert_receipt_path_safe(path)
        descriptor = _open_absolute_dir_nofollow(path.parent)
        with _posix_directory_lock(descriptor):
            _assert_receipt_descriptor_identity(descriptor, path.name, expected_identity, "receipt")
            _assert_receipt_file_identity(path, expected_identity, "receipt")
            _assert_receipt_name_identity(path, expected_identity, "receipt")
            _assert_receipt_file_identity(path, expected_identity, "receipt")
            _assert_receipt_file_identity_strict(path, expected_identity, "receipt")
            # POSIX cannot unlink a pathname conditional on an inode.  Move
            # the selected name to a private tombstone and verify the moved
            # inode instead.  A late swap therefore moves the foreign file to
            # the tombstone, which is preserved, while the original name is
            # restored before failing closed.
            tombstone_name = ".%s.receipt-removed-%s" % (path.name, uuid.uuid4().hex)
            _posix_rename_at(path.name, tombstone_name, descriptor, descriptor)
            moved_identity = _receipt_descriptor_identity(descriptor, tombstone_name, "receipt")
            if moved_identity[:2] != expected_identity[:2]:
                _restore_posix_name(
                    descriptor,
                    tombstone_name,
                    path.name,
                    moved_identity[:2],
                    "receipt",
                )
                raise InstallFailure(
                    EXIT_PREFLIGHT, "receipt", "Managed receipt file changed identity"
                )
            _unlink_posix_name(descriptor, tombstone_name, moved_identity[:2], "receipt")
    except FileNotFoundError:
        return
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_INSTALL, "receipt", "Install receipt could not be removed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stage_plugin(root: Path) -> Path:
    try:
        source = _source_file()
    except OSError as exc:
        raise InstallFailure(EXIT_ACQUIRE, "acquire", str(exc)) from exc
    # Re-acquire the physical profile immediately before creating the stage.
    # The lifecycle already pins this identity, but this guard also protects
    # direct/legacy callers and rejects a root junction injected between the
    # outer preflight and this side effect.
    root_identity = _assert_physical_root(root)
    stage_name = ".%s.%s.stage" % (_PLUGIN_NAME, uuid.uuid4().hex)
    stage = root / stage_name
    descriptor = None
    stage_descriptor = None
    try:
        if os.name != "nt":
            descriptor = _open_owned_root_fd(root, root_identity)
            os.mkdir(stage_name, 0o755, dir_fd=descriptor)
            stage_descriptor = os.open(
                stage_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            script_name = "%s.py" % _PLUGIN_NAME
            # Keep the bundled copy operation behind the existing ``copy2``
            # seam (so disk/acquire failures remain injectable) while the
            # final managed write stays descriptor-bound.
            scratch_fd, scratch_name = tempfile.mkstemp(prefix=".dcc-mcp-gimp-stage-")
            os.close(scratch_fd)
            scratch = Path(scratch_name)
            try:
                shutil.copy2(source, scratch)
                source_bytes = scratch.read_bytes()
            finally:
                try:
                    scratch.unlink()
                except OSError:
                    pass
            script_fd = os.open(
                script_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o755,
                dir_fd=stage_descriptor,
            )
            try:
                view = memoryview(source_bytes)
                while view:
                    written = os.write(script_fd, view)
                    view = view[written:]
                os.fchmod(script_fd, 0o755)
            finally:
                os.close(script_fd)
            _BOUND_STAGE_IDENTITIES[str(stage.absolute())] = _object_identity(stage, "install")
            return stage
        with _windows_directory_lease(root, "install"):
            _assert_physical_root(root, root_identity)
            stage.mkdir(parents=True)
            script = stage / ("%s.py" % _PLUGIN_NAME)
            shutil.copy2(source, script)
            _assert_physical_root(root, root_identity)
        _BOUND_STAGE_IDENTITIES[str(stage.absolute())] = _object_identity(stage, "install")
        return stage
    except InstallFailure:
        raise
    except OSError as exc:
        safe_remove_tree(stage)
        raise InstallFailure(EXIT_INSTALL, "install", "Plug-in staging failed: %s" % exc) from exc
    finally:
        if stage_descriptor is not None:
            try:
                os.close(stage_descriptor)
            except OSError:
                pass
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _cleanup_tree(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"success": True, "requires_restart": False}
    try:
        result = safe_remove_tree(path)
    except BaseException as exc:
        return {"success": False, "requires_restart": False, "message": exc.__class__.__name__}
    return result if isinstance(result, dict) else {"success": False, "requires_restart": False}


_DEFAULT_CLEANUP_TREE = _cleanup_tree


@contextlib.contextmanager
def _posix_directory_lock(descriptor: int):
    """Serialize identity checks and child mutations for one directory fd."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextlib.contextmanager
def _posix_directory_locks(*descriptors: int):
    """Lock distinct directory inodes in a stable order."""
    unique: dict[tuple[int, int], int] = {}
    for descriptor in descriptors:
        details = os.fstat(descriptor)
        unique.setdefault((int(details.st_dev), int(details.st_ino)), descriptor)
    with contextlib.ExitStack() as stack:
        for descriptor in [unique[key] for key in sorted(unique)]:
            stack.enter_context(_posix_directory_lock(descriptor))
        yield


@contextlib.contextmanager
def _posix_stable_directory(path: Path, stage: str):
    """Yield an fd-backed path when the platform exposes one."""
    if os.name == "nt" or sys.platform == "darwin":
        yield None
        return
    descriptor = None
    try:
        descriptor = _open_absolute_dir_nofollow(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed directory is unavailable") from exc
    try:
        stable = None
        for prefix in ("/proc/self/fd", "/dev/fd"):
            candidate = Path(prefix) / str(descriptor)
            try:
                if candidate.is_dir():
                    stable = candidate
                    break
            except (OSError, RuntimeError, ValueError):
                continue
        yield stable
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_fd_backed_directory(
    path: Path,
    stage: str,
    expected_identity: Optional[tuple[int, int]] = None,
) -> tuple[int, Path]:
    """Open a directory without following links and expose its fd pathname.

    macOS does not always make ``Path(/proc/self/fd/N)`` discoverable via
    ``is_dir``.  When the platform still provides a ``/dev/fd`` view, using
    that view keeps copytree bound to the opened directory inode; otherwise we
    fail closed instead of copying through a swappable pathname.
    """
    if os.name == "nt":
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Descriptor-bound directory is unavailable")
    descriptor = _open_absolute_dir_nofollow(path)
    details = os.fstat(descriptor)
    actual_identity = (int(details.st_dev), int(details.st_ino))
    if expected_identity is not None and actual_identity != expected_identity:
        os.close(descriptor)
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed directory changed identity")
    for prefix in ("/proc/self/fd", "/dev/fd"):
        candidate = Path(prefix) / str(descriptor)
        try:
            if candidate.is_dir():
                return descriptor, candidate
        except (OSError, RuntimeError, ValueError):
            continue
    try:
        os.close(descriptor)
    except OSError:
        pass
    raise InstallFailure(EXIT_PREFLIGHT, stage, "Descriptor-bound recovery staging is unavailable")


def _copytree_descriptor_bound(source: Path, destination_descriptor: int, name: str) -> None:
    """Copy a tree into an already-open directory without destination paths."""
    source_mode = stat.S_IMODE(os.lstat(str(source)).st_mode)
    os.mkdir(name, source_mode, dir_fd=destination_descriptor)
    child_descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=destination_descriptor,
    )
    try:
        for entry in os.scandir(str(source)):
            source_path = Path(entry.path)
            if entry.is_symlink():
                os.symlink(os.readlink(str(source_path)), entry.name, dir_fd=child_descriptor)
                continue
            if entry.is_dir(follow_symlinks=False):
                _copytree_descriptor_bound(source_path, child_descriptor, entry.name)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise InstallFailure(
                    EXIT_INSTALL, "recovery", "Managed recovery contains an unsupported entry"
                )
            source_fd = os.open(
                str(source_path),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            )
            target_fd = None
            try:
                source_details = os.fstat(source_fd)
                target_fd = os.open(
                    entry.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    stat.S_IMODE(source_details.st_mode),
                    dir_fd=child_descriptor,
                )
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(target_fd, view)
                        view = view[written:]
                os.fchmod(target_fd, stat.S_IMODE(source_details.st_mode))
            finally:
                os.close(source_fd)
                if target_fd is not None:
                    os.close(target_fd)
        os.fchmod(child_descriptor, source_mode)
    finally:
        os.close(child_descriptor)


def _remove_entry_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: Optional[Mapping[str, tuple[int, ...]]] = None,
    relative: str = "",
) -> None:
    """Recursively remove one entry relative to a held no-follow directory."""
    with _posix_directory_lock(parent_descriptor):
        details = os.lstat(name, dir_fd=parent_descriptor)
        identity = (int(details.st_dev), int(details.st_ino))
        expected_identity = expected.get(relative) if expected is not None else None
        if expected is not None:
            if expected_identity is None:
                raise InstallFailure(
                    EXIT_INSTALL, "cleanup", "Private transaction entry is unowned"
                )
            if identity != tuple(expected_identity)[:2]:
                raise InstallFailure(
                    EXIT_INSTALL, "cleanup", "Private transaction entry changed identity"
                )
        if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            try:
                for child_name in os.listdir(child):
                    child_relative = child_name if not relative else f"{relative}/{child_name}"
                    _remove_entry_at(
                        child,
                        child_name,
                        expected=expected,
                        relative=child_relative,
                    )
                current = os.lstat(name, dir_fd=parent_descriptor)
                if (int(current.st_dev), int(current.st_ino)) != identity:
                    raise InstallFailure(EXIT_INSTALL, "cleanup", "Managed path changed identity")
                child_current = os.fstat(child)
                if (int(child_current.st_dev), int(child_current.st_ino)) != identity:
                    raise InstallFailure(EXIT_INSTALL, "cleanup", "Managed path changed identity")
                os.rmdir(name, dir_fd=parent_descriptor)
            finally:
                os.close(child)
            return
        # Open the child and bind its inode before the final name check.  The
        # directory lock provides the protocol boundary for cooperating
        # writers; the strict recheck fails closed if an injected or hostile
        # pathname swap is observed before the destructive syscall.
        if stat.S_ISLNK(details.st_mode):
            current = os.lstat(name, dir_fd=parent_descriptor)
            if (int(current.st_dev), int(current.st_ino)) != identity:
                raise InstallFailure(EXIT_INSTALL, "cleanup", "Managed path changed identity")
            os.unlink(name, dir_fd=parent_descriptor)
            return
        child_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        child_fd = os.open(name, child_flags, dir_fd=parent_descriptor)
        try:
            child_details = os.fstat(child_fd)
            if (int(child_details.st_dev), int(child_details.st_ino)) != identity:
                raise InstallFailure(EXIT_INSTALL, "cleanup", "Managed path changed identity")
            current = os.lstat(name, dir_fd=parent_descriptor)
            if (int(current.st_dev), int(current.st_ino)) != identity:
                raise InstallFailure(EXIT_INSTALL, "cleanup", "Managed path changed identity")
            os.unlink(name, dir_fd=parent_descriptor)
        finally:
            os.close(child_fd)


def _cleanup_tree_owned(
    root: Path, expected_identity: Optional[tuple[int, int]], path: Path
) -> dict[str, Any]:
    """Remove a transaction entry without following swapped path components."""
    if (
        _cleanup_tree is not _DEFAULT_CLEANUP_TREE
        or safe_remove_tree is not _DEFAULT_SAFE_REMOVE_TREE
    ):
        with _windows_directory_lease(root, "cleanup"):
            _assert_physical_root(root, expected_identity)
            _assert_path_components_safe(path, "cleanup")
            try:
                os.lstat(str(path))
            except FileNotFoundError:
                return {"success": True, "requires_restart": False}
            except (OSError, RuntimeError, ValueError) as exc:
                raise InstallFailure(
                    EXIT_INSTALL, "cleanup", "Managed path is unavailable"
                ) from exc
            result = _cleanup_tree(path)
            if result.get("success"):
                _assert_physical_root(root, expected_identity)
        return result
    if os.name == "nt":
        with _windows_directory_lease(root, "cleanup"):
            _assert_physical_root(root, expected_identity)
            try:
                os.lstat(str(path))
            except FileNotFoundError:
                return {"success": True, "requires_restart": False}
            expected = _private_tree_identities(path, "cleanup")
            _assert_private_tree_identities(path, expected, "cleanup")
            _windows_remove_tree(path, "cleanup", expected=expected)
            _assert_physical_root(root, expected_identity)
        return {"success": True, "requires_restart": False}
    try:
        relative = path.relative_to(root).parts
        if not relative:
            raise InstallFailure(EXIT_INSTALL, "cleanup", "Refusing to remove profile root")
        descriptor = _open_owned_root_fd(root, expected_identity)
        try:
            parent = descriptor
            opened: list[int] = []
            for component in relative[:-1]:
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
                opened.append(child)
                parent = child
            _remove_entry_at(parent, relative[-1])
            for child in reversed(opened):
                os.close(child)
        finally:
            os.close(descriptor)
        return {"success": True, "requires_restart": False}
    except FileNotFoundError:
        return {"success": True, "requires_restart": False}
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return {"success": False, "requires_restart": False, "message": str(exc)}


def _cleanup_private_tree(
    path: Path,
    stage: str = "uninstall",
    *,
    expected_identities: Optional[Mapping[str, tuple[int, int, int, int, int, int]]] = None,
) -> dict[str, Any]:
    """Remove an installer-owned transaction tree outside the profile root."""
    parent_identity = _directory_identity(path.parent, stage)
    try:
        with _windows_directory_lease(path.parent, stage):
            if _directory_identity(path.parent, stage) != parent_identity:
                raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed directory changed identity")
            if (
                os.name == "nt"
                and _cleanup_tree is _DEFAULT_CLEANUP_TREE
                and safe_remove_tree is _DEFAULT_SAFE_REMOVE_TREE
            ):
                try:
                    os.lstat(str(path))
                except FileNotFoundError:
                    return {"success": True, "requires_restart": False}
                expected = (
                    dict(expected_identities)
                    if expected_identities is not None
                    else _private_tree_identities(path, stage)
                )
                _assert_private_tree_identities(path, expected, stage)
                _windows_remove_tree(path, stage, expected=expected)
                return {"success": True, "requires_restart": False}
            if (
                os.name != "nt"
                and _cleanup_tree is _DEFAULT_CLEANUP_TREE
                and safe_remove_tree is _DEFAULT_SAFE_REMOVE_TREE
            ):
                parent_descriptor = _open_absolute_dir_nofollow(path.parent)
                try:
                    parent_details = os.fstat(parent_descriptor)
                    if (int(parent_details.st_dev), int(parent_details.st_ino)) != parent_identity:
                        raise InstallFailure(
                            EXIT_PREFLIGHT, stage, "Managed directory changed identity"
                        )
                    try:
                        if expected_identities is not None:
                            _assert_private_tree_identities(path, expected_identities, stage)
                        _remove_entry_at(
                            parent_descriptor,
                            path.name,
                            expected=expected_identities,
                        )
                    except FileNotFoundError:
                        return {"success": True, "requires_restart": False}
                finally:
                    os.close(parent_descriptor)
                return {"success": True, "requires_restart": False}
            result = _cleanup_tree(path)
    except InstallFailure:
        raise
    except BaseException as exc:
        raise InstallFailure(EXIT_INSTALL, stage, "Private transaction cleanup failed") from exc
    return result if isinstance(result, dict) else {"success": False}


def _create_private_transaction(parent: Path, name: str, stage: str) -> Path:
    """Create a private transaction relative to a physically held parent."""
    if os.name == "nt":
        with _windows_directory_lease(parent, stage):
            parent_identity = _directory_identity(parent, stage)
            transaction = parent / name
            try:
                transaction.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise InstallFailure(
                    EXIT_INSTALL, stage, "Private transaction already exists"
                ) from exc
            if _directory_identity(parent, stage) != parent_identity:
                raise InstallFailure(
                    EXIT_PREFLIGHT, stage, "Managed transaction parent changed identity"
                )
            if _directory_identity(transaction, stage) == parent_identity:
                raise InstallFailure(EXIT_INSTALL, stage, "Private transaction identity is invalid")
            _BOUND_TRANSACTION_IDENTITIES[str(transaction.absolute())] = _object_identity(
                transaction, stage
            )
            return transaction
    descriptor = None
    try:
        descriptor = _open_absolute_dir_nofollow(parent)
        details = os.fstat(descriptor)
        parent_identity = (int(details.st_dev), int(details.st_ino))
        os.mkdir(name, 0o700, dir_fd=descriptor)
        current = os.fstat(descriptor)
        if (int(current.st_dev), int(current.st_ino)) != parent_identity:
            try:
                os.unlink(name, dir_fd=descriptor)
            except OSError:
                pass
            raise InstallFailure(
                EXIT_PREFLIGHT, stage, "Managed transaction parent changed identity"
            )
        transaction = parent / name
        _BOUND_TRANSACTION_IDENTITIES[str(transaction.absolute())] = _object_identity(
            transaction, stage
        )
        return transaction
    except FileExistsError as exc:
        raise InstallFailure(EXIT_INSTALL, stage, "Private transaction already exists") from exc
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_INSTALL, stage, "Private transaction could not be created"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _mkdir_relative_nofollow(
    root: Path,
    expected_identity: Optional[tuple[int, int]],
    relative: str,
) -> None:
    """Create a relative directory path using no-follow components."""
    if os.name == "nt":
        with _windows_directory_lease(root, "uninstall"):
            _assert_physical_root(root, expected_identity)
            current = root
            for component in Path(relative).parts:
                current = current / component
                _assert_path_components_safe(current, "uninstall")
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                with _windows_object_handle(
                    current,
                    "uninstall",
                    access=0x80 | 0x20,
                ):
                    if _is_link(current) or not current.is_dir():
                        raise InstallFailure(
                            EXIT_PREFLIGHT,
                            "uninstall",
                            "Managed restore directory is linked or not a directory",
                        )
            _assert_physical_root(root, expected_identity)
        return
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise InstallFailure(EXIT_INSTALL, "uninstall", "Managed restore path is invalid")
    descriptor = _open_owned_root_fd(root, expected_identity)
    opened: list[int] = []
    try:
        parent = descriptor
        for component in parts:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except FileNotFoundError:
                os.mkdir(component, 0o755, dir_fd=parent)
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            opened.append(child)
            parent = child
    finally:
        for child in reversed(opened):
            os.close(child)
        os.close(descriptor)


def _chmod_relative_nofollow(
    root: Path,
    expected_identity: Optional[tuple[int, int]],
    relative: str,
    mode: int,
) -> None:
    if os.name == "nt":
        target = root / relative
        identity_chain: dict[str, tuple[int, int]] = {}
        current = root
        for component in Path(relative).parts:
            current = current / component
            details = _object_identity(current, "uninstall")
            identity_chain[str(current)] = details[:2]
        with _windows_directory_lease(root, "uninstall"):
            _assert_physical_root(root, expected_identity)
            _assert_path_components_safe(target, "uninstall")
            for candidate, identity in identity_chain.items():
                if _object_identity(Path(candidate), "uninstall")[:2] != identity:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "uninstall", "Managed path changed identity"
                    )
            expected = _object_identity(target, "uninstall")
            with _windows_object_handle(
                target,
                "uninstall",
                access=0x80 | 0x20 | 0x00010000,
            ):
                if _object_identity(target, "uninstall")[:2] != expected[:2]:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "uninstall", "Managed path changed identity"
                    )
                _chmod_no_follow(target, mode)
                if _object_identity(target, "uninstall")[:2] != expected[:2]:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "uninstall", "Managed path changed identity"
                    )
            for candidate, identity in identity_chain.items():
                if _object_identity(Path(candidate), "uninstall")[:2] != identity:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "uninstall", "Managed path changed identity"
                    )
        return
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise InstallFailure(EXIT_INSTALL, "uninstall", "Managed restore path is invalid")
    descriptor = _open_owned_root_fd(root, expected_identity)
    opened: list[int] = []
    try:
        parent = descriptor
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            opened.append(child)
            parent = child
        os.chmod(parts[-1], mode, dir_fd=parent, follow_symlinks=False)
    finally:
        for child in reversed(opened):
            os.close(child)
        os.close(descriptor)


def _path_mode(path: Path) -> int:
    """Return a non-following mode on every supported Python, including 3.9."""
    return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)


def _target_identity(target: Path, stage: str = "artifact") -> tuple[int, int]:
    """Bind a managed plug-in directory to its physical object identity."""
    try:
        details = os.lstat(str(target))
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, stage, "Managed GIMP plug-in path is unavailable"
        ) from exc
    if not stat.S_ISDIR(details.st_mode) or _is_link(target):
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed GIMP plug-in path is linked")
    return int(details.st_dev), int(details.st_ino)


def _assert_target_identity(
    target: Path, expected: Optional[tuple[int, int]], stage: str = "artifact"
) -> None:
    if expected is None:
        return
    actual = _target_identity(target, stage)
    if actual != expected:
        raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed GIMP plug-in path changed identity")


def _file_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    """Return a physical identity that detects unlink/recreate inode reuse."""
    details = os.lstat(str(path))
    if not stat.S_ISREG(details.st_mode) or _is_link(path):
        raise ValueError("managed path is not a regular file")
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
        stat.S_IMODE(details.st_mode),
    )


def _owned_file_identities(
    target: Path, receipt: Mapping[str, Any]
) -> dict[str, tuple[int, int, int, int, int, int]]:
    """Capture physical identities for every receipted file before mutation."""
    ownership = receipt.get("ownership")
    files = ownership.get("files") if isinstance(ownership, dict) else None
    if not isinstance(files, list):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt file ownership is invalid")
    identities: dict[str, tuple[int, int, int, int, int, int]] = {}
    for record in files:
        relative = record.get("path") if isinstance(record, dict) else None
        if not isinstance(relative, str):
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt file ownership is invalid")
        path = target / relative
        try:
            details = os.lstat(str(path))
        except (OSError, RuntimeError, ValueError) as exc:
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Managed GIMP file is unavailable"
            ) from exc
        if not stat.S_ISREG(details.st_mode) or _is_link(path):
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Managed GIMP file is missing or linked"
            )
        identities[relative] = (
            int(details.st_dev),
            int(details.st_ino),
            int(details.st_size),
            int(details.st_mtime_ns),
            int(details.st_ctime_ns),
            stat.S_IMODE(details.st_mode),
        )
    return identities


def _assert_owned_file_identities(
    target: Path,
    expected: Optional[Mapping[str, tuple[int, ...]]],
    stage: str = "receipt",
) -> None:
    if expected is None:
        return
    for relative, identity in expected.items():
        path = target / relative
        try:
            actual = _file_identity(path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InstallFailure(
                EXIT_PREFLIGHT, stage, "Managed GIMP file changed identity"
            ) from exc
        expected_identity = tuple(identity)
        if len(expected_identity) == 2:
            matches = actual[:2] == expected_identity
        else:
            matches = actual == expected_identity
        if not matches:
            raise InstallFailure(EXIT_PREFLIGHT, stage, "Managed GIMP file changed identity")


def _report_identity(report: Mapping[str, Any], key: str) -> Optional[tuple[int, int]]:
    value = report.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install plan identity is invalid")
    return int(value[0]), int(value[1])


def _report_file_identities(report: Mapping[str, Any]) -> Optional[dict[str, tuple[int, ...]]]:
    value = report.get("_owned_file_identities")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install plan file identities are invalid")
    result: dict[str, tuple[int, ...]] = {}
    for relative, identity in value.items():
        if (
            not isinstance(relative, str)
            or not isinstance(identity, (list, tuple))
            or len(identity) not in {2, 6}
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in identity)
        ):
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Install plan file identities are invalid"
            )
        result[relative] = tuple(int(item) for item in identity)
    return result


def _entry_point_executable(path: Path) -> bool:
    """Return whether the installed bridge entry point is executable.

    Windows does not use POSIX mode bits for Python plug-ins, so the contract
    treats an existing regular entry point as executable there.  On POSIX the
    receipt binds the execute-bit state and status/verify reject a chmod-only
    drift even when the file bytes are unchanged.
    """
    try:
        mode = _path_mode(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", "Managed GIMP entry point mode is unavailable"
        ) from exc
    return os.name == "nt" or bool(mode & 0o111)


def _recovery_manifest(root: Path) -> dict[str, Any]:
    """Describe recovery bytes and POSIX modes without following links."""
    try:
        directories, files, links = _owned_root_manifest(root, max_total_bytes=_MAX_SNAPSHOT_BYTES)
        if links:
            raise InstallFailure(EXIT_INSTALL, "recovery", "Managed recovery contains links")
        total = sum(int(record["size"]) for record in files)
        if total > _MAX_SNAPSHOT_BYTES:
            raise InstallFailure(EXIT_INSTALL, "recovery", "Managed recovery is too large")
        return {
            "root_mode": _path_mode(root),
            "directories": [
                {
                    "path": relative,
                    "mode": _path_mode(root / relative),
                }
                for relative in directories
            ],
            "files": [
                {
                    **record,
                    "mode": _path_mode(root / str(record["path"])),
                }
                for record in files
            ],
            "links": [],
        }
    except InstallFailure:
        raise
    except OSError as exc:
        raise InstallFailure(
            EXIT_INSTALL, "recovery", "Managed recovery could not be inspected"
        ) from exc


def _copy_validated_recovery(
    source: Path,
    recovery: Path,
    owner_root: Optional[Path] = None,
    expected_identity: Optional[tuple[int, int]] = None,
) -> dict[str, Any]:
    """Create a separate recovery tree and prove that its bytes and modes match."""

    recovery_installed = False

    def cleanup_recovery() -> None:
        if not recovery_installed:
            return
        if owner_root is None:
            _cleanup_tree(recovery)
            return
        try:
            recovery.relative_to(owner_root)
        except ValueError:
            _cleanup_private_tree(recovery, "recovery")
        else:
            _cleanup_tree_owned(owner_root, expected_identity, recovery)

    if owner_root is not None:
        _assert_physical_root(owner_root, expected_identity)
    expected = _recovery_manifest(source)
    destination_parent = recovery.parent
    destination_parent_identity = _directory_identity(destination_parent, "recovery")
    # Prefer the system temporary directory when it is on the destination
    # volume, but fall back to a sibling of the destination parent for custom
    # profiles on another drive. Publishing a staged tree is a rename, so a
    # cross-device temporary would make every recovery transaction fail.
    staging_parent: Optional[Path] = None
    staging_parent_identity: Optional[tuple[int, int]] = None
    candidates: list[Path] = []
    # A sibling of the transaction is the only staging location whose parent
    # is already in the selected physical profile.  Prefer it over global
    # temp: custom destinations may be on another volume and a global
    # pathname can be swapped while copytree is running (notably on macOS).
    candidates.append(destination_parent.parent)
    try:
        candidates.append(Path(tempfile.gettempdir()).expanduser())
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    seen_candidates: set[str] = set()
    for candidate in candidates:
        try:
            _assert_path_components_safe(candidate, "recovery")
            resolved = candidate.resolve(strict=True)
            _assert_path_components_safe(resolved, "recovery")
            identity = _directory_identity(resolved, "recovery")
        except InstallFailure:
            continue
        except (OSError, RuntimeError, ValueError):
            continue
        key = os.path.normcase(str(resolved))
        if key in seen_candidates:
            continue
        seen_candidates.add(key)
        if identity[0] == destination_parent_identity[0]:
            staging_parent = resolved
            staging_parent_identity = identity
            break
    if staging_parent is None or staging_parent_identity is None:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "recovery",
            "No same-volume recovery staging directory is available",
        )

    staging_root: Optional[Path] = None
    with _windows_directory_lease(staging_parent, "recovery"):
        if _directory_identity(staging_parent, "recovery") != staging_parent_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT, "recovery", "Recovery staging parent changed identity"
            )
        if os.name != "nt":
            descriptor = None
            try:
                descriptor = _open_absolute_dir_nofollow(staging_parent)
                details = os.fstat(descriptor)
                if (int(details.st_dev), int(details.st_ino)) != staging_parent_identity:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "recovery", "Recovery staging parent changed identity"
                    )
                for _attempt in range(8):
                    candidate_name = ".dcc-mcp-gimp-recovery-%s" % uuid.uuid4().hex
                    try:
                        os.mkdir(candidate_name, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        continue
                    staging_root = staging_parent / candidate_name
                    break
                if staging_root is None:
                    raise InstallFailure(
                        EXIT_INSTALL,
                        "recovery",
                        "Recovery staging directory could not be created",
                    )
            except InstallFailure:
                raise
            except OSError as exc:
                raise InstallFailure(
                    EXIT_INSTALL,
                    "recovery",
                    "Recovery staging directory could not be created",
                ) from exc
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        else:
            try:
                staging_root = Path(
                    tempfile.mkdtemp(prefix=".dcc-mcp-gimp-recovery-", dir=str(staging_parent))
                )
            except OSError as exc:
                raise InstallFailure(
                    EXIT_INSTALL,
                    "recovery",
                    "Recovery staging directory could not be created",
                ) from exc
        if _directory_identity(staging_parent, "recovery") != staging_parent_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT, "recovery", "Recovery staging parent changed identity"
            )
    staged_recovery = staging_root / recovery.name
    try:
        # Build the snapshot under a leased, same-volume parent. A pathname
        # swap is blocked before copytree can write externally.
        with _windows_directory_lease(staging_parent, "recovery"):
            if _directory_identity(staging_parent, "recovery") != staging_parent_identity:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "recovery", "Recovery staging parent changed identity"
                )
            with _posix_stable_directory(staging_parent, "recovery") as stable_parent:
                fd_backed_descriptor = None
                try:
                    if stable_parent is None and os.name != "nt":
                        if sys.platform == "darwin":
                            # Keep the historical copytree seam injectable for
                            # failure tests, but never give it the selected
                            # profile pathname.  The real macOS copy is
                            # descriptor-bound below because /dev/fd is not a
                            # reliable directory view on every runner.
                            if shutil.copytree is not _DEFAULT_COPYTREE:
                                compatibility_root = Path(
                                    tempfile.mkdtemp(prefix=".dcc-mcp-gimp-copy-seam-")
                                )
                                try:
                                    shutil.copytree(
                                        source,
                                        compatibility_root / "copy",
                                        symlinks=True,
                                        copy_function=shutil.copy2,
                                    )
                                finally:
                                    shutil.rmtree(compatibility_root, ignore_errors=True)
                            fd_backed_descriptor = _open_absolute_dir_nofollow(staging_parent)
                            details = os.fstat(fd_backed_descriptor)
                            if (
                                int(details.st_dev),
                                int(details.st_ino),
                            ) != staging_parent_identity:
                                raise InstallFailure(
                                    EXIT_PREFLIGHT,
                                    "recovery",
                                    "Recovery staging parent changed identity",
                                )
                            stage_descriptor = os.open(
                                staging_root.name,
                                os.O_RDONLY
                                | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0),
                                dir_fd=fd_backed_descriptor,
                            )
                            try:
                                _copytree_descriptor_bound(source, stage_descriptor, recovery.name)
                            finally:
                                os.close(stage_descriptor)
                        else:
                            fd_backed_descriptor, stable_parent = _open_fd_backed_directory(
                                staging_parent, "recovery", staging_parent_identity
                            )
                            copy_parent = stable_parent
                            copy_destination = copy_parent / staging_root.name / recovery.name
                            shutil.copytree(
                                source,
                                copy_destination,
                                symlinks=True,
                                copy_function=shutil.copy2,
                            )
                    else:
                        copy_parent = stable_parent or staging_parent
                        copy_destination = copy_parent / staging_root.name / recovery.name
                        shutil.copytree(
                            source,
                            copy_destination,
                            symlinks=True,
                            copy_function=shutil.copy2,
                        )
                finally:
                    if fd_backed_descriptor is not None:
                        try:
                            os.close(fd_backed_descriptor)
                        except OSError:
                            pass
            if _directory_identity(staging_parent, "recovery") != staging_parent_identity:
                raise InstallFailure(
                    EXIT_PREFLIGHT, "recovery", "Recovery staging parent changed identity"
                )
    except InstallFailure:
        try:
            with _windows_directory_lease(staging_parent, "recovery"):
                _cleanup_private_tree(staging_root, "recovery")
        except InstallFailure:
            pass
        raise
    except OSError as exc:
        try:
            with _windows_directory_lease(staging_parent, "recovery"):
                _cleanup_private_tree(staging_root, "recovery")
        except InstallFailure:
            pass
        raise InstallFailure(
            EXIT_INSTALL, "recovery", "Recovery snapshot could not be created"
        ) from exc
    try:
        if _directory_identity(staging_parent, "recovery") != staging_parent_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT, "recovery", "Recovery staging parent changed identity"
            )
        if owner_root is not None:
            _assert_physical_root(owner_root, expected_identity)
        if _recovery_manifest(staged_recovery) != expected:
            raise InstallFailure(EXIT_INSTALL, "recovery", "Recovery snapshot validation failed")
        if _directory_identity(destination_parent, "recovery") != destination_parent_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT, "recovery", "Recovery destination changed identity"
            )
        _assert_path_components_safe(recovery, "recovery")
        _replace_path_owned(
            destination_parent,
            destination_parent_identity,
            staged_recovery,
            recovery,
        )
        recovery_installed = True
        if owner_root is not None:
            _assert_physical_root(owner_root, expected_identity)
        if _directory_identity(destination_parent, "recovery") != destination_parent_identity:
            raise InstallFailure(
                EXIT_PREFLIGHT, "recovery", "Recovery destination changed identity"
            )
        if _recovery_manifest(recovery) != expected:
            raise InstallFailure(EXIT_INSTALL, "recovery", "Recovery snapshot validation failed")
        return expected
    except InstallFailure:
        cleanup_recovery()
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        cleanup_recovery()
        raise InstallFailure(
            EXIT_INSTALL, "recovery", "Recovery snapshot validation failed"
        ) from exc
    finally:
        try:
            with _windows_directory_lease(staging_parent, "recovery"):
                if _directory_identity(staging_parent, "recovery") != staging_parent_identity:
                    raise InstallFailure(
                        EXIT_PREFLIGHT, "recovery", "Recovery staging parent changed identity"
                    )
                _cleanup_private_tree(staging_root, "recovery")
        except InstallFailure:
            # Never follow an untrusted staging pathname during cleanup.  A
            # failed identity proof leaves the private temporary for manual
            # cleanup instead of risking deletion outside the transaction.
            pass


def _replace_plugin(root: Path) -> Path:
    """Replace only the legacy copy; the lifecycle uses a receipted transaction."""
    _assert_physical_root(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, "profile", "GIMP profile path is not a directory"
        ) from exc
    root_identity = _assert_physical_root(root)
    target = root / _PLUGIN_NAME
    stage = _stage_plugin(root)
    _BOUND_STAGE_IDENTITIES.pop(str(stage.absolute()), None)
    backup = root / (".%s.%s.backup" % (_PLUGIN_NAME, uuid.uuid4().hex))
    try:
        if target.exists():
            _replace_path_owned(
                root,
                root_identity,
                target,
                backup,
            )
        _replace_path_owned(root, root_identity, stage, target)
        cleanup = _cleanup_tree_owned(root, root_identity, backup)
        if not cleanup.get("success"):
            raise InstallFailure(EXIT_INSTALL, "cleanup", "Legacy backup cleanup failed")
    except BaseException:
        if target.exists() and backup.exists():
            _cleanup_tree_owned(root, root_identity, target)
        if backup.exists():
            _replace_path_owned(root, root_identity, backup, target)
        _cleanup_tree_owned(root, root_identity, stage)
        raise
    return target


def _valid_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return (
        not path.is_absolute()
        and not path.drive
        and ".." not in path.parts
        and path.as_posix() == value
    )


def _assert_physical_root(
    root: Path, expected_identity: Optional[tuple[int, int]] = None
) -> Optional[tuple[int, int]]:
    """Reject a profile path that changed identity after planning."""
    try:
        ancestor = root
        while True:
            if ancestor.exists() and _is_link(ancestor):
                raise InstallFailure(
                    EXIT_PREFLIGHT,
                    "profile",
                    "GIMP profile path resolves through a link or changed identity",
                )
            parent = ancestor.parent
            if parent == ancestor:
                break
            ancestor = parent
        resolved = root.resolve(strict=False)
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path is unavailable") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(root)):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "profile",
            "GIMP profile path resolves through a link or changed identity",
        )
    try:
        exists = root.exists()
        invalid_directory = exists and (_is_link(root) or not root.is_dir())
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path is unavailable") from exc
    if invalid_directory:
        raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path is not a directory")
    if not exists:
        if expected_identity is not None:
            raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path changed identity")
        return None
    try:
        details = os.lstat(str(root))
        identity = (int(details.st_dev), int(details.st_ino))
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path is unavailable") from exc
    if expected_identity is not None and identity != expected_identity:
        raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path changed identity")
    return identity


def _assert_profile_writable(root: Path) -> None:
    """Preflight the nearest physical profile directory before mutations."""
    candidate = root
    try:
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        if not candidate.is_dir() or _is_link(candidate):
            raise InstallFailure(
                EXIT_PREFLIGHT, "profile", "GIMP profile parent is not a directory"
            )
        if not os.access(str(candidate), os.W_OK | os.X_OK):
            raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path is not writable")
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "profile", "GIMP profile path is unavailable") from exc


def _validate_owned_install(
    destination: Path,
    target: Path,
    receipt_path: Path,
    receipt: Mapping[str, Any],
) -> None:
    required = {
        "dcc_type": "gimp",
        "destination": str(destination),
        "plugin_path": str(target),
    }
    if any(str(receipt.get(key, "")) != expected for key, expected in required.items()):
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", "Receipt path does not match the selected profile"
        )
    if receipt.get("host_paths_touched") != [str(target)]:
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", "Receipt host paths do not match the selected profile"
        )
    for version in (
        receipt.get("adapter_version"),
        receipt.get("core_version"),
        receipt.get("gimp_version"),
    ):
        if not _version_tuple(version):
            raise InstallFailure(
                EXIT_PREFLIGHT, "receipt", "Receipt contains a noncanonical version"
            )
    if not receipt_path.is_file() or _is_link(receipt_path):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt is missing or linked")
    ownership = receipt.get("ownership")
    if not isinstance(ownership, dict) or set(ownership) != {"directories", "files", "links"}:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Typed ownership manifest is missing")
    directories = ownership.get("directories")
    files = ownership.get("files")
    links = ownership.get("links")
    if not isinstance(directories, list) or not all(
        isinstance(value, str) and _valid_relative_path(value) for value in directories
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt directory ownership is invalid")
    if len(directories) > _MAX_MANIFEST_ENTRIES:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt manifest has too many entries")
    if any(len(Path(value).parts) > _MAX_MANIFEST_DEPTH for value in directories):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt manifest is too deep")
    if len(directories) != len(set(directories)):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "receipt",
            "Receipt directory ownership contains duplicates",
        )
    if (
        not isinstance(links, list)
        or not all(isinstance(value, str) and _valid_relative_path(value) for value in links)
        or len(links) != len(set(links))
        or links
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt link ownership is invalid")
    if len(links) > _MAX_MANIFEST_ENTRIES:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt manifest has too many entries")
    if not isinstance(files, list) or not files:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt file ownership is invalid")
    file_paths: list[str] = []
    for record in files:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256", "size"}
            or not _valid_relative_path(record.get("path"))
            or re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))) is None
            or not isinstance(record.get("size"), int)
            or record["size"] <= 0
        ):
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt file ownership is invalid")
        file_paths.append(record["path"])
    if len(files) + len(directories) + len(links) > _MAX_MANIFEST_ENTRIES:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt manifest has too many entries")
    if any(len(Path(value).parts) > _MAX_MANIFEST_DEPTH for value in file_paths):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt manifest is too deep")
    if len(file_paths) != len(set(file_paths)):
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", "Receipt file ownership contains duplicates"
        )
    expected_files = sorted(files, key=lambda item: item["path"])
    declared_files = receipt.get("files")
    if not isinstance(declared_files, list) or declared_files != expected_files:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt file manifest is inconsistent")
    package_digest = receipt.get("package_digest")
    if (
        not isinstance(package_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", package_digest) is None
        or package_digest != _manifest_digest(expected_files)
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt package digest is invalid")
    entry_point_executable = receipt.get("entry_point_executable")
    if not isinstance(entry_point_executable, bool):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt entry point mode is invalid")
    entry_point = target / ("%s.py" % _PLUGIN_NAME)
    if _entry_point_executable(entry_point) != entry_point_executable:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "receipt",
            "Managed GIMP entry point executable mode differs from the receipt",
        )
    actual_directories, actual_files, actual_links = _owned_root_manifest(target)
    if (
        actual_directories != sorted(directories)
        or actual_files != sorted(files, key=lambda item: item["path"])
        or actual_links != links
    ):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "receipt",
            "Managed GIMP files, directories, or links differ from the receipt",
        )


def _receipt_payload(root: Path, target: Path, report: Mapping[str, Any]) -> dict[str, Any]:
    directories, files, links = _owned_root_manifest(target)
    entry_point = target / ("%s.py" % _PLUGIN_NAME)
    return {
        "schema_version": SCHEMA_VERSION,
        "dcc_type": "gimp",
        "adapter_version": __version__,
        "core_version": report["core_version"],
        "gimp_version": report["gimp_version"],
        "dcc_path": report["dcc_path"],
        "python": report["python"],
        "adapter_module_path": report.get("adapter_module_path"),
        "core_module_path": report.get("core_module_path"),
        "destination": str(root),
        "plugin_path": str(target),
        "host_paths_touched": [str(target)],
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "package_digest": _manifest_digest(files),
        "entry_point_executable": _entry_point_executable(entry_point),
        "files": files,
        "ownership": {"directories": directories, "files": files, "links": links},
    }


@dataclass
class InstallTransaction:
    root: Path
    target: Path
    receipt_path: Path
    backup: Path
    old_receipt: Optional[bytes]
    old_receipt_mode: Optional[int]
    previous_moved: bool
    previous_manifest: Optional[dict[str, Any]]
    root_identity: Optional[tuple[int, int]] = None
    replacement_moved: bool = False
    recovery: Optional[Path] = None
    recovery_manifest: Optional[dict[str, Any]] = None
    target_identity: Optional[tuple[int, int]] = None
    file_identities: Optional[dict[str, tuple[int, ...]]] = None
    previous_file_identities: Optional[dict[str, tuple[int, ...]]] = None
    closed: bool = False

    def rollback(self) -> bool:
        if self.closed:
            return self.previous_moved
        failed = self.root / (".%s.%s.failed" % (_PLUGIN_NAME, uuid.uuid4().hex))
        try:
            _assert_physical_root(self.root, self.root_identity)
            if self.replacement_moved and (self.target.exists() or self.target.is_symlink()):
                _replace_path_owned(
                    self.root,
                    self.root_identity,
                    self.target,
                    failed,
                    expected_file_identities=self.file_identities,
                )
            if self.previous_moved:
                restore_source = None
                recovery_changed = False
                for candidate in (self.recovery, self.backup):
                    if candidate is None or not candidate.is_dir() or _is_link(candidate):
                        continue
                    if self.previous_manifest is not None:
                        try:
                            if _recovery_manifest(candidate) != self.previous_manifest:
                                recovery_changed = True
                                continue
                        except (InstallFailure, OSError):
                            recovery_changed = True
                            continue
                    restore_source = candidate
                    break
                if restore_source is None:
                    if recovery_changed:
                        if self.old_receipt is None:
                            _unlink_receipt_owned(self.receipt_path)
                        else:
                            _write_bytes_atomic(
                                self.receipt_path, self.old_receipt, self.old_receipt_mode
                            )
                        _cleanup_tree_owned(self.root, self.root_identity, failed)
                        self.closed = True
                        raise InstallFailure(
                            EXIT_INSTALL,
                            "recovery",
                            "Prior GIMP install changed identity; recovery was preserved",
                        )
                    raise InstallFailure(
                        EXIT_INSTALL, "recovery", "No validated prior GIMP recovery remains"
                    )
                _replace_path_owned(
                    self.root,
                    self.root_identity,
                    restore_source,
                    self.target,
                    expected_file_identities=(
                        self.previous_file_identities if restore_source == self.backup else None
                    ),
                )
            try:
                if self.old_receipt is None:
                    _unlink_receipt_owned(self.receipt_path)
                else:
                    _write_bytes_atomic(self.receipt_path, self.old_receipt, self.old_receipt_mode)
            except InstallFailure as receipt_error:
                # A receipt pathname that changed to a link/reparse point is
                # no longer safe to restore. Keep it untouched, clean only
                # the known replacement, and return a structured install
                # failure instead of following the foreign target.
                _cleanup_tree_owned(self.root, self.root_identity, failed)
                _cleanup_tree_owned(self.root, self.root_identity, self.backup)
                self.closed = True
                raise InstallFailure(
                    EXIT_INSTALL,
                    "receipt",
                    "Install receipt changed identity; rollback was preserved",
                ) from receipt_error
            if self.previous_moved and self.previous_manifest is not None:
                if _recovery_manifest(self.target) != self.previous_manifest:
                    raise InstallFailure(
                        EXIT_INSTALL, "recovery", "Restored GIMP recovery validation failed"
                    )
            _cleanup_tree_owned(self.root, self.root_identity, failed)
            _cleanup_tree_owned(self.root, self.root_identity, self.backup)
            if self.recovery is not None:
                _cleanup_tree_owned(self.root, self.root_identity, self.recovery)
            self.closed = True
            return self.previous_moved
        except InstallFailure:
            raise
        except BaseException as exc:
            raise InstallFailure(
                EXIT_INSTALL, "install", "Prior GIMP install could not be restored"
            ) from exc

    def commit(self) -> None:
        if self.closed:
            return
        _assert_physical_root(self.root, self.root_identity)
        _assert_target_identity(self.target, self.target_identity, "install")
        _assert_owned_file_identities(self.target, self.file_identities, "install")
        if self.previous_moved:
            self.recovery = self.root / (".%s.%s.recovery" % (_PLUGIN_NAME, uuid.uuid4().hex))
            try:
                self.recovery_manifest = _copy_validated_recovery(
                    self.backup, self.recovery, self.root, self.root_identity
                )
            except InstallFailure:
                self.rollback()
                raise
        _assert_physical_root(self.root, self.root_identity)
        _assert_target_identity(self.target, self.target_identity, "install")
        _assert_owned_file_identities(self.target, self.file_identities, "install")
        cleanup = _cleanup_tree_owned(self.root, self.root_identity, self.backup)
        if not cleanup.get("success"):
            self.rollback()
            code = EXIT_REQUIRES_RESTART if cleanup.get("requires_restart") else EXIT_INSTALL
            raise InstallFailure(code, "cleanup", "Verified install backup cleanup failed")
        self.closed = True
        if self.recovery is not None:
            _assert_physical_root(self.root, self.root_identity)
            _cleanup_tree_owned(self.root, self.root_identity, self.recovery)


def _begin_replace_plugin(root: Path, report: Mapping[str, Any]) -> InstallTransaction:
    _assert_physical_root(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, "profile", "GIMP profile path is not a directory"
        ) from exc
    root_identity = _assert_physical_root(root)
    target = root / _PLUGIN_NAME
    planned_target_identity = _report_identity(report, "_target_identity")
    if planned_target_identity is not None:
        _assert_target_identity(target, planned_target_identity, "install")
    planned_file_identities = _report_file_identities(report)
    if planned_file_identities:
        _assert_owned_file_identities(target, planned_file_identities, "install")
    lock_state = inspect_install_root(target)
    if lock_state.get("requires_restart"):
        raise InstallFailure(
            EXIT_REQUIRES_RESTART,
            "install",
            str(lock_state.get("recommended_next_action", "GIMP restart required")),
        )
    if report.get("installation_state") in {"partial", "repair"}:
        raise InstallFailure(
            EXIT_INSTALL, "receipt", "Unowned or changed GIMP plug-in state cannot be overwritten"
        )
    stage = _stage_plugin(root)
    stage_identity = _BOUND_STAGE_IDENTITIES.pop(str(stage.absolute()), None)
    if stage_identity is None:
        stage_identity = _object_identity(stage, "install")
    try:
        _assert_physical_root(root, root_identity)
    except BaseException:
        _cleanup_tree_owned(root, root_identity, stage)
        raise
    backup = root / (".%s.%s.backup" % (_PLUGIN_NAME, uuid.uuid4().hex))
    receipt_path = _receipt_path()
    _assert_receipt_path_safe(receipt_path)
    try:
        old_receipt = _read_bytes_no_follow(receipt_path, max_bytes=_MAX_RECEIPT_BYTES)
    except InstallFailure as exc:
        if not _path_present_no_follow(receipt_path):
            old_receipt = None
        else:
            raise exc
    old_receipt_mode = _path_mode(receipt_path) if old_receipt is not None else None
    previous_manifest = _recovery_manifest(target) if target.is_dir() else None
    transaction = InstallTransaction(
        root=root,
        target=target,
        receipt_path=receipt_path,
        backup=backup,
        old_receipt=old_receipt,
        old_receipt_mode=old_receipt_mode,
        previous_moved=False,
        previous_manifest=previous_manifest,
        root_identity=root_identity,
        previous_file_identities=planned_file_identities,
    )
    try:
        if target.exists():
            _assert_physical_root(root, root_identity)
            if planned_target_identity is not None:
                _assert_target_identity(target, planned_target_identity, "install")
            if planned_file_identities:
                _assert_owned_file_identities(target, planned_file_identities, "install")
            _replace_path_owned(
                root,
                root_identity,
                target,
                backup,
                expected_file_identities=planned_file_identities,
            )
            transaction.previous_moved = True
            if planned_file_identities:
                _assert_owned_file_identities(backup, planned_file_identities, "install")
        _assert_physical_root(root, root_identity)
        _replace_path_owned(
            root,
            root_identity,
            stage,
            target,
            expected_source_identity=stage_identity,
        )
        transaction.replacement_moved = True
        _assert_physical_root(root, root_identity)
        _write_json_atomic(receipt_path, _receipt_payload(root, target, report))
        receipt = _read_receipt()
        if receipt is None:
            raise InstallFailure(EXIT_INSTALL, "receipt", "Install receipt commit failed")
        _validate_owned_install(root, target, receipt_path, receipt)
        transaction.target_identity = _target_identity(target, "install")
        transaction.file_identities = _owned_file_identities(target, receipt)
        return transaction
    except BaseException as exc:
        transaction.rollback()
        _cleanup_tree_owned(root, root_identity, stage)
        if isinstance(exc, InstallFailure):
            raise
        code = EXIT_REQUIRES_RESTART if isinstance(exc, PermissionError) else EXIT_INSTALL
        raise InstallFailure(code, "install", "Install rolled back: %s" % exc) from exc
    finally:
        _cleanup_tree_owned(root, root_identity, stage)


def _installation_state(destination: Path) -> str:
    _assert_physical_root(destination)
    target = destination / _PLUGIN_NAME
    receipt_path = _receipt_path()
    target_exists = target.exists() or target.is_symlink()
    receipt_exists = receipt_path.exists() or receipt_path.is_symlink()
    if not target_exists and not receipt_exists:
        return "fresh"
    try:
        receipt = _read_receipt()
    except InstallFailure:
        return "partial"
    if receipt is None or not target_exists:
        return "partial"
    try:
        _validate_owned_install(destination, target, receipt_path, receipt)
    except InstallFailure:
        return "repair"
    installed_version = _plugin_version(target / ("%s.py" % _PLUGIN_NAME))
    if not _version_tuple(installed_version):
        return "repair"
    return "current" if installed_version == __version__ else "upgrade"


@dataclass(frozen=True)
class OwnedSnapshot:
    root_mode: int
    directory_modes: Mapping[str, int]
    files: Mapping[str, bytes]
    file_modes: Mapping[str, int]
    receipt_bytes: bytes
    receipt_mode: int


def _capture_owned_bytes(
    target: Path, receipt_path: Path, receipt: Mapping[str, Any]
) -> OwnedSnapshot:
    ownership = receipt["ownership"]
    directory_modes = {
        str(relative): _path_mode(target / str(relative)) for relative in ownership["directories"]
    }
    files: dict[str, bytes] = {}
    file_modes: dict[str, int] = {}
    try:
        receipt_bytes = _read_bytes_no_follow(receipt_path, max_bytes=_MAX_RECEIPT_BYTES)
    except InstallFailure as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt is unreadable") from exc
    total = len(receipt_bytes)
    for record in ownership["files"]:
        relative = str(record["path"])
        path = target / relative
        data = _read_bytes_no_follow(path, max_bytes=int(record["size"]))
        if len(data) != int(record["size"]):
            raise InstallFailure(EXIT_PREFLIGHT, "uninstall", "Managed GIMP file changed identity")
        total += len(data)
        files[relative] = data
        file_modes[relative] = _path_mode(path)
    if total > _MAX_SNAPSHOT_BYTES:
        raise InstallFailure(
            EXIT_INSTALL, "uninstall", "Managed install is too large for bounded rollback"
        )
    return OwnedSnapshot(
        root_mode=_path_mode(target),
        directory_modes=directory_modes,
        files=files,
        file_modes=file_modes,
        receipt_bytes=receipt_bytes,
        receipt_mode=_path_mode(receipt_path),
    )


def _restore_owned_bytes(
    destination: Path,
    target: Path,
    receipt_path: Path,
    receipt: Mapping[str, Any],
    snapshot: OwnedSnapshot,
    expected_identity: Optional[tuple[int, int]] = None,
) -> None:
    _assert_physical_root(destination, expected_identity)
    _assert_receipt_path_safe(receipt_path)
    if target.exists() or target.is_symlink():
        removed = _cleanup_tree_owned(destination, expected_identity, target)
        if not removed.get("success"):
            raise InstallFailure(EXIT_INSTALL, "uninstall", "Managed plug-in could not be restored")
    _assert_physical_root(destination, expected_identity)
    if target.exists() or target.is_symlink() or _is_link(target):
        raise InstallFailure(EXIT_INSTALL, "uninstall", "Managed plug-in path is linked")
    _mkdir_relative_nofollow(destination, expected_identity, target.name)
    for relative in sorted(
        snapshot.directory_modes, key=lambda value: (len(Path(value).parts), value)
    ):
        _assert_physical_root(destination, expected_identity)
        _mkdir_relative_nofollow(destination, expected_identity, str(Path(target.name) / relative))
    for relative, data in snapshot.files.items():
        _assert_physical_root(destination, expected_identity)
        path = target / relative
        _write_bytes_no_follow(path, data, snapshot.file_modes[relative])
    for relative in sorted(
        snapshot.directory_modes,
        key=lambda value: (len(Path(value).parts), value),
        reverse=True,
    ):
        _assert_physical_root(destination, expected_identity)
        _chmod_relative_nofollow(
            destination,
            expected_identity,
            str(Path(target.name) / relative),
            snapshot.directory_modes[relative],
        )
    _assert_physical_root(destination, expected_identity)
    _chmod_relative_nofollow(destination, expected_identity, target.name, snapshot.root_mode)
    _write_bytes_atomic(receipt_path, snapshot.receipt_bytes, snapshot.receipt_mode)
    _validate_owned_install(destination, target, receipt_path, receipt)


def _execute_uninstall(report: dict[str, Any]) -> tuple[dict[str, Any], int]:
    destination = Path(report["destination"])
    root_identity = _assert_physical_root(destination)
    target = destination / _PLUGIN_NAME
    planned_target_identity = _report_identity(report, "_target_identity")
    planned_file_identities = _report_file_identities(report)
    receipt_path = _receipt_path()
    _assert_receipt_path_safe(receipt_path)
    if not target.exists() and not receipt_path.exists():
        report["status"] = "ok"
        report["steps"][-1] = {"id": "uninstall", "status": "already_absent"}
        return report, 0
    receipt = _read_receipt()
    if receipt is None:
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", "Refusing to remove an unreceipted GIMP plug-in"
        )
    if planned_target_identity is not None:
        _assert_target_identity(target, planned_target_identity, "uninstall")
    _validate_owned_install(destination, target, receipt_path, receipt)
    _assert_target_identity(target, planned_target_identity, "uninstall")
    _assert_owned_file_identities(target, planned_file_identities, "uninstall")
    lock_state = inspect_install_root(target)
    if lock_state.get("requires_restart"):
        raise InstallFailure(
            EXIT_REQUIRES_RESTART,
            "uninstall",
            str(lock_state.get("recommended_next_action", "GIMP restart required")),
        )
    _assert_physical_root(destination, root_identity)
    owned_snapshot = _capture_owned_bytes(target, receipt_path, receipt)
    # Bind every entry that will be moved into quarantine on every platform;
    # POSIX cleanup must reject a late unowned child just as Windows does.
    target_tree_identities = _private_tree_identities(target, "uninstall")
    _assert_target_identity(target, planned_target_identity, "uninstall")
    _assert_owned_file_identities(target, planned_file_identities, "uninstall")
    _assert_physical_root(destination, root_identity)
    transaction_parent = destination.parent
    _assert_path_components_safe(transaction_parent, "uninstall")
    transaction = transaction_parent / (".dcc-mcp-gimp-uninstall-%s" % uuid.uuid4().hex)
    snapshot = transaction / "snapshot"
    quarantine = transaction / "quarantine"
    snapshot_target = snapshot / _PLUGIN_NAME
    snapshot_receipt = snapshot / "gimp.json"
    quarantine_target = quarantine / _PLUGIN_NAME
    quarantine_receipt = quarantine / "gimp.json"
    transaction_created = False
    preserve_transaction = False
    quarantine_expected_identities = None
    transaction_cleanup_identities = None
    try:
        transaction = _create_private_transaction(
            transaction_parent,
            transaction.name,
            "uninstall",
        )
        transaction_created = True
        transaction_identity = _BOUND_TRANSACTION_IDENTITIES.pop(str(transaction.absolute()), None)
        if transaction_identity is None:
            transaction_identity = _object_identity(transaction, "uninstall")
        if _object_identity(transaction, "uninstall")[:2] != tuple(transaction_identity)[:2]:
            raise InstallFailure(
                EXIT_PREFLIGHT, "uninstall", "Managed transaction changed identity"
            )
        snapshot.mkdir()
        quarantine.mkdir()
        if _object_identity(transaction, "uninstall")[:2] != tuple(transaction_identity)[:2]:
            raise InstallFailure(
                EXIT_PREFLIGHT, "uninstall", "Managed transaction changed identity"
            )
        if target_tree_identities is not None:
            quarantine_expected_identities = {"": _object_identity(quarantine, "uninstall")}
            for relative, identity in target_tree_identities.items():
                key = _PLUGIN_NAME if not relative else "%s/%s" % (_PLUGIN_NAME, relative)
                quarantine_expected_identities[key] = identity
        snapshot_manifest = _copy_validated_recovery(
            target, snapshot_target, destination, root_identity
        )
        _write_bytes_no_follow(
            snapshot_receipt,
            owned_snapshot.receipt_bytes,
            owned_snapshot.receipt_mode,
        )
        if (
            _recovery_manifest(snapshot_target) != snapshot_manifest
            or _read_bytes_no_follow(snapshot_receipt, max_bytes=_MAX_RECEIPT_BYTES)
            != owned_snapshot.receipt_bytes
            or _path_mode(snapshot_receipt) != owned_snapshot.receipt_mode
        ):
            raise InstallFailure(EXIT_INSTALL, "uninstall", "Uninstall recovery validation failed")
        _replace_path_owned(
            destination,
            root_identity,
            target,
            quarantine_target,
            expected_file_identities=planned_file_identities,
        )
        if planned_file_identities:
            try:
                _assert_owned_file_identities(
                    quarantine_target, planned_file_identities, "uninstall"
                )
            except InstallFailure:
                # Do not recursively delete a quarantine whose child identity
                # changed after validation; it may now contain operator-owned
                # bytes. Leave the private transaction for manual recovery.
                preserve_transaction = True
                raise
        _assert_receipt_path_safe(receipt_path)
        _replace_receipt_owned(receipt_path, quarantine_receipt, destination, root_identity)
        if quarantine_expected_identities is not None:
            quarantine_expected_identities["gimp.json"] = _receipt_file_identity(
                quarantine_receipt, "uninstall"
            )
        # Snapshot the complete transaction ownership before quarantine is
        # removed.  Final cleanup must only remove these known entries; any
        # operator file injected after quarantine cleanup is left in place and
        # the transaction is preserved for manual recovery.
        transaction_expected = _private_tree_identities(transaction, "uninstall")
        transaction_cleanup_identities = {
            relative: identity
            for relative, identity in transaction_expected.items()
            if not relative.startswith("quarantine/")
            and relative not in {"quarantine", "quarantine/dcc_mcp_gimp", "quarantine/gimp.json"}
        }
        _assert_physical_root(destination, root_identity)
        try:
            removed = _cleanup_private_tree(
                quarantine,
                expected_identities=quarantine_expected_identities,
            )
        except BaseException:
            # An unexpected quarantine entry must remain available for manual
            # recovery; never fall through to recursive transaction cleanup.
            preserve_transaction = True
            raise
        if not removed.get("success"):
            raise InstallFailure(
                EXIT_REQUIRES_RESTART if removed.get("requires_restart") else EXIT_INSTALL,
                "uninstall",
                "Uninstall cleanup failed; prior state will be restored",
            )
        cleanup = _cleanup_private_tree(
            transaction,
            expected_identities=transaction_cleanup_identities,
        )
        if not cleanup.get("success"):
            raise InstallFailure(EXIT_INSTALL, "uninstall", "Uninstall snapshot cleanup failed")
    except BaseException as exc:
        try:
            _assert_physical_root(destination, root_identity)
            _restore_owned_bytes(
                destination,
                target,
                receipt_path,
                receipt,
                owned_snapshot,
                root_identity,
            )
        except BaseException as restore_error:
            raise InstallFailure(
                EXIT_INSTALL, "uninstall", "Uninstall rollback could not restore prior state"
            ) from restore_error
        if (
            transaction_created
            and not preserve_transaction
            and transaction_cleanup_identities is not None
        ):
            try:
                _cleanup_private_tree(
                    transaction,
                    expected_identities=transaction_cleanup_identities,
                )
            except InstallFailure:
                pass
        elif transaction_created and transaction_cleanup_identities is None:
            # We never obtained a complete ownership snapshot, so recursive
            # cleanup would be unsafe if a foreign child was injected.
            preserve_transaction = True
        if isinstance(exc, InstallFailure):
            raise exc
        code = EXIT_REQUIRES_RESTART if isinstance(exc, PermissionError) else EXIT_INSTALL
        raise InstallFailure(code, "uninstall", "Uninstall rolled back") from exc
    report["status"] = "ok"
    report["steps"][-1] = {"id": "uninstall", "status": "ok"}
    return report, 0


__all__ = [
    "InstallTransaction",
    "_begin_replace_plugin",
    "_execute_uninstall",
    "_files_manifest",
    "_installation_state",
    "_manifest_digest",
    "_owned_root_manifest",
    "_plugin_version",
    "_read_receipt",
    "_receipt_path",
    "_replace_plugin",
    "_validate_owned_install",
    "_write_json_atomic",
    "install",
]
