"""Receipt-owned transactional file operations for the GIMP Install SOP."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
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
    root = (destination or default_plugin_dir()).expanduser().resolve()
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
    except (AttributeError, OSError, RuntimeError, ValueError):
        return False
    # GetFileAttributesW returns INVALID_FILE_ATTRIBUTES (-1) for a missing
    # path.  ctypes may expose that sentinel as either signed or unsigned.
    if int(attributes) in (-1, 0xFFFFFFFF):
        return False
    return bool(int(attributes) & 0x400)


def _file_record(path: Path, root: Path) -> dict[str, Any]:
    if not path.is_file() or _is_link(path):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file is missing or linked")
    contents = path.read_bytes()
    if not contents:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed GIMP file is empty")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size": len(contents),
    }


def _owned_root_manifest(root: Path) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    if not root.is_dir() or _is_link(root):
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", "Managed GIMP plug-in root is missing or linked"
        )
    directories: list[str] = []
    files: list[dict[str, Any]] = []
    links: list[str] = []
    for current, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
        current_path = Path(current)
        traversable: list[str] = []
        for name in sorted(dirnames):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if _is_link(path):
                links.append(relative)
            else:
                directories.append(relative)
                traversable.append(name)
        dirnames[:] = traversable
        for name in sorted(filenames):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if _is_link(path):
                links.append(relative)
            else:
                files.append(_file_record(path, root))
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
    try:
        before = os.lstat(str(path))
        if not stat.S_ISREG(before.st_mode) or _is_link(path):
            raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed file is missing or linked")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags)
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
            os.close(descriptor)
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Managed file is unavailable") from exc


def _write_bytes_no_follow(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    """Create a new regular file without following a swapped symlink."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags, 0o666)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        finally:
            os.close(descriptor)
        if mode is not None:
            _chmod_no_follow(path, mode)
    except FileExistsError as exc:
        raise InstallFailure(EXIT_INSTALL, "uninstall", "Managed file changed identity") from exc
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_INSTALL, "uninstall", "Managed file could not be restored"
        ) from exc


