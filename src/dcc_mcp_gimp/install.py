"""Install and diagnose the GIMP 3 persistent Python plug-in."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

_PLUGIN_NAME = "dcc_mcp_gimp"


def default_plugin_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return root / "GIMP/3.0/plug-ins"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "GIMP/3.0/plug-ins"


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
    root.mkdir(parents=True, exist_ok=True)
    target = root / _PLUGIN_NAME
    source = _source_file()
    suffix = "%d-%d" % (os.getpid(), time.time_ns())
    backup = root / (".%s.backup-%s" % (_PLUGIN_NAME, suffix))

    with tempfile.TemporaryDirectory(prefix=".dcc-mcp-gimp-install-", dir=str(root)) as temp:
        staged = Path(temp) / _PLUGIN_NAME
        staged.mkdir()
        script = staged / source.name
        shutil.copy2(source, script)
        if os.name != "nt":
            script.chmod(0o755)
        moved_existing = False
        try:
            if target.exists():
                os.replace(str(target), str(backup))
                moved_existing = True
            os.replace(str(staged), str(target))
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            if moved_existing and backup.exists():
                os.replace(str(backup), str(target))
            raise
        else:
            if backup.exists():
                shutil.rmtree(backup)
    return target


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
    return {
        "ready": script.is_file(),
        "destination": str(root),
        "plugin_script": str(script),
        "plugin_script_exists": script.is_file(),
        "allowed_roots": roots,
        "file_access_enabled": bool(roots),
        "token_file": str(token_file),
        "token_file_exists": token_file.is_file(),
        "restart_required_after_install": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Install or inspect the DCC-MCP GIMP plug-in")
    parser.add_argument("--destination", type=Path, help="Override the GIMP plug-ins directory")
    parser.add_argument("--doctor", action="store_true", help="Print installation status as JSON")
    args = parser.parse_args()
    if args.doctor:
        result = doctor(args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ready"]:
            raise SystemExit(1)
        return
    target = install(args.destination)
    print("Installed DCC-MCP GIMP plug-in to %s; restart GIMP and run the bridge." % target)


def doctor_main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the DCC-MCP GIMP plug-in installation")
    parser.add_argument("--destination", type=Path, help="Override the GIMP plug-ins directory")
    result = doctor(parser.parse_args().destination)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ready"]:
        raise SystemExit(1)
