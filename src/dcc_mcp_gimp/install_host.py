"""GIMP host, profile, interpreter, and bootstrap-diagnostic I/O."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from .__version__ import __version__
from .install_contract import EXIT_PREFLIGHT, MIN_CORE_VERSION, InstallFailure, _version_tuple

_GIMP_VERSION_PATTERN = re.compile(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b")


def default_plugin_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/GIMP/3.0/plug-ins"
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return root / "GIMP/3.0/plug-ins"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "GIMP/3.0/plug-ins"


def _bootstrap_error_path() -> Path:
    configured = os.environ.get("DCC_MCP_GIMP_BOOTSTRAP_ERRORS")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home().joinpath(".dcc-mcp", "gimp-bootstrap-errors.jsonl").resolve()


def _bootstrap_error_summary() -> dict[str, Any]:
    path = _bootstrap_error_path()
    records = []
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        except OSError as exc:
            return {
                "path": str(path),
                "count": 0,
                "latest": None,
                "read_error": str(exc),
            }
    return {
        "path": str(path),
        "count": len(records),
        "latest": records[-1] if records else None,
        "read_error": None,
    }


def _resolve_python(value: Optional[Path], executable: Path) -> Path:
    configured = value
    if configured is None and os.environ.get("DCC_MCP_INSTALL_PYTHON"):
        configured = Path(os.environ["DCC_MCP_INSTALL_PYTHON"])
    if configured is None:
        configured = next(
            (
                candidate
                for candidate in (
                    executable.with_name("python.exe"),
                    executable.with_name("python3.exe"),
                    executable.with_name("python3"),
                    executable.with_name("python"),
                )
                if candidate.is_file()
            ),
            Path(sys.executable),
        )
    resolved = configured.expanduser().resolve()
    if not resolved.is_file():
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "Python interpreter not found: %s" % resolved,
        )
    return resolved


def _resolve_gimp(value: Optional[Path]) -> Path:
    if value is None:
        configured = os.environ.get("DCC_MCP_GIMP_PATH")
        if configured:
            value = Path(configured)
    if value is None:
        candidates: list[Path] = []
        if os.name == "nt":
            for variable in ("ProgramFiles", "ProgramFiles(x86)"):
                if os.environ.get(variable):
                    candidates.append(Path(os.environ[variable]) / "GIMP 3/bin/gimp-3.0.exe")
        elif sys.platform == "darwin":
            candidates.append(Path("/Applications/GIMP.app/Contents/MacOS/gimp"))
        discovered = shutil.which("gimp-3.0") or shutil.which("gimp")
        if discovered:
            candidates.append(Path(discovered))
        value = next((candidate for candidate in candidates if candidate.is_file()), None)
    if value is None:
        raise InstallFailure(EXIT_PREFLIGHT, "gimp", "GIMP 3 installation was not found")
    resolved = value.expanduser().resolve()
    if resolved.is_dir():
        candidates = (
            resolved / "gimp-3.0.exe",
            resolved / "bin/gimp-3.0.exe",
            resolved / "Contents/MacOS/gimp",
            resolved / "gimp-3.0",
            resolved / "gimp",
        )
        resolved = next((candidate for candidate in candidates if candidate.is_file()), resolved)
    if not resolved.is_file():
        raise InstallFailure(EXIT_PREFLIGHT, "gimp", "GIMP executable not found: %s" % resolved)
    return resolved


def _gimp_version(executable: Path) -> str:
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "gimp_version", str(exc)) from exc
    output = "%s\n%s" % (completed.stdout, completed.stderr)
    match = _GIMP_VERSION_PATTERN.search(output)
    if completed.returncode or match is None:
        raise InstallFailure(EXIT_PREFLIGHT, "gimp_version", "Could not determine GIMP version")
    version = ".".join(part for part in match.groups() if part is not None)
    if int(match.group(1)) != 3:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "gimp_version",
            "GIMP %s is unsupported; DCC-MCP GIMP requires GIMP 3" % version,
        )
    return version


def _target_versions(python: Path) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json; "
        "print(json.dumps({name: m.version(name) for name in "
        "('dcc-mcp-core', 'dcc-mcp-gimp')}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "python", str(exc)) from exc
    if completed.returncode:
        reason = completed.stderr.strip().splitlines()
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            reason[-1] if reason else "Target package metadata query failed",
        )
    try:
        versions = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "Target interpreter returned invalid metadata",
        ) from exc
    core_version = str(versions.get("dcc-mcp-core", ""))
    if _version_tuple(core_version) < _version_tuple(MIN_CORE_VERSION):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "core",
            "dcc-mcp-core %s is unsupported; version %s or newer is required"
            % (core_version, MIN_CORE_VERSION),
        )
    return {str(key): str(item) for key, item in versions.items()}


def _python_import_check(python: Path) -> dict[str, Any]:
    code = (
        "import json, dcc_mcp_gimp; "
        "from dcc_mcp_gimp.__version__ import __version__; "
        "print(json.dumps({'version': __version__, 'importable': True}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "reason": str(exc)}
    if completed.returncode:
        details = completed.stderr.strip().splitlines()
        return {"success": False, "reason": details[-1] if details else "Import failed"}
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        return {"success": False, "reason": "Target interpreter returned invalid output"}
    if payload.get("version") != __version__:
        return {
            "success": False,
            "version": payload.get("version"),
            "expected_version": __version__,
            "reason": "Target interpreter adapter version is stale",
        }
    return {"success": bool(payload.get("importable")), **payload}
