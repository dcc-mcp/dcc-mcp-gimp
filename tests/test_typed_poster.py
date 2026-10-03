"""Behavioral bounds and state-restoration checks for the typed poster tools."""

import math
import types

import pytest
from test_plugin_runtime import runtime as runtime


def setup_shape(runtime, monkeypatch):
    events = []
    layer = types.SimpleNamespace(
        is_group_layer=lambda: False,
        get_lock_content=lambda: False,
        get_id=lambda: 7,
        edit_fill=lambda _fill: events.append("fill") or False,
    )
    image = types.SimpleNamespace(
        select_polygon=lambda *_args: events.append("select") or True,
        select_item=lambda *_args: events.append("restore") or True,
        remove_channel=lambda *_args: events.append("remove") or True,
    )
    glob = runtime["_paint_shape"].__globals__
    monkeypatch.setitem(glob, "_resolve_layer", lambda *_args: layer)
    monkeypatch.setitem(glob, "_color", lambda _value: object())
    gimp = glob["Gimp"]
    gimp.context_push = lambda: events.append("push") or True
    gimp.context_pop = lambda: events.append("pop") or True
    gimp.context_set_opacity = lambda *_args: True
    gimp.context_set_antialias = lambda *_args: True
    gimp.context_set_feather = lambda *_args: True
    gimp.context_set_feather_radius = lambda *_args: True
    gimp.context_set_foreground = lambda *_args: True
    gimp.Selection = types.SimpleNamespace(
        save=lambda _image: types.SimpleNamespace(get_id=lambda: 41)
    )
    gimp.ChannelOps = types.SimpleNamespace(REPLACE=0)
    gimp.FillType = types.SimpleNamespace(FOREGROUND=0)
    return image, events


@pytest.mark.parametrize(
    "points",
    [
        [],
        [[1, 2]],
        [[0, 0]] * 257,
        [[0, 0], [1, 1], [2, math.nan]],
        [[0, 0], [1, 1], [2, math.inf]],
        [[0, 0], [1, 1], [2, 32769]],
    ],
)
def test_invalid_polygon_never_changes_selection(runtime, monkeypatch, points):
    image, events = setup_shape(runtime, monkeypatch)
    with pytest.raises(runtime["HostCommandError"]):
        runtime["_paint_shape"](
            image, {"layer_id": 7, "shape": "polygon", "color": [0, 0, 0], "points": points}
        )
    assert events == []


def test_failed_paint_restores_selection_and_context(runtime, monkeypatch):
    image, events = setup_shape(runtime, monkeypatch)
    with pytest.raises(runtime["HostCommandError"], match="paint the shape"):
        runtime["_paint_shape"](
            image,
            {
                "layer_id": 7,
                "shape": "polygon",
                "color": [0, 0, 0],
                "points": [[0, 0], [1, 1], [2, 0]],
            },
        )
    assert events == ["push", "select", "fill", "restore", "remove", "pop"]


def test_srgb_color_contract_uses_encoded_hex(runtime):
    colors = []
    runtime["_color"].__globals__["Gegl"].Color = types.SimpleNamespace(
        new=lambda value: colors.append(value) or value
    )
    assert runtime["_color"]([128, 64, 255, 128]) == "#8040ff80"
    assert runtime["_color"]([0, 127, 255]) == "#007fffff"
    assert colors == ["#8040ff80", "#007fffff"]


@pytest.mark.parametrize(
    "tool",
    [
        "create_group",
        "import_layer",
        "place_layer",
        "set_layer_mode",
        "paint_shape",
        "create_text",
        "list_fonts",
        "export_layer",
    ],
)
def test_tool_entrypoints_use_core_argument_and_result_protocol(monkeypatch, tool):
    """A successful native operation must not become a blank MCP success envelope."""
    import runpy
    from pathlib import Path

    import dcc_mcp_core.skill

    import dcc_mcp_gimp.skill_tools

    sent = []
    sentinel = object()
    monkeypatch.setattr(dcc_mcp_gimp.skill_tools, "bridge_main", lambda *_args: sentinel)
    monkeypatch.setattr(dcc_mcp_core.skill, "run_main", lambda function: sent.append(function))
    script = (
        Path(__file__).resolve().parents[1]
        / "src/dcc_mcp_gimp/skills/gimp-session/scripts"
        / (tool + ".py")
    )
    runpy.run_path(str(script), run_name="__main__")
    assert sent == [sentinel]


