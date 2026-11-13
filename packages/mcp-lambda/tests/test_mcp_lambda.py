"""Offline tests for mcp_lambda: re-entrant lifespan, transport settings, auth (#504)."""

import json
import threading

import pytest
from fastapi.testclient import TestClient
from mangum import Mangum
from mcp.server.mcpserver import MCPServer
from mcp_lambda import ReentrantMCP, create_mcp_app, tool_decorator
from mcp_lambda.testing import (
    HEADERS,
    INITIALIZE,
    TOOLS_LIST,
    call,
    invoke,
    new_event_loop,
)


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
                    path, json=INITIALIZE, headers=HEADERS, follow_redirects=False
                )
                assert r.status_code == 200, r.text
                assert r.headers["content-type"].startswith("application/json")
                assert r.json()["result"]["serverInfo"]["name"] == "toy"
                r = client.post(
                    path, json=TOOLS_LIST, headers=HEADERS, follow_redirects=False
                )
                assert [t["name"] for t in r.json()["result"]["tools"]] == ["echo"]
            r = client.post(
                "/mcp", json=call("echo", {"text": f"hi {i}"}), headers=HEADERS
            )
            assert _echo_result(r.json())["text"] == f"hi {i}"


def test_sync_tools_run_on_a_worker_thread():
    # mcp 2.x moved sync tools off the event loop (CLAUDE.md, Conventions).
    with TestClient(_app()) as client:
        r = client.post("/mcp", json=call("echo", {"text": "x"}), headers=HEADERS)
        assert _echo_result(r.json())["main_thread"] is False


def test_non_local_host_header_accepted():
    # The DNS-rebinding guard would answer 421 for this with mcp's default host.
    headers = {**HEADERS, "host": "abc123.execute-api.us-east-1.amazonaws.com"}
    with TestClient(_app()) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=headers)
        assert r.status_code == 200, r.text


def test_outside_lifespan_raises():
    client = TestClient(_app())  # no `with`: the lifespan never runs
    with pytest.raises(RuntimeError, match="outside its lifespan"):
        client.post("/mcp", json=INITIALIZE, headers=HEADERS)


def test_fixed_transport_settings_cannot_be_overridden():
    with pytest.raises(TypeError, match="json_response"):
        ReentrantMCP(_server(), json_response=False)


def test_max_request_body_size_passed_through():
    limit = 2048
    with TestClient(_app(max_request_body_size=limit)) as client:
        for size, expect in ((limit, 200), (limit + 1, 413)):
            payload = call("echo", {"text": ""})
            base = len(json.dumps(payload).encode())
            payload["params"]["arguments"]["text"] = "a" * (size - base)
            body = json.dumps(payload).encode()
            assert len(body) == size
            r = client.post("/mcp", content=body, headers=HEADERS)
            assert r.status_code == expect, (size, r.text)


@pytest.mark.parametrize("secret", [None, ""])
def test_auth_off_when_env_unset_or_empty(monkeypatch, secret):
    if secret is None:
        monkeypatch.delenv("TOY_SECRET", raising=False)
    else:
        monkeypatch.setenv("TOY_SECRET", secret)
    with TestClient(_app(auth_env="TOY_SECRET")) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=HEADERS)
        assert r.status_code == 200


def test_auth_enforced_when_env_set(monkeypatch):
    monkeypatch.setenv("TOY_SECRET", "s3cret")
    with TestClient(_app(auth_env="TOY_SECRET")) as client:
        for auth in (None, "Bearer wrong", "s3cret"):
            headers = (
                dict(HEADERS) if auth is None else {**HEADERS, "authorization": auth}
            )
            r = client.post("/mcp", json=INITIALIZE, headers=headers)
            assert r.status_code == 401
            assert r.json() == {"detail": "Unauthorized"}
        r = client.post(
            "/mcp/",
            json=INITIALIZE,
            headers={**HEADERS, "authorization": "Bearer s3cret"},
        )
        assert r.status_code == 200


def test_no_auth_without_auth_env(monkeypatch):
    monkeypatch.setenv("TOY_SECRET", "s3cret")  # ignored: auth_env not given
    with TestClient(_app()) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=HEADERS)
        assert r.status_code == 200


def test_mangum_warm_invocations():
    # Mangum runs the whole lifespan per call: 1 cold + 19 warm invocations.
    new_event_loop()
    handler = Mangum(_app(), lifespan="auto")
    for i in range(20):
        path = "/mcp" if i % 2 else "/mcp/"
        status, body = invoke(handler, path, call("echo", {"text": str(i)}))
        assert status == 200, body
        assert _echo_result(body)["text"] == str(i)


class _Exposed(Exception):
    pass


def test_tool_decorator_exposes_listed_errors_only():
    server = MCPServer("errors")
    tool = tool_decorator(server, expose=(_Exposed,))

    @tool
    def fail(kind: str) -> dict:
        """Raise the named error."""
        raise (_Exposed if kind == "exposed" else RuntimeError)(f"{kind} detail")

    assert fail.__name__ == "fail"  # returned unchanged, still raises directly
    app = create_mcp_app(server, title="Errors", description="Errors")
    with TestClient(app) as client:
        for kind, shown in (("exposed", True), ("hidden", False)):
            r = client.post("/mcp", json=call("fail", {"kind": kind}), headers=HEADERS)
            result = r.json()["result"]
            assert result["isError"] is True
            assert (f"{kind} detail" in result["content"][0]["text"]) is shown, result


def test_tool_decorator_rejects_async_tools():
    async def coro() -> dict:
        return {}

    with pytest.raises(TypeError, match="sync tools only"):
        tool_decorator(MCPServer("x"))(coro)
