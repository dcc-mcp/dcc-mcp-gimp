"""Native-copy export contract exercised without loading a GIMP host."""

import copy
import json
import struct
import types
from pathlib import Path

import jsonschema
import pytest
from dcc_mcp_core.skills_helper import yaml_loads
from test_plugin_runtime import runtime as runtime


def setup_preview(runtime, monkeypatch, tmp_path, dimensions=(2560, 1600), failure=None):
    execute = runtime["_execute_command"]
    glob = execute.__globals__
    events = []
    state = {
        "dimensions": dimensions,
        "layers": ["group", "editable text"],
        "metadata": "utf8 Δ",
        "selection": b"\x00\xff",
        "dirty": True,
        "selected": [7],
    }
    source_state = copy.deepcopy(state)
    temp_dimensions = list(dimensions)
    interpolation = ["cubic"]
    images = []

    def event(name, result=True):
        events.append(name)
        if failure == name:
            raise RuntimeError("private-path and private-metadata SHOULD_NOT_LEAK")
        return result

    def scale(width, height):
        event("scale")
        temp_dimensions[:] = [width, height]
        return True

    temporary = types.SimpleNamespace(
        get_id=lambda: 2,
        get_width=lambda: temp_dimensions[0],
        get_height=lambda: temp_dimensions[1],
        scale=scale,
    )

    def delete():
        event("delete")
        images.remove(temporary)
        return True

    def duplicate():
        event("duplicate")
        images.append(temporary)
        return temporary

    source = types.SimpleNamespace(get_id=lambda: 1, duplicate=duplicate)
    images.append(source)
    temporary.delete = delete
    gimp = glob["Gimp"]
    gimp.get_images = lambda: images
    gimp.context_get_interpolation = lambda: interpolation[0]
    gimp.context_push = lambda: event("push")

    def pop():
        event("pop")
        interpolation[0] = "cubic"
        return True

    def set_interpolation(value):
        event("interpolation")
        interpolation[0] = value
        return True

    def save(_run, image, file, _options):
        assert image is temporary
        # Even failure is allowed to leave a partial staged output, never final.
        Path(file).write_bytes(b"partial")
        event("save")
        width, height = temp_dimensions
        Path(file).write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I4sIIBBBBBI", 13, b"IHDR", width, height, 8, 6, 0, 0, 0, 0)
        )
        if failure == "source-change":
            state["dirty"] = False
        return True

    gimp.context_pop = pop
    gimp.context_set_interpolation = set_interpolation
    gimp.InterpolationType = types.SimpleNamespace(NOHALO="nohalo")
    gimp.RunMode = types.SimpleNamespace(NONINTERACTIVE=0)
    gimp.file_save = save
    glob["Gio"].File = types.SimpleNamespace(new_for_path=lambda path: path)
    monkeypatch.setitem(glob, "_resolve_image", lambda _id: source)
    monkeypatch.setitem(glob, "_preview_admit_source", lambda _image: dimensions)
    monkeypatch.setitem(glob, "_preview_state", lambda _image: copy.deepcopy(state))
    monkeypatch.setenv("DCC_MCP_GIMP_ALLOWED_ROOTS", str(tmp_path))
    args = {
        "image_id": 1,
        "path": str(tmp_path / "preview.png"),
        "max_width": 1600,
        "max_height": 1600,
    }
    return execute, args, events, state, source_state, images, interpolation


@pytest.mark.parametrize(
    "source,box,expected",
    [
        ((2560, 1600), (1600, 1600), (1600, 1000)),
        ((1600, 2560), (1600, 1600), (1000, 1600)),
        ((2561, 1600), (1600, 1600), (1600, 999)),
        ((7, 13), (5, 6), (3, 6)),
        ((1, 8192), (2048, 1), (1, 1)),
        ((300, 200), (2048, 2048), (300, 200)),
    ],
)
def test_integer_aspect_fit_without_upscale(runtime, source, box, expected):
    assert runtime["_preview_dimensions"](*source, *box) == expected


