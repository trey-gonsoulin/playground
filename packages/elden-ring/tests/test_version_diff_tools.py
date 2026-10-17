"""diff_entities / text_changed_between: resolved type and as-of versions (#439),
non-leaf field paths and paging (#438), and dict-only tool returns (#441)."""

import asyncio
import json
import typing

import pytest
from test_filter_validation import _Client, _doc

from elden_ring import _client, mcp_server


class _PagingClient(_Client):
    """Fake that honours size, a name.keyword sort and search_after, like OpenSearch."""

    def search(self, index, body):
        resp = super().search(index, body)
        if body.get("sort") != [{"name.keyword": "asc"}]:
            return resp
        hits = sorted(resp["hits"]["hits"], key=lambda h: h["_source"]["name"])
        if after := body.get("search_after"):
            hits = [h for h in hits if h["_source"]["name"] > after[0]]
        hits = [{**h, "sort": [h["_source"]["name"]]} for h in hits[: body["size"]]]
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}}


_LINES = [
    _doc("Line", "1.02.0", "npc_dialogue", text_content="old"),
    _doc("Line", "1.17.0", "npc_dialogue", text_content="new"),
]
_DAGGERS = [
    _doc("Dagger", v, attack=i) for i, v in enumerate(["1.02.0", "1.10.0", "1.17.0"])
]


def test_diff_sparse_type_without_entity_type():
    out = _client.diff_entities(
        _Client([*_LINES, *_DAGGERS]), "Line", "1.10.0", "1.17.0"
    )
    assert out["entity_type"] == "npc_dialogue"
    assert out["changed_fields"] == {"text_content": {"1.10.0": "old", "1.17.0": "new"}}
    assert out["resolved_patch_versions"] == {"1.10.0": "1.02.0"}
    assert "note" not in out


def test_diff_one_snapshot_says_so():
    out = _client.diff_entities(
        _Client([*_LINES, *_DAGGERS]), "Line", "1.02.0", "1.10.0"
    )
    assert out["changed"] is False
    assert "both resolve to the one npc_dialogue snapshot 1.02.0" in out["note"]


def test_diff_shared_name_stays_on_the_resolved_type():
    docs = [
        _doc("Lightning Spear", "1.17.0", attack=2),
        _doc("Lightning Spear", "1.02.0", "spell", fp=20),
        _doc("Lightning Spear", "1.17.0", "spell", fp=18),
        _doc("Lightning Spear", "1.02.0", attack=1),
    ]
    out = _client.diff_entities(_Client(docs), "Lightning Spear", "1.02.0", "1.17.0")
    assert out["entity_type"] == "weapon"
    assert out["changed_fields"] == {"attack": {"1.02.0": 1, "1.17.0": 2}}
    assert "resolved_patch_versions" not in out
    spell = _client.diff_entities(
        _Client(docs), "Lightning Spear", "1.02.0", "1.17.0", entity_type="spell"
    )
    assert spell["changed_fields"] == {"fp": {"1.02.0": 20, "1.17.0": 18}}


def test_diff_errors():
    c = _Client(_DAGGERS)
    assert "not loaded" in _client.diff_entities(c, "Dagger", "1.99", "1.17.0")["error"]
    out = _client.diff_entities(c, "Nobody", "1.02.0", "1.17.0")
    assert out["error"] == "'Nobody' not found in any loaded version"
    c = _Client([_doc("Milady", "1.17.0"), *_DAGGERS])
    out = _client.diff_entities(c, "Milady", "1.02.0", "1.17.0")
    assert out["error"] == "'Milady' not present in 1.02.0"


_SPELLS = [
    _doc(
        name,
        v,
        "weapon",
        attack_power={"magic": m},
        stats={"hp": m},
        spell_attacks=[{"attack_power": {"magic": m}}],
    )
    for name, v, m in (
        ("Pebble", "1.02.0", 50),
        ("Pebble", "1.17.0", 60),
        ("Comet", "1.02.0", 90),
        ("Comet", "1.17.0", 90),
    )
]


def test_text_changed_leaf_and_whole_group():
    c = _PagingClient(_SPELLS)
    out = _client.text_changed_between(
        c, "weapon", "attack_power.magic", "1.02", "1.17"
    )
    assert out["total"] == 1
    assert out["results"] == [{"name": "Pebble", "text_before": 50, "text_after": 60}]
    assert out["requested_patch_versions"] == {"1.02": "1.02.0", "1.17": "1.17.0"}
    out = _client.text_changed_between(c, "weapon", "stats", "1.02.0", "1.17.0")
    assert out["results"][0]["text_after"] == {"hp": 60}


