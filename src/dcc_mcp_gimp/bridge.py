"""Small JSON-lines client for the GIMP 3 plug-in bridge."""

from __future__ import annotations

import json
import os
import socket
from typing import Any


class GimpBridgeError(RuntimeError):
    """Raised when the GIMP plug-in bridge is unavailable or rejects a call."""


class GimpBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 3848, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "GimpBridge":
        return cls(
            host=os.environ.get("DCC_MCP_GIMP_BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("DCC_MCP_GIMP_BRIDGE_PORT", "3848")),
            timeout=float(os.environ.get("DCC_MCP_GIMP_BRIDGE_TIMEOUT", "10")),
        )

    def call(self, method: str, **params: Any) -> Any:
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as connection:
                connection.sendall((request + "\n").encode("utf-8"))
                response = connection.makefile("r", encoding="utf-8").readline()
        except OSError as exc:
            raise GimpBridgeError(
                f"GIMP bridge unavailable at {self.host}:{self.port}; "
                "install and run the GIMP 3 plug-in"
            ) from exc
        if not response:
            raise GimpBridgeError("GIMP bridge closed the connection without a response")
        payload = json.loads(response)
        if "error" in payload:
            raise GimpBridgeError(str(payload["error"]))
        return payload.get("result")


def get_bridge() -> GimpBridge:
    return GimpBridge.from_env()
