import json
import socket
import threading

from dcc_mcp_gimp.bridge import GimpBridge


def test_bridge_sends_json_lines_request():
    received = {}

    def serve(listener):
        connection, _ = listener.accept()
        with connection:
            received["request"] = json.loads(connection.makefile("r", encoding="utf-8").readline())
            connection.sendall(b'{"jsonrpc":"2.0","id":1,"result":{"ready":true}}\n')

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=serve, args=(listener,), daemon=True).start()
    bridge = GimpBridge(port=listener.getsockname()[1])
    assert bridge.call("gimp.ping") == {"ready": True}
    assert received["request"]["method"] == "gimp.ping"
    listener.close()
