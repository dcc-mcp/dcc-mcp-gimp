"""Exercise every bundled typed tool against a real GIMP 3 host."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from dcc_mcp_gimp.server import GimpMcpServer


def post(url: str, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def call(url: str, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    response = post(url, "tools/call", {"name": name, "arguments": arguments or {}})
    result = response.get("result", {})
    if response.get("error") or result.get("isError"):
        raise RuntimeError(json.dumps(response))
    envelope = result.get("structuredContent")
    if envelope is None:
        envelope = json.loads(result["content"][0]["text"])
    job_id = envelope.get("job_id") if isinstance(envelope, dict) else None
    if not job_id:
        return envelope
    deadline = time.monotonic() + 1_800
    while time.monotonic() < deadline:
        poll = post(
            url,
            "tools/call",
            {"name": "jobs_get_status", "arguments": {"job_id": job_id, "include_result": True}},
        )
        poll_result = poll.get("result", {})
        if poll.get("error") or poll_result.get("isError"):
            raise RuntimeError(json.dumps(poll))
        status = poll_result.get("structuredContent")
        if status is None:
            status = json.loads(poll_result["content"][0]["text"])
        if status.get("status") == "completed":
            return status["result"]
        if status.get("status") in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError(json.dumps(status))
        time.sleep(1)
    raise TimeoutError("MCP job %s did not complete within 1800 seconds" % job_id)


def list_tool_names(url: str) -> set[str]:
    names: set[str] = set()
    cursor: Optional[str] = None
    for _page in range(20):
        response = post(url, "tools/list", {"cursor": cursor} if cursor else None)
        if response.get("error"):
            raise RuntimeError(json.dumps(response))
        result = response.get("result", {})
        names.update(item["name"] for item in result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor:
            return names
    raise RuntimeError("MCP tools/list exceeded the 20-page smoke-test budget")


def typed_name(names: set[str], base_name: str) -> str:
    return next(name for name in names if name == base_name or name.endswith("__" + base_name))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath((str(path), str(root))) == str(root):
                return True
        except ValueError:
            continue
    return False


def main() -> None:
    smoke_root_value = os.environ.get("DCC_MCP_GIMP_SMOKE_ROOT")
    if not smoke_root_value:
        raise RuntimeError("DCC_MCP_GIMP_SMOKE_ROOT must name an allowed writable directory")
    smoke_root = Path(smoke_root_value).expanduser().resolve()
    if not smoke_root.is_dir():
        raise RuntimeError("DCC_MCP_GIMP_SMOKE_ROOT must name an existing directory")
    allowed = [
        Path(item).expanduser().resolve()
        for item in os.environ.get("DCC_MCP_GIMP_ALLOWED_ROOTS", "").split(os.pathsep)
        if item.strip()
    ]
    if not inside(smoke_root, allowed):
        raise RuntimeError("Smoke root must be inside DCC_MCP_GIMP_ALLOWED_ROOTS")

    evidence = Path(tempfile.mkdtemp(prefix="dcc-mcp-gimp-live-", dir=str(smoke_root)))
    registry = evidence / "registry"
    os.environ["DCC_MCP_DISABLE_DEFAULT_SKILL_PATHS"] = "1"
    server = GimpMcpServer(port=0, registry_dir=str(registry))
    try:
        server.register_builtin_actions()
        server.start(install_atexit_hook=False)
        call(server.mcp_url, "load_skill", {"skill_name": "gimp-session"})
        names = list_tool_names(server.mcp_url)
        base_names = (
            "get_status",
            "list_images",
            "get_active_image",
            "inspect_image",
            "list_layers",
            "create_image",
            "open_image",
            "save_image",
            "export_image",
            "create_layer",
            "fill_layer",
            "set_layer_properties",
            "set_active_layer",
            "delete_layer",
            "flatten_image",
            "close_image",
        )
        tools = {name: typed_name(names, name) for name in base_names}

        status = call(server.mcp_url, tools["get_status"])
        created = call(
            server.mcp_url,
            tools["create_image"],
            {"width": 512, "height": 512, "name": "Background", "background_color": [13, 28, 52]},
        )
        image_id = created["context"]["image_id"]
        base_layer_id = created["context"]["created_layer_id"]
        accent = call(
            server.mcp_url,
            tools["create_layer"],
            {"image_id": image_id, "name": "Accent", "color": [35, 196, 222, 220]},
        )
        accent_id = accent["context"]["layer_id"]
        foreground = call(
            server.mcp_url,
            tools["create_layer"],
            {"image_id": image_id, "name": "Foreground", "color": [157, 100, 255]},
        )
        foreground_id = foreground["context"]["layer_id"]
        call(
            server.mcp_url,
            tools["fill_layer"],
            {"image_id": image_id, "layer_id": accent_id, "color": [18, 174, 206, 210]},
        )
        call(
            server.mcp_url,
            tools["set_layer_properties"],
            {"image_id": image_id, "layer_id": accent_id, "name": "Accent 80%", "opacity": 80},
        )
        call(
            server.mcp_url,
            tools["set_active_layer"],
            {"image_id": image_id, "layer_id": foreground_id},
        )
        inspected = call(server.mcp_url, tools["inspect_image"], {"image_id": image_id})
        listed_layers = call(server.mcp_url, tools["list_layers"], {"image_id": image_id})
        listed_images = call(server.mcp_url, tools["list_images"])
        active = call(server.mcp_url, tools["get_active_image"])

        xcf = evidence / "dcc-mcp-gimp-live.xcf"
        png = evidence / "dcc-mcp-gimp-live.png"
        saved = call(
            server.mcp_url,
            tools["save_image"],
            {"image_id": image_id, "path": str(xcf), "timeout_secs": 300},
        )
        exported = call(
            server.mcp_url,
            tools["export_image"],
            {"image_id": image_id, "path": str(png), "timeout_secs": 300},
        )
        closed = call(server.mcp_url, tools["close_image"], {"image_id": image_id})
        reopened = call(
            server.mcp_url,
            tools["open_image"],
            {"path": str(xcf), "timeout_secs": 300},
        )
        reopened_id = reopened["context"]["image_id"]
        disposable = call(
            server.mcp_url,
            tools["create_layer"],
            {"image_id": reopened_id, "name": "DeleteMe", "color": [255, 0, 0]},
        )
        call(
            server.mcp_url,
            tools["delete_layer"],
            {"image_id": reopened_id, "layer_id": disposable["context"]["layer_id"]},
        )
        call(server.mcp_url, tools["flatten_image"], {"image_id": reopened_id, "confirm": True})
        call(
            server.mcp_url,
            tools["close_image"],
            {"image_id": reopened_id, "discard_changes": True},
        )
    finally:
        server.stop()

    assert status["success"] is True
    assert status["context"]["authenticated"] is True
    assert status["context"]["command_count"] == 16
    assert inspected["context"]["layer_count"] == 3
    assert {base_layer_id, accent_id, foreground_id}.issubset(
        {item["layer_id"] for item in listed_layers["context"]["layers"]}
    )
    assert any(item["image_id"] == image_id for item in listed_images["context"]["images"])
    assert active["context"]["image"] is not None
    assert saved["context"]["sha256"] == sha256(xcf)
    assert exported["context"]["sha256"] == sha256(png)
    assert xcf.read_bytes().startswith(b"gimp xcf ")
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert closed["context"]["closed"] is True
    print(
        json.dumps(
            {
                "gimp_version": status["context"]["gimp_version"],
                "typed_tools": len(tools),
                "layers": ["Background", "Accent 80%", "Foreground"],
                "xcf": {"path": str(xcf), "bytes": xcf.stat().st_size, "sha256": sha256(xcf)},
                "png": {"path": str(png), "bytes": png.stat().st_size, "sha256": sha256(png)},
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
