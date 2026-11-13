"""Test helpers for an ``mcp_lambda`` app: JSON-RPC payloads, API Gateway events, and
checks that the lifespan survives re-entry over HTTP and through Mangum.

Mangum runs the whole lifespan on every invocation, so a warm Lambda container
re-enters it each time. ``check_reentry`` and ``check_lambda`` cycle it many times,
so an mcp upgrade that breaks re-entry fails in tests instead of on the second live
invocation. Neither calls a tool, so they need no network.
"""

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "mcp_lambda.testing", "version": "0"},
    },
}

TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

_HOST = "example.execute-api.us-east-1.amazonaws.com"


def call(name: str, arguments: dict) -> dict:
    """A ``tools/call`` request."""
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def http_api_v2_event(path: str, payload: dict) -> dict:
    """Minimal API Gateway HTTP API (payload v2.0) event, as the Lambda sees it."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {**HEADERS, "host": _HOST},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "example",
            "domainName": _HOST,
            "domainPrefix": "example",
            "http": {
                "method": "POST",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "mcp_lambda.testing",
            },
            "requestId": "test",
            "routeKey": "$default",
            "stage": "$default",
            "time": "01/Jan/2026:00:00:00 +0000",
            "timeEpoch": 0,
        },
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


def invoke(
    handler: Callable[[dict, Any], dict], path: str, payload: dict
) -> tuple[int, dict]:
    """Send ``payload`` through a Mangum ``handler``; returns (status, JSON body).

    Call ``new_event_loop()`` first: Mangum uses the thread's current loop.
    """
    resp = handler(http_api_v2_event(path, payload), None)
    body = resp["body"]
    if resp.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    # Plain JSON, not SSE: API Gateway needs json_response (fixed by mcp_lambda).
    assert resp["headers"]["content-type"].startswith("application/json"), resp
    return resp["statusCode"], json.loads(body)


def new_event_loop() -> None:
    """Give the thread a fresh event loop, as a Lambda process starts with one.

    An earlier ``asyncio.run()`` in the same pytest process may have cleared it.
    """
    asyncio.set_event_loop(asyncio.new_event_loop())


def check_initialize(status: int, body: dict) -> None:
    assert status == 200, body
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"], body


def check_tools(status: int, body: dict, n_tools: int) -> None:
    assert status == 200, body
    tools = body["result"]["tools"]
    assert len(tools) == n_tools, [t["name"] for t in tools]


def check_reentry(app: FastAPI, *, n_tools: int, cycles: int = 20) -> None:
    """``initialize`` and ``tools/list`` on ``/mcp`` and ``/mcp/`` over ``cycles`` lifespans."""
    for _ in range(cycles):  # later blocks = warm container re-entering the lifespan
        with TestClient(app) as client:
            for path in ("/mcp", "/mcp/"):
                r = client.post(path, json=INITIALIZE, headers=HEADERS)
                check_initialize(r.status_code, r.json())
                r = client.post(path, json=TOOLS_LIST, headers=HEADERS)
                check_tools(r.status_code, r.json(), n_tools)


def check_lambda(
    handler: Callable[[dict, Any], dict], *, n_tools: int, rounds: int = 5
) -> None:
    """The same through Mangum: each call runs the full lifespan (1 cold, rest warm)."""
    new_event_loop()
    for _ in range(rounds):
        for path in ("/mcp", "/mcp/"):
            check_initialize(*invoke(handler, path, INITIALIZE))
            check_tools(*invoke(handler, path, TOOLS_LIST), n_tools)


def check_bearer_auth(app: FastAPI, secret: str) -> None:
    """``app`` answers 401 without ``Authorization: Bearer <secret>`` and 200 with it."""
    with TestClient(app) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=HEADERS)
        assert r.status_code == 401, r.text
        auth = {**HEADERS, "authorization": f"Bearer {secret}"}
        r = client.post("/mcp", json=INITIALIZE, headers=auth)
        check_initialize(r.status_code, r.json())
