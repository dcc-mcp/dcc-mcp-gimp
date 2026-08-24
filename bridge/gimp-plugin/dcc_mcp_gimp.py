#!/usr/bin/env python3
"""Authenticated GIMP 3 bridge with bounded, main-thread host execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


def _bootstrap_error_path() -> Path:
    configured = os.environ.get("DCC_MCP_GIMP_BOOTSTRAP_ERRORS")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home().joinpath(".dcc-mcp", "gimp-bootstrap-errors.jsonl").resolve()


def _capture_bootstrap_error(stage: str, error: BaseException) -> None:
    path = _bootstrap_error_path()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "error_type": type(error).__name__,
        "message": str(error),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


try:
    import gi

    gi.require_version("Gegl", "0.4")
    gi.require_version("Gimp", "3.0")
    from gi.repository import Gegl, Gimp, Gio, GLib  # noqa: E402
except BaseException as exc:
    _capture_bootstrap_error("gi-import", exc)
    raise


VERSION = "0.4.0"  # x-release-please-version
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = int(os.environ.get("DCC_MCP_GIMP_BRIDGE_PORT", "3848"))
MAX_CONNECTIONS = 16
MAX_PENDING_COMMANDS = 32
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000
MAX_LAYER_NODES = 20_000
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMMAND_TIMEOUT_SECS = 1_800.0
OPEN_SUFFIXES = frozenset(
    {".xcf", ".ora", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".psd", ".exr"}
)
EXPORT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"})


class HostCommandError(RuntimeError):
    """A typed request violates the GIMP host or safety contract."""


class _PendingCommand:
    def __init__(self, method: str, params: Mapping[str, Any]) -> None:
        self.method = method
        self.params = dict(params)
        self.completed = threading.Event()
        self.lock = threading.Lock()
        self.started = False
        self.cancelled = False
        self.result: Any = None
        self.error: Optional[BaseException] = None


_pending_commands = threading.BoundedSemaphore(MAX_PENDING_COMMANDS)
_owned_displays: dict[int, Any] = {}
_bridge_token = ""


def _split_roots(value: str) -> tuple[Path, ...]:
    return tuple(
        Path(item.strip()).expanduser().resolve()
        for item in value.split(os.pathsep)
        if item.strip()
    )


def _allowed_roots() -> tuple[Path, ...]:
    return _split_roots(os.environ.get("DCC_MCP_GIMP_ALLOWED_ROOTS", ""))


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    candidate = os.path.normcase(str(path))
    for root in roots:
        normalized_root = os.path.normcase(str(root))
        try:
            if os.path.commonpath((candidate, normalized_root)) == normalized_root:
                return True
        except ValueError:
            continue
    return False


def _safe_text(value: Any, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HostCommandError("%s must be a non-empty string" % label)
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise HostCommandError("%s is invalid or exceeds %d characters" % (label, maximum))
    return value.strip()


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostCommandError("%s must be an integer" % label)
    if value < minimum or value > maximum:
        raise HostCommandError("%s must be between %d and %d" % (label, minimum, maximum))
    return value


def _bounded_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostCommandError("%s must be a number" % label)
    number = float(value)
    if number < minimum or number > maximum:
        raise HostCommandError("%s must be between %s and %s" % (label, minimum, maximum))
    return number


def _token_path() -> Path:
    configured = os.environ.get("DCC_MCP_GIMP_BRIDGE_TOKEN_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home().joinpath(".dcc-mcp", "gimp-bridge-token").resolve()


def _load_or_create_token() -> str:
    configured = os.environ.get("DCC_MCP_GIMP_BRIDGE_TOKEN", "")
    if configured:
        if len(configured) < 32:
            raise RuntimeError("DCC_MCP_GIMP_BRIDGE_TOKEN must contain at least 32 characters")
        return configured
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(token)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token
    except FileExistsError:
        for _attempt in range(10):
            try:
                existing = path.read_text(encoding="utf-8").strip()
            except OSError:
                existing = ""
            if len(existing) >= 32:
                return existing
            time.sleep(0.02)
    raise RuntimeError("GIMP bridge token file is missing, unreadable, or invalid")


def _input_path(value: Any) -> Path:
    roots = _allowed_roots()
    if not roots:
        raise HostCommandError("DCC_MCP_GIMP_ALLOWED_ROOTS is required for file access")
    path = Path(_safe_text(value, "path", 2_048)).expanduser().resolve()
    if not _within(path, roots):
        raise HostCommandError("Input path is outside DCC_MCP_GIMP_ALLOWED_ROOTS")
    if path.suffix.lower() not in OPEN_SUFFIXES:
        raise HostCommandError("Input file type is not supported by the adapter")
    if not path.is_file():
        raise HostCommandError("Input file does not exist")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise HostCommandError("Input file exceeds the configured size limit")
    return path


def _output_path(value: Any, suffixes: frozenset[str], overwrite: bool) -> Path:
    roots = _allowed_roots()
    if not roots:
        raise HostCommandError("DCC_MCP_GIMP_ALLOWED_ROOTS is required for file access")
    path = Path(_safe_text(value, "path", 2_048)).expanduser().resolve()
    if not _within(path, roots):
        raise HostCommandError("Output path is outside DCC_MCP_GIMP_ALLOWED_ROOTS")
    if path.suffix.lower() not in suffixes:
        raise HostCommandError("Output file type is not supported by the adapter")
    if not path.parent.is_dir():
        raise HostCommandError("Output parent directory does not exist")
    if path.exists() and not overwrite:
        raise HostCommandError("Output exists; set overwrite=true to replace it")
    if path.exists() and not path.is_file():
        raise HostCommandError("Output path is not a regular file")
    return path


def _file_digest(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _enum_name(value: Any) -> str:
    for attribute in ("value_nick", "value_name"):
        name = getattr(value, attribute, None)
        if name:
            return str(name)
    return str(value)


def _safe_image_path(image: Any) -> tuple[Optional[str], bool]:
    file_object = image.get_file() or image.get_imported_file() or image.get_exported_file()
    if file_object is None:
        return None, False
    raw = file_object.get_path()
    if not raw:
        return None, False
    path = Path(raw).expanduser().resolve()
    allowed = bool(_allowed_roots()) and _within(path, _allowed_roots())
    return (str(path) if allowed else path.name), allowed


def _image_info(image: Any) -> dict[str, Any]:
    path, path_allowed = _safe_image_path(image)
    selected = list(image.get_selected_layers())
    return {
        "image_id": int(image.get_id()),
        "name": str(image.get_name()),
        "width": int(image.get_width()),
        "height": int(image.get_height()),
        "base_type": _enum_name(image.get_base_type()),
        "precision": _enum_name(image.get_precision()),
        "dirty": bool(image.is_dirty()),
        "file_name": path,
        "file_path_allowed": path_allowed,
        "selected_layer_ids": [int(layer.get_id()) for layer in selected],
        "bridge_owned_display": int(image.get_id()) in _owned_displays,
    }


def _resolve_image(value: Any) -> Any:
    image_id = _bounded_int(value, "image_id", 1, 2_147_483_647)
    for image in Gimp.get_images():
        if int(image.get_id()) == image_id:
            return image
    raise HostCommandError("GIMP image was not found in this instance")


def _walk_layers(image: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    stack: list[tuple[Any, Optional[int], int]] = [
        (layer, None, 0) for layer in reversed(list(image.get_layers()))
    ]
    while stack:
        layer, parent_id, depth = stack.pop()
        if len(result) >= MAX_LAYER_NODES:
            raise HostCommandError("Layer tree exceeds the configured node limit")
        layer_id = int(layer.get_id())
        children = list(layer.get_children()) if layer.is_group_layer() else []
        result.append(
            {
                "layer_id": layer_id,
                "parent_id": parent_id,
                "depth": depth,
                "name": str(layer.get_name()),
                "visible": bool(layer.get_visible()),
                "locked": bool(layer.get_lock_content()),
                "opacity": float(layer.get_opacity()),
                "is_group": bool(layer.is_group_layer()),
                "child_count": len(children),
            }
        )
        stack.extend((child, layer_id, depth + 1) for child in reversed(children))
    return result


def _resolve_layer(image: Any, value: Any) -> Any:
    layer_id = _bounded_int(value, "layer_id", 1, 2_147_483_647)
    stack = list(image.get_layers())
    visited = 0
    while stack:
        layer = stack.pop()
        visited += 1
        if visited > MAX_LAYER_NODES:
            break
        if int(layer.get_id()) == layer_id:
            return layer
        if layer.is_group_layer():
            stack.extend(layer.get_children())
    raise HostCommandError("GIMP layer was not found in this image")


def _color(value: Any) -> Any:
    if not isinstance(value, list) or len(value) not in {3, 4}:
        raise HostCommandError("color must be [red, green, blue] or [red, green, blue, alpha]")
    channels = [_bounded_int(channel, "color channel", 0, 255) for channel in value]
    if len(channels) == 3:
        channels.append(255)
    red, green, blue, alpha = (channel / 255.0 for channel in channels)
    color = Gegl.Color.new("rgba(0,0,0,0)")
    color.set_rgba(red, green, blue, alpha)
    return color


def _new_layer(image: Any, name: str, color_value: Any) -> Any:
    layer = Gimp.Layer.new(
        image,
        name,
        int(image.get_width()),
        int(image.get_height()),
        Gimp.ImageType.RGBA_IMAGE,
        100.0,
        image.get_default_new_layer_mode(),
    )
    if layer is None:
        raise HostCommandError("GIMP failed to create the layer")
    if color_value is None:
        if not layer.fill(Gimp.FillType.TRANSPARENT):
            raise HostCommandError("GIMP failed to initialize the layer")
    else:
        Gimp.context_push()
        try:
            if not Gimp.context_set_foreground(_color(color_value)):
                raise HostCommandError("GIMP rejected the layer fill color")
            if not layer.fill(Gimp.FillType.FOREGROUND):
                raise HostCommandError("GIMP failed to fill the layer")
        finally:
            Gimp.context_pop()
    if not image.insert_layer(layer, None, 0):
        layer.delete()
        raise HostCommandError("GIMP failed to insert the layer")
    if not image.set_selected_layers([layer]):
        raise HostCommandError("GIMP failed to select the new layer")
    return layer


def _execute_command(method: str, params: Mapping[str, Any]) -> Any:
    if method in {"gimp.get_status", "gimp.ping"}:
        return {
            "ready": True,
            "gimp_version": str(Gimp.version()),
            "adapter_version": VERSION,
            "bridge_host": BRIDGE_HOST,
            "bridge_port": BRIDGE_PORT,
            "authenticated": True,
            "gimp_pid": os.getppid(),
            "plugin_pid": os.getpid(),
            "plugin_module_path": str(Path(__file__).resolve()),
            "main_thread_id": threading.get_ident(),
            "allowed_roots": [str(root) for root in _allowed_roots()],
            "command_count": 16,
            "arbitrary_script_input": False,
        }
    if method == "gimp.list_images":
        return [_image_info(image) for image in Gimp.get_images()]
    if method == "gimp.get_active_image":
        images = list(Gimp.get_images())
        return _image_info(images[0]) if images else None
    if method == "gimp.create_image":
        width = _bounded_int(params.get("width"), "width", 1, 16_384)
        height = _bounded_int(params.get("height"), "height", 1, 16_384)
        if width * height > MAX_IMAGE_PIXELS:
            raise HostCommandError("Image exceeds the configured pixel limit")
        name = _safe_text(params.get("name", "Untitled"), "name", 256)
        image = Gimp.Image.new(width, height, Gimp.ImageBaseType.RGB)
        if image is None:
            raise HostCommandError("GIMP failed to create the image")
        layer = _new_layer(image, name, params.get("background_color"))
        display = Gimp.Display.new(image)
        if display is None:
            image.delete()
            raise HostCommandError("GIMP failed to display the image")
        _owned_displays[int(image.get_id())] = display
        Gimp.displays_flush()
        return {**_image_info(image), "created_layer_id": int(layer.get_id())}
    if method == "gimp.open_image":
        path = _input_path(params.get("path"))
        image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(str(path)))
        if image is None:
            raise HostCommandError("GIMP failed to open the image")
        display = Gimp.Display.new(image)
        if display is None:
            image.delete()
            raise HostCommandError("GIMP failed to display the image")
        _owned_displays[int(image.get_id())] = display
        Gimp.displays_flush()
        return _image_info(image)

    image = _resolve_image(params.get("image_id"))
    if method in {"gimp.inspect_image", "gimp.list_layers"}:
        layers = _walk_layers(image)
        if method == "gimp.list_layers":
            return layers
        return {**_image_info(image), "layer_count": len(layers), "layers": layers}
    if method == "gimp.save_image":
        path = _output_path(
            params.get("path"), frozenset({".xcf"}), bool(params.get("overwrite", False))
        )
        if not Gimp.file_save(
            Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(str(path)), None
        ):
            raise HostCommandError("GIMP failed to save the XCF image")
        image.clean_all()
        if not path.is_file() or path.stat().st_size <= 0:
            raise HostCommandError("GIMP reported success but produced no XCF artifact")
        return _file_digest(path)
    if method == "gimp.export_image":
        path = _output_path(
            params.get("path"), EXPORT_SUFFIXES, bool(params.get("overwrite", False))
        )
        if not Gimp.file_save(
            Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(str(path)), None
        ):
            raise HostCommandError("GIMP failed to export the image")
        if not path.is_file() or path.stat().st_size <= 0:
            raise HostCommandError("GIMP reported success but produced no export artifact")
        return _file_digest(path)
    if method == "gimp.create_layer":
        layer = _new_layer(
            image,
            _safe_text(params.get("name"), "name", 256),
            params.get("color"),
        )
        Gimp.displays_flush()
        return next(item for item in _walk_layers(image) if item["layer_id"] == layer.get_id())
    if method == "gimp.fill_layer":
        layer = _resolve_layer(image, params.get("layer_id"))
        if layer.is_group_layer():
            raise HostCommandError("fill_layer requires a paintable layer")
        Gimp.context_push()
        try:
            if not Gimp.context_set_foreground(_color(params.get("color"))):
                raise HostCommandError("GIMP rejected the fill color")
            if not layer.fill(Gimp.FillType.FOREGROUND):
                raise HostCommandError("GIMP failed to fill the layer")
        finally:
            Gimp.context_pop()
        Gimp.displays_flush()
        return {"layer_id": int(layer.get_id()), "filled": True}
    if method == "gimp.set_layer_properties":
        layer = _resolve_layer(image, params.get("layer_id"))
        changed = []
        if "name" in params:
            if not layer.set_name(_safe_text(params["name"], "name", 256)):
                raise HostCommandError("GIMP failed to rename the layer")
            changed.append("name")
        if "visible" in params:
            if not layer.set_visible(bool(params["visible"])):
                raise HostCommandError("GIMP failed to change layer visibility")
            changed.append("visible")
        if "locked" in params:
            if not layer.set_lock_content(bool(params["locked"])):
                raise HostCommandError("GIMP failed to change the layer lock")
            changed.append("locked")
        if "opacity" in params:
            opacity = _bounded_float(params["opacity"], "opacity", 0.0, 100.0)
            if not layer.set_opacity(opacity):
                raise HostCommandError("GIMP failed to change layer opacity")
            changed.append("opacity")
        if not changed:
            raise HostCommandError("At least one layer property must be provided")
        Gimp.displays_flush()
        return {"layer_id": int(layer.get_id()), "changed": changed}
    if method == "gimp.set_active_layer":
        layer = _resolve_layer(image, params.get("layer_id"))
        if not image.set_selected_layers([layer]):
            raise HostCommandError("GIMP failed to select the layer")
        Gimp.displays_flush()
        return {"layer_id": int(layer.get_id()), "active": True}
    if method == "gimp.delete_layer":
        layer = _resolve_layer(image, params.get("layer_id"))
        if len(_walk_layers(image)) <= 1:
            raise HostCommandError("The last remaining layer cannot be deleted")
        layer_id = int(layer.get_id())
        if not image.remove_layer(layer):
            raise HostCommandError("GIMP failed to delete the layer")
        Gimp.displays_flush()
        return {"layer_id": layer_id, "deleted": True}
    if method == "gimp.flatten_image":
        if params.get("confirm") is not True:
            raise HostCommandError("flatten_image requires confirm=true")
        layer = image.flatten()
        if layer is None:
            raise HostCommandError("GIMP failed to flatten the image")
        Gimp.displays_flush()
        return {**_image_info(image), "flattened": True, "layer_id": int(layer.get_id())}
    if method == "gimp.close_image":
        image_id = int(image.get_id())
        display = _owned_displays.get(image_id)
        if display is None:
            raise HostCommandError("Only images opened by this bridge may be closed")
        dirty = bool(image.is_dirty())
        if dirty and params.get("discard_changes") is not True:
            raise HostCommandError("Image has unsaved changes; set discard_changes=true to close")
        if not display.delete():
            raise HostCommandError("GIMP failed to close the bridge-owned display")
        _owned_displays.pop(image_id, None)
        return {"image_id": image_id, "closed": True, "discarded_changes": dirty}
    raise HostCommandError("Unsupported GIMP bridge method")


def _command_timeout(params: Mapping[str, Any]) -> float:
    return _bounded_float(
        params.get("timeout_secs", 120.0), "timeout_secs", 1.0, MAX_COMMAND_TIMEOUT_SECS
    )


def _dispatch(method: str, params: Mapping[str, Any]) -> Any:
    if not _pending_commands.acquire(blocking=False):
        raise HostCommandError("GIMP main-thread command queue is full")
    pending = _PendingCommand(method, params)

    def run_on_main() -> bool:
        try:
            with pending.lock:
                if pending.cancelled:
                    return GLib.SOURCE_REMOVE
                pending.started = True
            pending.result = _execute_command(pending.method, pending.params)
        except BaseException as exc:
            pending.error = exc
        finally:
            pending.completed.set()
            _pending_commands.release()
        return GLib.SOURCE_REMOVE

    try:
        GLib.idle_add(run_on_main)
    except BaseException:
        _pending_commands.release()
        raise
    if not pending.completed.wait(_command_timeout(params)):
        with pending.lock:
            if not pending.started:
                pending.cancelled = True
                raise HostCommandError(
                    "GIMP main thread did not start the command before timeout; request cancelled"
                )
        raise HostCommandError(
            "GIMP command exceeded timeout after host execution began; host outcome is unknown"
        )
    if pending.error is not None:
        raise pending.error
    return pending.result


def _error_response(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(5.0)
        request_id: Any = None
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                response = _error_response(
                    None, "request_too_large", "Request exceeds the size limit"
                )
            else:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise HostCommandError("Request must be a JSON object")
                request_id = request.get("id")
                method = request.get("method")
                params = request.get("params", {})
                token = request.get("token", "")
                if not isinstance(token, str) or not hmac.compare_digest(token, _bridge_token):
                    response = _error_response(
                        request_id, "unauthorized", "Bridge authentication failed"
                    )
                elif not isinstance(method, str) or not method.startswith("gimp."):
                    response = _error_response(
                        request_id, "invalid_method", "Method must use gimp.*"
                    )
                elif not isinstance(params, dict):
                    response = _error_response(
                        request_id, "invalid_params", "params must be an object"
                    )
                else:
                    try:
                        value = _dispatch(method, params)
                        response = {"jsonrpc": "2.0", "id": request_id, "result": value}
                    except HostCommandError as exc:
                        response = _error_response(request_id, "host_command_error", str(exc))
                    except Exception:
                        response = _error_response(
                            request_id, "bridge_error", "GIMP host command failed"
                        )
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = _error_response(
                request_id, "invalid_json", "Request is not valid UTF-8 JSON"
            )
        except (OSError, HostCommandError) as exc:
            response = _error_response(request_id, "bridge_error", str(exc))
        encoded = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = (
                json.dumps(
                    _error_response(
                        request_id, "response_too_large", "Response exceeds the size limit"
                    ),
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        try:
            self.connection.sendall(encoded)
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, server_address: tuple[str, int], handler_class: type[_Handler]) -> None:
        super().__init__(server_address, handler_class)
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


class DccMcpGimp(Gimp.PlugIn):
    def do_query_procedures(self) -> list[str]:
        return ["python-fu-dcc-mcp-gimp-bridge"]

    def do_create_procedure(self, name: str) -> Any:
        procedure = Gimp.Procedure.new(
            self, name, Gimp.PDBProcType.PERSISTENT, self._run, self, None
        )
        procedure.set_documentation(
            "Start the DCC-MCP GIMP bridge",
            "Starts an authenticated loopback bridge for dcc-mcp-gimp.",
            "dcc-mcp-gimp",
        )
        procedure.set_attribution("loonghao", "dcc-mcp", "2026")
        return procedure

    @staticmethod
    def _run(procedure: Any, run_mode: Any, config: Any, plugin: Any) -> Any:
        try:
            return DccMcpGimp._run_bridge(procedure, run_mode, config, plugin)
        except BaseException as exc:
            _capture_bootstrap_error("bridge-startup", exc)
            raise

    @staticmethod
    def _run_bridge(procedure: Any, run_mode: Any, config: Any, plugin: Any) -> Any:
        del run_mode, config
        global _bridge_token
        _bridge_token = _load_or_create_token()
        server = _Server((BRIDGE_HOST, BRIDGE_PORT), _Handler)
        threading.Thread(
            target=server.serve_forever, name="dcc-mcp-gimp-bridge", daemon=True
        ).start()
        procedure.persistent_ready()
        plugin.persistent_enable()
        try:
            GLib.MainLoop().run()
        finally:
            server.shutdown()
            server.server_close()
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)


Gimp.main(DccMcpGimp.__gtype__, sys.argv)
