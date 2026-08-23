"""Lifecycle service coordinating GIMP host and receipt-owned file operations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from dcc_mcp_core.install_lifecycle import wait_for_sidecar_ready

from .__version__ import __version__
from .install_contract import _PLUGIN_NAME, SCHEMA_VERSION
from .install_files import (
    _files_manifest,
    _installation_state,
    _manifest_digest,
    _plugin_version,
    _read_receipt,
    _receipt_path,
)
from .install_host import (
    _bootstrap_error_summary,
    _gimp_version,
    _python_import_check,
    _resolve_gimp,
    _resolve_python,
    _target_versions,
    default_plugin_dir,
)


def verify_install(destination: Path, python: Path, timeout: float) -> dict[str, Any]:
    target = destination / _PLUGIN_NAME
    receipt = _read_receipt()
    result: dict[str, Any] = {
        "directly_usable": False,
        "failure_stage": None,
        "failure_reason": None,
        "artifact": {"success": False},
        "import": {"success": False},
        "readiness": {"success": False},
    }
    if receipt is None or not target.is_dir():
        result.update(failure_stage="artifact", failure_reason="Plug-in or receipt is missing")
        return result
    if (
        Path(str(receipt.get("destination", ""))).resolve() != destination.resolve()
        or Path(str(receipt.get("plugin_path", ""))).resolve() != target.resolve()
    ):
        result.update(
            failure_stage="artifact",
            failure_reason="Receipt path does not match profile",
        )
        return result
    files = _files_manifest(target)
    actual_digest = _manifest_digest(files)
    expected_digest = receipt.get("package_digest")
    installed_version = _plugin_version(target / ("%s.py" % _PLUGIN_NAME))
    result["artifact"] = {
        "success": actual_digest == expected_digest and installed_version == __version__,
        "expected_sha256": expected_digest,
        "actual_sha256": actual_digest,
        "installed_adapter_version": installed_version,
        "expected_adapter_version": __version__,
    }
    if not result["artifact"]["success"]:
        result.update(
            failure_stage="artifact",
            failure_reason="Plug-in differs from receipt or wheel",
        )
        return result
    result["import"] = _python_import_check(python)
    if not result["import"].get("success"):
        result.update(failure_stage="import", failure_reason=result["import"].get("reason"))
        return result
    readiness = wait_for_sidecar_ready(
        dcc_type="gimp",
        timeout_secs=max(0.0, timeout),
        probe_tool="gimp_session__get_status",
    )
    result["readiness"] = readiness
    if not readiness.get("success"):
        result.update(
            failure_stage="readiness",
            failure_reason=readiness.get("message", "GIMP adapter is not ready"),
        )
        return result
    result["directly_usable"] = True
    return result


def _next_steps(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "restart-gimp",
            "description": (
                "Restart GIMP 3, then invoke the registered "
                "python-fu-dcc-mcp-gimp-bridge persistent procedure."
            ),
            "command": [report.get("dcc_path") or "gimp-3.0"],
            "why": (
                "GIMP discovers Python plug-ins during startup and must run the "
                "persistent procedure to open the authenticated bridge."
            ),
        },
        {
            "id": "start-adapter",
            "description": "Start the DCC-MCP GIMP adapter process.",
            "command": ["dcc-mcp-gimp"],
            "why": "The MCP service must connect to the authenticated GIMP bridge.",
        },
        {
            "id": "verify-ready",
            "description": "Verify installed artifacts, target import, and live readiness.",
            "command": ["dcc-mcp-gimp", "verify", "--json"],
            "why": "Installed files alone do not prove the adapter is usable.",
        },
    ]


def plan(
    verb: str,
    destination: Optional[Path],
    python_value: Optional[Path],
    dcc_path: Optional[Path],
) -> dict[str, Any]:
    root = (destination or default_plugin_dir()).expanduser().resolve()
    executable = _resolve_gimp(dcc_path)
    python = _resolve_python(python_value, executable)
    gimp_version = _gimp_version(executable)
    versions = _target_versions(python)
    state = _installation_state(root)
    installed_version = _plugin_version(
        root / _PLUGIN_NAME / ("%s.py" % _PLUGIN_NAME)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "dcc_type": "gimp",
        "verb": verb,
        "adapter_version": __version__,
        "core_version": versions.get("dcc-mcp-core"),
        "target_adapter_version": versions.get("dcc-mcp-gimp"),
        "installed_adapter_version": installed_version,
        "expected_adapter_version": __version__,
        "gimp_version": gimp_version,
        "dcc_path": str(executable),
        "python": str(python),
        "destination": str(root),
        "installation_state": state,
        "steps": [
            {"id": "preflight", "status": "ok", "gimp_version": gimp_version},
            {"id": "resolve-python", "status": "ok", "path": str(python)},
            {"id": verb, "status": "planned", "installation_state": state},
        ],
        "next_steps": [],
        "receipt_path": str(_receipt_path()),
        "verify": None,
    }


def doctor(destination: Optional[Path] = None) -> dict[str, object]:
    root = (destination or default_plugin_dir()).expanduser().resolve()
    script = root / _PLUGIN_NAME / ("%s.py" % _PLUGIN_NAME)
    roots = [
        str(Path(item).expanduser().resolve())
        for item in os.environ.get("DCC_MCP_GIMP_ALLOWED_ROOTS", "").split(os.pathsep)
        if item.strip()
    ]
    token_file = (
        Path(
            os.environ.get(
                "DCC_MCP_GIMP_BRIDGE_TOKEN_FILE",
                Path.home().joinpath(".dcc-mcp", "gimp-bridge-token"),
            )
        )
        .expanduser()
        .resolve()
    )
    installed_version = _plugin_version(script) if script.is_file() else None
    version_matches = installed_version == __version__
    return {
        "ready": script.is_file() and version_matches,
        "destination": str(root),
        "plugin_script": str(script),
        "plugin_script_exists": script.is_file(),
        "installed_adapter_version": installed_version,
        "expected_adapter_version": __version__,
        "version_matches": version_matches,
        "allowed_roots": roots,
        "file_access_enabled": bool(roots),
        "token_file": str(token_file),
        "token_file_exists": token_file.is_file(),
        "bootstrap_errors": _bootstrap_error_summary(),
        "restart_required_after_install": True,
    }
