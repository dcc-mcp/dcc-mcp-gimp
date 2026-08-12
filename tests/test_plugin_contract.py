import ast
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "bridge/gimp-plugin/dcc_mcp_gimp.py"


def test_plugin_is_python39_syntax_and_has_no_generic_execution_surface():
    source = PLUGIN.read_text(encoding="utf-8")
    ast.parse(source, filename=str(PLUGIN), feature_version=(3, 9))
    forbidden = ("eval(", "exec(", "subprocess", "lookup_procedure", "run_procedure")
    assert not any(marker in source for marker in forbidden)
    assert "arbitrary_script_input" in source
    assert "hmac.compare_digest" in source


def test_plugin_contract_is_bounded_and_main_thread_marshalled():
    source = PLUGIN.read_text(encoding="utf-8")
    required = (
        "MAX_CONNECTIONS = 16",
        "MAX_PENDING_COMMANDS = 32",
        "MAX_REQUEST_BYTES",
        "MAX_RESPONSE_BYTES",
        "MAX_IMAGE_PIXELS",
        "MAX_LAYER_NODES",
        "MAX_FILE_BYTES",
        "GLib.idle_add(run_on_main)",
        "request cancelled",
        "host outcome is unknown",
        "DCC_MCP_GIMP_ALLOWED_ROOTS",
        "Only images opened by this bridge may be closed",
    )
    assert all(marker in source for marker in required)


def test_plugin_exposes_exact_typed_command_catalog():
    source = PLUGIN.read_text(encoding="utf-8")
    commands = {
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
    }
    assert all(('"gimp.%s"' % command) in source for command in commands)
    assert '"command_count": 16' in source
