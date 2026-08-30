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

_GIMP_VERSION_PATTERN = re.compile(
    r"^(?:GNU Image Manipulation Program version|GIMP) "
    r"(?P<version>(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5}))$"
)
_GIMP_EXECUTABLES = frozenset(("gimp", "gimp.exe", "gimp-3.0", "gimp-3.0.exe"))
_MAX_PROBE_OUTPUT = 16 * 1024
_MAX_METADATA_OUTPUT = 256 * 1024
_MAX_BOOTSTRAP_RECORD_BYTES = 64 * 1024
_MAX_BOOTSTRAP_RECORDS = 1024


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        if path.is_symlink() or bool(is_junction and is_junction()):
            return True
        if os.name != "nt":
            return False
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if int(attributes) in (-1, 0xFFFFFFFF):
            return False
        return bool(int(attributes) & 0x400)
    except AttributeError:
        return False
    except (OSError, RuntimeError, ValueError):
        return True


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
        return Path(configured).expanduser().absolute()
    return Path.home().joinpath(".dcc-mcp", "gimp-bootstrap-errors.jsonl").absolute()


def _bootstrap_error_summary() -> dict[str, Any]:
    path = _bootstrap_error_path()
    records = []
    try:
        linked = _path_is_link_or_reparse(path)
        is_file = path.is_file()
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "path": str(path),
            "count": 0,
            "latest": None,
            "read_error": str(exc),
        }
    if linked:
        return {
            "path": str(path),
            "count": 0,
            "latest": None,
            "read_error": "Bootstrap error path is linked",
        }
    if is_file:
        try:
            with path.open("rb") as stream:
                for _ in range(_MAX_BOOTSTRAP_RECORDS):
                    raw_line = stream.readline(_MAX_BOOTSTRAP_RECORD_BYTES + 1)
                    if not raw_line:
                        break
                    oversized = len(raw_line) > _MAX_BOOTSTRAP_RECORD_BYTES
                    if oversized and not raw_line.endswith(b"\n"):
                        # Do not scan an attacker-controlled unterminated line;
                        # one bounded prefix is enough for the diagnostic.
                        stream.seek(0, os.SEEK_END)
                    line = raw_line[:_MAX_BOOTSTRAP_RECORD_BYTES].decode("utf-8", errors="replace")
                    if oversized:
                        records.append(
                            {
                                "stage": "unknown",
                                "message": line[:_MAX_BOOTSTRAP_RECORD_BYTES],
                                "truncated": True,
                            }
                        )
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        if isinstance(record.get("message"), str):
                            record["message"] = record["message"][:_MAX_BOOTSTRAP_RECORD_BYTES]
                        records.append(record)
        except (OSError, RuntimeError, ValueError) as exc:
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
    try:
        candidate = configured.expanduser().absolute()
        if _path_is_link_or_reparse(candidate):
            raise InstallFailure(EXIT_PREFLIGHT, "python", "Python interpreter path is linked")
        resolved = candidate.resolve()
        if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
            raise InstallFailure(EXIT_PREFLIGHT, "python", "Python interpreter path is linked")
        valid = resolved.is_file() and resolved.stat().st_size > 0
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, "python", "Python interpreter path is unavailable"
        ) from exc
    if not valid:
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
    try:
        candidate = value.expanduser().absolute()
        if _path_is_link_or_reparse(candidate):
            raise InstallFailure(EXIT_PREFLIGHT, "gimp", "GIMP executable path is linked")
        resolved = candidate.resolve()
        if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
            raise InstallFailure(EXIT_PREFLIGHT, "gimp", "GIMP executable path is linked")
        if resolved.is_dir():
            candidates = (
                resolved / "gimp-3.0.exe",
                resolved / "bin/gimp-3.0.exe",
                resolved / "Contents/MacOS/gimp",
                resolved / "gimp-3.0",
                resolved / "gimp",
            )
            resolved = next(
                (candidate for candidate in candidates if candidate.is_file()), resolved
            )
        valid = resolved.is_file() and not resolved.is_symlink() and resolved.stat().st_size > 0
    except InstallFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "gimp", "GIMP executable path is unavailable") from exc
    if not valid:
        raise InstallFailure(EXIT_PREFLIGHT, "gimp", "GIMP executable not found: %s" % resolved)
    name = resolved.name.lower()
    is_appimage = name.endswith(".appimage") and "gimp" in name
    if name not in _GIMP_EXECUTABLES and not is_appimage:
        raise InstallFailure(
            EXIT_PREFLIGHT, "gimp", "--dcc-path must select a canonical GIMP executable"
        )
    return resolved


