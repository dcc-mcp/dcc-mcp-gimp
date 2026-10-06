"""Effective ICC profile byte identity without starting a GIMP host."""

import hashlib
import types

import pytest
from test_plugin_runtime import runtime as runtime  # noqa: F401


def icc_profile(payload, label="sRGB IEC61966-2.1", representation="signed"):
    if representation == "signed":
        data = [
            value if type(value) is not int or not 0 <= value <= 255 else
            (value if value < 128 else value - 256)
            for value in payload
        ]
    else:
        data = payload
    return types.SimpleNamespace(
        get_icc_profile=lambda: data,
        get_label=lambda: label,
    )


def icc_image(effective, stored=None, effective_error=None, stored_error=None):
    def get_effective_color_profile():
        if effective_error is not None:
            raise effective_error
        return effective

    def get_color_profile():
        if stored_error is not None:
            raise stored_error
        return stored

    return types.SimpleNamespace(
        get_effective_color_profile=get_effective_color_profile,
        get_color_profile=get_color_profile,
    )


def test_effective_icc_identity_reports_bytes_and_digest(runtime):
    payload = b"\x00\x00\x02\x0cmntrRGB XYZ " + bytes(range(200))
    identity = runtime["_effective_icc_identity"](icc_image(icc_profile(payload)))
    assert identity == {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "explicitly_stored": False,
        "label": "sRGB IEC61966-2.1",
    }


def test_effective_icc_identity_distinguishes_builtin_from_stored_profile(runtime):
    payload = b"built-in-sRGB"
    builtin = runtime["_effective_icc_identity"](
        icc_image(icc_profile(payload), stored=None)
    )
    stored = runtime["_effective_icc_identity"](
        icc_image(icc_profile(payload), stored=icc_profile(payload))
    )
    assert builtin["explicitly_stored"] is False
    assert stored["explicitly_stored"] is True
    assert builtin["sha256"] == stored["sha256"]


def test_effective_icc_identity_separates_equal_labels_with_different_bytes(runtime):
    left = runtime["_effective_icc_identity"](
        icc_image(icc_profile(b"AAAAAAAAAAAA", label="Same Label"))
    )
    right = runtime["_effective_icc_identity"](
        icc_image(icc_profile(b"BBBBBBBBBBBB", label="Same Label"))
    )
    assert left["label"] == right["label"]
    assert left["bytes"] == right["bytes"]
    assert left["sha256"] != right["sha256"]


@pytest.mark.parametrize("representation", ["signed", "bytes"])
def test_effective_icc_identity_preserves_signed_byte_values(runtime, representation):
    payload = bytes(range(256))
    identity = runtime["_effective_icc_identity"](
        icc_image(icc_profile(payload, representation=representation))
    )
    assert identity["sha256"] == hashlib.sha256(payload).hexdigest()
    assert identity["bytes"] == 256


@pytest.mark.parametrize("invalid", [-129, 256, -65536, 65536, 1.0, "1", None, True, False])
def test_effective_icc_identity_rejects_invalid_byte_domains(runtime, invalid):
    image = icc_image(icc_profile([65, invalid, 66]))
    with pytest.raises(runtime["HostCommandError"], match="outside the byte domain"):
        runtime["_effective_icc_identity"](image)


def test_effective_icc_identity_rejects_integral_like_objects_without_coercion(runtime):
    class IntegralLike:
        def __int__(self):
            raise AssertionError("Must not coerce arbitrary values")

        def __index__(self):
            raise AssertionError("Must not coerce arbitrary values")

    image = icc_image(types.SimpleNamespace(
        get_icc_profile=lambda: [IntegralLike()],
        get_label=lambda: "Broken",
    ))
    with pytest.raises(runtime["HostCommandError"], match="outside the byte domain"):
        runtime["_effective_icc_identity"](image)


@pytest.mark.parametrize("missing", [None])
def test_effective_icc_identity_rejects_missing_native_profile(runtime, missing):
    with pytest.raises(runtime["HostCommandError"], match="no effective color profile"):
        runtime["_effective_icc_identity"](icc_image(missing))


def test_effective_icc_identity_rejects_oversize_before_materializing(runtime, monkeypatch):
    """The size bound must reject the payload before it is copied into bytes.

    A lazy source proves this: it can report a length and fail loudly if any
    element is consumed, so an implementation that materializes before checking
    the limit cannot pass. The mutant that checks the limit only after building
    the bytearray fails here.
    """
    class LazyOversize:
        def __init__(self, length):
            self._length = length

        def __len__(self):
            return self._length

        def __iter__(self):
            raise AssertionError("Oversized payload must be rejected before iteration")

    monkeypatch.setitem(
        runtime["_effective_icc_identity"].__globals__, "MAX_ICC_PROFILE_BYTES", 64
    )
    image = icc_image(types.SimpleNamespace(
        get_icc_profile=lambda: LazyOversize(65),
        get_label=lambda: "Oversize",
    ))
    with pytest.raises(runtime["HostCommandError"], match="exceeds the 64 byte"):
        runtime["_effective_icc_identity"](image)


def test_read_gchar_bytes_rejects_oversized_payload_without_iterating(runtime, monkeypatch):
    class LazyOversize:
        def __len__(self):
            return 1_000_000

        def __iter__(self):
            raise AssertionError("Oversized payload must be rejected before iteration")

    with pytest.raises(runtime["HostCommandError"], match="exceeds the 64 byte"):
        runtime["_read_gchar_bytes"](LazyOversize(), "fixture data", 64)


@pytest.mark.parametrize("unusable", [42, None, object()])
def test_read_gchar_bytes_wraps_unusable_native_data(runtime, unusable):
    with pytest.raises(runtime["HostCommandError"], match="unusable fixture data"):
        runtime["_read_gchar_bytes"](unusable, "fixture data", 64)


