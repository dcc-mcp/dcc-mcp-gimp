"""Authenticated JSON-lines client for the GIMP 3 persistent plug-in bridge."""

from __future__ import annotations

import hmac
import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any, Optional

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_COMMAND_TIMEOUT_SECS = 1_800.0


class GimpBridgeError(RuntimeError):
    """Raised when the GIMP plug-in bridge is unavailable or rejects a call."""


def _is_loopback(host: str) -> bool:
    try:
        return all(item[4][0] in {"127.0.0.1", "::1"} for item in socket.getaddrinfo(host, None))
    except OSError:
        return False


def _token_path() -> Path:
    configured = os.environ.get("DCC_MCP_GIMP_BRIDGE_TOKEN_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home().joinpath(".dcc-mcp", "gimp-bridge-token").resolve()


def _load_token() -> str:
    token = os.environ.get("DCC_MCP_GIMP_BRIDGE_TOKEN", "")
    if not token:
        try:
            token = _token_path().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GimpBridgeError(
                "GIMP bridge token is unavailable; run the installed GIMP plug-in first"
            ) from exc
    if len(token) < 32:
        raise GimpBridgeError("GIMP bridge token must contain at least 32 characters")
    return token


class GimpBridge:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3848,
        timeout: float = 120.0,
        token: Optional[str] = None,
    ) -> None:
        if not _is_loopback(host):
            raise GimpBridgeError("GIMP bridge host must resolve only to loopback addresses")
        if port < 1 or port > 65_535:
            raise GimpBridgeError("GIMP bridge port must be between 1 and 65535")
        if timeout < 1 or timeout > MAX_COMMAND_TIMEOUT_SECS:
            raise GimpBridgeError("GIMP bridge timeout must be between 1 and 1800 seconds")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.token = token or _load_token()
        if len(self.token) < 32:
            raise GimpBridgeError("GIMP bridge token must contain at least 32 characters")

    @classmethod
    def from_env(cls) -> "GimpBridge":
        return cls(
            host=os.environ.get("DCC_MCP_GIMP_BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("DCC_MCP_GIMP_BRIDGE_PORT", "3848")),
            timeout=float(os.environ.get("DCC_MCP_GIMP_BRIDGE_TIMEOUT", "120")),
        )

    def call(self, method: str, **params: Any) -> Any:
        if not isinstance(method, str) or not method.startswith("gimp."):
            raise GimpBridgeError("GIMP bridge methods must use the gimp.* namespace")
        timeout = params.get("timeout_secs", self.timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise GimpBridgeError("timeout_secs must be a number")
        if float(timeout) < 1 or float(timeout) > MAX_COMMAND_TIMEOUT_SECS:
            raise GimpBridgeError("timeout_secs must be between 1 and 1800 seconds")
        request_id = uuid.uuid4().hex
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
                "token": self.token,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=min(float(timeout), self.timeout)
            ) as connection:
                connection.settimeout(float(timeout))
                connection.sendall((request + "\n").encode("utf-8"))
                response = connection.makefile("rb").readline(MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise GimpBridgeError(
                "GIMP bridge unavailable at %s:%d; install, start, and keep the GIMP 3 "
                "plug-in running" % (self.host, self.port)
            ) from exc
        if not response:
            raise GimpBridgeError("GIMP bridge closed the connection without a response")
        if len(response) > MAX_RESPONSE_BYTES or not response.endswith(b"\n"):
            raise GimpBridgeError("GIMP bridge response exceeds the size limit")
        try:
            payload = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GimpBridgeError("GIMP bridge returned invalid UTF-8 JSON") from exc
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise GimpBridgeError("GIMP bridge returned an invalid JSON-RPC response")
        response_id = payload.get("id")
        if not isinstance(response_id, str) or not hmac.compare_digest(response_id, request_id):
            raise GimpBridgeError("GIMP bridge returned a mismatched response id")
        if "error" in payload:
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise GimpBridgeError(message or "GIMP bridge rejected the request")
        if "result" not in payload:
            raise GimpBridgeError("GIMP bridge response does not contain a result")
        return payload["result"]


def get_bridge() -> GimpBridge:
    return GimpBridge.from_env()