@pytest.mark.parametrize(
    "dimensions,expected,scaled",
    [
        ((2560, 1600), [1600, 1000], True),
        ((300, 200), [300, 200], False),
    ],
)
def test_success_cleanup_before_publish_and_source_unchanged(
    runtime,
    monkeypatch,
    tmp_path,
    dimensions,
    expected,
    scaled,
):
    execute, args, events, state, before, images, interpolation = setup_preview(
        runtime,
        monkeypatch,
        tmp_path,
        dimensions,
    )
    glob = execute.__globals__
    native_link = glob["os"].link

    def publish(*args, **kwargs):
        assert events[-2:] == ["delete", "pop"]
        assert len(images) == 1 and state == before and interpolation == ["cubic"]
        events.append("publish")
        return native_link(*args, **kwargs)

    monkeypatch.setattr(glob["os"], "link", publish)
    result = execute("gimp.export_preview", args)
    assert result["output_dimensions"] == expected
    assert result["source_dimensions"] == list(dimensions)
    assert result["source_image_mutated"] is False
    assert result["context_restored"] is True and result["temporary_image_deleted"] is True
    assert ("scale" in events) is scaled
    assert events[-1] == "publish"
    assert state == before and len(images) == 1 and interpolation == ["cubic"]
    assert [p.name for p in tmp_path.iterdir()] == ["preview.png"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_width", 0),
        ("max_width", 2049),
        ("max_width", True),
        ("max_width", 3.2),
        ("max_height", 0),
        ("max_height", 2049),
        ("max_height", "1"),
        ("overwrite", "false"),
        ("interpolation", "linear"),
        ("timeout_secs", 5),
        ("path", ""),
        ("path", "preview.jpg"),
    ],
)
def test_invalid_arguments_fail_before_duplicate(runtime, monkeypatch, tmp_path, field, value):
    execute, args, events, *_ = setup_preview(runtime, monkeypatch, tmp_path)
    args[field] = value
    with pytest.raises(runtime["HostCommandError"]):
        execute("gimp.export_preview", args)
    assert events == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "failure", ["push", "interpolation", "duplicate", "scale", "save", "source-change"]
)
def test_failure_never_publishes_and_removes_staging(runtime, monkeypatch, tmp_path, failure):
    execute, args, events, _, _, images, interpolation = setup_preview(
        runtime,
        monkeypatch,
        tmp_path,
        failure=failure,
    )
    with pytest.raises(runtime["HostCommandError"]) as caught:
        execute("gimp.export_preview", args)
    assert "SHOULD_NOT_LEAK" not in str(caught.value)
    assert len(images) == 1 and interpolation == ["cubic"]
    assert list(tmp_path.iterdir()) == []
    if failure in {"scale", "save", "source-change"}:
        assert events[-2:] == ["delete", "pop"]


@pytest.mark.parametrize("failure", ["delete", "pop"])
def test_cleanup_failure_blocks_publication_and_attempts_remaining_cleanup(
    runtime,
    monkeypatch,
    tmp_path,
    failure,
):
    execute, args, events, _, _, _, interpolation = setup_preview(
        runtime,
        monkeypatch,
        tmp_path,
        failure=failure,
    )
    with pytest.raises(runtime["HostCommandError"], match="cleanup failed"):
        execute("gimp.export_preview", args)
    assert "delete" in events and "pop" in events
    assert interpolation == ["cubic"]
    assert list(tmp_path.iterdir()) == []


def test_no_overwrite_and_atomic_late_collision(runtime, monkeypatch, tmp_path):
    execute, args, events, *_ = setup_preview(runtime, monkeypatch, tmp_path)
    target = Path(args["path"])
    target.write_bytes(b"existing")
    with pytest.raises(runtime["HostCommandError"], match="exists"):
        execute("gimp.export_preview", args)
    assert events == [] and target.read_bytes() == b"existing"
    target.unlink()
    original_link = execute.__globals__["os"].link

    def collide(source, destination, **kwargs):
        target.write_bytes(b"concurrent")
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr(execute.__globals__["os"], "link", collide)
    with pytest.raises(runtime["HostCommandError"]):
        execute("gimp.export_preview", args)
    assert target.read_bytes() == b"concurrent"
    assert len(list(tmp_path.iterdir())) == 1


