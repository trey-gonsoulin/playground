"""The Lambda front end against the real box app (in-process), with EC2 faked."""

import asyncio

import pytest
from conftest import _reset
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from vault_mcp import _ready, proxy
from vault_mcp.app import create_app

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


@pytest.fixture
def ec2(vault, monkeypatch, tmp_path):
    """A running box (the real app over TestClient) behind a fake EC2."""
    monkeypatch.setenv("VAULT_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("VAULT_MCP_ACTIVITY_FILE", str(tmp_path / "last_activity"))
    _reset()
    state = {
        "state": "running",
        "host": "ec2-1-2-3-4.compute.amazonaws.com",
        "starts": 0,
    }
    proxy._cache.clear()
    proxy._cache["params"] = {"mcp-token": TOKEN, "ca-cert": "unused", "readonly": "0"}
    proxy._cache["params_at"] = proxy.time.monotonic()
    monkeypatch.setattr(
        proxy, "_instance", lambda: {k: state[k] for k in ("state", "host")}
    )

    def start():
        state["starts"] += 1
        state["state"] = "pending"

    monkeypatch.setattr(proxy, "_start", start)
    monkeypatch.setattr(proxy.time, "sleep", lambda _s: None)
    monkeypatch.setattr(proxy, "START_BUDGET", 0.0)
    with TestClient(create_app(), headers=AUTH) as box:
        monkeypatch.setattr(proxy, "_client", lambda: box)
        yield state
    proxy._cache.clear()
    _ready._latched["ready"] = False


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


def test_tools_mirror_the_box(ec2):
    box_tools = {t.name: t for t in asyncio.run(create_app_server().list_tools())}
    front = {
        t.name: t for t in asyncio.run(proxy.create_proxy_server(False).list_tools())
    }
    assert set(front) == set(box_tools) | {"start_vault"}
    for name, tool in box_tools.items():
        if name != "health":
            assert front[name].input_schema == tool.input_schema, name
            assert front[name].description == tool.description, name


def create_app_server():
    from vault_mcp import mcp_server

    return mcp_server.create_server(readonly=False)


def test_readonly_front_hides_write_tools(ec2):
    names = {t.name for t in asyncio.run(proxy.create_proxy_server(True).list_tools())}
    assert "create_note" not in names and {"search", "start_vault"} <= names


def test_forwards_calls_to_the_running_box(ec2, tmp_path):
    res = proxy._call("search", {"query": "killer"})
    assert any(r["path"].endswith("Vex.md") for r in res["results"])
    assert (tmp_path / "last_activity").exists()  # the box counted the activity


def test_box_errors_reach_the_model(ec2):
    with pytest.raises(ToolError, match="no note found"):
        proxy._call("read_note", {"note": "Nope"})


def test_stopped_box_is_started_and_claude_told_to_wait(ec2):
    ec2["state"] = "stopped"
    with pytest.raises(ToolError, match="asleep and is starting"):
        proxy._call("search", {"query": "x"})
    assert ec2["starts"] == 1
    with pytest.raises(ToolError, match="starting"):  # pending: no second start
        proxy._call("search", {"query": "x"})
    assert ec2["starts"] == 1


def test_stopping_box_says_retry(ec2):
    ec2["state"] = "stopping"
    with pytest.raises(ToolError, match="stopping"):
        proxy._call("search", {"query": "x"})


def test_start_vault(ec2):
    ec2["state"] = "stopped"
    res = proxy.start_vault()
    assert res["status"] == "starting" and ec2["starts"] == 1
    ec2["state"] = "running"
    assert proxy.start_vault()["status"] == "ready"  # off Linux: uptime counts as long


def test_box_gates_tools_until_sync_settles(ec2, monkeypatch):
    monkeypatch.setenv("VAULT_REQUIRE_READY", "1")
    _reset()
    _ready._latched["ready"] = False
    monkeypatch.setattr(_ready, "uptime", lambda: 30.0)  # just booted
    with pytest.raises(ToolError, match="still catching up"):
        proxy._call("search", {"query": "killer"})
    assert proxy.start_vault()["status"] == "starting"
    monkeypatch.setattr(_ready, "uptime", lambda: 900.0)
    assert proxy._call("search", {"query": "killer"})["count"] >= 1
    assert proxy.start_vault()["status"] == "ready"


def test_health_does_not_wake_the_box(ec2):
    ec2["state"] = "stopped"
    assert proxy.health() == {"instance": "stopped"}
    assert ec2["starts"] == 0
    ec2["state"] = "running"
    assert proxy.health()["server"]["vault_found"]


def test_unauthenticated_requests_dont_count_as_activity(ec2, tmp_path):
    with TestClient(create_app()) as anon:
        assert anon.post("/mcp", json={}, headers=HEADERS).status_code == 401
    assert not (tmp_path / "last_activity").exists()


def test_proxy_app_requires_token(ec2):
    app = proxy.create_proxy_app()
    with TestClient(app) as client:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert client.post("/mcp", json=body, headers=HEADERS).status_code == 401
        ok = client.post("/mcp", json=body, headers={**HEADERS, **AUTH})
        assert ok.status_code == 200 and len(ok.json()["result"]["tools"]) == 34
    proxy._cache["params"] = {"ca-cert": "x"}
    proxy._cache["params_at"] = proxy.time.monotonic()
    with pytest.raises(RuntimeError, match="mcp-token"):
        proxy.create_proxy_app()


def test_rotated_token_reaches_a_warm_front_end(ec2, monkeypatch):
    app = proxy.create_proxy_app()
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    class FakeSSM:
        def get_parameters(self, Names, WithDecryption):
            values = {"mcp-token": "n3w", "ca-cert": "unused", "readonly": "0"}
            return {
                "Parameters": [
                    {"Name": proxy.PARAM_PREFIX + k, "Value": v}
                    for k, v in values.items()
                ]
            }

    monkeypatch.setattr(proxy.boto3, "client", lambda _name: FakeSSM())
    proxy._cache["params_at"] -= proxy.PARAMS_TTL + 1  # the cached settings expire
    with TestClient(app) as client:
        new = {**HEADERS, "Authorization": "Bearer n3w"}
        assert client.post("/mcp", json=body, headers=new).status_code == 200
        old = {**HEADERS, **AUTH}
        assert client.post("/mcp", json=body, headers=old).status_code == 401


def test_dropped_connection_is_a_readable_error(ec2, monkeypatch):
    import httpx

    class Dropping:
        def post(self, *_a, **_k):
            raise httpx.ReadError("connection reset")

    monkeypatch.setattr(proxy, "_client", Dropping)
    with pytest.raises(ToolError, match="dropped"):
        proxy._call("search", {"query": "x"})
    assert "host" not in proxy._cache
