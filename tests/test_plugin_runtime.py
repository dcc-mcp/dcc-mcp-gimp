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


@pytest.mark.skipif(os.name == "nt", reason="Exercises POSIX bootstrap first-create race")
def test_runtime_bootstrap_first_create_does_not_append_to_raced_file(
    runtime, tmp_path, monkeypatch
):
    errors = tmp_path / "bootstrap-errors.jsonl"
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_open = os.open
    state = {"injected": False}

    def race_open(path, flags, *args, **kwargs):
        if path == errors.name and flags & os.O_CREAT and not state["injected"]:
            errors.write_bytes(b"OPERATOR-BOOTSTRAP")
            state["injected"] = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race_open)
    capture("bridge-startup", RuntimeError("must not append"))

    assert state["injected"] is True
    assert errors.read_bytes() == b"OPERATOR-BOOTSTRAP"


@pytest.mark.skipif(os.name == "nt", reason="Exercises POSIX bootstrap publication")
def test_runtime_bootstrap_rotation_ignores_swapped_python_rename(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.write_bytes((b'{"stage":"old"}\n' * 20000))
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_link = capture.__globals__["_BOOTSTRAP_POSIX_LINK"]
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
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setitem(capture.__globals__, "_BOOTSTRAP_POSIX_LINK", race)
    capture("rotation", RuntimeError("probe"))

    assert swapped
    assert b"FOREIGN\n" not in errors.read_bytes()
    assert any(
        item.read_bytes() == b"FOREIGN\n" for item in errors.parent.iterdir() if item.is_file()
    )


@pytest.mark.skipif(os.name == "nt", reason="Exercises POSIX bootstrap rotation")
def test_runtime_bootstrap_rotation_failure_preserves_previous_log(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    original_bytes = b'{"stage":"old"}\n' * 400
    errors.write_bytes(original_bytes)
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_write = os.write
    state = {"failed": False}

    def fail_rotation_write(fd, data):
        if not state["failed"] and len(data) > 5:
            state["failed"] = True
            original_write(fd, data[:5])
            raise OSError("injected bootstrap write failure")
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", fail_rotation_write)
    capture("rotation", RuntimeError("probe"))

    assert state["failed"]
    assert errors.read_bytes() == original_bytes


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap temp identity lease")
def test_runtime_windows_bootstrap_rotation_rejects_temp_swap(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.write_bytes(b"x" * (260 * 1024))
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_rename = capture.__globals__["_windows_rename_by_handle"]

    def swap(source, destination, *args, **kwargs):
        if not source.name.endswith(".tmp"):
            return original_rename(source, destination, *args, **kwargs)
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


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows no-replace rotation")
def test_runtime_windows_bootstrap_rotation_preserves_final_name_occupant(
    runtime, tmp_path, monkeypatch
):
    errors = tmp_path / "bootstrap-errors.jsonl"
    previous = b"x" * (260 * 1024)
    errors.write_bytes(previous)
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    globals_ = capture.__globals__
    original_rename = globals_["_windows_rename_by_handle"]
    state = {"occupied": False}

    def occupy_final_name(source, destination, *args, **kwargs):
        if not state["occupied"] and source.name.endswith(".tmp"):
            if errors.exists():
                errors.rename(tmp_path / "previous-bootstrap.log")
            errors.write_bytes(b"OPERATOR-BOOTSTRAP")
            state["occupied"] = True
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setitem(globals_, "_windows_rename_by_handle", occupy_final_name)
    capture("bridge-startup", RuntimeError("fresh failure"))

    assert state["occupied"] is True
    assert errors.read_bytes() == b"OPERATOR-BOOTSTRAP"
    assert any(
        candidate.is_file() and candidate.read_bytes() == previous
        for candidate in tmp_path.iterdir()
        if candidate != errors
    )


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap append")
def test_runtime_windows_bootstrap_append_completes_short_writes(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.write_bytes(b'{"stage":"old"}\n')
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_write = os.write
    state = {"short": False}

    def write_part(descriptor, data):
        if not state["short"] and len(data) > 1:
            state["short"] = True
            return original_write(descriptor, data[:1])
        return original_write(descriptor, data)

    monkeypatch.setattr(os, "write", write_part)
    capture("append", RuntimeError("complete append record"))

    assert state["short"] is True
    records = [json.loads(line) for line in errors.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["message"] == "complete append record"


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap append rollback")
def test_runtime_windows_bootstrap_append_rolls_back_partial_failure(
    runtime, tmp_path, monkeypatch
):
    errors = tmp_path / "bootstrap-errors.jsonl"
    original_bytes = b'{"stage":"old"}\n'
    errors.write_bytes(original_bytes)
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_write = os.write
    calls = 0

    def fail_after_part(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, data[:1])
        raise OSError("injected append failure")

    monkeypatch.setattr(os, "write", fail_after_part)
    capture("append", RuntimeError("incomplete append record"))

    assert calls == 2
    assert errors.read_bytes() == original_bytes


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap rotation staging")
def test_runtime_windows_bootstrap_rotation_staging_completes_short_writes(
    runtime, tmp_path, monkeypatch
):
    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.write_bytes(b"x" * (260 * 1024))
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_write = os.write
    state = {"short": False}

    def write_part(descriptor, data):
        if not state["short"] and len(data) > 1:
            state["short"] = True
            return original_write(descriptor, data[:1])
        return original_write(descriptor, data)

    monkeypatch.setattr(os, "write", write_part)
    capture("rotation", RuntimeError("complete rotation record"))

    assert state["short"] is True
    assert b"complete rotation record" in errors.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap rotation rollback")
def test_runtime_windows_bootstrap_rotation_staging_discards_partial_failure(
    runtime, tmp_path, monkeypatch
):
    errors = tmp_path / "bootstrap-errors.jsonl"
    original_bytes = b"x" * (260 * 1024)
    errors.write_bytes(original_bytes)
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_write = os.write
    calls = 0

    def fail_after_part(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, data[:1])
        raise OSError("injected rotation staging failure")

    monkeypatch.setattr(os, "write", fail_after_part)
    capture("rotation", RuntimeError("incomplete rotation record"))

    assert calls == 2
    assert errors.read_bytes() == original_bytes
    assert not list(tmp_path.glob(".bootstrap-errors.jsonl.*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap creation")
def test_runtime_windows_bootstrap_new_file_completes_short_writes(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_write = os.write
    state = {"short": False}

    def write_part(descriptor, data):
        if not state["short"] and len(data) > 1:
            state["short"] = True
            return original_write(descriptor, data[:1])
        return original_write(descriptor, data)

    monkeypatch.setattr(os, "write", write_part)
    capture("bridge-startup", RuntimeError("complete record"))

    assert state["short"] is True
    record = json.loads(errors.read_text(encoding="utf-8").splitlines()[-1])
    assert record["message"] == "complete record"


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows bootstrap creation")
def test_runtime_windows_bootstrap_new_file_discards_failed_write(runtime, tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    capture = runtime["_capture_bootstrap_error"]
    original_write = os.write
    calls = 0

    def fail_after_part(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, data[:1])
        raise OSError("injected write failure")

    monkeypatch.setattr(os, "write", fail_after_part)
    capture("bridge-startup", RuntimeError("incomplete record"))

    assert calls == 2
    assert not errors.exists()


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