def test_explicit_overwrite_is_staged(runtime, monkeypatch, tmp_path):
    execute, args, events, *_ = setup_preview(runtime, monkeypatch, tmp_path, failure="save")
    target = Path(args["path"])
    target.write_bytes(b"existing")
    args["overwrite"] = True
    with pytest.raises(runtime["HostCommandError"]):
        execute("gimp.export_preview", args)
    assert target.read_bytes() == b"existing"
    execute, args, *_ = setup_preview(runtime, monkeypatch, tmp_path)
    args["overwrite"] = True
    execute("gimp.export_preview", args)
    assert target.read_bytes().startswith(b"\x89PNG")


@pytest.mark.parametrize("kind", ["target", "parent", "outside"])
def test_rejects_links_and_outside_root_before_native_work(runtime, monkeypatch, tmp_path, kind):
    execute, args, events, *_ = setup_preview(runtime, monkeypatch, tmp_path)
    target = tmp_path / "other.png"
    target.write_bytes(b"untouched")
    if kind == "target":
        Path(args["path"]).symlink_to(target)
    elif kind == "parent":
        (tmp_path / "linked").symlink_to(tmp_path, target_is_directory=True)
        args["path"] = str(tmp_path / "linked/preview.png")
    else:
        args["path"] = str(tmp_path.parent / "outside.png")
    with pytest.raises(runtime["HostCommandError"]):
        execute("gimp.export_preview", args)
    assert events == [] and target.read_bytes() == b"untouched"


def test_source_admission_pixel_layers_precision_and_native_args(runtime):
    glob = runtime["_preview_admit_source"].__globals__
    glob["Gimp"].ImageBaseType = types.SimpleNamespace(RGB="rgb")
    glob["Gimp"].Precision = types.SimpleNamespace(U8_NON_LINEAR="u8")
    layer = types.SimpleNamespace(
        get_width=lambda: 2560,
        get_height=lambda: 1600,
        get_mask=lambda: None,
        get_parasite_list=lambda: [],
        get_filters=lambda: [],
        is_group_layer=lambda: False,
    )
    image = types.SimpleNamespace(
        get_width=lambda: 2560,
        get_height=lambda: 1600,
        get_base_type=lambda: "rgb",
        get_precision=lambda: "u8",
        get_channels=lambda: [],
        get_parasite_list=lambda: [],
        get_selection=lambda: types.SimpleNamespace(
            get_parasite_list=lambda: [], get_filters=lambda: []
        ),
        get_paths=lambda: [],
        get_floating_sel=lambda: None,
        get_layers=lambda: [layer],
    )
    admit = runtime["_preview_admit_source"]
    assert admit(image) == (2560, 1600)
    for attribute, value in [
        ("get_width", 8193),
        ("get_width", 8192),
        ("get_precision", "half"),
        ("get_base_type", "indexed"),
        ("get_channels", [1]),
        ("get_paths", [1]),
        ("get_floating_sel", object()),
        ("get_layers", [layer] * 257),
        ("get_layers", [layer] * 33),
    ]:
        previous = getattr(image, attribute)
        setattr(image, attribute, lambda value=value: value)
        if attribute == "get_width" and value == 8192:
            old_height = image.get_height
            image.get_height = lambda: 8192
        with pytest.raises(runtime["HostCommandError"]):
            admit(image)
        if attribute == "get_width" and value == 8192:
            image.get_height = old_height
        setattr(image, attribute, previous)


