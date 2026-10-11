"""regions / locations literal searches also match drop_regions / drop_locations (#344).

No OpenSearch needed.
"""

import inspect

from elden_ring import _client, mcp_server
from elden_ring._client import _FIELD_NOTES, _with_drop_fields


def test_with_drop_fields_adds_twins_after_their_field():
    assert _with_drop_fields(["locations"]) == ["locations", "drop_locations"]
    assert _with_drop_fields(["name", "regions"]) == ["name", "regions", "drop_regions"]
    assert _with_drop_fields(["regions", "locations"]) == [
        "regions",
        "drop_regions",
        "locations",
        "drop_locations",
    ]


def test_with_drop_fields_leaves_other_fields_alone():
    assert _with_drop_fields(["description"]) == ["description"]
    assert _with_drop_fields(["drop_regions"]) == ["drop_regions"]
    assert _with_drop_fields(["areas"]) == ["areas"]
    # already named: no duplicate
    assert _with_drop_fields(["regions", "drop_regions"]) == ["regions", "drop_regions"]


def _capture(monkeypatch):
    monkeypatch.setattr(_client, "_canonical_version", lambda c, v: "1.17.1")
    seen = {}

    def fake(client, **kw):
        seen.update(kw)
        return {"total": 1, "results": []}

    monkeypatch.setattr(_client, "_search_literal", fake)
    return seen


def test_search_literal_expands_by_default(monkeypatch):
    seen = _capture(monkeypatch)
    _client.search_literal(None, pattern="Murkwater Catacombs", fields=["locations"])
    assert seen["fields"] == ["locations", "drop_locations"]


def test_search_literal_opt_out_keeps_pickups_only(monkeypatch):
    seen = _capture(monkeypatch)
    _client.search_literal(
        None, pattern="Caelid", fields=["regions"], include_drop_fields=False
    )
    assert seen["fields"] == ["regions"]


def test_search_literal_default_fields_untouched(monkeypatch):
    seen = _capture(monkeypatch)
    _client.search_literal(None, pattern="x")
    assert seen["fields"] is None


def test_empty_result_warns_only_about_named_fields(monkeypatch):
    """The drop twins are searched but never warned about (#344 review)."""
    monkeypatch.setattr(_client, "_canonical_version", lambda c, v: v)
    monkeypatch.setattr(
        _client, "_search_literal", lambda client, **kw: {"total": 0, "results": []}
    )
    monkeypatch.setattr(_client, "_check_filter_values", lambda *a: None)
    warned = []

    def fake_warnings(client, fields, entity_type):
        warned.append(list(fields))
        return [f"{f}: no {entity_type} documents carry this field" for f in fields]

    monkeypatch.setattr(_client, "_field_coverage_warnings", fake_warnings)
    out = _client.search_literal(
        None, pattern="Nowhere", fields=["regions"], entity_type="enemy"
    )
    assert warned == [["regions"]]
    assert not any("drop_regions" in w for w in out["warnings"])


def test_expanded_fields_reach_one_multi_match(monkeypatch):
    """Both fields sit in one multi_match under must, filters beside it (no should)."""
    monkeypatch.setattr(_client, "_canonical_version", lambda c, v: "1.17.1")
    bodies = []

    class _C:
        def search(self, index, body):
            bodies.append(body)
            return {"hits": {"total": {"value": 1}, "hits": []}}

    _client.search_literal(
        _C(), pattern="Caelid", fields=["regions"], patch_version="1.17.1"
    )
    q = bodies[0]["query"]["bool"]
    assert q["must"][0]["multi_match"]["fields"] == ["regions", "drop_regions"]
    assert "should" not in q


def test_tool_passes_include_drop_fields(monkeypatch):
    params = list(inspect.signature(_client.search_literal).parameters)[1:]
    seen = {}
    monkeypatch.setattr(_client, "get_client", lambda: None)
    monkeypatch.setattr(
        _client, "search_literal", lambda *a: seen.setdefault("args", a)
    )
    mcp_server.search_entities_literal(fields=["regions"], include_drop_fields=False)
    assert seen["args"][1 + params.index("include_drop_fields")] is False


def test_field_notes_describe_the_default():
    for f in ("regions", "locations", "drop_regions", "drop_locations"):
        assert "#344" in _FIELD_NOTES[f]
    assert "include_drop_fields=False" in _FIELD_NOTES["regions"]
    assert "include_drop_fields=False" in _FIELD_NOTES["locations"]