def test_persistent_callback_uses_one_run_data_object(runtime, monkeypatch):
    plugin_class = runtime["DccMcpGimp"]
    plugin = plugin_class()
    captured = []
    procedure = types.SimpleNamespace(
        set_documentation=lambda *_: None,
        set_attribution=lambda *_: None,
    )
    globals_ = plugin_class.do_create_procedure.__globals__
    gimp = globals_["Gimp"]
    gimp.PDBProcType = types.SimpleNamespace(PERSISTENT="persistent")
    gimp.Procedure = types.SimpleNamespace(new=lambda *args: captured.append(args) or procedure)
    assert plugin.do_create_procedure("fixture") is procedure
    args = captured[0]
    assert len(args) == 5
    assert args[-1] is plugin
    calls = []
    monkeypatch.setattr(
        plugin_class, "_run_bridge", staticmethod(lambda *parts: calls.append(parts))
    )
    args[3](procedure, "configuration", plugin)
    assert calls == [(procedure, "configuration", plugin)]


@pytest.mark.parametrize("started", [False, True])
def test_main_dispatch_retains_timeout_cancellation_boundary(runtime, monkeypatch, started):
    dispatch = runtime["_dispatch"]
    globals_ = dispatch.__globals__
    pending = []
    callbacks = []
    executions = []
    original = globals_["_PendingCommand"]

    def make_pending(method, params):
        value = original(method, params)
        value.completed = types.SimpleNamespace(wait=lambda _: False, set=lambda: None)
        pending.append(value)
        return value

    def enqueue(callback):
        callbacks.append(callback)
        if started:
            callback()

    monkeypatch.setitem(globals_, "_PendingCommand", make_pending)
    monkeypatch.setitem(globals_, "_execute_command", lambda *args: executions.append(args))
    monkeypatch.setattr(globals_["GLib"], "idle_add", enqueue)
    message = "host outcome is unknown" if started else "request cancelled"
    with pytest.raises(runtime["HostCommandError"], match=message):
        dispatch("gimp.paint_shape", {"timeout_secs": 1})
    assert pending[0].cancelled is (not started)
    if not started:
        callbacks[0]()
    assert len(executions) == int(started)
    semaphore = globals_["_pending_commands"]
    acquired = [semaphore.acquire(blocking=False) for _ in range(runtime["MAX_PENDING_COMMANDS"])]
    assert all(acquired)
    for _ in acquired:
        semaphore.release()


def shape_arguments():
    return {
        "layer_id": 7,
        "shape": "polygon",
        "color": [80, 120, 200, 128],
        "points": [[0, 0], [3, 0], [0, 3]],
        "feather": 6,
    }


@pytest.mark.parametrize("save_result", ["none", "raise"])
def test_selection_save_failure_leaves_context_and_pixels_untouched(
    runtime, monkeypatch, save_result
):
    image, events = setup_shape(runtime, monkeypatch)
    gimp = runtime["_paint_shape"].__globals__["Gimp"]

    def save(_image):
        if save_result == "raise":
            raise RuntimeError("selection save failed")
        return None

    gimp.Selection.save = save
    with pytest.raises((RuntimeError, runtime["HostCommandError"])):
        runtime["_paint_shape"](image, shape_arguments())
    # Saving occurs before context_push, so neither a push nor a pop is needed.
    assert events == []


@pytest.mark.parametrize("restore_result", ["false", "raise"])
def test_failed_selection_restore_keeps_recovery_channel_and_pops_context(
    runtime, monkeypatch, restore_result
):
    image, events = setup_shape(runtime, monkeypatch)
    glob = runtime["_paint_shape"].__globals__
    layer = glob["_resolve_layer"](image, 7)
    layer.edit_fill = lambda _: events.append("fill") or True

    def restore(*_args):
        events.append("restore")
        if restore_result == "raise":
            raise RuntimeError("native restore failed")
        return False

    image.select_item = restore
    with pytest.raises(runtime["HostCommandError"], match="channel 41 is retained"):
        runtime["_paint_shape"](image, shape_arguments())
    assert events == ["push", "select", "fill", "restore", "pop"]


