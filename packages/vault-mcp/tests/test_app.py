"""HTTP layer: bearer auth (fail closed), read-only mode and the tool list."""

import pytest
from conftest import _reset
from fastapi.testclient import TestClient
from mcp_lambda.testing import HEADERS, INITIALIZE, TOOLS_LIST, call
from vault_mcp.app import create_app

WRITE_TOOLS = {
    "create_note",
    "update_note",
    "append_to_note",
    "move_note",
    "delete_note",
    "bulk_update",
}


@pytest.fixture
def token(vault, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_TOKEN", "s3cret")
    return {**HEADERS, "Authorization": "Bearer s3cret"}


def _tools(client, headers) -> set[str]:
    r = client.post("/mcp", json=TOOLS_LIST, headers=headers)
    assert r.status_code == 200, r.text
    return {t["name"] for t in r.json()["result"]["tools"]}


def test_refuses_to_start_without_token(vault, monkeypatch):
    monkeypatch.delenv("VAULT_MCP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="VAULT_MCP_TOKEN"):
        create_app()


def test_auth_required(token):
    with TestClient(create_app()) as client:
        assert client.post("/mcp", json=INITIALIZE, headers=HEADERS).status_code == 401
        bad = {**HEADERS, "Authorization": "Bearer nope"}
        assert client.post("/mcp", json=INITIALIZE, headers=bad).status_code == 401
        assert client.get("/healthz").status_code == 401
        assert client.post("/mcp", json=INITIALIZE, headers=token).status_code == 200
        assert client.get("/healthz", headers=token).json()["vault_found"]


def test_all_tools_listed(token):
    with TestClient(create_app()) as client:
        tools = _tools(client, token)
    assert (
        WRITE_TOOLS <= tools
        and {"search", "read_note", "daily_note", "health"} <= tools
    )
    assert len(tools) == 33


def test_readonly_hides_write_tools(token, monkeypatch, vault):
    monkeypatch.setenv("VAULT_MCP_READONLY", "1")
    _reset()
    with TestClient(create_app()) as client:
        tools = _tools(client, token)
        assert not (WRITE_TOOLS & tools) and "daily_note" in tools
        daily = call("daily_note", {"day": "2030-01-01"})
        r = client.post("/mcp", json=daily, headers=token).json()
    assert r["result"]["structuredContent"]["exists"] is False
    assert not (vault / "Personal" / "Daily" / "2030-01-01.md").exists()


def test_tool_call_over_http(token):
    with TestClient(create_app()) as client:
        r = client.post(
            "/mcp", json=call("search", {"query": "killer"}), headers=token
        ).json()
    paths = [x["path"] for x in r["result"]["structuredContent"]["results"]]
    assert "TTRPG/Murder on the Eberron Express/NPCs/Vex.md" in paths
