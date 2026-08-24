"""Receipt-owned transactional file operations for the GIMP Install SOP."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

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
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


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


def _read_receipt() -> Optional[dict[str, Any]]:
    path = _receipt_path()
    if not path.is_file():
        return None
    try:
        if _is_link(path) or not 0 < path.stat().st_size <= _MAX_RECEIPT_BYTES:
            raise ValueError("receipt is empty, linked, or unbounded")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
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


def _replace_plugin(root: Path) -> Path:
    """Replace only the legacy copy; the lifecycle uses a receipted transaction."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / _PLUGIN_NAME
    stage = _stage_plugin(root)
    backup = root / (".%s.%s.backup" % (_PLUGIN_NAME, uuid.uuid4().hex))
    try:
        if target.exists():
            _replace_path(target, backup)
        _replace_path(stage, target)
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
    if (
        not isinstance(directories, list)
        or len(directories) != len(set(directories))
        or not all(_valid_relative_path(value) for value in directories)
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt directory ownership is invalid")
    if not isinstance(links, list) or len(links) != len(set(links)) or links:
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
    previous_moved: bool
    replacement_moved: bool = False
    closed: bool = False

    def rollback(self) -> bool:
        if self.closed:
            return self.previous_moved
        failed = self.root / (".%s.%s.failed" % (_PLUGIN_NAME, uuid.uuid4().hex))
        try:
            if self.replacement_moved and self.target.exists():
                _replace_path(self.target, failed)
            if self.previous_moved and self.backup.exists():
                _replace_path(self.backup, self.target)
            if self.old_receipt is None:
                self.receipt_path.unlink(missing_ok=True)
            else:
                self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
                self.receipt_path.write_bytes(self.old_receipt)
            _cleanup_tree(failed)
            _cleanup_tree(self.backup)
            self.closed = True
            return self.previous_moved
        except BaseException as exc:
            raise InstallFailure(
                EXIT_INSTALL, "install", "Prior GIMP install could not be restored"
            ) from exc

    def commit(self) -> None:
        if self.closed:
            return
        cleanup = _cleanup_tree(self.backup)
        if not cleanup.get("success"):
            self.rollback()
            code = EXIT_REQUIRES_RESTART if cleanup.get("requires_restart") else EXIT_INSTALL
            raise InstallFailure(code, "cleanup", "Verified install backup cleanup failed")
        self.closed = True


def _begin_replace_plugin(root: Path, report: Mapping[str, Any]) -> InstallTransaction:
    root.mkdir(parents=True, exist_ok=True)
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
    backup = root / (".%s.%s.backup" % (_PLUGIN_NAME, uuid.uuid4().hex))
    receipt_path = _receipt_path()
    old_receipt = receipt_path.read_bytes() if receipt_path.is_file() else None
    transaction = InstallTransaction(root, target, receipt_path, backup, old_receipt, False)
    try:
        if target.exists():
            _replace_path(target, backup)
            transaction.previous_moved = True
        _replace_path(stage, target)
        transaction.replacement_moved = True
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


def _capture_owned_bytes(
    target: Path, receipt_path: Path, receipt: Mapping[str, Any]
) -> tuple[list[str], dict[str, bytes], bytes]:
    ownership = receipt["ownership"]
    directories = list(ownership["directories"])
    files: dict[str, bytes] = {}
    total = receipt_path.stat().st_size
    for record in ownership["files"]:
        relative = str(record["path"])
        data = (target / relative).read_bytes()
        total += len(data)
        files[relative] = data
    receipt_bytes = receipt_path.read_bytes()
    if total > _MAX_SNAPSHOT_BYTES:
        raise InstallFailure(
            EXIT_INSTALL, "uninstall", "Managed install is too large for bounded rollback"
        )
    return directories, files, receipt_bytes


def _restore_owned_bytes(
    destination: Path,
    target: Path,
    receipt_path: Path,
    receipt: Mapping[str, Any],
    directories: Sequence[str],
    files: Mapping[str, bytes],
    receipt_bytes: bytes,
) -> None:
    if target.exists() or target.is_symlink():
        _cleanup_tree(target)
    target.mkdir(parents=True, exist_ok=True)
    for relative in sorted(directories, key=lambda value: (len(Path(value).parts), value)):
        (target / relative).mkdir()
    for relative, data in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt_bytes)
    _validate_owned_install(destination, target, receipt_path, receipt)


def _execute_uninstall(report: dict[str, Any]) -> tuple[dict[str, Any], int]:
    destination = Path(report["destination"])
    target = destination / _PLUGIN_NAME
    receipt_path = _receipt_path()
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
    directories, files, receipt_bytes = _capture_owned_bytes(target, receipt_path, receipt)
    transaction = destination / (".dcc-mcp-gimp-uninstall-%s" % uuid.uuid4().hex)
    snapshot = transaction / "snapshot"
    quarantine = transaction / "quarantine"
    snapshot_target = snapshot / _PLUGIN_NAME
    snapshot_receipt = snapshot / "gimp.json"
    quarantine_target = quarantine / _PLUGIN_NAME
    quarantine_receipt = quarantine / "gimp.json"
    try:
        snapshot.mkdir(parents=True)
        shutil.copytree(target, snapshot_target)
        shutil.copy2(receipt_path, snapshot_receipt)
        _replace_path(target, quarantine_target)
        _replace_path(receipt_path, quarantine_receipt)
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
            _restore_owned_bytes(
                destination,
                target,
                receipt_path,
                receipt,
                directories,
                files,
                receipt_bytes,
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
