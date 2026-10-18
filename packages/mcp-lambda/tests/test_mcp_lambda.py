"""Offline tests for mcp_lambda: re-entrant lifespan, transport settings, auth (#504)."""

import asyncio
import base64
import json
import threading

import pytest
from fastapi.testclient import TestClient
from mangum import Mangum
from mcp.server.mcpserver import MCPServer
from mcp_lambda import ReentrantMCP, create_mcp_app

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
        "clientInfo": {"name": "test_mcp_lambda", "version": "0"},
    },
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _call(name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _server() -> MCPServer:
    server = MCPServer("toy")

    @server.tool()
    def echo(text: str) -> dict:
        """Echo text back, with the thread it ran on."""
        return {
            "text": text,
            "main_thread": threading.current_thread() is threading.main_thread(),
        }

    return server


def _app(**kwargs):
    return create_mcp_app(_server(), title="Toy", description="Toy MCP", **kwargs)


def _echo_result(body: dict) -> dict:
    assert body["result"]["isError"] is False, body
    return json.loads(body["result"]["content"][0]["text"])


def test_lifespan_reentry_via_testclient():
    app = _app()
    for i in range(20):  # every block after the first = a warm re-entry
        with TestClient(app) as client:
            for path in ("/mcp", "/mcp/"):
                r = client.post(
                    path, json=_INITIALIZE, headers=_HEADERS, follow_redirects=False
                )
                assert r.status_code == 200, r.text
                assert r.headers["content-type"].startswith("application/json")
                assert r.json()["result"]["serverInfo"]["name"] == "toy"
                r = client.post(
                    path, json=_TOOLS_LIST, headers=_HEADERS, follow_redirects=False
                )
                assert [t["name"] for t in r.json()["result"]["tools"]] == ["echo"]
            r = client.post(
                "/mcp", json=_call("echo", {"text": f"hi {i}"}), headers=_HEADERS
            )
            assert _echo_result(r.json())["text"] == f"hi {i}"


def test_sync_tools_run_on_a_worker_thread():
    # mcp 2.x moved sync tools off the event loop (CLAUDE.md, Conventions).
    with TestClient(_app()) as client:
        r = client.post("/mcp", json=_call("echo", {"text": "x"}), headers=_HEADERS)
        assert _echo_result(r.json())["main_thread"] is False


def test_non_local_host_header_accepted():
    # The DNS-rebinding guard would answer 421 for this with mcp's default host.
    headers = {**_HEADERS, "host": "abc123.execute-api.us-east-1.amazonaws.com"}
    with TestClient(_app()) as client:
        r = client.post("/mcp", json=_INITIALIZE, headers=headers)
        assert r.status_code == 200, r.text


def test_outside_lifespan_raises():
    client = TestClient(_app())  # no `with`: the lifespan never runs
    with pytest.raises(RuntimeError, match="outside its lifespan"):
        client.post("/mcp", json=_INITIALIZE, headers=_HEADERS)


def test_fixed_transport_settings_cannot_be_overridden():
    with pytest.raises(TypeError, match="json_response"):
        ReentrantMCP(_server(), json_response=False)


def test_max_request_body_size_passed_through():
    limit = 2048
    with TestClient(_app(max_request_body_size=limit)) as client:
        for size, expect in ((limit, 200), (limit + 1, 413)):
            payload = _call("echo", {"text": ""})
            base = len(json.dumps(payload).encode())
            payload["params"]["arguments"]["text"] = "a" * (size - base)
            body = json.dumps(payload).encode()
            assert len(body) == size
            r = client.post("/mcp", content=body, headers=_HEADERS)
            assert r.status_code == expect, (size, r.text)


@pytest.mark.parametrize("secret", [None, ""])
def test_auth_off_when_env_unset_or_empty(monkeypatch, secret):
    if secret is None:
        monkeypatch.delenv("TOY_SECRET", raising=False)
    else:
        monkeypatch.setenv("TOY_SECRET", secret)
    with TestClient(_app(auth_env="TOY_SECRET")) as client:
        r = client.post("/mcp", json=_INITIALIZE, headers=_HEADERS)
        assert r.status_code == 200


def test_auth_enforced_when_env_set(monkeypatch):
    monkeypatch.setenv("TOY_SECRET", "s3cret")
    with TestClient(_app(auth_env="TOY_SECRET")) as client:
        for auth in (None, "Bearer wrong", "s3cret"):
            headers = (
                dict(_HEADERS) if auth is None else {**_HEADERS, "authorization": auth}
            )
            r = client.post("/mcp", json=_INITIALIZE, headers=headers)
            assert r.status_code == 401
            assert r.json() == {"detail": "Unauthorized"}
        r = client.post(
            "/mcp/",
            json=_INITIALIZE,
            headers={**_HEADERS, "authorization": "Bearer s3cret"},
        )
        assert r.status_code == 200


def test_no_auth_without_auth_env(monkeypatch):
    monkeypatch.setenv("TOY_SECRET", "s3cret")  # ignored: auth_env not given
    with TestClient(_app()) as client:
        r = client.post("/mcp", json=_INITIALIZE, headers=_HEADERS)
        assert r.status_code == 200


def _http_api_v2_event(path: str, payload: dict) -> dict:
    host = "example.execute-api.us-east-1.amazonaws.com"
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {**_HEADERS, "host": host},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "example",
            "domainName": host,
            "domainPrefix": "example",
            "http": {
                "method": "POST",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "test_mcp_lambda",
            },
            "requestId": "test",
            "routeKey": "$default",
            "stage": "$default",
            "time": "18/Oct/2026:00:00:00 +0000",
            "timeEpoch": 0,
        },
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


def test_mangum_warm_invocations():
    # Mangum runs the whole lifespan per call: 1 cold + 19 warm invocations.
    asyncio.set_event_loop(asyncio.new_event_loop())
    handler = Mangum(_app(), lifespan="auto")
    for i in range(20):
        path = "/mcp" if i % 2 else "/mcp/"
        resp = handler(_http_api_v2_event(path, _call("echo", {"text": str(i)})), None)
        assert resp["statusCode"] == 200, resp
        assert resp["headers"]["content-type"].startswith("application/json")
        body = resp["body"]
        if resp.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        assert _echo_result(json.loads(body))["text"] == str(i)
