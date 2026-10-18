"""Offline tests for the HTTP request path: FastAPI app, lifespan and Lambda handler.

Mangum runs the whole lifespan on every invocation, so a warm Lambda container
re-enters it each time; ``mcp_lambda.ReentrantMCP`` rebuilds the session manager
per entry (#504). These tests cycle the lifespan 20 times, so an mcp upgrade that
breaks re-entry fails here instead of on the second live invocation.
No network access: ``initialize`` and ``tools/list`` never call a tool.
"""

import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient
from genealogy import lambda_handler
from genealogy.app import create_app

N_TOOLS = 19

_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test_app", "version": "0"},
    },
}

_TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.fixture(autouse=True)
def _offline_env(tmp_path, monkeypatch):
    # Tools are never called, but keep any storage access local, never S3.
    monkeypatch.delenv("TREE_BUCKET", raising=False)
    monkeypatch.setenv("TREE_DIR", str(tmp_path))
    monkeypatch.delenv("GENEALOGY_MCP_SECRET", raising=False)


def _check_initialize(status: int, body: dict) -> None:
    assert status == 200, body
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"]


def _check_tools(status: int, body: dict) -> None:
    assert status == 200, body
    tools = body["result"]["tools"]
    assert len(tools) == N_TOOLS, [t["name"] for t in tools]


def test_lifespan_reentry_via_testclient():
    app = create_app()
    for _ in range(20):  # later blocks = warm container re-entering the lifespan
        with TestClient(app) as client:
            for path in ("/mcp", "/mcp/"):
                r = client.post(path, json=_INITIALIZE, headers=_HEADERS)
                _check_initialize(r.status_code, r.json())
                r = client.post(path, json=_TOOLS_LIST, headers=_HEADERS)
                _check_tools(r.status_code, r.json())


def test_bearer_secret_enforced_when_set(monkeypatch):
    monkeypatch.setenv("GENEALOGY_MCP_SECRET", "s3cret")
    with TestClient(create_app()) as client:
        r = client.post("/mcp", json=_INITIALIZE, headers=_HEADERS)
        assert r.status_code == 401
        auth = {**_HEADERS, "authorization": "Bearer s3cret"}
        r = client.post("/mcp", json=_INITIALIZE, headers=auth)
        _check_initialize(r.status_code, r.json())


def _http_api_v2_event(path: str, payload: dict) -> dict:
    """Minimal API Gateway HTTP API (payload v2.0) event, as the Lambda sees it."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {**_HEADERS, "host": "example.execute-api.us-east-1.amazonaws.com"},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "example",
            "domainName": "example.execute-api.us-east-1.amazonaws.com",
            "domainPrefix": "example",
            "http": {
                "method": "POST",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "test_app",
            },
            "requestId": "test",
            "routeKey": "$default",
            "stage": "$default",
            "time": "17/Oct/2026:00:00:00 +0000",
            "timeEpoch": 0,
        },
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


def _invoke(path: str, payload: dict) -> tuple[int, dict]:
    resp = lambda_handler.handler(_http_api_v2_event(path, payload), None)
    body = resp["body"]
    if resp.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    # Plain JSON, not SSE: API Gateway needs json_response (fixed by mcp_lambda).
    assert resp["headers"]["content-type"].startswith("application/json"), resp
    return resp["statusCode"], json.loads(body)


def test_lambda_handler_warm_invocations():
    # Each Mangum call runs the full lifespan; 20 calls = one cold + 19 warm.
    # Mangum uses the thread's current loop, which an earlier asyncio.run() in the
    # same pytest process may have cleared (a Lambda process starts with one).
    asyncio.set_event_loop(asyncio.new_event_loop())
    for _ in range(5):
        for path in ("/mcp", "/mcp/"):
            _check_initialize(*_invoke(path, _INITIALIZE))
            _check_tools(*_invoke(path, _TOOLS_LIST))


# Lambda refuses synchronous invocation events over 6 MB (6 MiB).
_LAMBDA_EVENT_LIMIT = 6 * 1024 * 1024


def _import_gedcom_body(size: int, line: str) -> bytes:
    """A tree_import_gedcom request exactly ``size`` bytes long, padded with ``line``s."""
    head = "0 HEAD\n0 @I1@ INDI\n1 NAME Ann /Lee/\n"
    tail = "0 TRLR\n"

    def body(text: str) -> bytes:
        call = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "tree_import_gedcom",
                "arguments": {"gedcom_text": text},
            },
        }
        return json.dumps(call).encode()

    base = len(body(head + tail))
    pad = line * ((size - base) // len(json.dumps(line)[1:-1]))
    pad_bytes = len(body(head + pad + tail)) - base
    # Top up with a NOTE whose text fills the remaining bytes exactly.
    note = "1 NOTE "
    room = size - base - pad_bytes - len(json.dumps(note + "\n")[1:-1])
    if room < 0:
        pad = pad[len(line) :]
        room += len(json.dumps(line)[1:-1])
    out = body(head + pad + note + "x" * room + "\n" + tail)
    assert len(out) == size
    return out


def test_request_body_limit_boundary():
    # #505: the largest accepted body still imports; one byte more is a 413.
    from genealogy.mcp_server import MAX_REQUEST_BODY_BYTES

    line = "1 NOTE " + "x" * 52 + "\n"
    with TestClient(create_app()) as client:
        r = client.post(
            "/mcp",
            content=_import_gedcom_body(MAX_REQUEST_BODY_BYTES, line),
            headers=_HEADERS,
        )
        assert r.status_code == 200, r.text[:200]
        result = r.json()["result"]
        assert result["isError"] is False, result
        assert json.loads(result["content"][0]["text"])["people"] == 1
        r = client.post(
            "/mcp",
            content=_import_gedcom_body(MAX_REQUEST_BODY_BYTES + 1, line),
            headers=_HEADERS,
        )
        assert r.status_code == 413


def test_max_request_body_fits_a_lambda_event():
    # API Gateway puts the body in the event as an escaped JSON string, so every
    # newline grows again; GEDCOM made only of the shortest real lines
    # ("1 SEX F", an escape every 8 characters) must still fit.
    from genealogy.mcp_server import MAX_REQUEST_BODY_BYTES

    body = _import_gedcom_body(MAX_REQUEST_BODY_BYTES, "1 SEX F\n")
    event = _http_api_v2_event("/mcp", {})
    event["body"] = body.decode()
    size = len(json.dumps(event).encode())
    assert size < _LAMBDA_EVENT_LIMIT, size


def test_main_cli_runs_uvicorn(monkeypatch):
    import uvicorn
    from genealogy import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    main.cli()
    assert calls == [
        (("genealogy.app:app",), {"host": "0.0.0.0", "port": 8000, "reload": True})
    ]