def test_closed_schema_has_no_script_or_interpolation_surface():
    catalog = Path(__file__).parents[1] / "src/dcc_mcp_gimp/skills/gimp-session/tools.yaml"
    tools = yaml_loads(catalog.read_text(encoding="utf-8"))["tools"]
    schema = next(t["input_schema"] for t in tools if t["name"] == "export_preview")
    assert set(schema["properties"]) == {"image_id", "path", "max_width", "max_height", "overwrite"}
    valid = {"image_id": 1, "path": "preview.png", "max_width": 1600, "max_height": 1600}
    jsonschema.validate(valid, schema)
    for extra in ({"max_width": 2049}, {"script": "print(1)"}, {"overwrite": "false"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**valid, **extra}, schema)
    assert json.loads(json.dumps(schema)) == schema


def test_source_state_fingerprint_covers_nonvisual_text_hidden_pixels_masks_and_compositing(
    runtime,
    monkeypatch,
):
    glob = runtime["_preview_state"].__globals__
    state = {
        "dirty": True,
        "selection": b"\x00\x80",
        "metadata": "α",
        "text": "editable",
        "selected": [7],
        "images": [1],
        "rgba": b"\x00\x00\x00\xff" * 2,
        "mask": b"\xff\x80",
        "mask_present": True,
        "mask_apply": True,
        "mask_show": False,
        "mask_edit": False,
        "mask_parasites": [],
        "selection_parasites": [],
        "blend_space": "rgb-linear",
        "composite_space": "rgb-nonlinear",
        "composite_mode": "auto",
        "font_size": 12.0,
        "markup": "<span>editable</span>",
        "icc": b"profile-a",
        "component_visible": True,
        "component_active": True,
    }

    def drawable(kind, channels):
        return types.SimpleNamespace(
            kind=kind,
            get_width=lambda: 2,
            get_height=lambda: 1,
            get_buffer=lambda: types.SimpleNamespace(get=lambda *_: state[kind]),
        )

    mask = drawable("mask", 1)
    mask.get_id = lambda: 8
    mask.get_offsets = lambda: (True, 0, 0)
    selection = drawable("selection", 1)
    layer = drawable("rgba", 4)
    layer.get_mask = lambda: mask if state["mask_present"] else None
    for name, key in [
        ("get_apply_mask", "mask_apply"),
        ("get_show_mask", "mask_show"),
        ("get_edit_mask", "mask_edit"),
        ("get_blend_space", "blend_space"),
        ("get_composite_space", "composite_space"),
        ("get_composite_mode", "composite_mode"),
    ]:
        setattr(layer, name, lambda key=key: state[key])
    layer.is_text_layer = lambda: True
    layer.get_font_size = lambda: (state["font_size"], types.SimpleNamespace(get_id=lambda: 0))
    layer.get_markup = lambda: state["markup"]
    for name, value in [
        ("get_antialias", True),
        ("get_hint_style", "medium"),
        ("get_kerning", True),
        ("get_language", "en"),
        ("get_base_direction", "ltr"),
        ("get_justification", "left"),
        ("get_indent", 0.0),
        ("get_line_spacing", 0.0),
        ("get_letter_spacing", 0.0),
    ]:
        setattr(layer, name, lambda value=value: value)
    image = types.SimpleNamespace(
        get_metadata=lambda: types.SimpleNamespace(serialize=lambda: state["metadata"]),
        get_selection=lambda: selection,
        get_width=lambda: 2,
        get_height=lambda: 1,
        get_selected_channels=lambda: [],
        get_selected_paths=lambda: [],
        get_component_visible=lambda _: state["component_visible"],
        get_component_active=lambda _: state["component_active"],
        get_effective_color_profile=lambda: types.SimpleNamespace(
            get_icc_profile=lambda: state["icc"],
            get_label=lambda: "Fixture ICC",
        ),
        get_color_profile=lambda: None,
    )
    glob["Gegl"].Rectangle = types.SimpleNamespace(new=lambda *args: args)
    glob["Gegl"].AbyssPolicy = types.SimpleNamespace(NONE=0)
    glob["Gimp"].ChannelType = types.SimpleNamespace(RED="r", GREEN="g", BLUE="b", ALPHA="a")
    glob["Gimp"].get_images = lambda: [
        types.SimpleNamespace(get_id=lambda value=x: value) for x in state["images"]
    ]
    monkeypatch.setitem(
        glob, "_image_info", lambda _: {"dirty": state["dirty"], "selected": state["selected"]}
    )
    monkeypatch.setitem(
        glob, "_walk_layers", lambda _: [{"layer_id": 7, "text": state["text"], "visible": False}]
    )
    monkeypatch.setitem(glob, "_resolve_layer", lambda *_: layer)
    monkeypatch.setitem(glob, "_parasite_report", lambda item: state[item.kind + "_parasites"])
    monkeypatch.setitem(glob, "_metadata_report", lambda _: {"metadata_xml": state["metadata"]})
    fingerprint = runtime["_preview_state"]
    before = fingerprint(image)
    assert len(before) == 64
    for key, value in [
        ("dirty", False),
        ("selection", b"\xff\x80"),
        ("metadata", "β"),
        ("text", "changed"),
        ("selected", [9]),
        ("images", [2, 1]),
        ("rgba", b"\xff\x00\x00\xff" * 2),
        ("rgba", b"\x00\x00\x00\x80" * 2),
        ("mask", b"\x00\x80"),
        ("mask_present", False),
        ("mask_apply", False),
        ("mask_show", True),
        ("mask_edit", True),
        ("mask_parasites", ["new"]),
        ("selection_parasites", ["new"]),
        ("font_size", 13.0),
        ("markup", "<b>editable</b>"),
        ("blend_space", "rgb-perceptual"),
        ("composite_space", "rgb-linear"),
        ("composite_mode", "clip-to-backdrop"),
        ("component_visible", False),
        ("component_active", False),
        ("icc", b"profile-b"),
    ]:
        original = state[key]
        state[key] = value
        assert fingerprint(image) != before, key
        state[key] = original
    assert fingerprint(image) == before