def test_successful_mask_restore_uses_exact_context_and_restores_user_settings(
    runtime, monkeypatch
):
    image, events = setup_shape(runtime, monkeypatch)
    glob = runtime["_paint_shape"].__globals__
    layer = glob["_resolve_layer"](image, 7)
    layer.edit_fill = lambda _: events.append("fill") or True
    gimp = glob["Gimp"]
    original = {"feather": True, "antialias": False, "radius": (9, 13), "opacity": 37.5}
    state = dict(original)
    saved_context = []

    def push():
        saved_context.append(dict(state))
        return True

    def pop():
        state.clear()
        state.update(saved_context.pop())
        return True

    def setting(name, value):
        state[name] = value
        return True

    gimp.context_push = push
    gimp.context_pop = pop
    gimp.context_set_opacity = lambda value: setting("opacity", value)
    gimp.context_set_feather = lambda value: setting("feather", value)
    gimp.context_set_antialias = lambda value: setting("antialias", value)
    gimp.context_set_feather_radius = lambda *values: setting("radius", values)
    gimp.displays_flush = lambda: None
    captured = []
    image.select_item = lambda *_: captured.append(dict(state)) or True
    result = runtime["_paint_shape"](image, shape_arguments())
    assert result["painted"] is True
    assert captured[0]["feather"] is False and captured[0]["antialias"] is False
    assert state == original
    assert saved_context == []
    assert events == ["select", "fill", "remove"]


def test_failed_recovery_channel_cleanup_reports_failure_and_restores_context(runtime, monkeypatch):
    image, events = setup_shape(runtime, monkeypatch)
    glob = runtime["_paint_shape"].__globals__
    glob["_resolve_layer"](image, 7).edit_fill = lambda _: events.append("fill") or True
    image.remove_channel = lambda *_: events.append("remove-failed") or False
    with pytest.raises(
        runtime["HostCommandError"], match="could not remove saved recovery channel 41"
    ):
        runtime["_paint_shape"](image, shape_arguments())
    assert events == ["push", "select", "fill", "restore", "remove-failed", "pop"]


@pytest.mark.parametrize("push_failure", ["false", "raise"])
@pytest.mark.parametrize("cleanup", ["success", "false", "raise"])
def test_failed_context_push_removes_unused_mask_without_painting(
    runtime, monkeypatch, push_failure, cleanup
):
    image, events = setup_shape(runtime, monkeypatch)
    glob = runtime["_paint_shape"].__globals__
    gimp = glob["Gimp"]
    state = {"selection": b"original mask", "pixels": b"original pixels", "channels": []}
    channel = types.SimpleNamespace(get_id=lambda: 41)

    def save(_image):
        state["channels"].append(channel)
        events.append("save")
        return channel

    def push():
        events.append("push")
        if push_failure == "raise":
            raise RuntimeError("original context-push sentinel")
        return False

    def remove(saved):
        assert saved is channel
        events.append("remove")
        if cleanup == "raise":
            raise OSError("cleanup sentinel")
        if cleanup == "false":
            return False
        state["channels"].remove(saved)
        return True

    gimp.Selection.save = save
    gimp.context_push = push
    image.remove_channel = remove
    with pytest.raises((runtime["HostCommandError"], RuntimeError)) as caught:
        runtime["_paint_shape"](image, shape_arguments())
    assert events == ["save", "push", "remove"]
    assert state["pixels"] == b"original pixels"
    assert state["selection"] == b"original mask"
    if cleanup == "success":
        assert state["channels"] == []
        expected = "original context-push sentinel" if push_failure == "raise" else "failed to save"
        assert expected in str(caught.value)
    else:
        assert state["channels"] == [channel]
        assert "cleanup of unused saved selection channel 41 also failed" in str(caught.value)
        assert caught.value.__cause__ is not None
        if push_failure == "raise":
            assert isinstance(caught.value.__cause__, RuntimeError)
            assert "original context-push sentinel" in str(caught.value.__cause__)
            assert "original context-push sentinel" in str(caught.value)
        else:
            assert "failed to save the user context" in str(caught.value.__cause__)


