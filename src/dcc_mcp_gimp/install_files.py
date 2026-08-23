"""Receipt-owned staged file operations for the GIMP Install SOP."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
)
from .install_host import default_plugin_dir


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
            isinstance(target, ast.Name) and target.id == "VERSION"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    return None


def _files_manifest(root: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        contents = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "size": len(contents),
            }
        )
    return files


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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "receipt",
            "Install receipt is unreadable: %s" % path,
        ) from exc
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("dcc_type") != "gimp":
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Install receipt is unsupported: %s" % path)
    return payload


def _replace_path(source: Path, destination: Path) -> None:
    """Rename a staged path, tolerating short-lived Windows filesystem handles."""
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
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
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
        raise InstallFailure(
            EXIT_INSTALL,
            "install",
            "Plug-in staging failed: %s" % exc,
        ) from exc


def _replace_plugin(root: Path, report: Optional[dict[str, Any]] = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / _PLUGIN_NAME
    lock_state = inspect_install_root(target)
    if lock_state.get("requires_restart"):
        raise InstallFailure(
            EXIT_REQUIRES_RESTART,
            "install",
            str(lock_state.get("recommended_next_action", "GIMP restart required")),
        )
    stage = _stage_plugin(root)
    backup = root / (".%s.%s.backup" % (_PLUGIN_NAME, uuid.uuid4().hex))
    receipt_path = _receipt_path()
    old_receipt = receipt_path.read_bytes() if receipt_path.is_file() else None
    previous_moved = False
    replacement_moved = False
    try:
        if target.exists():
            _replace_path(target, backup)
            previous_moved = True
        _replace_path(stage, target)
        replacement_moved = True
        if report is not None:
            files = _files_manifest(target)
            _write_json_atomic(
                receipt_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "dcc_type": "gimp",
                    "adapter_version": __version__,
                    "core_version": report["core_version"],
                    "gimp_version": report["gimp_version"],
                    "dcc_path": report["dcc_path"],
                    "python": report["python"],
                    "destination": str(root),
                    "plugin_path": str(target),
                    "host_paths_touched": [str(target)],
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "package_digest": _manifest_digest(files),
                    "files": files,
                },
            )
    except BaseException as exc:
        failed = root / (".%s.%s.failed" % (_PLUGIN_NAME, uuid.uuid4().hex))
        if replacement_moved and target.exists():
            _replace_path(target, failed)
        if previous_moved and backup.exists():
            _replace_path(backup, target)
        if old_receipt is None:
            receipt_path.unlink(missing_ok=True)
        else:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_bytes(old_receipt)
        safe_remove_tree(failed)
        safe_remove_tree(stage)
        if isinstance(exc, InstallFailure):
            raise
        code = EXIT_REQUIRES_RESTART if isinstance(exc, PermissionError) else EXIT_INSTALL
        raise InstallFailure(code, "install", "Install rolled back: %s" % exc) from exc
    finally:
        if stage.exists():
            safe_remove_tree(stage)
    if backup.exists():
        removed = safe_remove_tree(backup)
        if not removed.get("success"):
            code = EXIT_REQUIRES_RESTART if removed.get("requires_restart") else EXIT_INSTALL
            raise InstallFailure(
                code,
                "cleanup",
                str(removed.get("message", "Backup cleanup failed")),
            )
    return target


def _installation_state(destination: Path) -> str:
    script = destination / _PLUGIN_NAME / ("%s.py" % _PLUGIN_NAME)
    if not script.exists():
        return "partial" if _read_receipt() is not None else "fresh"
    receipt = _read_receipt()
    if receipt is None:
        return "partial"
    target = destination / _PLUGIN_NAME
    try:
        if (
            Path(str(receipt.get("destination", ""))).resolve() != destination.resolve()
            or Path(str(receipt.get("plugin_path", ""))).resolve() != target.resolve()
        ):
            return "partial"
        digest_matches = _manifest_digest(_files_manifest(target)) == receipt.get(
            "package_digest"
        )
    except OSError:
        return "partial"
    if not digest_matches:
        return "repair"
    installed_version = _plugin_version(script)
    if installed_version is None:
        return "partial"
    return "current" if installed_version == __version__ else "upgrade"


def _execute_uninstall(report: dict[str, Any]) -> tuple[dict[str, Any], int]:
    destination = Path(report["destination"])
    target = destination / _PLUGIN_NAME
    receipt_path = _receipt_path()
    receipt = _read_receipt()
    if not target.exists() and receipt is None:
        report["status"] = "ok"
        report["steps"][-1] = {"id": "uninstall", "status": "already-absent"}
        return report, 0
    if receipt is None:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "receipt",
            "Refusing to remove an unreceipted GIMP plug-in; run install --yes to repair it",
        )
    if (
        Path(str(receipt.get("destination", ""))).resolve() != destination.resolve()
        or Path(str(receipt.get("plugin_path", ""))).resolve() != target.resolve()
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "Receipt path does not match profile")
    lock_state = inspect_install_root(target)
    if lock_state.get("requires_restart"):
        raise InstallFailure(
            EXIT_REQUIRES_RESTART,
            "uninstall",
            str(lock_state.get("recommended_next_action", "GIMP restart required")),
        )
    backup = destination / (".%s.%s.uninstall" % (_PLUGIN_NAME, uuid.uuid4().hex))
    try:
        if target.exists():
            _replace_path(target, backup)
        receipt_path.unlink()
    except OSError as exc:
        if backup.exists():
            _replace_path(backup, target)
        code = EXIT_REQUIRES_RESTART if isinstance(exc, PermissionError) else EXIT_INSTALL
        raise InstallFailure(code, "uninstall", "Uninstall rolled back: %s" % exc) from exc
    removed = safe_remove_tree(backup)
    if not removed.get("success"):
        code = EXIT_REQUIRES_RESTART if removed.get("requires_restart") else EXIT_INSTALL
        raise InstallFailure(code, "uninstall", str(removed.get("message", "Cleanup failed")))
    report["status"] = "ok"
    report["steps"][-1] = {"id": "uninstall", "status": "ok"}
    return report, 0
