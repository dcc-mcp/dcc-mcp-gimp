"""Metadata byte fidelity without starting a GIMP host."""

import hashlib
import types

import pytest
from test_plugin_runtime import runtime as runtime


def parasite_item(raw, representation="signed"):
    data = ([value if value < 128 else value - 256 for value in raw]
            if representation == "signed" else raw)
    parasite = types.SimpleNamespace(get_data=lambda: data, get_flags=lambda: 3)
    return types.SimpleNamespace(
        get_parasite_list=lambda: ["fixture-metadata"],
        get_parasite=lambda _name: parasite,
    )


@pytest.mark.parametrize("raw", [b"", b"ASCII\0", "Δt − ²".encode(), bytes(range(256))])
@pytest.mark.parametrize("representation", ["signed", "bytes"])
def test_parasite_report_preserves_exact_binary_data(runtime, raw, representation):
    report = runtime["_parasite_report"](parasite_item(raw, representation))
    assert report == [{
        "name": "fixture-metadata", "flags": 3, "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "text": raw.decode("utf-8", errors="replace"), "truncated": False,
    }]


def test_metadata_report_preserves_image_and_layer_parasites(runtime, monkeypatch):
    raw = "Δt = 220 exp(−q)".encode()
    image = parasite_item(raw)
    image.get_metadata = lambda: types.SimpleNamespace(serialize=lambda: "<metadata/>")
    layer = parasite_item(bytes(range(256)))
    globals_ = runtime["_metadata_report"].__globals__
    monkeypatch.setitem(globals_, "_walk_layers", lambda _image: [{"name": "Text", "layer_id": 7}])
    monkeypatch.setitem(globals_, "_resolve_layer", lambda _image, _layer_id: layer)
    report = runtime["_metadata_report"](image)
    assert report["metadata_xml"] == "<metadata/>"
    assert report["metadata_xml_truncated"] is False
    assert report["image_parasites"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["image_parasites"][0]["text"] == raw.decode()
    assert report["layer_parasites"][0]["name"] == "Text"
    assert report["layer_parasites"][0]["parasites"][0]["bytes"] == 256
    assert report["layer_parasites"][0]["parasites"][0]["sha256"] == hashlib.sha256(
        bytes(range(256))
    ).hexdigest()


def test_parasite_preview_stays_bounded_while_hash_covers_full_data(runtime):
    raw = b"a" * 16383 + "Δ".encode() + b"\xff" * 200
    report = runtime["_parasite_report"](parasite_item(raw))[0]
    assert report["bytes"] == len(raw)
    assert report["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["text"] == "a" * 16383 + "\ufffd"
    assert report["truncated"] is True


@pytest.mark.parametrize("invalid", [-129, 256, -65536, 65536, 1.0, "1", None, True, False])
def test_parasite_report_rejects_invalid_types_and_byte_domains(runtime, invalid):
    parasite = types.SimpleNamespace(get_data=lambda: [65, invalid, 66], get_flags=lambda: 1)
    item = types.SimpleNamespace(
        get_parasite_list=lambda: ["fixture-invalid"],
        get_parasite=lambda _name: parasite,
    )
    with pytest.raises(runtime["HostCommandError"], match="outside the byte domain"):
        runtime["_parasite_report"](item)


def test_parasite_report_rejects_integral_like_objects_without_coercion(runtime):
    class IntegralLike:
        def __int__(self):
            raise AssertionError("Must not coerce arbitrary values")

        def __index__(self):
            raise AssertionError("Must not coerce arbitrary values")

    parasite = types.SimpleNamespace(get_data=lambda: [IntegralLike()], get_flags=lambda: 1)
    item = types.SimpleNamespace(
        get_parasite_list=lambda: ["fixture-invalid"],
        get_parasite=lambda _name: parasite,
    )
    with pytest.raises(runtime["HostCommandError"], match="outside the byte domain"):
        runtime["_parasite_report"](item)
