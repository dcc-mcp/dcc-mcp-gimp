import builtins
import json
import os
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
    from dcc_mcp_gimp.install_host import _process_start_identity

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
        assert result["gimp_pid"] > 0
        assert isinstance(result["gimp_start_identity"], str)
        assert result["gimp_start_identity"]
        assert result["gimp_start_identity"] == _process_start_identity(result["gimp_pid"])
        assert result["plugin_pid"] > 0
        assert Path(result["plugin_module_path"]).resolve() == PLUGIN.resolve()
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


def test_runtime_captures_bridge_bootstrap_failure(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    run = runtime["DccMcpGimp"]._run

    def fail_token_load():
        raise RuntimeError("token bootstrap failed")

    monkeypatch.setitem(run.__globals__, "_load_or_create_token", fail_token_load)

    with pytest.raises(RuntimeError, match="token bootstrap failed"):
        run(None, None, None, None)

    record = json.loads(errors.read_text(encoding="utf-8").splitlines()[-1])
    assert record["stage"] == "bridge-startup"
    assert record["error_type"] == "RuntimeError"
    assert record["message"] == "token bootstrap failed"


def test_runtime_bounds_bootstrap_error_log(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]

    for index in range(5000):
        capture("bridge-startup", RuntimeError("failure-%d" % index))

    assert errors.stat().st_size <= 256 * 1024


def test_runtime_bootstrap_rejects_reparse_parent(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "linked" / "bootstrap-errors.jsonl"
    capture = runtime["_capture_bootstrap_error"]
    globals_ = capture.__globals__
    original = globals_["_path_is_link_or_reparse"]

    def mark_parent(path):
        return path == errors.parent or original(path)

    monkeypatch.setitem(globals_, "_path_is_link_or_reparse", mark_parent)
    capture("bridge-startup", RuntimeError("must not follow a reparse parent"))

    assert not errors.exists()


def test_runtime_bootstrap_concurrent_writers_remain_bounded(runtime, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.parent.mkdir(parents=True, exist_ok=True)
    errors.write_bytes(b"x" * (200 * 1024))
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(
            pool.map(
                lambda index: capture("bridge-startup", RuntimeError("failure-%d" % index)),
                range(100),
            )
        )

    assert errors.stat().st_size <= 256 * 1024


def test_runtime_bootstrap_rotation_ignores_swapped_python_rename(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.write_bytes((b'{"stage":"old"}\n' * 400))
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_rename = os.rename
    swapped = False

    def race(source, destination, *args, **kwargs):
        nonlocal swapped
        source_path = Path(source)
        if not source_path.is_absolute() and source_path.name.startswith(
            ".bootstrap-errors.jsonl."
        ):
            temporary = errors.parent / source_path.name
            if temporary.exists() and not swapped:
                temporary.rename(temporary.with_name(temporary.name + ".owned"))
                foreign = errors.parent / "foreign-bootstrap.log"
                foreign.write_bytes(b"FOREIGN\n")
                foreign.rename(temporary)
                swapped = True
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", race)
    monkeypatch.setattr(os, "replace", race)
    capture("rotation", RuntimeError("probe"))

    assert not swapped
    assert b"FOREIGN\n" not in errors.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap temp identity lease")
def test_runtime_windows_bootstrap_rotation_rejects_temp_swap(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.write_bytes(b"x" * (260 * 1024))
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]

    def swap(source, _destination):
        foreign = source.with_name("foreign-bootstrap.bin")
        foreign.write_bytes(b"FOREIGN-BOOTSTRAP")
        # The capture transaction holds a no-delete-share handle on the
        # temporary, so an injected pathname swap must fail before publish.
        source.unlink()

    monkeypatch.setitem(capture.__globals__, "_windows_rename_by_handle", swap)
    capture("bridge-startup", RuntimeError("new failure"))

    assert b"FOREIGN-BOOTSTRAP" not in errors.read_bytes()
    assert b"new failure" not in errors.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap handle rotation")
def test_runtime_windows_bootstrap_rotation_publishes_new_record(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.write_bytes(b"x" * (260 * 1024))
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))

    runtime["_capture_bootstrap_error"]("bridge-startup", RuntimeError("fresh failure"))

    payload = errors.read_bytes()
    assert len(payload) <= 256 * 1024
    assert b"fresh failure" in payload


def test_plugin_captures_gi_import_failure(tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    original_import = builtins.__import__

    def block_gi(name, *args, **kwargs):
        if name == "gi":
            raise ImportError("GIMP GI bindings unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_gi)

    with pytest.raises(ImportError, match="GIMP GI bindings unavailable"):
        runpy.run_path(str(PLUGIN))

    record = json.loads(errors.read_text(encoding="utf-8").splitlines()[-1])
    assert record["stage"] == "gi-import"
    assert record["error_type"] == "ImportError"