def _replace_path_owned(
    root: Path,
    expected_identity: Optional[tuple[int, int]],
    source: Path,
    destination: Path,
) -> None:
    """Rename only while the selected profile still owns the pathname."""
    _assert_physical_root(root, expected_identity)
    _replace_path(source, destination)
    # A concurrent junction/symlink swap must fail closed before the caller
    # performs its next operation or reports success.
    _assert_physical_root(root, expected_identity)


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
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data)
        if mode is not None:
            _chmod_no_follow(temporary, mode)
        _replace_path(temporary, path)
        if mode is not None:
            _chmod_no_follow(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _assert_receipt_path_safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _replace_path(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stage_plugin(root: Path) -> Path:
    try:
        source = _source_file()
    except OSError as exc:
        raise InstallFailure(EXIT_ACQUIRE, "acquire", str(exc)) from exc
    stage = root / (".%s.%s.stage" % (_PLUGIN_NAME, uuid.uuid4().hex))
    try:
        stage.mkdir(parents=True)
        script = stage / ("%s.py" % _PLUGIN_NAME)
        shutil.copy2(source, script)
        if os.name != "nt":
            script.chmod(0o755)
        return stage
    except OSError as exc:
        safe_remove_tree(stage)
        raise InstallFailure(EXIT_INSTALL, "install", "Plug-in staging failed: %s" % exc) from exc


def _cleanup_tree(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"success": True, "requires_restart": False}
    try:
        result = safe_remove_tree(path)
    except BaseException as exc:
        return {"success": False, "requires_restart": False, "message": exc.__class__.__name__}
    return result if isinstance(result, dict) else {"success": False, "requires_restart": False}


def _path_mode(path: Path) -> int:
    """Return a non-following mode on every supported Python, including 3.9."""
    return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)


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
        directories, files, links = _owned_root_manifest(root)
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


def _copy_validated_recovery(source: Path, recovery: Path) -> dict[str, Any]:
    """Create a separate recovery tree and prove that its bytes and modes match."""
    expected = _recovery_manifest(source)
    try:
        shutil.copytree(source, recovery, symlinks=True, copy_function=shutil.copy2)
    except OSError as exc:
        _cleanup_tree(recovery)
        raise InstallFailure(
            EXIT_INSTALL, "recovery", "Recovery snapshot could not be created"
        ) from exc
    if _recovery_manifest(recovery) != expected:
        _cleanup_tree(recovery)
        raise InstallFailure(EXIT_INSTALL, "recovery", "Recovery snapshot validation failed")
    return expected


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
    backup = root / (".%s.%s.backup" % (_PLUGIN_NAME, uuid.uuid4().hex))
    try:
        if target.exists():
            _replace_path_owned(root, root_identity, target, backup)
        _replace_path_owned(root, root_identity, stage, target)
        cleanup = _cleanup_tree(backup)
        if not cleanup.get("success"):
            raise InstallFailure(EXIT_INSTALL, "cleanup", "Legacy backup cleanup failed")
    except BaseException:
        if target.exists() and backup.exists():
            _cleanup_tree(target)
        if backup.exists():
            _replace_path(backup, target)
        _cleanup_tree(stage)
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
        resolved = root.resolve(strict=False)
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
    if len(directories) != len(set(directories)):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "receipt",
            "Receipt directory ownership contains duplicates",
        )
    if not isinstance(links, list) or not all(
        isinstance(value, str) and _valid_relative_path(value) for value in links
    ) or len(links) != len(set(links)) or links:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt link ownership is invalid")
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
    closed: bool = False

    def rollback(self) -> bool:
        if self.closed:
            return self.previous_moved
        failed = self.root / (".%s.%s.failed" % (_PLUGIN_NAME, uuid.uuid4().hex))
        try:
            _assert_physical_root(self.root, self.root_identity)
            _assert_receipt_path_safe(self.receipt_path)
            if self.replacement_moved and (self.target.exists() or self.target.is_symlink()):
                _replace_path_owned(self.root, self.root_identity, self.target, failed)
            if self.previous_moved:
                restore_source = None
                for candidate in (self.recovery, self.backup):
                    if candidate is None or not candidate.is_dir() or _is_link(candidate):
                        continue
                    if self.previous_manifest is not None:
                        try:
                            if _recovery_manifest(candidate) != self.previous_manifest:
                                continue
                        except (InstallFailure, OSError):
                            continue
                    restore_source = candidate
                    break
                if restore_source is None:
                    raise InstallFailure(
                        EXIT_INSTALL, "recovery", "No validated prior GIMP recovery remains"
                    )
                _replace_path_owned(self.root, self.root_identity, restore_source, self.target)
            if self.old_receipt is None:
                self.receipt_path.unlink(missing_ok=True)
            else:
                _write_bytes_atomic(self.receipt_path, self.old_receipt, self.old_receipt_mode)
            if self.previous_moved and self.previous_manifest is not None:
                if _recovery_manifest(self.target) != self.previous_manifest:
                    raise InstallFailure(
                        EXIT_INSTALL, "recovery", "Restored GIMP recovery validation failed"
                    )
            _cleanup_tree(failed)
            _cleanup_tree(self.backup)
            if self.recovery is not None:
                _cleanup_tree(self.recovery)
            self.closed = True
            return self.previous_moved
        except BaseException as exc:
            raise InstallFailure(
                EXIT_INSTALL, "install", "Prior GIMP install could not be restored"
            ) from exc

    def commit(self) -> None:
        if self.closed:
            return
        _assert_physical_root(self.root, self.root_identity)
        if self.previous_moved:
            self.recovery = self.root / (".%s.%s.recovery" % (_PLUGIN_NAME, uuid.uuid4().hex))
            try:
                self.recovery_manifest = _copy_validated_recovery(self.backup, self.recovery)
            except InstallFailure:
                self.rollback()
                raise
        _assert_physical_root(self.root, self.root_identity)
        cleanup = _cleanup_tree(self.backup)
        if not cleanup.get("success"):
            self.rollback()
            code = EXIT_REQUIRES_RESTART if cleanup.get("requires_restart") else EXIT_INSTALL
            raise InstallFailure(code, "cleanup", "Verified install backup cleanup failed")
        self.closed = True
        if self.recovery is not None:
            _assert_physical_root(self.root, self.root_identity)
            _cleanup_tree(self.recovery)


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
    try:
        _assert_physical_root(root, root_identity)
    except BaseException:
        _cleanup_tree(stage)
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
    )
    try:
        if target.exists():
            _assert_physical_root(root, root_identity)
            _replace_path_owned(root, root_identity, target, backup)
            transaction.previous_moved = True
        _assert_physical_root(root, root_identity)
        _replace_path_owned(root, root_identity, stage, target)
        transaction.replacement_moved = True
        _assert_physical_root(root, root_identity)
        _write_json_atomic(receipt_path, _receipt_payload(root, target, report))
        receipt = _read_receipt()
        if receipt is None:
            raise InstallFailure(EXIT_INSTALL, "receipt", "Install receipt commit failed")
        _validate_owned_install(root, target, receipt_path, receipt)
        return transaction
    except BaseException as exc:
        transaction.rollback()
        _cleanup_tree(stage)
        if isinstance(exc, InstallFailure):
            raise
        code = EXIT_REQUIRES_RESTART if isinstance(exc, PermissionError) else EXIT_INSTALL
        raise InstallFailure(code, "install", "Install rolled back: %s" % exc) from exc
    finally:
        _cleanup_tree(stage)


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
        data = path.read_bytes()
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
        removed = _cleanup_tree(target)
        if not removed.get("success"):
            raise InstallFailure(EXIT_INSTALL, "uninstall", "Managed plug-in could not be restored")
    _assert_physical_root(destination, expected_identity)
    if target.exists() or target.is_symlink() or _is_link(target):
        raise InstallFailure(EXIT_INSTALL, "uninstall", "Managed plug-in path is linked")
    target.mkdir(parents=True, exist_ok=True)
    for relative in sorted(
        snapshot.directory_modes, key=lambda value: (len(Path(value).parts), value)
    ):
        _assert_physical_root(destination, expected_identity)
        (target / relative).mkdir()
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
        (target / relative).chmod(snapshot.directory_modes[relative])
    _assert_physical_root(destination, expected_identity)
    _chmod_no_follow(target, snapshot.root_mode)
    _write_bytes_atomic(receipt_path, snapshot.receipt_bytes, snapshot.receipt_mode)
    _validate_owned_install(destination, target, receipt_path, receipt)


