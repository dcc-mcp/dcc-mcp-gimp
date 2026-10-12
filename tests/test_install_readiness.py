import copy
import json

import pytest

from dcc_mcp_gimp import install_readiness as readiness

ENTRY = {"instance_id": "gui-instance", "mcp_url": "http://127.0.0.1:18812/mcp"}
READY = {"success": True, "context": {"ready": True, "authenticated": True}}


def wire(data, *, text=False):
    result = (
        {"content": [{"type": "text", "text": json.dumps(data)}]}
        if text
        else {"structuredContent": data}
    )
    return {"success": True, "result": result}


def job(status="pending", **updates):
    return {
        "job_id": "owned-job",
        "tool": "gimp_session__get_status",
        "status": status,
        "core_poll": {
            "owner": "core",
            "tool": "jobs_get_status",
            "arguments": {"job_id": "owned-job", "include_result": True},
        },
        **updates,
    }


def fixture_calls(monkeypatch, responses):
    calls = []
    iterator = iter(responses)

    def probe(url, tool, arguments, *, timeout_secs):
        assert 0 < timeout_secs <= 2
        calls.append((url, tool, arguments))
        return next(iterator)

    monkeypatch.setattr(readiness, "_probe_tool", probe)
    monkeypatch.setattr(readiness.time, "sleep", lambda _: None)
    return calls


@pytest.mark.parametrize("text", [False, True])
def test_gui_probe_loads_skill_and_checks_direct_typed_result(monkeypatch, text):
    calls = fixture_calls(monkeypatch, [wire({"loaded": True}, text=True), wire(READY, text=text)])
    before = copy.deepcopy(ENTRY)
    result = readiness.probe_gui_readiness(ENTRY, timeout=2)
    assert result["success"] is True
    assert result["probe"]["result"]["structuredContent"] == READY
    assert ENTRY == before
    assert [call[1] for call in calls] == ["load_skill", "gimp_session__get_status"]
    assert all(call[0] == ENTRY["mcp_url"] for call in calls)


def test_gui_probe_awaits_own_core_job_instead_of_accepting_pending(monkeypatch):
    responses = [
        wire({"loaded": True}),
        wire(job()),
        wire(job("running")),
        wire(job("completed", result=READY)),
    ]
    calls = fixture_calls(monkeypatch, responses)
    result = readiness.probe_gui_readiness(ENTRY, timeout=2)
    assert result["success"] is True
    assert result["probes"] == responses
    assert (
        calls[2:]
        == [
            (
                ENTRY["mcp_url"],
                "jobs_get_status",
                {
                    "job_id": "owned-job",
                    "include_result": True,
                },
            )
        ]
        * 2
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"success": False},
        {"success": True},
        {"success": True, "context": {"ready": True, "authenticated": False}},
        {"success": True, "context": {"ready": False, "authenticated": True}},
        job("failed", error="host error"),
        job("cancelled"),
        job(job_id=None),
        job(tool="foreign_tool"),
        job(error="host error"),
        job(core_poll={"owner": "foreign", "tool": "jobs_get_status", "arguments": {}}),
        job(core_poll={"owner": "core", "tool": "delete_image", "arguments": {}}),
        job(
            core_poll={
                "owner": "core",
                "tool": "jobs_get_status",
                "arguments": {"job_id": "foreign"},
            }
        ),
    ],
)
def test_gui_probe_fails_closed_and_keeps_diagnostics(monkeypatch, payload):
    responses = [wire({"loaded": True}), wire(payload)]
    calls = fixture_calls(monkeypatch, responses)
    result = readiness.probe_gui_readiness(ENTRY, timeout=2)
    assert result["success"] is False
    assert result["probes"] == responses
    assert len(calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "error": "connection refused"},
        {
            "success": True,
            "result": {"isError": True, "content": [{"type": "text", "text": "error"}]},
        },
        {"success": True, "result": {}},
        {"success": True, "result": {"content": [{"type": "image", "data": ""}]}},
        {"success": True, "result": {"content": [{"type": "text", "text": "not-json"}]}},
        wire([]),
        wire({"loaded": False}),
        wire({"loaded": True, "partial": True}),
    ],
)
def test_gui_probe_rejects_wire_and_skill_errors(monkeypatch, response):
    calls = fixture_calls(monkeypatch, [response])
    result = readiness.probe_gui_readiness(ENTRY, timeout=2)
    assert result["success"] is False
    assert result["probes"] == [response]
    assert len(calls) == 1