def _gimp_version(executable: Path) -> str:
    command = [str(executable)]
    if executable.name.lower().endswith(".appimage"):
        command.append("--appimage-extract-and-run")
    command.append("--version")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "gimp_version", str(exc)) from exc
    output = "%s\n%s" % (completed.stdout, completed.stderr)
    if len(output.encode("utf-8", errors="replace")) > _MAX_PROBE_OUTPUT:
        raise InstallFailure(EXIT_PREFLIGHT, "gimp_version", "GIMP version output is unbounded")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    matches = [match for line in lines if (match := _GIMP_VERSION_PATTERN.fullmatch(line))]
    match = matches[0] if len(matches) == 1 else None
    if completed.returncode or match is None:
        raise InstallFailure(EXIT_PREFLIGHT, "gimp_version", "Could not determine GIMP version")
    version = match.group("version")
    parsed = _version_tuple(version)
    if not parsed or parsed[0] != 3:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "gimp_version",
            "GIMP %s is unsupported; DCC-MCP GIMP requires GIMP 3" % version,
        )
    return version


def _target_versions(python: Path) -> dict[str, str]:
    code = r"""
import importlib.metadata as metadata
import json
import pathlib
import sys
import urllib.parse
import urllib.request

import dcc_mcp_core
import dcc_mcp_gimp
from dcc_mcp_gimp.__version__ import __version__ as adapter_module_version


def owned(distribution_name, package_name, module, module_version):
    distribution = metadata.distribution(distribution_name)
    module_path = pathlib.Path(module.__file__).resolve()
    record_paths = {
        pathlib.Path(distribution.locate_file(item)).resolve()
        for item in tuple(distribution.files or ())
    }
    record_owned = module_path in record_paths
    editable_root = None
    try:
        raw = distribution.read_text("direct_url.json")
        direct_url = json.loads(raw) if raw else None
        url = direct_url.get("url") if isinstance(direct_url, dict) else None
        editable = (
            direct_url.get("dir_info", {}).get("editable") is True
            if isinstance(direct_url, dict)
            else False
        )
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if (
            editable
            and parsed is not None
            and parsed.scheme == "file"
            and not parsed.query
            and not parsed.fragment
        ):
            editable_root = pathlib.Path(
                urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
            ).resolve()
    except Exception:
        editable_root = None
    editable_owned = bool(
        editable_root
        and module_path
        in {
            editable_root / "src" / package_name / "__init__.py",
            editable_root / package_name / "__init__.py",
        }
    )
    return {
        "version": distribution.version,
        "module_version": module_version,
        "module_path": str(module_path),
        "owned": record_owned or editable_owned,
        "editable_root": str(editable_root) if editable_root else "",
    }


print(
    json.dumps(
        {
            "python_executable": sys.executable,
            "core": owned(
                "dcc-mcp-core",
                "dcc_mcp_core",
                dcc_mcp_core,
                getattr(dcc_mcp_core, "__version__", ""),
            ),
            "adapter": owned(
                "dcc-mcp-gimp",
                "dcc_mcp_gimp",
                dcc_mcp_gimp,
                adapter_module_version,
            ),
        }
    )
)
""".strip()
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
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        reason = stderr[:_MAX_METADATA_OUTPUT].strip().splitlines()
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            reason[-1] if reason else "Target package metadata query failed",
        )
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    if len(stdout.encode("utf-8", errors="replace")) > _MAX_METADATA_OUTPUT:
        raise InstallFailure(EXIT_PREFLIGHT, "python", "Target interpreter metadata is unbounded")
    try:
        versions = json.loads(stdout.strip())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "Target interpreter returned invalid metadata",
        ) from exc
    if not isinstance(versions, dict):
        raise InstallFailure(
            EXIT_PREFLIGHT, "python", "Target interpreter returned invalid metadata"
        )
    try:
        reported_python = Path(str(versions["python_executable"])).resolve()
        core = versions["core"]
        adapter = versions["adapter"]
    except (KeyError, TypeError, OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, "python", "Target interpreter returned incomplete metadata"
        ) from exc
    if not isinstance(core, dict) or not isinstance(adapter, dict):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "Target interpreter returned incomplete metadata",
        )
    for name, package in (("core", core), ("adapter", adapter)):
        if (
            not isinstance(package.get("version"), str)
            or not isinstance(package.get("module_version"), str)
            or not isinstance(package.get("module_path"), str)
            or not isinstance(package.get("owned"), bool)
        ):
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "python",
                "Target interpreter returned incomplete %s metadata" % name,
            )
    try:
        selected_python = python.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, "python", "Target interpreter path is unavailable"
        ) from exc
    if reported_python != selected_python:
        raise InstallFailure(
            EXIT_PREFLIGHT, "python", "Target interpreter identity does not match --python"
        )
    core_version = str(core.get("version", ""))
    adapter_version = str(adapter.get("version", ""))
    if not _version_tuple(core_version) or not _version_tuple(adapter_version):
        raise InstallFailure(
            EXIT_PREFLIGHT, "python", "Target distributions returned noncanonical versions"
        )
    if core.get("module_version") != core_version or not core.get("owned"):
        raise InstallFailure(
            EXIT_PREFLIGHT, "python", "Imported Core module is outside its selected distribution"
        )
    if (
        adapter.get("module_version") != adapter_version
        or adapter_version != __version__
        or not adapter.get("owned")
    ):
        raise InstallFailure(
            EXIT_PREFLIGHT, "python", "Imported GIMP adapter is outside its selected distribution"
        )
    if _version_tuple(core_version) < _version_tuple(MIN_CORE_VERSION):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "core",
            "dcc-mcp-core %s is unsupported; version %s or newer is required"
            % (core_version, MIN_CORE_VERSION),
        )
    return {
        "dcc-mcp-core": core_version,
        "dcc-mcp-gimp": adapter_version,
        "core_module_path": str(core.get("module_path", "")),
        "adapter_module_path": str(adapter.get("module_path", "")),
        "python_executable": str(reported_python),
    }


