"""Bounded typed readiness for the standalone GIMP GUI adapter.

Core's sidecar readiness helper deliberately selects per-DCC sidecars. The
standalone GUI server has no sidecar dispatch metadata; verify its typed bridge
response instead, then let the lifecycle service validate the host identity.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Mapping

_STATUS_TOOL = "gimp_session__get_status"


def _probe_tool(
    url: str, tool: str, arguments: dict[str, Any], *, timeout_secs: float
) -> dict[str, Any]:
    """Call the selected stateless MCP endpoint with Streamable HTTP headers.

    Older Core sidecar probes accept JSON alone; GUI servers require both MCP
    response media types. No gateway discovery or endpoint fallback is used.
    """
    request_id = "gimp-ready-" + uuid.uuid4().hex
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    diagnostic: dict[str, Any] = {"success": False, "tool_name": tool, "request_id": request_id}
    try:
        try:
            response = urllib.request.urlopen(request, timeout=max(0.1, timeout_secs))
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read(1048577)
            diagnostic["http_status"] = response.status
        if len(raw) > 1048576:
            raise ValueError("GIMP readiness MCP response exceeded the 1 MiB limit")
        text = raw.decode("utf-8")
        diagnostic["response"] = text
        if diagnostic["http_status"] != 200:
            raise ValueError("GIMP readiness MCP endpoint returned an HTTP error")
        if text.lstrip().startswith(("event:", "data:", ":")):
            candidates = [
                json.loads(
                    "\n".join(
                        line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                    )
                )
                for event in text.replace("\r\n", "\n").split("\n\n")
                if any(line.startswith("data:") for line in event.splitlines())
            ]
            matches = [
                value
                for value in candidates
                if isinstance(value, dict) and value.get("id") == request_id
            ]
            if len(matches) != 1:
                raise ValueError("GIMP readiness stream omitted one matching response")
            payload = matches[0]
        else:
            payload = json.loads(text)
        diagnostic["response"] = payload
        if not isinstance(payload, dict) or payload.get("id") != request_id or payload.get("error"):
            raise ValueError("GIMP readiness returned a mismatched or failed JSON-RPC response")
        diagnostic.update(success=True, result=payload.get("result"))
    except (OSError, ValueError) as exc:
        diagnostic["error"] = str(exc)
    return diagnostic


def _payload(probe: Mapping[str, Any]) -> dict[str, Any]:
    if probe.get("success") is not True:
        raise ValueError("GIMP readiness MCP call failed")
    result = probe.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        raise ValueError("GIMP readiness returned an invalid MCP result")
    data = result.get("structuredContent", result.get("structured_content"))
    if data is None:
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("GIMP readiness omitted its structured payload")
        block = content[0]
        if not isinstance(block, dict) or block.get("type") != "text":
            raise ValueError("GIMP readiness payload is not JSON text")
        data = json.loads(block.get("text", ""))
    if not isinstance(data, dict):
        raise ValueError("GIMP readiness payload is not an object")
    return data


def probe_gui_readiness(entry: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """Load the official skill and await only its own typed Core readiness job.

    Never change the registry role or accept job acceptance as host readiness.
    Keep all wire results, including errors, for the install diagnostic report.
    """
    started = time.monotonic()
    deadline = started + max(0.05, timeout)
    probes: list[dict[str, Any]] = []
    verdict: dict[str, Any] = {"success": False, "ready": False, "entry": entry, "probes": probes}

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for GIMP GUI readiness")
        probe = _probe_tool(entry["mcp_url"], name, arguments, timeout_secs=remaining)
        probes.append(probe)
        return _payload(probe)

    try:
        loaded = call("load_skill", {"skill_name": "gimp-session"})
        if loaded.get("loaded") is not True or loaded.get("partial") is True:
            raise ValueError("GIMP session skill did not load completely")
        data = call(_STATUS_TOOL, {})
        job_id = None
        while "status" in data:
            if data["status"] not in {"pending", "running", "completed"}:
                raise ValueError("GIMP readiness job did not complete successfully")
            current_id = data.get("job_id")
            if not isinstance(current_id, str) or not current_id or len(current_id) > 256:
                raise ValueError("GIMP readiness job omitted a bounded job id")
            first_job_response = job_id is None
            if first_job_response:
                job_id = current_id
            # Core's initial queue receipt omits `tool`; subsequent job status
            # must identify the tool as well as the original queued job id.
            reported_tool = data.get("tool", _STATUS_TOOL if first_job_response else None)
            if current_id != job_id or reported_tool != _STATUS_TOOL:
                raise ValueError("GIMP readiness job identity changed")
            if data.get("error"):
                raise ValueError("GIMP readiness job reported an error")
            if data["status"] == "completed":
                data = data.get("result")
                break
            poll = data.get("core_poll")
            if (
                not isinstance(poll, dict)
                or poll.get("owner") != "core"
                or poll.get("tool") != "jobs_get_status"
                or not isinstance(poll.get("arguments"), dict)
                or poll["arguments"].get("job_id") != job_id
            ):
                raise ValueError("GIMP readiness returned an unexpected job polling contract")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for GIMP GUI readiness")
            time.sleep(min(0.1, remaining))
            data = call("jobs_get_status", {"job_id": job_id, "include_result": True})
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ValueError("Typed GIMP readiness probe did not succeed")
        context = data.get("context")
        if (
            not isinstance(context, dict)
            or context.get("ready") is not True
            or context.get("authenticated") is not True
        ):
            raise ValueError("GIMP bridge is not ready and authenticated")
        verdict.update(
            success=True,
            ready=True,
            status="ready",
            probe={"success": True, "result": {"structuredContent": data}},
            message="GIMP GUI typed bridge probe is ready; host identity must still be verified",
        )
    except (ValueError, TypeError, KeyError, TimeoutError) as exc:
        verdict.update(status="probe_failed", message=str(exc))
    verdict["elapsed_secs"] = round(time.monotonic() - started, 3)
    return verdict