def test_gui_probe_rejects_job_id_change(monkeypatch):
    fixture_calls(
        monkeypatch,
        [
            wire({"loaded": True}),
            wire(job()),
            wire(job("completed", job_id="foreign", result=READY)),
        ],
    )
    result = readiness.probe_gui_readiness(ENTRY, timeout=2)
    assert result["success"] is False
    assert "identity changed" in result["message"]


def test_gui_probe_pending_timeout_is_not_ready(monkeypatch):
    fixture_calls(monkeypatch, [wire({"loaded": True}), wire(job())])
    ticks = iter([0, 0, 0, 3, 3])
    monkeypatch.setattr(readiness.time, "monotonic", lambda: next(ticks))
    result = readiness.probe_gui_readiness(ENTRY, timeout=2)
    assert result["success"] is False
    assert "Timed out" in result["message"]


@pytest.mark.parametrize("sse", [False, True])
def test_transport_accepts_both_mcp_media_types_and_matches_response(monkeypatch, sse):
    import io

    def urlopen(request, timeout):
        assert request.get_header("Accept") == "application/json, text/event-stream"
        assert request.full_url == ENTRY["mcp_url"]
        body = json.loads(request.data)
        assert body["method"] == "tools/call"
        assert body["params"] == {"name": "load_skill", "arguments": {"skill_name": "gimp-session"}}
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": wire({"loaded": True})["result"]}
        text = json.dumps(payload)
        if sse:
            text = "event: message\r\ndata: " + text + "\r\n\r\n"
        response = io.BytesIO(text.encode())
        response.status = 200
        return response

    monkeypatch.setattr(readiness.urllib.request, "urlopen", urlopen)
    result = readiness._probe_tool(
        ENTRY["mcp_url"], "load_skill", {"skill_name": "gimp-session"}, timeout_secs=2
    )
    assert result["success"] is True
    assert readiness._payload(result) == {"loaded": True}


@pytest.mark.parametrize(
    "raw,status",
    [
        (b'{"jsonrpc":"2.0","id":"foreign","result":{}}', 200),
        (b"Not Acceptable: media types required", 406),
        (b"not json", 200),
        (b'data: {"id":"foreign"}\n\n', 200),
        (b"x" * 1048577, 200),
    ],
)
def test_transport_preserves_bounded_errors_and_rejects_mismatch(monkeypatch, raw, status):
    import io

    def urlopen(*args, **kwargs):
        response = io.BytesIO(raw)
        response.status = status
        return response

    monkeypatch.setattr(readiness.urllib.request, "urlopen", urlopen)
    result = readiness._probe_tool(ENTRY["mcp_url"], "load_skill", {}, timeout_secs=2)
    assert result["success"] is False
    assert result["error"]
    if status == 406:
        assert result["response"] == raw.decode()


def test_cancelled_job_cannot_claim_ready(monkeypatch):
    fixture_calls(monkeypatch, [wire({"loaded": True}), wire(job("cancelled", **READY))])
    assert readiness.probe_gui_readiness(ENTRY, timeout=2)["success"] is False


def test_gui_probe_accepts_actual_core_queue_receipt_without_tool(monkeypatch):
    queued = job()
    del queued["tool"]
    fixture_calls(
        monkeypatch, [wire({"loaded": True}), wire(queued), wire(job("completed", result=READY))]
    )
    assert readiness.probe_gui_readiness(ENTRY, timeout=2)["success"] is True


def test_gui_probe_requires_tool_identity_after_queue_receipt(monkeypatch):
    completed = job("completed", result=READY)
    del completed["tool"]
    fixture_calls(monkeypatch, [wire({"loaded": True}), wire(job()), wire(completed)])
    assert readiness.probe_gui_readiness(ENTRY, timeout=2)["success"] is False
