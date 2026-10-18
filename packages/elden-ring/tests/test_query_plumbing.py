"""Query plumbing (#458) and untyped shared-name resolution (#468), offline."""

import inspect

import pytest

import elden_ring._client as _client
from elden_ring import mcp_server


def _doc(name, version, entity_type, **kw):
    return {"name": name, "entity_type": entity_type, "patch_version": version, **kw}


class _Client:
    """Fake OpenSearch: term filters over ``docs``, newest patch first with
    entity_type breaking ties, and the versions / types terms aggregations."""

    def __init__(self, docs):
        self.docs, self.bodies = list(docs), []

    def search(self, index, body):
        self.bodies.append(body)
        filters = body.get("query", {}).get("bool", {}).get("filter", [])
        hits = [d for d in self.docs if all(self._ok(d, f) for f in filters)]
        hits.sort(key=lambda d: d["entity_type"])
        hits.sort(key=lambda d: _client._ver_key(d["patch_version"]), reverse=True)
        aggs = body.get("aggs", {})
        out: dict = {"hits": {"hits": [{"_source": d} for d in hits][: body["size"]]}}
        if "versions" in aggs:
            vs = {d["patch_version"] for d in hits}
            out["aggregations"] = {"versions": {"buckets": [{"key": v} for v in vs]}}
        if "types" in aggs:
            ts = {d["entity_type"] for d in hits}
            out["aggregations"] = {"types": {"buckets": [{"key": t} for t in ts]}}
        return out

    @staticmethod
    def _ok(doc, f):
        if "term" not in f:
            return True
        ((field, value),) = f["term"].items()
        field = field.removesuffix(".keyword").removesuffix(".folded")
        return doc.get(field) == value

    def version_aggs(self):
        """The entity_type each versions aggregation was scoped to (None = global)."""
        return [
            (b["query"].get("term") or {}).get("entity_type")
            for b in self.bodies
            if "versions" in b.get("aggs", {})
        ]


# Godrick is a boss and an enemy at the same patches; the spell is only at 1.17.0.
_DOCS = [
    _doc("Godrick the Grafted", v, t, description=d)
    for v, d in (("1.10.0", "old"), ("1.17.0", "new"))
    for t in ("enemy", "boss")
] + [_doc("Golden Vow", "1.17.0", "spell")]


# --- #468: deterministic type + the other types carrying the name


def test_untyped_shared_name_resolves_to_first_type_and_lists_the_others():
    out = _client.get_entity(_Client(_DOCS), "Godrick the Grafted")
    assert out["entity_type"] == "boss"  # tie at 1.17.0 -> alphabetical
    assert out["other_entity_types"] == ["enemy"]


def test_typed_or_unshared_name_has_no_other_types():
    c = _Client(_DOCS)
    out = _client.get_entity(c, "Godrick the Grafted", entity_type="enemy")
    assert out["entity_type"] == "enemy" and "other_entity_types" not in out
    # A typed lookup runs no types aggregation.
    assert not any("types" in b.get("aggs", {}) for b in c.bodies)
    assert "other_entity_types" not in _client.get_entity(c, "Golden Vow")


def test_get_entity_at_a_patch_keeps_other_types():
    out = _client.get_entity(
        _Client(_DOCS), "Godrick the Grafted", patch_version="1.10.0"
    )
    assert (out["entity_type"], out["patch_version"]) == ("boss", "1.10.0")
    assert out["other_entity_types"] == ["enemy"]


def test_diff_untyped_shared_name_names_the_other_types():
    out = _client.diff_entities(_Client(_DOCS), "Godrick the Grafted", "1.10", "1.17")
    assert out["entity_type"] == "boss"
    assert out["other_entity_types"] == ["enemy"]
    assert out["changed_fields"] == {"description": {"1.10.0": "old", "1.17.0": "new"}}
    typed = _client.diff_entities(
        _Client(_DOCS), "Godrick the Grafted", "1.10.0", "1.17.0", entity_type="enemy"
    )
    assert typed["entity_type"] == "enemy" and "other_entity_types" not in typed


# --- #458: one version aggregation per scope per call


def test_diff_runs_one_global_and_one_type_version_aggregation():
    c = _Client(_DOCS)
    _client.diff_entities(c, "Godrick the Grafted", "1.10", "1.17.0")
    assert c.version_aggs() == [None, "boss"]


def test_text_changed_between_runs_one_aggregation_per_scope():
    c = _Client(_DOCS)
    out = _client.text_changed_between(c, "enemy", "description", "1.10.0", "1.17")
    assert out["total"] == 1
    assert c.version_aggs() == [None, "enemy"]


def test_get_entity_at_a_patch_runs_one_aggregation_per_scope():
    c = _Client(_DOCS)
    _client.get_entity(c, "Golden Vow", patch_version="1.17")
    assert c.version_aggs() == [None, "spell"]


def test_unloaded_version_errors_before_name_lookup():
    c = _Client(_DOCS)
    out = _client.diff_entities(c, "Nobody", "1.10.0", "1.99")
    assert out["error"].startswith("patch version '1.99' is not loaded")
    assert c.version_aggs() == [None] and len(c.bodies) == 1


def test_canonical_version_is_pure():
    assert _client._canonical_version("1.10", ["1.02", "1.10.0"]) == "1.10.0"
    assert _client._request_version(None, None) is None  # no query without a version


