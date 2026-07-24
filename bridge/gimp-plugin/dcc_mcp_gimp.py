#!/usr/bin/env python3
"""GIMP 3 Python plug-in exposing a loopback JSON-lines bridge."""

import json
import os
import socketserver
import sys
import threading

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, GLib


PORT = int(os.environ.get("DCC_MCP_GIMP_BRIDGE_PORT", "3848"))


def _image_info(image):
    return {
        "id": int(image.get_id()),
        "name": image.get_name(),
        "width": image.get_width(),
        "height": image.get_height(),
    }


def _dispatch_main(method):
    if method == "gimp.get_status":
        return {"ready": True, "gimp_version": Gimp.version(), "bridge_port": PORT}
    if method == "gimp.list_images":
        return [_image_info(image) for image in Gimp.get_images()]
    if method == "gimp.get_active_image":
        image = Gimp.get_images()[0] if Gimp.get_images() else None
        return _image_info(image) if image else None
    if method == "gimp.ping":
        return {"ready": True}
    raise ValueError(f"Unsupported GIMP bridge method: {method}")


def _dispatch(method):
    result = {}
    completed = threading.Event()

    def run_on_main():
        try:
            result["value"] = _dispatch_main(method)
        except Exception as exc:
            result["error"] = exc
        finally:
            completed.set()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run_on_main)
    if not completed.wait(10):
        raise TimeoutError("GIMP main thread did not answer within 10 seconds")
    if "error" in result:
        raise result["error"]
    return result.get("value")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode("utf-8")
        if not line:
            return
        request = json.loads(line)
        try:
            result = _dispatch(request["method"])
            response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        except Exception as exc:  # GIMP procedure errors must reach the MCP caller.
            response = {"jsonrpc": "2.0", "id": request.get("id"), "error": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class DccMcpGimp(Gimp.PlugIn):
    def do_query_procedures(self):
        return ["python-fu-dcc-mcp-gimp-bridge"]

    def do_create_procedure(self, name):
        procedure = Gimp.Procedure.new(
            self, name, Gimp.PDBProcType.PERSISTENT, self._run, self, None
        )
        procedure.set_documentation(
            "Start the DCC-MCP GIMP bridge",
            "Starts a loopback bridge for dcc-mcp-gimp.",
            "dcc-mcp-gimp",
        )
        return procedure

    @staticmethod
    def _run(procedure, run_mode, config, plugin):
        server = _Server(("127.0.0.1", PORT), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        procedure.persistent_ready()
        plugin.persistent_enable()
        GLib.MainLoop().run()
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)


Gimp.main(DccMcpGimp.__gtype__, sys.argv)