def test_successful_context_push_only_pushes_without_touching_saved_mask(runtime, monkeypatch):
    events = []
    image = types.SimpleNamespace(remove_channel=lambda _: events.append("remove"))
    glob = runtime["_push_shape_context"].__globals__
    monkeypatch.setattr(
        glob["Gimp"], "context_push", lambda: events.append("push") or True, raising=False
    )
    assert runtime["_push_shape_context"](image, object()) is True
    assert events == ["push"]


@pytest.mark.parametrize("restore_failure", ["false", "raise"])
@pytest.mark.parametrize("pop_failure", ["false", "raise"])
def test_dual_restore_failures_report_recovery_mask_and_context(
    runtime, monkeypatch, restore_failure, pop_failure
):
    image, events = setup_shape(runtime, monkeypatch)
    glob = runtime["_paint_shape"].__globals__
    glob["_resolve_layer"](image, 7).edit_fill = lambda _: events.append("fill") or True

    def restore(*_args):
        events.append("restore-failed")
        if restore_failure == "raise":
            raise RuntimeError("selection restore sentinel")
        return False

    def pop():
        events.append("pop-failed")
        if pop_failure == "raise":
            raise RuntimeError("context-pop sentinel")
        return False

    image.select_item = restore
    glob["Gimp"].context_pop = pop
    with pytest.raises(runtime["HostCommandError"]) as caught:
        runtime["_paint_shape"](image, shape_arguments())
    message = str(caught.value)
    assert "channel 41 is retained for recovery" in message
    assert "context" in message
    assert caught.value.__cause__ is not None
    if pop_failure == "raise":
        assert "context-pop sentinel" in message
    assert events == ["push", "select", "fill", "restore-failed", "pop-failed"]


def test_paint_and_dual_cleanup_failures_keep_every_error(runtime, monkeypatch):
    image, events = setup_shape(runtime, monkeypatch)
    glob = runtime["_paint_shape"].__globals__
    image.select_item = lambda *_: events.append("restore-failed") or False
    glob["Gimp"].context_pop = lambda: events.append("pop-failed") or False
    with pytest.raises(runtime["HostCommandError"]) as caught:
        runtime["_paint_shape"](image, shape_arguments())
    message = str(caught.value)
    assert "failed to paint the shape" in message
    assert "channel 41 is retained for recovery" in message
    assert "failed to restore the original user context" in message
    assert "failed to paint the shape" in str(caught.value.__cause__)
    assert events == ["push", "select", "fill", "restore-failed", "pop-failed"]


def test_oversized_native_text_is_deleted_before_image_insertion(runtime, monkeypatch):
    events = []
    layer = types.SimpleNamespace(
        get_width=lambda: 500000,
        get_height=lambda: 1024,
        delete=lambda: events.append("delete") or True,
        set_name=lambda _: events.append("name") or True,
        set_offsets=lambda *_: events.append("offsets") or True,
        set_color=lambda _: events.append("color") or True,
    )
    image = types.SimpleNamespace(insert_layer=lambda *_: events.append("insert") or True)
    execute = runtime["_execute_command"]
    glob = execute.__globals__
    monkeypatch.setitem(glob, "_resolve_image", lambda _: image)
    monkeypatch.setitem(glob, "_color", lambda _: object())
    monkeypatch.setitem(glob, "_layer_info", lambda *_: {"layer_id": 7})
    glob["Gimp"].Font = types.SimpleNamespace(get_by_name=lambda _: object())
    glob["Gimp"].Unit = types.SimpleNamespace(pixel=lambda: object())
    glob["Gimp"].TextLayer = types.SimpleNamespace(new=lambda *_: layer)
    glob["Gimp"].displays_flush = lambda: events.append("flush")
    with pytest.raises(runtime["HostCommandError"], match="pixel limit"):
        execute("gimp.create_text", {"image_id": 1, "name": "Text", "text": "Bounded"})
    assert events == ["delete"]


