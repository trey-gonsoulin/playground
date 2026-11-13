"""Offline tests for the HTTP request path: FastAPI app, lifespan and Lambda handler.

``mcp_lambda.testing`` cycles the lifespan many times, so an mcp upgrade that breaks
re-entry in a warm Lambda container fails here instead of on the second live
invocation (#504). No network access: ``initialize`` and ``tools/list`` never call a tool.
"""

import pytest
from mcp_lambda import testing
from ynab_mcp import lambda_handler
from ynab_mcp.app import create_app

N_TOOLS = 11


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch):
    monkeypatch.delenv("YNAB_MCP_SECRET", raising=False)


def test_lifespan_reentry_via_testclient():
    testing.check_reentry(create_app(), n_tools=N_TOOLS)


def test_bearer_secret_enforced_when_set(monkeypatch):
    monkeypatch.setenv("YNAB_MCP_SECRET", "s3cret")
    testing.check_bearer_auth(create_app(), "s3cret")


def test_lambda_handler_warm_invocations():
    testing.check_lambda(lambda_handler.handler, n_tools=N_TOOLS)


def test_main_cli_runs_uvicorn(monkeypatch):
    import uvicorn
    from ynab_mcp import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    main.cli()
    assert calls == [
        (("ynab_mcp.app:app",), {"host": "0.0.0.0", "port": 8001, "reload": True})
    ]
