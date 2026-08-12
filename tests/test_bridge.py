import json
import socket
import threading

import pytest

from dcc_mcp_gimp.bridge import GimpBridge, GimpBridgeError


def test_bridge_sends_authenticated_json_lines_request():
    received = {}

    def serve(listener):
        connection, _ = listener.accept()
        with connection:
            received["request"] = json.loads(connection.makefile("r", encoding="utf-8").readline())
            response = {
                "jsonrpc": "2.0",
                "id": received["request"]["id"],
                "result": {"ready": True},
            }
            connection.sendall((json.dumps(response) + "\n").encode())

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=serve, args=(listener,), daemon=True).start()
    bridge = GimpBridge(port=listener.getsockname()[1], token="x" * 32)
    assert bridge.call("gimp.ping") == {"ready": True}
    assert received["request"]["method"] == "gimp.ping"
    assert received["request"]["token"] == "x" * 32
    assert isinstance(received["request"]["id"], str)
    listener.close()


def test_bridge_rejects_non_loopback_short_token_and_invalid_method():
    with pytest.raises(GimpBridgeError, match="loopback"):
        GimpBridge(host="192.0.2.10", token="x" * 32)
    with pytest.raises(GimpBridgeError, match="at least 32"):
        GimpBridge(token="short")
    bridge = GimpBridge(token="x" * 32)
    with pytest.raises(GimpBridgeError, match=r"gimp\.\*"):
        bridge.call("python.eval")


def test_bridge_rejects_mismatched_response_id():
    def serve(listener):
        connection, _ = listener.accept()
        with connection:
            connection.makefile("r", encoding="utf-8").readline()
            connection.sendall(b'{"jsonrpc":"2.0","id":"wrong","result":{}}\n')

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=serve, args=(listener,), daemon=True).start()
    bridge = GimpBridge(port=listener.getsockname()[1], token="x" * 32)
    with pytest.raises(GimpBridgeError, match="mismatched"):
        bridge.call("gimp.get_status")
    listener.close()


def test_bridge_surfaces_stable_host_error_message():
    def serve(listener):
        connection, _ = listener.accept()
        with connection:
            request = json.loads(connection.makefile("r", encoding="utf-8").readline())
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": "host_command_error", "message": "typed request rejected"},
            }
            connection.sendall((json.dumps(response) + "\n").encode())

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=serve, args=(listener,), daemon=True).start()
    bridge = GimpBridge(port=listener.getsockname()[1], token="x" * 32)
    with pytest.raises(GimpBridgeError, match="typed request rejected"):
        bridge.call("gimp.get_status")
    listener.close()


def test_bridge_rejects_invalid_timeout():
    bridge = GimpBridge(token="x" * 32)
    with pytest.raises(GimpBridgeError, match="timeout_secs"):
        bridge.call("gimp.open_image", timeout_secs=1801)