def test_read_gchar_bytes_wraps_iteration_failure(runtime):
    class BrokenIteration:
        def __len__(self):
            return 2

        def __iter__(self):
            raise RuntimeError("native iteration failed")

    with pytest.raises(runtime["HostCommandError"], match="unusable fixture data"):
        runtime["_read_gchar_bytes"](BrokenIteration(), "fixture data", 64)


def test_effective_icc_identity_rejects_empty_payload(runtime):
    with pytest.raises(runtime["HostCommandError"], match="empty effective color profile"):
        runtime["_effective_icc_identity"](icc_image(icc_profile(b"")))


def test_effective_icc_identity_bounds_oversized_payload(runtime, monkeypatch):
    monkeypatch.setitem(
        runtime["_effective_icc_identity"].__globals__, "MAX_ICC_PROFILE_BYTES", 64
    )
    with pytest.raises(runtime["HostCommandError"], match="exceeds the 64 byte"):
        runtime["_effective_icc_identity"](icc_image(icc_profile(b"a" * 65)))


@pytest.mark.parametrize(
    "failure", [RuntimeError("boom"), ValueError("bad"), TypeError("wrong")]
)
def test_effective_icc_identity_wraps_native_getter_failures(runtime, failure):
    with pytest.raises(runtime["HostCommandError"], match="failed to read"):
        runtime["_effective_icc_identity"](
            icc_image(icc_profile(b"payload"), effective_error=failure)
        )
    with pytest.raises(runtime["HostCommandError"], match="failed to read"):
        runtime["_effective_icc_identity"](
            icc_image(icc_profile(b"payload"), stored_error=failure)
        )


def test_effective_icc_identity_never_returns_raw_payload_bytes(runtime):
    payload = b"secret-icc-payload"
    identity = runtime["_effective_icc_identity"](icc_image(icc_profile(payload)))
    serialized = repr(identity)
    assert payload not in serialized.encode()
    assert b"secret-icc-payload" not in serialized.encode()


def test_inspect_image_default_omits_icc_identity(runtime, monkeypatch):
    seen = {}

    def fake_identity(_image):
        seen["called"] = True
        return {"bytes": 1, "sha256": "x", "explicitly_stored": True, "label": "y"}

    image = icc_image(icc_profile(b"payload"))
    image.get_id = lambda: 1
    image.get_name = lambda: "Fixture"
    image.get_width = lambda: 2
    image.get_height = lambda: 2
    image.get_base_type = lambda: "rgb"
    image.get_precision = lambda: "u8"
    image.is_dirty = lambda: False
    image.get_file = lambda: None
    image.get_imported_file = lambda: None
    image.get_exported_file = lambda: None
    image.get_selected_layers = lambda: []
    globals_ = runtime["_execute_command"].__globals__
    monkeypatch.setitem(globals_, "_resolve_image", lambda _value: image)
    monkeypatch.setitem(globals_, "_walk_layers", lambda _image: [])
    monkeypatch.setitem(globals_, "_effective_icc_identity", fake_identity)
    result = runtime["_execute_command"]("gimp.inspect_image", {"image_id": 1})
    assert "effective_icc" not in result
    assert seen == {}


def test_inspect_image_opt_in_reports_icc_identity(runtime, monkeypatch):
    image = icc_image(icc_profile(b"payload"), stored=icc_profile(b"payload"))
    image.get_id = lambda: 1
    image.get_name = lambda: "Fixture"
    image.get_width = lambda: 2
    image.get_height = lambda: 2
    image.get_base_type = lambda: "rgb"
    image.get_precision = lambda: "u8"
    image.is_dirty = lambda: False
    image.get_file = lambda: None
    image.get_imported_file = lambda: None
    image.get_exported_file = lambda: None
    image.get_selected_layers = lambda: []
    globals_ = runtime["_execute_command"].__globals__
    monkeypatch.setitem(globals_, "_resolve_image", lambda _value: image)
    monkeypatch.setitem(globals_, "_walk_layers", lambda _image: [])
    result = runtime["_execute_command"](
        "gimp.inspect_image", {"image_id": 1, "include_icc_identity": True}
    )
    assert result["effective_icc"] == {
        "bytes": len(b"payload"),
        "sha256": hashlib.sha256(b"payload").hexdigest(),
        "explicitly_stored": True,
        "label": "sRGB IEC61966-2.1",
    }
    assert result["color_profile"] == "sRGB IEC61966-2.1"


@pytest.mark.parametrize("invalid", [1, 0, "true", None, [], 1.0])
def test_inspect_image_rejects_untyped_icc_identity_flag(runtime, monkeypatch, invalid):
    image = icc_image(icc_profile(b"payload"))
    image.get_id = lambda: 1
    image.get_name = lambda: "Fixture"
    image.get_width = lambda: 2
    image.get_height = lambda: 2
    image.get_base_type = lambda: "rgb"
    image.get_precision = lambda: "u8"
    image.is_dirty = lambda: False
    image.get_file = lambda: None
    image.get_imported_file = lambda: None
    image.get_exported_file = lambda: None
    image.get_selected_layers = lambda: []
    globals_ = runtime["_execute_command"].__globals__
    monkeypatch.setitem(globals_, "_resolve_image", lambda _value: image)
    monkeypatch.setitem(globals_, "_walk_layers", lambda _image: [])
    with pytest.raises(runtime["HostCommandError"], match="include_icc_identity must be a boolean"):
        runtime["_execute_command"](
            "gimp.inspect_image", {"image_id": 1, "include_icc_identity": invalid}
        )