def test_routed_literal_fields_derive_from_literal_fields():
    for routed, suffix in (
        (_client._LITERAL_FIELDS_MORPH, ".morph"),
        (_client._LITERAL_FIELDS_LEMMA, ".lemma"),
    ):
        assert len(routed) == len(_client._LITERAL_FIELDS)
        for base, f in zip(_client._LITERAL_FIELDS, routed):
            ja = base in _client._JP_BASE_FIELDS
            assert f == (base + suffix if ja else base)
    assert "name_ja.morph" in _client._LITERAL_FIELDS_MORPH


def test_queryable_paths_is_cached():
    assert _client._queryable_paths() is _client._queryable_paths()


def test_base_filters_shared_by_both_searches():
    out = _client._base_filters(
        entity_type="weapon",
        patch_version="1.17.0",
        source="EquipParamWeapon",
        include_unavailable=False,
        collapse_variants=True,
        is_dlc=False,
    )
    assert out[:3] == [
        {"term": {"entity_type": "weapon"}},
        {"term": {"patch_version": "1.17.0"}},
        {"term": {"source": "EquipParamWeapon"}},
    ]
    assert {"term": {"is_dlc": False}} in out
    assert len(out) == 6
    assert (
        _client._base_filters(
            entity_type=None,
            patch_version=None,
            source=None,
            include_unavailable=True,
            collapse_variants=False,
            is_dlc=None,
        )
        == []
    )


# --- #458: collapse_affinity removed, keyword hand-offs from the tools


def test_collapse_affinity_removed():
    for fn in (
        mcp_server.search_entities,
        mcp_server.search_entities_literal,
        _client.search,
        _client.search_literal,
        _client._search_literal,
    ):
        assert "collapse_affinity" not in inspect.signature(fn).parameters


# tool -> (client function, wrapper-only params). A limit stays under the cap.
_TOOLS = {
    "search_entities": ("search", ()),
    "search_entities_literal": ("search_literal", ()),
    "get_entity": ("get_entity", ("include_placements",)),
    "diff_entities": ("diff_entities", ()),
    "text_changed_between": ("text_changed_between", ()),
    "list_menu_categories": ("list_menu_categories", ()),
}
_ORIG = {fn: getattr(_client, fn) for fn, _ in _TOOLS.values()}


@pytest.mark.parametrize("tool", sorted(_TOOLS))
def test_tool_forwards_every_param_to_its_name(monkeypatch, tool):
    """Each tool param reaches the client param of the same name (#458): a distinct
    value per param, so a positional mis-bind would show up as a swap."""
    fn, wrapper_only = _TOOLS[tool]
    seen = {}

    def record(client, *args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return {}

    monkeypatch.setattr(_client, "get_client", lambda: "client")
    monkeypatch.setattr(_client, fn, record)
    wrapper = getattr(mcp_server, tool)
    vals = {n: f"{n}-value" for n in inspect.signature(wrapper).parameters}
    if "limit" in vals:
        vals["limit"] = 30
    wrapper(**vals)
    positional = [
        n
        for n, p in inspect.signature(_ORIG[fn]).parameters.items()
        if p.kind is p.POSITIONAL_OR_KEYWORD and n != "client"
    ]
    got = dict(zip(positional, seen["args"])) | seen["kwargs"]
    assert got == {k: v for k, v in vals.items() if k not in wrapper_only}


def test_wrappers_pass_optional_params_by_keyword(monkeypatch):
    """Every optional client param is keyword-only, so a reorder can't mis-bind.

    search_literal keeps ``pattern`` positional (as on main): the live suite
    calls ``search_literal(client, "...")``.
    """
    for fn in (
        "search",
        "search_literal",
        "get_entity",
        "diff_entities",
        "text_changed_between",
        "calculate_attack_rating",
    ):
        for name, p in inspect.signature(getattr(_client, fn)).parameters.items():
            if p.default is not p.empty and (fn, name) != ("search_literal", "pattern"):
                assert p.kind is p.KEYWORD_ONLY, (fn, name)


def test_attack_rating_tool_forwards_by_name(monkeypatch):
    seen = {}
    monkeypatch.setattr(_client, "get_client", lambda: "client")
    monkeypatch.setattr(
        _client,
        "calculate_attack_rating",
        lambda client, *a, **kw: seen.update(args=a, kwargs=kw) or {},
    )
    mcp_server.calculate_attack_rating(
        "Dagger",
        str=11,
        dex=12,
        int=13,
        fai=14,
        arc=15,
        level=5,
        two_handed=True,
        affinity="Heavy",
        patch_version="1.10.0",
        spell="Glintstone Pebble",
    )
    assert seen["args"] == (
        "Dagger",
        {"str": 11, "dex": 12, "int": 13, "fai": 14, "arc": 15},
    )
    assert seen["kwargs"] == {
        "level": 5,
        "two_handed": True,
        "affinity": "Heavy",
        "patch_version": "1.10.0",
        "spell": "Glintstone Pebble",
    }


def test_live_suite_calls_bind_to_client_signatures():
    """The opt-in live suite never runs offline; make sure its calls still bind."""
    import ast
    from pathlib import Path

    src = Path(__file__).with_name("test_integration.py").read_text()
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ("_os", "_client")
        ):
            continue
        fn = getattr(_client, node.func.attr, None)
        if not callable(fn) or any(isinstance(a, ast.Starred) for a in node.args):
            continue
        kws = {k.arg: None for k in node.keywords if k.arg is not None}
        try:
            inspect.signature(fn).bind(*([None] * len(node.args)), **kws)
        except TypeError as exc:
            bad.append((node.lineno, node.func.attr, str(exc)))
    assert not bad, bad
