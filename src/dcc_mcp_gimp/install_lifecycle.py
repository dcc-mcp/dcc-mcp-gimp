"""Lifecycle service coordinating GIMP host and receipt-owned file operations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional

from dcc_mcp_core.install_lifecycle import query_runtime_state, wait_for_sidecar_ready

from .__version__ import __version__
from .install_contract import (
    _PLUGIN_NAME,
    EXIT_PREFLIGHT,
    SCHEMA_VERSION,
    InstallFailure,
    _version_tuple,
)
from .install_files import (
    _assert_owned_file_identities,
    _assert_physical_root,
    _assert_profile_writable,
    _assert_target_identity,
    _installation_state,
    _is_link,
    _owned_file_identities,
    _plugin_version,
    _read_receipt,
    _read_receipt_evidence,
    _receipt_path,
    _receipt_tree_identities,
    _target_identity,
    _validate_owned_install,
)
from .install_host import (
    _bootstrap_error_summary,
    _executable_identity,
    _gimp_version,
    _process_executable_path,
    _process_start_identity,
    _python_import_check,
    _resolve_gimp,
    _resolve_python,
    _target_versions,
    default_plugin_dir,
)

_READINESS_TOOL = "gimp_session__get_status"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except (OSError, RuntimeError, ValueError):
        return False


def _probe_context(readiness: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    probe = readiness.get("probe")
    result = probe.get("result") if isinstance(probe, dict) else None
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if structured is None:
        structured = result.get("structured_content")
    if not isinstance(structured, dict) or structured.get("success") is not True:
        return None
    context = structured.get("context")
    return context if isinstance(context, dict) else None


def _entry_adapter_version(entry: Mapping[str, Any]) -> object:
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    return entry.get("adapter_version") or metadata.get("adapter_version")


def _runtime_identity_failure(
    readiness: Mapping[str, Any],
    *,
    target: Path,
    dcc_path: Path,
    gimp_version: str,
    instance_id: Optional[str],
    host_pid: Optional[int],
) -> Optional[str]:
    entry = readiness.get("entry")
    if not isinstance(entry, dict):
        return "Ready response did not identify one GIMP adapter instance"
    actual_instance = entry.get("instance_id")
    if not isinstance(actual_instance, str) or not actual_instance.strip():
        return "Ready response omitted the GIMP adapter instance id"
    if instance_id is not None and actual_instance != instance_id:
        return "Ready response belongs to a different GIMP adapter instance"
    entry_adapter = _entry_adapter_version(entry)
    if entry_adapter is not None and entry_adapter != __version__:
        return "Ready response adapter version differs from this installation"

    context = _probe_context(readiness)
    if context is None:
        return "Ready response omitted the authenticated GIMP structured payload"
    if context.get("ready") is not True or context.get("authenticated") is not True:
        return "GIMP bridge payload is not ready and authenticated"
    if context.get("adapter_version") != __version__:
        return "GIMP bridge adapter version differs from this installation"
    if (
        _version_tuple(context.get("gimp_version")) is None
        or context.get("gimp_version") != gimp_version
    ):
        return "GIMP bridge version differs from the selected installation"
    try:
        actual_host_pid = int(context.get("gimp_pid"))
        plugin_pid = int(context.get("plugin_pid"))
        bridge_port = int(context.get("bridge_port"))
    except (TypeError, ValueError):
        return "GIMP bridge payload omitted its bounded process or endpoint identity"
    if actual_host_pid <= 0 or plugin_pid <= 0 or bridge_port not in range(1, 65536):
        return "GIMP bridge payload contains an invalid process or endpoint identity"
    if host_pid is not None and actual_host_pid != host_pid:
        return "GIMP bridge belongs to a different GIMP host PID"
    expected_host = os.environ.get("DCC_MCP_GIMP_BRIDGE_HOST", "127.0.0.1")
    try:
        expected_port = int(os.environ.get("DCC_MCP_GIMP_BRIDGE_PORT", "3848"))
    except ValueError:
        return "Configured GIMP bridge port is invalid"
    if context.get("bridge_host") != expected_host or bridge_port != expected_port:
        return "GIMP bridge endpoint differs from the configured adapter endpoint"
    module_value = context.get("plugin_module_path")
    if not isinstance(module_value, str) or not module_value:
        return "GIMP bridge payload omitted the installed plug-in module path"
    try:
        if not _same_path(Path(module_value), target / ("%s.py" % _PLUGIN_NAME)):
            return "GIMP bridge module does not belong to the receipted installation"
    except (OSError, RuntimeError, ValueError):
        return "GIMP bridge module path is invalid"
    before = _process_start_identity(actual_host_pid)
    process_path = _process_executable_path(actual_host_pid)
    after = _process_start_identity(actual_host_pid)
    if process_path is None or not _same_path(process_path, dcc_path):
        return "Ready GIMP process path differs from the selected executable"
    if before is None or before != after:
        return "Ready GIMP process start identity is unavailable or changed"
    captured_start = context.get("gimp_start_identity")
    if not isinstance(captured_start, str) or captured_start != before:
        return "GIMP plug-in captured start identity differs from the selected process"
    return None


def verify_install(
    destination: Path,
    python: Path,
    timeout: float,
    dcc_path: Path,
    gimp_version: str,
    *,
    instance_id: Optional[str] = None,
    host_pid: Optional[int] = None,
    expected_target_identity: Optional[tuple[int, int]] = None,
    expected_file_identities: Optional[Mapping[str, tuple[int, ...]]] = None,
    expected_python_identity: Optional[tuple[int, int, int, int]] = None,
) -> dict[str, Any]:
    target = destination / _PLUGIN_NAME
    result: dict[str, Any] = {
        "directly_usable": False,
        "failure_stage": None,
        "failure_reason": None,
        "artifact": {"success": False},
        "import": {"success": False},
        "readiness": {"success": False},
    }
    try:
        receipt = _read_receipt()
    except InstallFailure as exc:
        result.update(failure_stage=exc.stage, failure_reason=str(exc))
        return result
    if receipt is None or not target.is_dir():
        result.update(failure_stage="artifact", failure_reason="Plug-in or receipt is missing")
        return result
    try:
        target_identity = (
            tuple(expected_target_identity)
            if expected_target_identity is not None
            else _target_identity(target)
        )
        owned_file_identities = (
            {relative: tuple(identity) for relative, identity in expected_file_identities.items()}
            if expected_file_identities is not None
            else _owned_file_identities(target, receipt)
        )
    except (InstallFailure, TypeError, ValueError) as exc:
        if isinstance(exc, InstallFailure):
            result.update(failure_stage=exc.stage, failure_reason=str(exc))
        else:
            result.update(
                failure_stage="artifact", failure_reason="Install plan identity is invalid"
            )
        return result
    try:
        _validate_owned_install(destination, target, _receipt_path(), receipt)
    except InstallFailure as exc:
        result.update(failure_stage="artifact", failure_reason=str(exc))
        return result
    try:
        _assert_target_identity(target, target_identity)
        _assert_owned_file_identities(target, owned_file_identities, "artifact")
    except InstallFailure as exc:
        result.update(failure_stage="artifact", failure_reason=str(exc))
        return result
    installed_version = _plugin_version(target / ("%s.py" % _PLUGIN_NAME))
    result["artifact"] = {
        "success": installed_version == __version__,
        "installed_adapter_version": installed_version,
        "expected_adapter_version": __version__,
    }
    if not result["artifact"]["success"]:
        result.update(
            failure_stage="artifact", failure_reason="Plug-in version differs from the wheel"
        )
        return result
    result["import"] = _python_import_check(python, expected_identity=expected_python_identity)
    if not result["import"].get("success"):
        result.update(failure_stage="import", failure_reason=result["import"].get("reason"))
        return result
    expected_origins = {
        "adapter_module_path": result["import"].get("adapter_module_path"),
        "core_module_path": result["import"].get("core_module_path"),
    }
    if any(receipt.get(key) != value for key, value in expected_origins.items()):
        result.update(
            failure_stage="import",
            failure_reason="Target interpreter module origins differ from the install receipt",
        )
        return result
    try:
        _assert_target_identity(target, target_identity)
        _assert_owned_file_identities(target, owned_file_identities, "artifact")
    except InstallFailure as exc:
        result.update(failure_stage="artifact", failure_reason=str(exc))
        return result

    errors = _bootstrap_error_summary()
    latest = errors.get("latest")
    installed_at = receipt.get("installed_at")
    if (
        isinstance(latest, dict)
        and isinstance(latest.get("timestamp"), str)
        and isinstance(installed_at, str)
        and latest["timestamp"] >= installed_at
    ):
        result.update(
            failure_stage="bootstrap",
            failure_reason="GIMP recorded a plug-in bootstrap failure; inspect the doctor report",
        )
        return result
    try:
        runtime = query_runtime_state(
            os.environ.get("DCC_MCP_REGISTRY_DIR"),
            dcc_type="gimp",
            include_dead=False,
        )
    except Exception:
        result.update(
            failure_stage="readiness",
            failure_reason="GIMP runtime state is unavailable",
        )
        return result
    if not isinstance(runtime, Mapping):
        result.update(
            failure_stage="readiness",
            failure_reason="GIMP runtime state is unavailable",
        )
        return result
    raw_entries = runtime.get("entries", [])
    if not isinstance(raw_entries, list):
        result.update(
            failure_stage="readiness",
            failure_reason="GIMP runtime state entries are invalid",
        )
        return result
    entries = [entry for entry in raw_entries if isinstance(entry, dict) and entry.get("mcp_url")]
    if instance_id is not None:
        entries = [entry for entry in entries if entry.get("instance_id") == instance_id]
    if len(entries) != 1:
        reason = (
            "No live GIMP adapter is registered"
            if not entries
            else "Multiple live GIMP adapters are registered"
        )
        result.update(failure_stage="readiness", failure_reason=reason)
        return result
    selected_instance = entries[0].get("instance_id")
    if not isinstance(selected_instance, str) or not selected_instance.strip():
        result.update(
            failure_stage="readiness_identity",
            failure_reason="Selected GIMP registry entry omitted its instance id",
        )
        return result
    try:
        readiness = wait_for_sidecar_ready(
            os.environ.get("DCC_MCP_REGISTRY_DIR"),
            dcc_type="gimp",
            instance_id=selected_instance,
            timeout_secs=max(0.05, timeout),
            poll_interval_secs=min(max(0.05, timeout), 0.1),
            probe_tool=_READINESS_TOOL,
            probe_timeout_secs=max(0.05, timeout),
        )
    except Exception:
        readiness = {
            "success": False,
            "message": "GIMP sidecar readiness probe failed",
        }
    result["readiness"] = readiness
    if not isinstance(readiness, Mapping):
        result.update(
            failure_stage="readiness",
            failure_reason="GIMP sidecar readiness payload is invalid",
        )
        return result
    if readiness.get("success") is not True:
        result.update(
            failure_stage="readiness",
            failure_reason=(
                readiness.get("message")
                if isinstance(readiness.get("message"), str)
                else "Typed GIMP readiness probe failed"
            ),
        )
        return result
    failure = _runtime_identity_failure(
        readiness,
        target=target,
        dcc_path=dcc_path,
        gimp_version=gimp_version,
        instance_id=selected_instance,
        host_pid=host_pid,
    )
    if failure is not None:
        result.update(failure_stage="readiness_identity", failure_reason=failure)
        return result
    try:
        _assert_target_identity(target, target_identity)
        _assert_owned_file_identities(target, owned_file_identities, "artifact")
    except InstallFailure as exc:
        result.update(failure_stage="artifact", failure_reason=str(exc))
        return result
    result["directly_usable"] = True
    return result


def _next_steps(report: dict[str, Any]) -> list[dict[str, Any]]:
    launch = [str(report["dcc_path"])]
    if str(report["dcc_path"]).lower().endswith(".appimage"):
        launch.append("--appimage-extract-and-run")
    launch.append("--new-instance")
    selected_python = str(report["python"])
    profile_directory = str(Path(report["destination"]).parent)
    profile_environment = {"GIMP3_DIRECTORY": profile_directory}
    start_adapter = [selected_python, "-m", "dcc_mcp_gimp"]
    verify = [
        selected_python,
        "-m",
        "dcc_mcp_gimp",
        "verify",
        "--json",
        "--dcc-path",
        str(report["dcc_path"]),
        "--python",
        str(report["python"]),
        "--destination",
        str(report["destination"]),
    ]
    if report.get("instance_id"):
        verify.extend(("--instance-id", str(report["instance_id"])))
    if report.get("host_pid"):
        verify.extend(("--host-pid", str(report["host_pid"])))
    return [
        {
            "id": "start-selected-gimp",
            "description": (
                "Start a new selected GIMP instance; its no-argument persistent bridge "
                "procedure starts automatically."
            ),
            "command": launch,
            "environment": profile_environment,
            "profile_selector": str(report["destination"]),
            "why": (
                "A new process discovers the receipted plug-in without reusing a foreign instance."
            ),
        },
        {
            "id": "start-selected-adapter",
            "description": "Start the adapter with the exact selected Python interpreter.",
            "command": start_adapter,
            "environment": profile_environment,
            "profile_selector": str(report["destination"]),
            "why": "The adapter must register before the exact readiness probe can run.",
        },
        {
            "id": "verify-selected-gimp",
            "description": "Verify the exact selected GIMP instance and authenticated bridge.",
            "command": verify,
            "environment": profile_environment,
            "profile_selector": str(report["destination"]),
            "why": (
                "A typed PID-, origin-, endpoint-, and instance-bound probe is required before use."
            ),
        },
    ]


def plan(
    verb: str,
    destination: Optional[Path],
    python_value: Optional[Path],
    dcc_path: Optional[Path],
    *,
    instance_id: Optional[str] = None,
    host_pid: Optional[int] = None,
) -> dict[str, Any]:
    try:
        # Preserve the operator-selected path identity.  Resolving first would
        # turn a symlink/junction destination into its target and silently bind
        # the receipt to an external profile.
        root = (destination or default_plugin_dir()).expanduser().absolute()
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "profile",
            "GIMP profile path could not be resolved",
        ) from exc
    _assert_physical_root(root)
    _assert_profile_writable(root)
    executable = _resolve_gimp(dcc_path)
    python = _resolve_python(python_value, executable)
    gimp_identity = _executable_identity(executable)
    python_identity = _executable_identity(python)
    gimp_version = _gimp_version(executable, expected_identity=gimp_identity)
    versions = _target_versions(python, expected_identity=python_identity)
    receipt, receipt_identity, receipt_expected_absent = _read_receipt_evidence()
    if receipt is not None:
        owner = receipt.get("destination")
        if isinstance(owner, str) and os.path.normcase(owner) != os.path.normcase(str(root)):
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "receipt",
                "An install receipt already belongs to another GIMP profile; "
                "multi-profile installs are not supported",
            )
    state = _installation_state(root)
    target = root / _PLUGIN_NAME
    installed_version = _plugin_version(target / ("%s.py" % _PLUGIN_NAME))
    target_identity = None
    owned_file_identities = None
    owned_tree_identities = None
    if target.is_dir():
        target_identity = _target_identity(target)
        if receipt is not None and state in {"current", "upgrade"}:
            owned_file_identities = _owned_file_identities(target, receipt)
            owned_tree_identities = _receipt_tree_identities(target, receipt, "receipt")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "dcc_type": "gimp",
        "verb": verb,
        "adapter_version": __version__,
        "core_version": versions["dcc-mcp-core"],
        "target_adapter_version": versions["dcc-mcp-gimp"],
        "adapter_module_path": versions["adapter_module_path"],
        "core_module_path": versions["core_module_path"],
        "installed_adapter_version": installed_version,
        "expected_adapter_version": __version__,
        "gimp_version": gimp_version,
        "dcc_path": str(executable),
        "python": str(python),
        "destination": str(root),
        "instance_id": instance_id,
        "host_pid": host_pid,
        "_target_identity": list(target_identity) if target_identity is not None else None,
        "_python_identity": list(python_identity),
        "_gimp_identity": list(gimp_identity),
        "_owned_file_identities": {
            relative: list(identity) for relative, identity in (owned_file_identities or {}).items()
        },
        "_owned_tree_identities": {
            relative: list(identity) for relative, identity in (owned_tree_identities or {}).items()
        },
        "_receipt_identity": list(receipt_identity) if receipt_identity is not None else None,
        "_receipt_expected_absent": receipt_expected_absent,
        "installation_state": state,
        "steps": [
            {"id": "preflight", "status": "ok", "gimp_version": gimp_version},
            {"id": "resolve-python", "status": "ok", "path": str(python)},
            {"id": verb, "status": "planned", "installation_state": state},
        ],
        "next_steps": [],
        "receipt_path": str(_receipt_path()),
        "verify": {"directly_usable": False, "failure_stage": None, "failure_reason": None},
    }


def doctor(destination: Optional[Path] = None) -> dict[str, object]:
    root = (destination or default_plugin_dir()).expanduser().absolute()
    physical_error: Optional[str] = None
    try:
        _assert_physical_root(root)
    except InstallFailure as exc:
        physical_error = str(exc)
    script = root / _PLUGIN_NAME / ("%s.py" % _PLUGIN_NAME)
    target = root / _PLUGIN_NAME
    target_linked = _is_link(target)
    script_linked = _is_link(script)
    if physical_error is None and (target_linked or script_linked):
        physical_error = "Managed GIMP plug-in path is linked"
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
    # Never inspect script bytes once any containing component has failed the
    # physical-path check.  ``Path.is_file`` follows a junction/symlink and
    # would otherwise report an operator-owned external script as installed.
    installed_version = (
        _plugin_version(script)
        if physical_error is None and script.is_file() and not script_linked
        else None
    )
    version_matches = installed_version == __version__
    plugin_script_exists = (
        physical_error is None and script.is_file() and not target_linked and not script_linked
    )
    return {
        "ready": plugin_script_exists and version_matches,
        "destination": str(root),
        "plugin_script": str(script),
        "plugin_script_exists": plugin_script_exists,
        "installed_adapter_version": installed_version,
        "expected_adapter_version": __version__,
        "version_matches": version_matches,
        "allowed_roots": roots,
        "file_access_enabled": bool(roots),
        "token_file": str(token_file),
        "token_file_exists": token_file.is_file(),
        "bootstrap_errors": _bootstrap_error_summary(),
        "restart_required_after_install": True,
        "physical_path_error": physical_error,
    }
