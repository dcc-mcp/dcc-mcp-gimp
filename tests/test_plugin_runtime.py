import json
import runpy
import socket
import sys
import threading
import types
from pathlib import Path

import pytest

from dcc_mcp_gimp.bridge import GimpBridge

PLUGIN = Path(__file__).resolve().parents[1] / "bridge/gimp-plugin/dcc_mcp_gimp.py"


@pytest.fixture
def runtime(monkeypatch):
    class PlugIn:
        __gtype__ = object()

    gimp = types.SimpleNamespace(
        PlugIn=PlugIn,
        main=lambda *_args: None,
        version=lambda: "3.0-test",
        get_images=lambda: [],
    )
    glib = types.SimpleNamespace(
        SOURCE_REMOVE=False,
        idle_add=lambda callback: callback(),
    )
    repository = types.ModuleType("gi.repository")
    repository.Gegl = types.SimpleNamespace()
    repository.Gimp = gimp
    repository.Gio = types.SimpleNamespace()
    repository.GLib = glib
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    return runpy.run_path(str(PLUGIN))


def test_runtime_authenticated_server_round_trip(runtime):
    runtime["_execute_command"].__globals__["_bridge_token"] = "x" * 32
    server = runtime["_Server"](("127.0.0.1", 0), runtime["_Handler"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bridge = GimpBridge(port=server.server_address[1], token="x" * 32)
        result = bridge.call("gimp.ping")
        assert result["ready"] is True
        assert result["gimp_version"] == "3.0-test"
        assert result["command_count"] == 16
        assert result["authenticated"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_runtime_rejects_unauthenticated_request(runtime):
    runtime["_execute_command"].__globals__["_bridge_token"] = "x" * 32
    server = runtime["_Server"](("127.0.0.1", 0), runtime["_Handler"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(server.server_address, timeout=2) as connection:
            request = {
                "jsonrpc": "2.0",
                "id": "request-1",
                "method": "gimp.ping",
                "params": {},
                "token": "wrong",
            }
            connection.sendall((json.dumps(request) + "\n").encode())
            response = json.loads(connection.makefile("r", encoding="utf-8").readline())
        assert response["id"] == "request-1"
        assert response["error"]["code"] == "unauthorized"
        assert "token" not in json.dumps(response).lower()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_runtime_bounds_main_thread_queue(runtime):
    semaphore = runtime["_pending_commands"]
    for _index in range(runtime["MAX_PENDING_COMMANDS"]):
        assert semaphore.acquire(blocking=False)
    try:
        with pytest.raises(runtime["HostCommandError"], match="queue is full"):
            runtime["_dispatch"]("gimp.ping", {})
    finally:
        for _index in range(runtime["MAX_PENDING_COMMANDS"]):
            semaphore.release()
