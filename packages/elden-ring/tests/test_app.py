"""Offline tests for the HTTP request path: FastAPI app, lifespan and Lambda handler.

``app._lifespan`` resets private ``mcp.session_manager`` state so that a warm
Lambda container (Mangum runs the lifespan on every invocation) can re-enter
``run()``. These tests run the lifespan more than once, so an mcp upgrade that
renames those attributes fails here instead of on the second live invocation.
No OpenSearch access: ``initialize`` and ``tools/list`` never touch the client.
"""

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
    for _ in range(2):  # second block = warm container re-entering the lifespan
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
    return resp["statusCode"], json.loads(body)


def test_lambda_handler_warm_invocations():
    # Each Mangum call runs the full lifespan; four calls = one cold + three warm.
    for path in ("/mcp", "/mcp/"):
        _check_initialize(*_invoke(path, _INITIALIZE))
        _check_tools(*_invoke(path, _TOOLS_LIST))


def test_session_manager_private_attrs_exist():
    """Guard the private attributes the lifespan hack relies on."""
    from elden_ring.mcp_server import mcp

    sm = mcp.session_manager
    assert hasattr(sm, "_has_started")
    assert hasattr(sm, "_task_group")


def test_main_cli_runs_uvicorn(monkeypatch):
    import uvicorn

    from elden_ring import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    main.cli()
    assert calls == [
        (("elden_ring.app:app",), {"host": "0.0.0.0", "port": 8002, "reload": True})
    ]