def test_masked_export_is_rejected_before_path_or_native_allocation(runtime, monkeypatch):
    execute = runtime["_execute_command"]
    glob = execute.__globals__
    layer = types.SimpleNamespace(is_group_layer=lambda: False, get_mask=lambda: object())
    monkeypatch.setitem(glob, "_resolve_image", lambda _: object())
    monkeypatch.setitem(glob, "_resolve_layer", lambda *_: layer)

    def unexpected(*_args):
        raise AssertionError("Masked export must be rejected before file or native allocation")

    monkeypatch.setitem(glob, "_output_path", unexpected)
    glob["Gimp"].Image = types.SimpleNamespace(new_with_precision=unexpected)
    with pytest.raises(runtime["HostCommandError"], match="masked"):
        execute("gimp.export_layer", {"image_id": 1, "layer_id": 7, "path": "layer.png"})


def setup_owned_layer(runtime, monkeypatch, failure=None, outcome="false", cleanup="true"):
    """Model one new native item alongside a user-owned layer that must survive."""
    events = []
    user = types.SimpleNamespace(
        name="Existing user layer", pixels=b"existing pixels", visible=False, offset=(9, 13)
    )
    state = {"layers": [user], "attached": False, "deleted": False}
    layer = types.SimpleNamespace(get_width=lambda: 48, get_height=lambda: 24)

    def operation(name):
        events.append(name)
        if name != failure:
            return True
        if outcome == "raise":
            raise RuntimeError(name + " original sentinel")
        return False

    def insert(item, _parent, _position):
        assert item is layer
        if not operation("insert"):
            return False
        state["layers"].append(item)
        state["attached"] = True
        return True

    def clean(name, item):
        assert item is layer
        events.append(name)
        assert not state["deleted"]
        if name == "delete":
            assert not state["attached"], "Never delete an item owned by its image"
        else:
            assert state["attached"], "Only remove the new item after insertion succeeded"
        if cleanup == "raise":
            raise OSError(name + " cleanup sentinel")
        if cleanup == "false":
            return False
        if name == "remove":
            state["layers"].remove(item)
            state["attached"] = False
        else:
            state["deleted"] = True
        return True

    layer.delete = lambda: clean("delete", layer)
    layer.set_name = lambda _: operation("name")
    layer.set_offsets = lambda *_: operation("offsets")
    layer.set_color = lambda _: operation("color")
    image = types.SimpleNamespace(
        insert_layer=insert, remove_layer=lambda item: clean("remove", item)
    )
    execute = runtime["_execute_command"]
    glob = execute.__globals__
    monkeypatch.setitem(glob, "_resolve_image", lambda _: image)
    monkeypatch.setitem(glob, "_color", lambda _: object())
    monkeypatch.setitem(
        glob, "_layer_info", lambda *_: events.append("readback") or {"layer_id": 7}
    )

    def allocate(*_args):
        events.append("allocate")
        return None if failure == "allocate" else layer

    gimp = glob["Gimp"]
    gimp.GroupLayer = types.SimpleNamespace(new=allocate)
    gimp.TextLayer = types.SimpleNamespace(new=allocate)
    gimp.Font = types.SimpleNamespace(get_by_name=lambda _: object())
    gimp.Unit = types.SimpleNamespace(pixel=lambda: object())
    gimp.displays_flush = lambda: events.append("flush")
    return execute, events, state, user, layer


def assert_user_layer_preserved(state, user):
    assert state["layers"][0] is user
    assert vars(user) == {
        "name": "Existing user layer",
        "pixels": b"existing pixels",
        "visible": False,
        "offset": (9, 13),
    }


def assert_owned_cleanup_error(runtime, caught, failure, outcome, cleanup):
    error = caught.value
    if cleanup == "true":
        if outcome == "raise":
            assert isinstance(error, RuntimeError)
            assert str(error) == failure + " original sentinel"
        else:
            assert isinstance(error, runtime["HostCommandError"])
        return
    assert isinstance(error, runtime["HostCommandError"])
    assert error.__cause__ is not None
    message = str(error).lower()
    assert "cleanup" in message
    if outcome == "raise":
        assert isinstance(error.__cause__, RuntimeError)
        assert failure + " original sentinel" in str(error.__cause__)
        assert failure + " original sentinel" in str(error)
    else:
        assert isinstance(error.__cause__, runtime["HostCommandError"])
        assert str(error.__cause__) in str(error)
    if cleanup == "raise":
        assert "cleanup sentinel" in str(error)
    else:
        assert "false" in message