def _execute_uninstall(report: dict[str, Any]) -> tuple[dict[str, Any], int]:
    destination = Path(report["destination"])
    root_identity = _assert_physical_root(destination)
    target = destination / _PLUGIN_NAME
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
    _validate_owned_install(destination, target, receipt_path, receipt)
    lock_state = inspect_install_root(target)
    if lock_state.get("requires_restart"):
        raise InstallFailure(
            EXIT_REQUIRES_RESTART,
            "uninstall",
            str(lock_state.get("recommended_next_action", "GIMP restart required")),
        )
    _assert_physical_root(destination, root_identity)
    owned_snapshot = _capture_owned_bytes(target, receipt_path, receipt)
    _assert_physical_root(destination, root_identity)
    transaction = destination / (".dcc-mcp-gimp-uninstall-%s" % uuid.uuid4().hex)
    snapshot = transaction / "snapshot"
    quarantine = transaction / "quarantine"
    snapshot_target = snapshot / _PLUGIN_NAME
    snapshot_receipt = snapshot / "gimp.json"
    quarantine_target = quarantine / _PLUGIN_NAME
    quarantine_receipt = quarantine / "gimp.json"
    try:
        snapshot.mkdir(parents=True)
        snapshot_manifest = _copy_validated_recovery(target, snapshot_target)
        snapshot_receipt.write_bytes(owned_snapshot.receipt_bytes)
        snapshot_receipt.chmod(owned_snapshot.receipt_mode)
        if (
            _recovery_manifest(snapshot_target) != snapshot_manifest
            or snapshot_receipt.read_bytes() != owned_snapshot.receipt_bytes
            or _path_mode(snapshot_receipt) != owned_snapshot.receipt_mode
        ):
            raise InstallFailure(EXIT_INSTALL, "uninstall", "Uninstall recovery validation failed")
        _replace_path_owned(destination, root_identity, target, quarantine_target)
        _assert_receipt_path_safe(receipt_path)
        _replace_path(receipt_path, quarantine_receipt)
        _assert_physical_root(destination, root_identity)
        removed = _cleanup_tree(quarantine)
        if not removed.get("success"):
            raise InstallFailure(
                EXIT_REQUIRES_RESTART if removed.get("requires_restart") else EXIT_INSTALL,
                "uninstall",
                "Uninstall cleanup failed; prior state will be restored",
            )
        cleanup = _cleanup_tree(transaction)
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
        _cleanup_tree(transaction)
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
