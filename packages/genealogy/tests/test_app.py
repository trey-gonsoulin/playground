"""Offline tests for the HTTP request path: FastAPI app, lifespan and Lambda handler.

``mcp_lambda.testing`` cycles the lifespan many times, so an mcp upgrade that breaks
re-entry in a warm Lambda container fails here instead of on the second live
invocation (#504). No network access: ``initialize`` and ``tools/list`` never call a tool.
"""

import json

import pytest
from fastapi.testclient import TestClient
from genealogy import lambda_handler
from genealogy.app import create_app
from mcp_lambda import testing
from mcp_lambda.testing import HEADERS

N_TOOLS = 19


@pytest.fixture(autouse=True)
def _offline_env(tmp_path, monkeypatch):
    # Tools are never called, but keep any storage access local, never S3.
    monkeypatch.delenv("TREE_BUCKET", raising=False)
    monkeypatch.setenv("TREE_DIR", str(tmp_path))
    monkeypatch.delenv("GENEALOGY_MCP_SECRET", raising=False)


def test_lifespan_reentry_via_testclient():
    testing.check_reentry(create_app(), n_tools=N_TOOLS)


def test_bearer_secret_enforced_when_set(monkeypatch):
    monkeypatch.setenv("GENEALOGY_MCP_SECRET", "s3cret")
    testing.check_bearer_auth(create_app(), "s3cret")


def test_tool_error_message_reaches_client():
    # mcp hides the text of unexpected exceptions; bad arguments must still say why.
    bad = testing.call("tree_import_gedcom", {"gedcom_text": "0 TRLR", "mode": "x"})
    with TestClient(create_app()) as client:
        result = client.post("/mcp/", json=bad, headers=HEADERS).json()["result"]
    assert result["isError"] is True
    assert "mode must be 'merge' or 'replace'" in result["content"][0]["text"]


def test_lambda_handler_warm_invocations():
    testing.check_lambda(lambda_handler.handler, n_tools=N_TOOLS)


# Lambda refuses synchronous invocation events over 6 MB (6 MiB).
_LAMBDA_EVENT_LIMIT = 6 * 1024 * 1024
# The same cap applies to the response it returns (#509).
_LAMBDA_RESPONSE_LIMIT = _LAMBDA_EVENT_LIMIT


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
            headers=HEADERS,
        )
        assert r.status_code == 200, r.text[:200]
        result = r.json()["result"]
        assert result["isError"] is False, result
        assert json.loads(result["content"][0]["text"])["people"] == 1
        r = client.post(
            "/mcp",
            content=_import_gedcom_body(MAX_REQUEST_BODY_BYTES + 1, line),
            headers=HEADERS,
        )
        assert r.status_code == 413


@pytest.mark.parametrize("line", ["1 SEX F\n", "1 CHR\r\n", "1 BIRT\r\n"])
def test_max_request_body_fits_a_lambda_event(line):
    # API Gateway puts the body in the event as an escaped JSON string, so every
    # newline grows again; GEDCOM made only of very short lines, CRLF ones
    # included, must still fit, so an oversize body gets a 413 (#505).
    from genealogy.mcp_server import MAX_REQUEST_BODY_BYTES

    body = _import_gedcom_body(MAX_REQUEST_BODY_BYTES, line)
    event = testing.http_api_v2_event("/mcp", {})
    event["body"] = body.decode()
    size = len(json.dumps(event).encode())
    assert size < _LAMBDA_EVENT_LIMIT, size


def _export_gedcom_text(size: int, line: str) -> str:
    """GEDCOM whose escaped size in the response (_inline_gedcom_bytes) is ``size``."""
    from genealogy.mcp_server import _inline_gedcom_bytes

    head, tail = "0 HEAD\n1 NOTE ", "\n0 TRLR\n"
    unit = _inline_gedcom_bytes(line)
    text = head + line * ((size - _inline_gedcom_bytes(head + tail)) // unit - 1)
    # Each "x" costs exactly one byte, so pad the NOTE up to the exact size.
    text += "x" * (size - _inline_gedcom_bytes(text + tail))
    assert _inline_gedcom_bytes(text + tail) == size
    return text + tail


def _export(monkeypatch, text: str) -> tuple[dict, int]:
    """Call tree_export_gedcom(include_text=True) through the Lambda handler."""
    from genealogy import mcp_server

    monkeypatch.setattr(mcp_server, "to_gedcom", lambda tree: text)
    testing.new_event_loop()
    call = testing.call("tree_export_gedcom", {"include_text": True})
    resp = lambda_handler.handler(testing.http_api_v2_event("/mcp", call), None)
    assert resp["statusCode"] == 200 and not resp.get("isBase64Encoded"), resp
    result = json.loads(resp["body"])["result"]
    assert result["isError"] is False, result
    # The Python runtime serializes the response with json.dumps (ASCII-escaped).
    return json.loads(result["content"][0]["text"]), len(json.dumps(resp).encode())


@pytest.mark.parametrize("line", ['1 NOTE ""\n', "1 NOTE é\\\n", "1 CHR\r\n", "字"])
def test_max_inline_gedcom_fits_a_lambda_response(monkeypatch, line):
    # #509: the largest inline export still fits Lambda's 6 MB response cap even
    # when it is all quotes, backslashes, non-ASCII or short CRLF lines, and one
    # escaped byte more returns the stored path and a note instead of the text.
    from genealogy.mcp_server import MAX_INLINE_GEDCOM_BYTES

    text = _export_gedcom_text(MAX_INLINE_GEDCOM_BYTES, line)
    out, size = _export(monkeypatch, text)
    assert out["gedcom"] == text and "note" not in out
    assert size < _LAMBDA_RESPONSE_LIMIT, size

    out, size = _export(monkeypatch, text + "x")
    assert "gedcom" not in out and "not returned inline" in out["note"]
    assert out["path"].startswith("exports/tree-")
    assert out["bytes"] == len(text.encode()) + 1
    assert size < 64 * 1024, size


def test_main_cli_runs_uvicorn(monkeypatch):
    import uvicorn
    from genealogy import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    main.cli()
    assert calls == [
        (("genealogy.app:app",), {"host": "0.0.0.0", "port": 8000, "reload": True})
    ]
