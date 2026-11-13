"""Offline tests for the HTTP request path: FastAPI app, lifespan and Lambda handler.

Mangum runs the whole lifespan on every invocation, so a warm Lambda container
re-enters it each time; ``mcp_lambda.ReentrantMCP`` rebuilds the session manager
per entry. These tests cycle the lifespan 20 times, so an mcp upgrade that breaks
re-entry fails here instead of on the second live invocation. Tool calls use the
recorded fixtures through the fake provider; nothing touches the network.
"""

import json

import pytest
from fastapi.testclient import TestClient
from markets._data import DataSourceError
from markets.app import create_app
from mcp_lambda import testing
from mcp_lambda.testing import HEADERS, TOOLS_LIST, call

N_TOOLS = 7


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch, fake_provider):
    monkeypatch.delenv("MARKETS_MCP_SECRET", raising=False)


def test_lifespan_reentry_via_testclient():
    testing.check_reentry(create_app(), n_tools=N_TOOLS)


def test_tool_names():
    with TestClient(create_app()) as client:
        r = client.post("/mcp/", json=TOOLS_LIST, headers=HEADERS)
    assert {t["name"] for t in r.json()["result"]["tools"]} == {
        "quote",
        "price_history",
        "earnings_event_study",
        "implied_volatility",
        "volatility_forecast",
        "price_target_odds",
        "evaluate_sell_plan",
    }


def test_tool_call_and_tool_error():
    with TestClient(create_app()) as client:
        r = client.post(
            "/mcp/", json=call("quote", {"ticker": "AAPL"}), headers=HEADERS
        )
        result = r.json()["result"]
        assert result["isError"] is False, result
        body = json.loads(result["content"][0]["text"])
        assert body["spot_source"] == "post_market"
        assert body["source"] == "yahoo"

        r = client.post(
            "/mcp/",
            json=call("implied_volatility", {"ticker": "SPYX"}),
            headers=HEADERS,
        )
        result = r.json()["result"]
        assert result["isError"] is True
        assert "no listed options" in result["content"][0]["text"]


def test_data_source_error_message_reaches_client(fake_provider, monkeypatch):
    # mcp hides the text of unexpected exceptions; Yahoo failures must still say "retry later".
    def refuse(ticker):
        raise DataSourceError(
            "Yahoo Finance refused or failed the request (HTTP 429); retry later"
        )

    monkeypatch.setattr(fake_provider, "quote", refuse)
    with TestClient(create_app()) as client:
        r = client.post(
            "/mcp/", json=call("quote", {"ticker": "AAPL"}), headers=HEADERS
        )
    result = r.json()["result"]
    assert result["isError"] is True
    assert "HTTP 429); retry later" in result["content"][0]["text"]


def test_bearer_secret_enforced_when_set(monkeypatch):
    monkeypatch.setenv("MARKETS_MCP_SECRET", "s3cret")
    testing.check_bearer_auth(create_app(), "s3cret")


def test_lambda_handler_warm_invocations():
    from markets import lambda_handler  # Mangum needs the loop set by check_lambda

    testing.check_lambda(lambda_handler.handler, n_tools=N_TOOLS)
    status, body = testing.invoke(
        lambda_handler.handler, "/mcp/", call("quote", {"ticker": "AAPL"})
    )
    assert status == 200 and body["result"]["isError"] is False, body


def test_main_cli_runs_uvicorn(monkeypatch):
    import uvicorn
    from markets import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    main.cli()
    assert calls == [
        (("markets.app:app",), {"host": "0.0.0.0", "port": 8003, "reload": True})
    ]