def _python_import_check(python: Path) -> dict[str, Any]:
    try:
        versions = _target_versions(python)
    except InstallFailure as exc:
        return {"success": False, "reason": str(exc)}
    return {
        "success": True,
        "version": versions["dcc-mcp-gimp"],
        "core_version": versions["dcc-mcp-core"],
        "adapter_module_path": versions["adapter_module_path"],
        "core_module_path": versions["core_module_path"],
    }


def _process_executable_path(pid: int) -> Optional[Path]:
    """Resolve one live PID to its executable without trusting registry metadata."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32_768)
            length = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None
            return Path(buffer.value).resolve()
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform == "darwin":
        import ctypes

        buffer = ctypes.create_string_buffer(4096)
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            length = int(libproc.proc_pidpath(int(pid), buffer, len(buffer)))
        except (OSError, AttributeError):
            return None
        return Path(buffer.value.decode("utf-8")).resolve() if length > 0 else None
    try:
        return Path("/proc/%d/exe" % pid).resolve(strict=True)
    except OSError:
        return None


def _process_start_identity(pid: int) -> Optional[str]:
    """Return a stable process creation identity so PID reuse fails closed."""
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
        stat = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8")
        closing = stat.rfind(") ")
        start_ticks = stat[closing + 2 :].split()[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (IndexError, OSError, ValueError):
        return None
    return "linux:%s:%s" % (boot_id, start_ticks)