def test_text_changed_rejects_paths_with_no_single_value():
    c = _PagingClient(_SPELLS)
    for field in ("name.keyword", "description_ja.lemma", "stats.*"):
        assert "error" in _client.text_changed_between(
            c, "weapon", field, "1.02.0", "1.17.0"
        )
    assert c.bodies == []
    out = _client.text_changed_between(
        c, "weapon", "spell_attacks.attack_power.magic", "1.02.0", "1.17.0"
    )
    assert "runs through 'spell_attacks'" in out["error"]
    out = _client.text_changed_between(c, "weapon", "spell_attacks", "1.02.0", "1.17.0")
    assert [r["name"] for r in out["results"]] == ["Pebble"]


def test_text_changed_pages_past_one_page(monkeypatch):
    monkeypatch.setattr(_client, "_CHANGED_PAGE_SIZE", 2)
    docs = [
        _doc(f"W{i}", v, description=f"{v} {i}" if i % 2 else "same")
        for i in range(5)
        for v in ("1.02.0", "1.17.0")
    ]
    c = _PagingClient(docs)
    out = _client.text_changed_between(c, "weapon", "description", "1.02.0", "1.17.0")
    assert [r["name"] for r in out["results"]] == ["W1", "W3"]
    assert sum("search_after" in b for b in c.bodies) == 4  # 3 pages per version
    assert _client.text_changed_between(
        c, "weapon", "description", "1.02.0", "1.17.0", count_only=True
    ) == {"total": 2}


def test_text_changed_sparse_type_reports_resolution():
    c = _PagingClient([*_LINES, *_DAGGERS])
    out = _client.text_changed_between(
        c, "npc_dialogue", "text_content", "1.10.0", "1.17.0"
    )
    assert out["total"] == 1 and out["resolved_patch_versions"] == {"1.10.0": "1.02.0"}
    out = _client.text_changed_between(
        c, "npc_dialogue", "text_content", "1.02.0", "1.10.0", count_only=True
    )
    assert out["total"] == 0 and "both resolve" in out["note"]


def test_attack_rating_rejects_unloaded_version():
    out = _client.calculate_attack_rating(
        _Client(_DAGGERS), "Dagger", {}, None, False, None, "1.99"
    )
    assert out["error"].startswith("patch version '1.99' is not loaded")


# Every registered tool, called through FastMCP, returns one JSON-object block (#441).
_ARGS = {
    "search_entities": {"query": "x"},
    "get_entity": {"name": "Nobody"},
    "calculate_attack_rating": {"weapon": "Nobody"},
    "list_menu_categories": {"entity_type": "weapon"},
    "analyze_text": {"text": "x"},
    "search_entities_literal": {"pattern": "x"},
    "text_changed_between": {
        "entity_type": "weapon",
        "field": "description",
        "v1": "1.02.0",
        "v2": "1.17.0",
    },
    "diff_entities": {"name": "Nobody", "v1": "1.02.0", "v2": "1.17.0"},
}


class _Aggs(dict):
    """Any agg a tool asks for comes back empty."""

    def __missing__(self, key):
        return {"buckets": [], "value": 0, "doc_count": 0}


class _ToolClient(_PagingClient):
    """Empty index that answers every query and agg shape the tools send."""

    class indices:  # noqa: N801 — mirrors client.indices
        @staticmethod
        def analyze(index, body):
            return {"tokens": []}

    def search(self, index, body):
        resp = super().search(index, body)
        resp.setdefault("hits", {"hits": [], "total": {"value": 0}})
        resp["aggregations"] = _Aggs(resp.get("aggregations", {}))
        return resp


def _tools():
    return asyncio.run(mcp_server.mcp.list_tools())


@pytest.mark.parametrize("tool", [t.name for t in _tools()])
def test_every_tool_returns_one_json_object(monkeypatch, tool):
    if tool == "start_search_service":
        monkeypatch.setattr(_client, "start_instance", lambda **_: "host")
    monkeypatch.setattr(_client, "get_client", lambda: _ToolClient())
    out = asyncio.run(mcp_server.mcp.call_tool(tool, _ARGS.get(tool, {})))
    blocks = out[0] if isinstance(out, tuple) else out
    assert len(blocks) == 1, blocks
    assert isinstance(json.loads(blocks[0].text), dict)


@pytest.mark.parametrize("tool", [t.name for t in _tools()])
def test_every_tool_is_annotated_dict(tool):
    fn = getattr(mcp_server, tool)
    assert typing.get_type_hints(fn)["return"] is dict