def test_native_pixel_hash_streams_bounded_strips_and_checks_byte_lengths(runtime):
    glob = runtime["_preview_buffer_hash"].__globals__
    glob["Gegl"].Rectangle = types.SimpleNamespace(new=lambda *args: args)
    glob["Gegl"].AbyssPolicy = types.SimpleNamespace(NONE=0)
    calls = []

    def read(rect, scale, pixel_format, _abyss):
        x, y, width, height = rect
        calls.append((rect, scale, pixel_format))
        return b"\x00" * (width * height * 4)

    image = types.SimpleNamespace(
        get_width=lambda: 8192,
        get_height=lambda: 100,
        get_buffer=lambda: types.SimpleNamespace(get=read),
    )
    assert len(runtime["_preview_buffer_hash"](image, "R'G'B'A u8", 4)) == 64
    assert len(calls) > 1 and all(c[0][2] * c[0][3] * 4 <= 1024 * 1024 for c in calls)
    assert all(c[1] == 1.0 for c in calls)
    image.get_buffer = lambda: types.SimpleNamespace(get=lambda *_: b"wrong")
    with pytest.raises(runtime["HostCommandError"], match="byte length"):
        runtime["_preview_buffer_hash"](image, "R'G'B'A u8", 4)


@pytest.mark.parametrize("count", [257, 1000])
def test_parasite_admission_refuses_silent_metadata_truncation(runtime, count):
    item = types.SimpleNamespace(get_parasite_list=lambda: range(count))
    with pytest.raises(runtime["HostCommandError"], match="256 parasites"):
        runtime["_preview_parasite_admission"](item)


def test_noop_interpolation_setter_fails_before_duplication(runtime, monkeypatch, tmp_path):
    execute, args, events, *_ = setup_preview(runtime, monkeypatch, tmp_path)
    execute.__globals__["Gimp"].context_set_interpolation = lambda _: True
    with pytest.raises(runtime["HostCommandError"]):
        execute("gimp.export_preview", args)
    assert "duplicate" not in events and list(tmp_path.iterdir()) == []