@pytest.mark.parametrize("outcome", ["false", "raise"])
@pytest.mark.parametrize("cleanup", ["true", "false", "raise"])
def test_failed_group_insertion_cleans_only_new_detached_group(
    runtime, monkeypatch, outcome, cleanup
):
    execute, events, state, user, _layer = setup_owned_layer(
        runtime, monkeypatch, "insert", outcome, cleanup
    )
    with pytest.raises((runtime["HostCommandError"], RuntimeError)) as caught:
        execute("gimp.create_group", {"image_id": 1, "name": "New group"})
    assert events == ["allocate", "insert", "delete"]
    assert state["layers"] == [user]
    assert state["attached"] is False
    assert state["deleted"] is (cleanup == "true")
    assert_user_layer_preserved(state, user)
    assert_owned_cleanup_error(runtime, caught, "insert", outcome, cleanup)


@pytest.mark.parametrize(
    ("failure", "outcome", "cleanup"),
    [
        ("offsets", "false", "true"),
        ("offsets", "raise", "true"),
        ("color", "false", "true"),
        ("color", "raise", "true"),
        ("offsets", "false", "false"),
        ("offsets", "raise", "raise"),
        ("color", "false", "raise"),
        ("color", "raise", "false"),
    ],
)
def test_failed_text_setup_removes_only_new_inserted_text(
    runtime, monkeypatch, failure, outcome, cleanup
):
    execute, events, state, user, layer = setup_owned_layer(
        runtime, monkeypatch, failure, outcome, cleanup
    )
    with pytest.raises((runtime["HostCommandError"], RuntimeError)) as caught:
        execute("gimp.create_text", {"image_id": 1, "name": "Text", "text": "Bounded"})
    expected = ["allocate", "name", "insert", "offsets"]
    if failure == "color":
        expected.append("color")
    assert events == expected + ["remove"]
    assert state["deleted"] is False
    assert state["attached"] is (cleanup != "true")
    assert state["layers"] == ([user] if cleanup == "true" else [user, layer])
    assert_user_layer_preserved(state, user)
    assert_owned_cleanup_error(runtime, caught, failure, outcome, cleanup)


@pytest.mark.parametrize("outcome", ["false", "raise"])
def test_failed_text_insertion_deletes_detached_text_without_removing_user_layer(
    runtime, monkeypatch, outcome
):
    execute, events, state, user, _layer = setup_owned_layer(
        runtime, monkeypatch, "insert", outcome
    )
    with pytest.raises((runtime["HostCommandError"], RuntimeError)) as caught:
        execute("gimp.create_text", {"image_id": 1, "name": "Text", "text": "Bounded"})
    assert events == ["allocate", "name", "insert", "delete"]
    assert state["layers"] == [user]
    assert state["attached"] is False and state["deleted"] is True
    assert_user_layer_preserved(state, user)
    assert_owned_cleanup_error(runtime, caught, "insert", outcome, "true")


@pytest.mark.parametrize("kind", ["group", "text"])
def test_missing_native_allocation_never_inserts_or_cleans_another_layer(
    runtime, monkeypatch, kind
):
    execute, events, state, user, _layer = setup_owned_layer(runtime, monkeypatch, "allocate")
    params = {"image_id": 1, "name": "New"}
    if kind == "text":
        params["text"] = "Bounded"
    with pytest.raises(runtime["HostCommandError"]):
        execute("gimp.create_" + kind, params)
    assert events == ["allocate"]
    assert state["layers"] == [user]
    assert state["attached"] is False and state["deleted"] is False
    assert_user_layer_preserved(state, user)


@pytest.mark.parametrize("kind", ["group", "text"])
def test_successful_new_layer_stays_owned_and_text_color_follows_insertion(
    runtime, monkeypatch, kind
):
    execute, events, state, user, layer = setup_owned_layer(runtime, monkeypatch)
    params = {"image_id": 1, "name": "New"}
    if kind == "text":
        params["text"] = "Bounded"
    result = execute("gimp.create_" + kind, params)
    expected = ["allocate", "insert", "flush", "readback"]
    if kind == "text":
        expected = ["allocate", "name", "insert", "offsets", "color", "flush", "readback"]
    assert events == expected
    assert result["layer_id"] == 7
    assert state["layers"] == [user, layer]
    assert state["attached"] is True and state["deleted"] is False
    assert_user_layer_preserved(state, user)
