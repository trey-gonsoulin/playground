"""Offline tests for the HTTP request path: FastAPI app, lifespan and Lambda handler.

Mangum runs the whole lifespan on every invocation, so a warm Lambda container
re-enters it each time; ``mcp_lambda.ReentrantMCP`` rebuilds the session manager
per entry (#504). These tests cycle the lifespan 20 times, so an mcp upgrade that
breaks re-entry fails here instead of on the second live invocation.
No OpenSearch access: ``initialize`` and ``tools/list`` never touch the client.
"""

import asyncio
import base64
import json

from fastapi.testclient import TestClient

from elden_ring import lambda_handler
from elden_ring.app import create_app

N_TOOLS = 12

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


def test_main_cli_runs_uvicorn(monkeypatch):
    import uvicorn

    from elden_ring import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    main.cli()
    assert calls == [
        (("elden_ring.app:app",), {"host": "0.0.0.0", "port": 8002, "reload": True})
    ]