def test_postpublication_cleanup_failure_reports_committed_output(runtime, monkeypatch, tmp_path):
    execute, args, events, *_ = setup_preview(runtime, monkeypatch, tmp_path)
    original = execute.__globals__["os"].rmdir

    def fail_stage(path, *args, **kwargs):
        if Path(path).name.startswith(".dcc-preview-"):
            raise OSError("private filesystem details")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(execute.__globals__["os"], "rmdir", fail_stage)
    monkeypatch.setattr(Path, "rmdir", fail_stage)
    with pytest.raises(runtime["HostCommandError"], match="output was published") as caught:
        execute("gimp.export_preview", args)
    assert "private filesystem details" not in str(caught.value)
    assert Path(args["path"]).read_bytes().startswith(b"\x89PNG")
    assert len(list(tmp_path.glob(".dcc-preview-*"))) == 1


@pytest.mark.parametrize("operation", ["scale", "save", "delete", "pop"])
def test_false_native_results_do_not_publish(runtime, monkeypatch, tmp_path, operation):
    execute, args, events, _, _, images, _ = setup_preview(runtime, monkeypatch, tmp_path)
    gimp = execute.__globals__["Gimp"]
    if operation in {"save", "pop"}:
        setattr(gimp, "file_save" if operation == "save" else "context_pop", lambda *_: False)
    else:
        original_duplicate = images[0].duplicate

        def duplicate():
            temporary = original_duplicate()
            setattr(temporary, operation, lambda *_: False)
            return temporary

        images[0].duplicate = duplicate
    with pytest.raises(runtime["HostCommandError"]):
        execute("gimp.export_preview", args)
    assert not Path(args["path"]).exists()
    assert not list(tmp_path.glob(".dcc-preview-*"))


def test_source_admission_exception_does_not_leak_raw_native_payload(
    runtime, monkeypatch, tmp_path
):
    execute, args, events, *_ = setup_preview(runtime, monkeypatch, tmp_path)

    def fail(_):
        raise RuntimeError("secret source path and metadata")

    monkeypatch.setitem(execute.__globals__, "_preview_admit_source", fail)
    with pytest.raises(runtime["HostCommandError"]) as caught:
        execute("gimp.export_preview", args)
    assert "secret" not in str(caught.value)
    assert events == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", ["parasite-257", "new-filter"])
def test_post_export_admission_rejects_new_untracked_source_state(
    runtime,
    monkeypatch,
    tmp_path,
    change,
):
    execute, args, events, _, _, images, _ = setup_preview(runtime, monkeypatch, tmp_path)
    glob = execute.__globals__
    gimp = glob["Gimp"]
    gimp.ImageBaseType = types.SimpleNamespace(RGB="rgb")
    gimp.Precision = types.SimpleNamespace(U8_NON_LINEAR="u8")
    parasites = list(range(256))
    filters = []
    layer = types.SimpleNamespace(
        get_width=lambda: 2560,
        get_height=lambda: 1600,
        get_mask=lambda: None,
        get_parasite_list=lambda: [],
        get_filters=lambda: filters,
        is_group_layer=lambda: False,
    )
    source = images[0]
    for name, function in {
        "get_width": lambda: 2560,
        "get_height": lambda: 1600,
        "get_base_type": lambda: "rgb",
        "get_precision": lambda: "u8",
        "get_channels": lambda: [],
        "get_paths": lambda: [],
        "get_floating_sel": lambda: None,
        "get_parasite_list": lambda: parasites,
        "get_layers": lambda: [layer],
        "get_selection": lambda: types.SimpleNamespace(
            get_parasite_list=lambda: [], get_filters=lambda: []
        ),
    }.items():
        setattr(source, name, function)
    monkeypatch.setitem(glob, "_preview_admit_source", runtime["_preview_admit_source"])
    original_save = gimp.file_save

    def mutate_after_export(*arguments):
        result = original_save(*arguments)
        if change == "parasite-257":
            parasites.append(256)
        else:
            filters.append(object())
        return result

    gimp.file_save = mutate_after_export
    with pytest.raises(runtime["HostCommandError"]):
        execute("gimp.export_preview", args)
    assert events[-2:] == ["delete", "pop"]
    assert len(images) == 1 and list(tmp_path.iterdir()) == []
