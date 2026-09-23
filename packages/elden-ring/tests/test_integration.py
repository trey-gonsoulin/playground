"""Integration test against the live deployed stack.

Requires real AWS credentials and a running (or stopped) EC2 instance.
Set env vars before running:

    export AWS_PROFILE=personal
    export AWS_REGION=us-east-1
    export EC2_INSTANCE_ID=i-xxxxxxxxxxxxxxxxxxxx   # from sam deploy output
    export OPENSEARCH_USER=admin
    export OPENSEARCH_PASSWORD_SSM_PATH=/elden-ring/opensearch-password

Then run:
    uv run --package elden-ring pytest packages/elden-ring/tests/test_integration.py -v -s
"""

import os

import pytest

REQUIRED_ENV = ["EC2_INSTANCE_ID", "OPENSEARCH_USER", "OPENSEARCH_PASSWORD_SSM_PATH"]
missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if missing:
    pytest.skip(
        f"Integration env vars not set: {missing}",
        allow_module_level=True,
    )

# Import after env-var check so the skip fires before any import side-effects.
import elden_ring._client as _os


@pytest.fixture(scope="module")
def client():
    """Start the EC2 instance once for the whole test module, yield the client."""
    print("\nStarting search service (may take ~2-3 min on cold start)…")
    endpoint = _os.start_instance(timeout_seconds=300)
    print(f"OpenSearch healthy at {endpoint}")

    # Ensure the index exists so queries don't 404.
    c = _os.get_client()
    _os.ensure_index(c)
    yield c


def test_cluster_health(client):
    """OpenSearch cluster should report green or yellow."""
    health = client.cluster.health()
    assert health["status"] in ("green", "yellow"), health


def test_index_exists(client):
    """The entities index should exist after ensure_index."""
    assert client.indices.exists(index=_os.INDEX)


def test_search_empty_returns_list(client):
    """Search with no matching documents returns an empty list (not an error).

    Uses a nonsense token so the invariant holds against the populated production
    index too (the original 'bleed katana' matched real weapons).
    """
    results = _os.search(client, "zzq_no_such_search_token_qzz")
    assert isinstance(results, list)
    assert results == []


def test_get_entity_missing_returns_none(client):
    """get_entity returns None (not an error) when no entity matches.

    Uses a name guaranteed absent so it holds against the populated index too.
    """
    result = _os.get_entity(client, "__no_such_entity_zzq__")
    assert result is None


def test_list_entity_types_empty(client):
    """list_entity_types returns an empty list on an index with no documents.

    This invariant only holds on a fresh index; against the populated production
    index it returns the live type list, so the empty case is skipped there.
    """
    if client.count(index=_os.INDEX)["count"] > 0:
        pytest.skip("index is populated; empty-index invariant only holds on a fresh index")
    types = _os.list_entity_types(client)
    assert isinstance(types, list)
    assert types == []


def test_index_and_retrieve(client):
    """Round-trip: index a document, search for it, retrieve it, then delete it."""
    doc = {
        "entity_type": "weapon",
        "name": "__test_sword__",
        "patch_version": "test",
        "source": "test",
        "description": "A test weapon used only for integration testing.",
        "text_content": "integration test placeholder",
        "tags": ["test"],
        "req_str": 10,
        "req_dex": 20,
        "attack_physical": 100,
    }
    doc_id = "weapon::__test_sword__::test"

    try:
        # Index
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        # Search
        results = _os.search(
            client, "integration test placeholder", entity_type="weapon"
        )
        names = [r["name"] for r in results]
        assert "__test_sword__" in names, f"Expected test doc in results, got: {names}"

        # Get by exact name
        retrieved = _os.get_entity(client, "__test_sword__", entity_type="weapon")
        assert retrieved is not None
        assert retrieved["req_dex"] == 20
        assert retrieved["attack_physical"] == 100

        # entity type aggregation
        types = _os.list_entity_types(client)
        assert "weapon" in types

    finally:
        # Always clean up the test document
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_count_only(client):
    """count_only=True returns {"total": N} instead of a document list."""
    doc = {
        "entity_type": "weapon",
        "name": "__test_count_sword__",
        "patch_version": "test",
        "source": "test",
        "description": "count_only integration test weapon.",
        "text_content": "count only test placeholder",
    }
    doc_id = "weapon::__test_count_sword__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.search(
            client, "count only test placeholder", entity_type="weapon", count_only=True
        )
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "total" in result
        assert result["total"] >= 1

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_include_fields(client):
    """include_fields restricts returned document fields."""
    doc = {
        "entity_type": "weapon",
        "name": "__test_fields_sword__",
        "patch_version": "test",
        "source": "test",
        "description": "include_fields integration test weapon.",
        "text_content": "fields test placeholder",
        "req_str": 15,
    }
    doc_id = "weapon::__test_fields_sword__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        results = _os.search(
            client,
            "fields test placeholder",
            entity_type="weapon",
            include_fields=["name"],
        )
        assert results, "Expected at least one result"
        for hit in results:
            # score is added by the client layer, not from _source
            keys = {k for k in hit if k != "score"}
            assert keys == {"name"}, f"Unexpected keys in projected doc: {keys}"

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_count_only(client):
    """search_literal count_only=True returns {"total": N} with no results key."""
    doc = {
        "entity_type": "weapon",
        "name": "__test_lit_count__",
        "patch_version": "test",
        "source": "test",
        "description": "literal count only test unique xyz987.",
        "text_content": "",
    }
    doc_id = "weapon::__test_lit_count__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.search_literal(
            client, "literal count only test unique xyz987", count_only=True
        )
        assert isinstance(result, dict)
        assert "total" in result
        assert "results" not in result
        assert result["total"] >= 1

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_match_all(client):
    """search_literal with no pattern enumerates entities via match_all."""
    doc = {
        "entity_type": "weapon",
        "name": "__test_match_all__",
        "patch_version": "test",
        "source": "test",
        "description": "match_all test weapon.",
        "text_content": "",
    }
    doc_id = "weapon::__test_match_all__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.search_literal(client, entity_type="weapon", patch_version="test")
        assert "total" in result
        assert "results" in result
        names = [r["name"] for r in result["results"]]
        assert "__test_match_all__" in names

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_sort_id_mod(client):
    """sort_id_mod filter keeps only entities where sort_id % mod == remainder."""
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_sortid_match__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "sort_id": 2000,  # 2000 % 1000 == 0 → should match
        },
        {
            "entity_type": "weapon",
            "name": "__test_sortid_nomatch__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "sort_id": 2500,  # 2500 % 1000 == 500 → should not match
        },
    ]
    ids = [
        "weapon::__test_sortid_match__::test",
        "weapon::__test_sortid_nomatch__::test",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.search_literal(
            client,
            entity_type="weapon",
            patch_version="test",
            sort_id_mod=1000,
            sort_id_remainder=0,
        )
        names = [r["name"] for r in result["results"]]
        assert "__test_sortid_match__" in names, f"Expected match, got: {names}"
        assert "__test_sortid_nomatch__" not in names, (
            f"Expected no-match excluded, got: {names}"
        )

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_sort_id_range(client):
    """sort_id_gte / sort_id_lte filter entities by sort_id range."""
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_sortid_low__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "sort_id": 100,
        },
        {
            "entity_type": "weapon",
            "name": "__test_sortid_high__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "sort_id": 900,
        },
    ]
    ids = [
        "weapon::__test_sortid_low__::test",
        "weapon::__test_sortid_high__::test",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.search_literal(
            client,
            entity_type="weapon",
            patch_version="test",
            sort_id_gte=200,
            sort_id_lte=1000,
        )
        names = [r["name"] for r in result["results"]]
        assert "__test_sortid_high__" in names, f"Expected high in range, got: {names}"
        assert "__test_sortid_low__" not in names, (
            f"Expected low excluded, got: {names}"
        )

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_patterns_single_element_equals_pattern(client):
    """patterns=[X] must return the same total as pattern=X for any X.

    Regression for #64 — a single-element patterns array was silently returning 0
    for strings that matched correctly via the singular pattern= parameter. The bug
    was specific to strings that came through the patterns code path; the fix routes
    patterns=[X] through the same recursive keyword-arg path as pattern=X.
    """
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_parity_a__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "呪具テスト固有",
        },
        {
            "entity_type": "weapon",
            "name": "__test_parity_b__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "護身具テスト固有",
        },
        {
            "entity_type": "weapon",
            "name": "__test_parity_c__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "描くテスト固有",
        },
    ]
    ids = [
        "weapon::__test_parity_a__::test",
        "weapon::__test_parity_b__::test",
        "weapon::__test_parity_c__::test",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        for pat in ["呪具テスト固有", "護身具テスト固有", "描くテスト固有"]:
            r_singular = _os.search_literal(client, pattern=pat, source="test")
            r_array = _os.search_literal(client, patterns=[pat], source="test")
            assert r_array["total"] == r_singular["total"], (
                f"patterns=['{pat}'] total {r_array['total']} != "
                f"pattern='{pat}' total {r_singular['total']} — "
                "single-element patterns array must equal singular pattern"
            )

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_patterns_or_union_shared_token(client):
    """patterns OR returns the union when patterns share a CJK token.

    Regression for #64 — patterns=["呪具","護身具"] dropped 呪具 (Magma Whip Candlestick)
    because both patterns tokenize to include 具. The shared token caused the nested
    bool.must > bool.should query to silently under-count. Uses patch_version=None so
    the cardinality-agg + collapse path is exercised (the failing code path).
    """
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_or_shared_a__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "呪具テスト固有文字列",
        },
        {
            "entity_type": "weapon",
            "name": "__test_or_shared_b__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "護身具テスト固有文字列",
        },
    ]
    ids = [
        "weapon::__test_or_shared_a__::test",
        "weapon::__test_or_shared_b__::test",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        # Baseline: each pattern alone finds its document.
        result_a = _os.search_literal(
            client, pattern="呪具テスト固有文字列", source="test"
        )
        names_a = [r["name"] for r in result_a["results"]]
        assert "__test_or_shared_a__" in names_a, (
            f"Single-pattern baseline failed: {names_a}"
        )

        result_b = _os.search_literal(
            client, pattern="護身具テスト固有文字列", source="test"
        )
        names_b = [r["name"] for r in result_b["results"]]
        assert "__test_or_shared_b__" in names_b, (
            f"Single-pattern baseline failed: {names_b}"
        )

        # OR across token-sharing patterns must return the union.
        result_or = _os.search_literal(
            client,
            patterns=["呪具テスト固有文字列", "護身具テスト固有文字列"],
            source="test",
        )
        names_or = [r["name"] for r in result_or["results"]]
        assert "__test_or_shared_a__" in names_or, (
            f"Token-sharing OR dropped 呪具 pattern; got names={names_or}, total={result_or['total']}"
        )
        assert "__test_or_shared_b__" in names_or, (
            f"Token-sharing OR dropped 護身具 pattern; got names={names_or}, total={result_or['total']}"
        )
        assert result_or["total"] == 2, (
            f"Expected total=2 for union of two disjoint singletons, got {result_or['total']}"
        )

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_patterns_or_union_disjoint_token(client):
    """patterns OR returns the union for token-disjoint patterns (positive control).

    Regression for #64 — verifies that the OR fix didn't break the working case.
    描く and 擬す share no CJK tokens; this pair worked before the fix and must
    continue to work after.
    """
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_or_disjoint_a__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "描くテスト固有文字列",
        },
        {
            "entity_type": "weapon",
            "name": "__test_or_disjoint_b__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "擬すテスト固有文字列",
        },
    ]
    ids = [
        "weapon::__test_or_disjoint_a__::test",
        "weapon::__test_or_disjoint_b__::test",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.search_literal(
            client,
            patterns=["描くテスト固有文字列", "擬すテスト固有文字列"],
            source="test",
        )
        names = [r["name"] for r in result["results"]]
        assert "__test_or_disjoint_a__" in names, (
            f"Token-disjoint OR dropped 描く pattern; got names={names}, total={result['total']}"
        )
        assert "__test_or_disjoint_b__" in names, (
            f"Token-disjoint OR dropped 擬す pattern; got names={names}, total={result['total']}"
        )
        assert result["total"] == 2, (
            f"Expected total=2 for union of two disjoint singletons, got {result['total']}"
        )

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_fields_composes_with_lemmatize(client):
    """An explicit fields arg must not silently defeat use_lemmatize (#65).

    The lemmatizing analyzer lives only on the .lemma subfield, so a plain
    fields=["description_ja"] used to search the surface field and return a hard zero
    on a verb that appears only in inflected forms. The doc below carries 呼ばれる
    (an inflection of 呼ぶ), reachable only via the lemma analyzer; querying the
    baseform 呼ぶ with an explicit description_ja field must find it and return the
    same total as the fields-omitted query.
    """
    doc = {
        "entity_type": "weapon",
        "name": "__test_lemma_fields__",
        "patch_version": "test",
        "source": "test",
        "description": "",
        "text_content": "",
        "description_ja": "魔力を宿すと呼ばれるテスト固有文字列",
    }
    doc_id = "weapon::__test_lemma_fields__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        # Baseline: fields omitted, lemmatize on — baseform 呼ぶ finds the inflected doc.
        result_omitted = _os.search_literal(
            client, pattern="呼ぶ", use_lemmatize=True, source="test"
        )
        names_omitted = [r["name"] for r in result_omitted["results"]]
        assert "__test_lemma_fields__" in names_omitted, (
            f"Lemmatize baseline (no fields) failed to match inflected form: {names_omitted}"
        )

        # Bug case: explicit description_ja field must route to .lemma and match too.
        result_fields = _os.search_literal(
            client,
            pattern="呼ぶ",
            fields=["description_ja"],
            use_lemmatize=True,
            source="test",
        )
        names_fields = [r["name"] for r in result_fields["results"]]
        assert "__test_lemma_fields__" in names_fields, (
            f"fields=['description_ja'] with use_lemmatize dropped the inflected match; "
            f"got names={names_fields}, total={result_fields['total']}"
        )
        assert result_fields["total"] == result_omitted["total"], (
            f"fields arg changed the count: with={result_fields['total']} "
            f"without={result_omitted['total']}"
        )

        # Isolation control: without lemmatize, the surface field does not match the
        # baseform — proving the match above comes from lemma routing, not coincidence.
        result_surface = _os.search_literal(
            client, pattern="呼ぶ", fields=["description_ja"], source="test"
        )
        names_surface = [r["name"] for r in result_surface["results"]]
        assert "__test_lemma_fields__" not in names_surface, (
            f"Surface-form query unexpectedly matched the inflected doc: {names_surface}"
        )

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_text_changed_between_count_only(client):
    """text_changed_between count_only=True returns {"total": N}, not a full list.

    Regression for commit a0c6d9f — count_only was accepted by the MCP tool but not
    forwarded to _client.text_changed_between, so the full diff list was always returned.
    """
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_tcb__",
            "patch_version": "test-v1",
            "source": "test",
            "description": "old description unique abc123",
            "text_content": "",
        },
        {
            "entity_type": "weapon",
            "name": "__test_tcb__",
            "patch_version": "test-v2",
            "source": "test",
            "description": "new description unique abc123",
            "text_content": "",
        },
    ]
    ids = [
        "weapon::__test_tcb__::test-v1",
        "weapon::__test_tcb__::test-v2",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.text_changed_between(
            client,
            entity_type="weapon",
            field="description",
            v1="test-v1",
            v2="test-v2",
            count_only=True,
        )
        assert isinstance(result, dict), (
            f"Expected dict with count_only=True, got {type(result)}"
        )
        assert "total" in result, f"Expected 'total' key, got {result}"
        assert "text_before" not in str(result), (
            "Full diff list leaked through with count_only=True"
        )
        assert result["total"] >= 1

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_reload_prunes_stale_docs_in_scope(client):
    """Reloading a version with --prune retracts docs the new build no longer produces.

    Regression for #67 — the additive loader never deleted, so enemies stamped onto
    historical patches from 1.17 NpcName (e.g. Messmer at 1.02) survived every
    correct per-version rebuild. Pruning is scoped to the reloaded patch_version and
    to entity_types present in the dataset.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from load_data import load_documents, prune_stale

    def _d(entity_type, name, version="test-prune"):
        return {"entity_type": entity_type, "name": f"__prune_{name}__",
                "patch_version": version, "source": "test"}

    seeded = [
        _d("enemy", "A"), _d("enemy", "B"), _d("weapon", "W"),
        _d("npc_dialogue", "D"), _d("enemy", "B", "test-prune-other"),
    ]
    ids = {
        (d["name"], d["patch_version"]): f"{d['entity_type']}::{d['name']}::{d['patch_version']}"
        for d in seeded
    }
    try:
        load_documents(client, seeded, dry_run=False)
        client.indices.refresh(index=_os.INDEX)

        rebuilt = [_d("enemy", "A"), _d("weapon", "W")]
        load_documents(client, rebuilt, dry_run=False)
        client.indices.refresh(index=_os.INDEX)
        pruned = prune_stale(client, rebuilt, dry_run=False)
        client.indices.refresh(index=_os.INDEX)

        def exists(name, version="test-prune"):
            return client.exists(index=_os.INDEX, id=ids[(f"__prune_{name}__", version)])

        assert pruned == 1
        assert not exists("B"), "stale enemy at the reloaded patch was not pruned"
        assert exists("A") and exists("W"), "rebuilt docs must survive"
        assert exists("D"), "entity_type absent from the dataset must be untouched"
        assert exists("B", "test-prune-other"), "other patch versions must be untouched"
    finally:
        for doc_id in ids.values():
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_location_stored_as_list(client):
    """location field is stored as list[str], not a comma-joined string.

    Regression for #50 — location was joined as a single string before being indexed,
    so callers could not filter or iterate individual location strings.
    """
    doc = {
        "entity_type": "weapon",
        "name": "__test_location_list__",
        "patch_version": "test",
        "source": "test",
        "description": "",
        "text_content": "",
        "location": ["Stormveil Castle", "Liurnia of the Lakes"],
    }
    doc_id = "weapon::__test_location_list__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        retrieved = _os.get_entity(
            client, "__test_location_list__", entity_type="weapon"
        )
        assert retrieved is not None
        loc = retrieved.get("location")
        assert isinstance(loc, list), (
            f"Expected location to be list[str], got {type(loc).__name__}: {loc!r}"
        )
        assert "Stormveil Castle" in loc
        assert "Liurnia of the Lakes" in loc

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_mcp_list_entity_types_returns_dict(client):
    """The MCP list_entity_types tool returns {"entity_types": [...]} not a bare list.

    Regression for #52 — returning a bare list caused FastMCP to emit one TextContent
    block per item (concatenated string) instead of a single JSON response. The fix
    wraps the list in a dict; the underlying _client function still returns list[str].
    """
    import elden_ring.mcp_server as mcp_server  # noqa: PLC0415

    # Ensure at least one entity type is present so the result is non-trivial.
    doc = {
        "entity_type": "weapon",
        "name": "__test_mcp_types__",
        "patch_version": "test",
        "source": "test",
        "description": "",
        "text_content": "",
    }
    doc_id = "weapon::__test_mcp_types__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = mcp_server.list_entity_types()
        assert isinstance(result, dict), (
            f"MCP list_entity_types must return a dict, got {type(result).__name__}"
        )
        assert "entity_types" in result, f"Expected 'entity_types' key, got {result}"
        assert isinstance(result["entity_types"], list)
        assert "weapon" in result["entity_types"]

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_get_entity_returns_newest_patch(client):
    """get_entity returns the newest patch version when multiple snapshots exist.

    Regression for #28 — get_entity had no sort clause, so OpenSearch returned an
    arbitrary document. In practice this meant an older snapshot lacking fields added
    in later patches was returned instead of the current one.
    """
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_newest__",
            "patch_version": "0.0.1-test",
            "source": "test",
            "description": "old version",
            "text_content": "",
            "req_str": 1,
        },
        {
            "entity_type": "weapon",
            "name": "__test_newest__",
            "patch_version": "0.0.2-test",
            "source": "test",
            "description": "new version",
            "text_content": "",
            "req_str": 99,
        },
    ]
    ids = [
        "weapon::__test_newest__::0.0.1-test",
        "weapon::__test_newest__::0.0.2-test",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.get_entity(client, "__test_newest__", entity_type="weapon")
        assert result is not None
        assert result["patch_version"] == "0.0.2-test", (
            f"Expected newest patch, got '{result['patch_version']}'; "
            "get_entity may be missing the patch_version desc sort (#28)"
        )
        assert result["req_str"] == 99

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_get_entity_diacritic_insensitive(client):
    """get_entity resolves diacritical names when queried without diacritics.

    Regression for #36 — get_entity used an exact name.keyword match, so
    "Misericorde" failed to find "Miséricorde". The fix adds a name.folded
    subfield (ascii_normalizer) and a fallback query.
    """
    doc = {
        "entity_type": "weapon",
        "name": "Míséricorde__test__",  # "Míséricorde__test__" with diacritics
        "patch_version": "test",
        "source": "test",
        "description": "diacritic test weapon",
        "text_content": "",
    }
    doc_id = "weapon::Miseericorde__test__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        # Exact match should still work
        exact = _os.get_entity(client, "Míséricorde__test__", entity_type="weapon")
        assert exact is not None, "Exact diacritical name lookup failed"

        # Folded (diacritic-stripped) match should also resolve: "Míséricorde"
        # folds to "misericorde" (single e), so the ascii query drops the accents.
        folded = _os.get_entity(client, "Misericorde__test__", entity_type="weapon")
        assert folded is not None, (
            "Diacritic-insensitive fallback lookup failed; "
            "name.folded subfield or ascii_normalizer may be missing (#36)"
        )

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_list_patch_versions_client_returns_list(client):
    """_client.list_patch_versions returns a sorted list[str], not a dict.

    Regression for #46 — the underlying _version_info() helper returns a dict;
    list_patch_versions() must unwrap it so callers get a plain list.
    """
    doc = {
        "entity_type": "weapon",
        "name": "__test_lpv__",
        "patch_version": "test-lpv",
        "source": "test",
        "description": "",
        "text_content": "",
    }
    doc_id = "weapon::__test_lpv__::test-lpv"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        versions = _os.list_patch_versions(client)
        assert isinstance(versions, list), (
            f"_client.list_patch_versions must return list, got {type(versions).__name__} (#46)"
        )
        assert "test-lpv" in versions

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_mcp_list_patch_versions_returns_dict(client):
    """MCP list_patch_versions tool returns {"versions": [...]}.

    Regression for #46 — the MCP tool previously returned a bare list[str], which
    FastMCP serialized as one concatenated TextContent block per element. It must
    return a dict wrapping the versions list. (The per-version 'sources' map was
    dropped when the erdb/fextralife layer was retired — all data is now native.)
    """
    import elden_ring.mcp_server as mcp_server  # noqa: PLC0415

    doc = {
        "entity_type": "weapon",
        "name": "__test_mcp_lpv__",
        "patch_version": "test-mcp-lpv",
        "source": "test",
        "description": "",
        "text_content": "",
    }
    doc_id = "weapon::__test_mcp_lpv__::test-mcp-lpv"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = mcp_server.list_patch_versions()
        assert isinstance(result, dict), (
            f"MCP list_patch_versions must return dict, got {type(result).__name__} (#46)"
        )
        assert "versions" in result, f"Expected 'versions' key, got {result}"
        assert isinstance(result["versions"], list)
        assert "test-mcp-lpv" in result["versions"]

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_diff_entities_missing_version_returns_error(client):
    """diff_entities returns a descriptive error for unloaded patch versions.

    Regression for #45 — before the fix, diff_entities queried OpenSearch for the
    entity without first verifying both versions are loaded, producing a confusing
    "not found" error when the real issue was a missing snapshot.
    """
    result = _os.diff_entities(
        client,
        name="Uchigatana",
        v1="99.99.99-nonexistent",
        v2="99.99.98-nonexistent",
    )
    assert isinstance(result, dict)
    assert "error" in result, f"Expected error dict, got {result}"
    assert "not loaded" in result["error"], (
        f"Error message should name the missing version; got: {result['error']}"
    )
    assert (
        "99.99.99-nonexistent" in result["error"]
        or "99.99.98-nonexistent" in result["error"]
    )


def test_diff_entities_no_source_guard(client):
    """diff_entities proceeds regardless of a doc's source label.

    The cross-source guard (old #44) was removed when the erdb/fextralife layer was
    retired: every doc is now native first-party extraction, and `source` was
    repurposed to name the internal param/FMG origin, so it no longer gates diffing.
    Two snapshots of the same entity must diff cleanly even if their source labels
    differ.
    """
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_xsrc__",
            "patch_version": "test-xsrc-a",
            "source": "EquipParamWeapon",
            "description": "version a",
            "text_content": "",
        },
        {
            "entity_type": "weapon",
            "name": "__test_xsrc__",
            "patch_version": "test-xsrc-b",
            "source": "SomeOtherParam",
            "description": "version b",
            "text_content": "",
        },
    ]
    ids = [
        "weapon::__test_xsrc__::test-xsrc-a",
        "weapon::__test_xsrc__::test-xsrc-b",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.diff_entities(
            client, "__test_xsrc__", "test-xsrc-a", "test-xsrc-b"
        )
        assert "error" not in result, f"Unexpected error: {result}"
        assert result["changed"] is True
        assert "description" in result["changed_fields"], result

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_list_patch_versions_semver_order(client):
    """list_patch_versions returns versions in semantic (not lexical) order.

    Regression for the migration: the old _version_info() sorted version strings
    lexically, so "1.10.0" sorted before "1.2.1". With the full native timeline the
    list must be true semver order.
    """
    labels = ["9.2.0", "9.10.0", "9.9.0"]
    ids = [f"weapon::__test_semver__::{v}" for v in labels]
    try:
        for v, doc_id in zip(labels, ids):
            client.index(
                index=_os.INDEX,
                id=doc_id,
                body={
                    "entity_type": "weapon",
                    "name": "__test_semver__",
                    "patch_version": v,
                    "source": "EquipParamWeapon",
                    "description": "",
                    "text_content": "",
                },
                refresh="wait_for",
            )
        versions = _os.list_patch_versions(client)
        idx = {v: versions.index(v) for v in labels}
        assert idx["9.2.0"] < idx["9.9.0"] < idx["9.10.0"], (
            f"versions not in semver order: {versions}"
        )
    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_describe_fields_reports_schema(client):
    """describe_index reports entity_types, sources, and the mapped field catalog."""
    result = _os.describe_index(client)
    assert set(result) >= {"entity_types", "sources", "fields"}, result
    assert isinstance(result["entity_types"], dict)
    assert isinstance(result["sources"], dict)
    # A mapped field carries at least its type; annotated ones carry a note.
    assert result["fields"]["source"]["type"] == "keyword"
    assert "note" in result["fields"]["source"]
    assert "subfields" in result["fields"]["name_ja"]


def test_no_phantom_affinity_rows(client):
    """Non-infusable weapons must contribute exactly one indexed document.

    Regression guard for #56 — erdb generates 12 affinity-variant rows per
    non-infusable weapon that should be dropped at ingest. If the filter is
    missing, Serpentbone Blade (infusable=False) returns 13 instead of 1.

    Requires a loaded index; skips gracefully on an empty one.
    """
    result = _os.search_literal(
        client, pattern="Serpentbone Blade", entity_type="weapon", count_only=True
    )
    if result["total"] == 0:
        pytest.skip("Serpentbone Blade not in index — load data first")
    assert result["total"] == 1, (
        f"Expected 1 document for Serpentbone Blade, got {result['total']}; "
        "phantom affinity variants may have slipped through the ingest filter (#56)"
    )


def test_search_collapse_returns_newest_patch(client):
    """search() collapse returns the newest-patch representative, preserving relevance.

    Regression for the collapse-without-sort bug — search() collapsed by name.keyword
    with no sort, so the representative was the highest-scoring version. Identical-content
    patches tie on _score, letting an arbitrary (often older) doc win and dropping fields
    stamped only on the newest patch (e.g. a talisman's effect). The fix sorts by
    _score desc then patch_version desc, so ties resolve to the latest version while
    between-entity relevance ordering is unchanged.
    """
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_collapse_newest__",
            "patch_version": "0.0.1-test",
            "source": "test",
            "description": "collapse newest regression unique qwerty",
            "text_content": "",
            "req_str": 1,
        },
        {
            "entity_type": "weapon",
            "name": "__test_collapse_newest__",
            "patch_version": "0.0.2-test",
            "source": "test",
            "description": "collapse newest regression unique qwerty",
            "text_content": "",
            "req_str": 99,
            "effect": "newest-only field",  # stamped only on the newer patch
        },
        {
            "entity_type": "weapon",
            "name": "__test_collapse_relevance__",
            "patch_version": "0.0.2-test",
            "source": "test",
            "description": "collapse newest regression unique qwerty qwerty qwerty",
            "text_content": "",
        },
    ]
    ids = [
        "weapon::__test_collapse_newest__::0.0.1-test",
        "weapon::__test_collapse_newest__::0.0.2-test",
        "weapon::__test_collapse_relevance__::0.0.2-test",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        results = _os.search(client, "qwerty", entity_type="weapon")
        by_name = {r["name"]: r for r in results}
        assert "__test_collapse_newest__" in by_name, (
            f"Expected collapsed entity in results, got: {list(by_name)}"
        )
        rep = by_name["__test_collapse_newest__"]
        assert rep["patch_version"] == "0.0.2-test", (
            f"Expected newest patch as collapse representative, got '{rep['patch_version']}'; "
            "search() may be missing the patch_version desc tiebreak"
        )
        assert rep.get("effect") == "newest-only field", (
            "Newest-patch-only field was dropped by the collapse representative"
        )

        # Relevance ordering between entities must be unchanged: the entity whose
        # description matches "qwerty" more strongly ranks first.
        names_in_order = [r["name"] for r in results]
        assert names_in_order[0] == "__test_collapse_relevance__", (
            f"Relevance ordering broken by the sort; got order: {names_in_order}"
        )

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_patterns_include_fields_without_name(client):
    """patterns + include_fields that omits 'name' must not collapse results to one.

    Regression for the patterns-union dedup bug — the union keyed on doc.get('name', ''),
    so when include_fields excluded 'name' every doc collapsed under '' and the call
    returned a single result with total=1. The fix forces 'name' into the sub-query
    source (stripping it from the returned docs when the caller didn't ask for it).
    """
    docs = [
        {
            "entity_type": "weapon",
            "name": "__test_incl_a__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "収録テスト固有文字列",
            "sort_id": 1111,
        },
        {
            "entity_type": "weapon",
            "name": "__test_incl_b__",
            "patch_version": "test",
            "source": "test",
            "description": "",
            "text_content": "",
            "description_ja": "護身具テスト固有文字列",
            "sort_id": 2222,
        },
    ]
    ids = [
        "weapon::__test_incl_a__::test",
        "weapon::__test_incl_b__::test",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.search_literal(
            client,
            patterns=["収録テスト固有文字列", "護身具テスト固有文字列"],
            source="test",
            include_fields=["sort_id"],
        )
        assert result["total"] == 2, (
            f"Expected total=2 for two disjoint matches, got {result['total']}; "
            "include_fields without 'name' may have collapsed the union to one entry"
        )
        assert len(result["results"]) == 2, (
            f"Expected 2 result docs, got {len(result['results'])}"
        )
        # 'name' was not requested, so it must be stripped from the returned docs.
        for doc in result["results"]:
            assert "name" not in doc, f"'name' leaked into projected result: {doc}"
            assert set(doc) <= {"sort_id"}, f"Unexpected projected keys: {set(doc)}"

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_excludes_cut_content_by_default(client):
    """Cut content (availability='cut') is hidden by default, shown with include_unavailable.

    Regression for #70: cut/unobtainable items (e.g. Millicent's set, whose in-game
    name row is [ERROR]-marked) are kept in the index but flagged availability='cut'
    and filtered out of search results unless include_unavailable=True is passed.
    Covers both search() and search_literal().
    """
    live = {
        "entity_type": "armor", "name": "__test_live_helm__", "patch_version": "test",
        "source": "test", "description": "availability regression test helm zqx.",
        "text_content": "availability regression zqx",
    }
    cut = {
        "entity_type": "armor", "name": "__test_cut_helm__", "patch_version": "test",
        "source": "test", "availability": "cut",
        "description": "availability regression test helm zqx.",
        "text_content": "availability regression zqx",
    }
    ids = ["armor::__test_live_helm__::test", "armor::__test_cut_helm__::test"]
    try:
        for doc, doc_id in zip((live, cut), ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        # search(): default hides cut, include_unavailable surfaces it
        default_names = {r["name"] for r in _os.search(
            client, "availability regression zqx", entity_type="armor")}
        assert "__test_live_helm__" in default_names
        assert "__test_cut_helm__" not in default_names, (
            "cut content must be excluded from search() by default (#70)")
        incl_names = {r["name"] for r in _os.search(
            client, "availability regression zqx", entity_type="armor",
            include_unavailable=True)}
        assert {"__test_live_helm__", "__test_cut_helm__"} <= incl_names, (
            "include_unavailable=True must surface cut content")

        # search_literal(): same contract, and count_only reflects the filter
        lit_default = _os.search_literal(
            client, "availability regression zqx", entity_type="armor", source="test")
        lit_names = {r["name"] for r in lit_default["results"]}
        assert "__test_cut_helm__" not in lit_names
        assert lit_default["total"] == 1, (
            f"literal count must exclude cut by default, got {lit_default['total']}")
        lit_incl = _os.search_literal(
            client, "availability regression zqx", entity_type="armor", source="test",
            include_unavailable=True, count_only=True)
        assert lit_incl["total"] == 2, (
            f"literal count with include_unavailable must count cut, got {lit_incl['total']}")

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_new_goods_entity_types_are_searchable(client):
    """New goods/ammo entity_types index and filter by entity_type (#72).

    Coverage expansion added consumable/key_item/ammo/spirit_ash/… as first-class
    entity_types (dynamic — no mapping change). Verify a doc of a new type is both
    full-text searchable and narrowable via the entity_type filter.
    """
    docs = {
        "consumable::__test_grease__::test": {
            "entity_type": "consumable", "name": "__test_grease__",
            "patch_version": "test", "source": "EquipParamGoods",
            "menu_category": "Consumable",
            "description": "coverage expansion consumable wqz.",
        },
        "key_item::__test_bell__::test": {
            "entity_type": "key_item", "name": "__test_bell__",
            "patch_version": "test", "source": "EquipParamGoods",
            "menu_category": "Key Item",
            "description": "coverage expansion keyitem wqz.",
        },
    }
    try:
        for doc_id, doc in docs.items():
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        # entity_type filter narrows to just the consumable
        cons = {r["name"] for r in _os.search(
            client, "coverage expansion wqz", entity_type="consumable")}
        assert "__test_grease__" in cons
        assert "__test_bell__" not in cons, "entity_type filter must exclude key_item"

        # the key_item is reachable under its own type
        keys = {r["name"] for r in _os.search(
            client, "coverage expansion wqz", entity_type="key_item")}
        assert "__test_bell__" in keys

    finally:
        for doc_id in docs:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_excludes_unobtainable_content_by_default(client):
    """Unobtainable content (availability='unobtainable') is hidden like cut (#71).

    Real-named armor with no acquisition path (enemy-only gear / reused assets, e.g.
    the Ragged set) is kept in the index but flagged availability='unobtainable' and
    filtered out of search results unless include_unavailable=True — the same contract
    as availability='cut', now covering both flagged states.
    """
    live = {
        "entity_type": "armor", "name": "__test_live_rag__", "patch_version": "test",
        "source": "test", "description": "unobtainable regression armor jvx.",
        "text_content": "unobtainable regression jvx",
    }
    unob = {
        "entity_type": "armor", "name": "__test_unob_rag__", "patch_version": "test",
        "source": "test", "availability": "unobtainable",
        "description": "unobtainable regression armor jvx.",
        "text_content": "unobtainable regression jvx",
    }
    ids = ["armor::__test_live_rag__::test", "armor::__test_unob_rag__::test"]
    try:
        for doc, doc_id in zip((live, unob), ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        default_names = {r["name"] for r in _os.search(
            client, "unobtainable regression jvx", entity_type="armor")}
        assert "__test_live_rag__" in default_names
        assert "__test_unob_rag__" not in default_names, (
            "unobtainable content must be excluded from search() by default (#71)")
        incl_names = {r["name"] for r in _os.search(
            client, "unobtainable regression jvx", entity_type="armor",
            include_unavailable=True)}
        assert {"__test_live_rag__", "__test_unob_rag__"} <= incl_names, (
            "include_unavailable=True must surface unobtainable content")

        lit_default = _os.search_literal(
            client, "unobtainable regression jvx", entity_type="armor", source="test")
        assert lit_default["total"] == 1, (
            f"literal count must exclude unobtainable by default, got {lit_default['total']}")
        lit_incl = _os.search_literal(
            client, "unobtainable regression jvx", entity_type="armor", source="test",
            include_unavailable=True, count_only=True)
        assert lit_incl["total"] == 2, (
            f"literal count with include_unavailable must count unobtainable, got "
            f"{lit_incl['total']}")

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_diff_reports_acquisition_change_across_patches(client):
    """Acquisition fields are per-patch and diff cleanly (#60).

    Filed against the old pipeline, where `dropped_by`/`acquisition_*` came from a
    single Discord-bot scrape backfilled onto every snapshot, so diff_entities
    reported them permanently "unchanged". That source was retired in the native
    migration: `dropped_by` is no longer produced, and `sold_by`/`acquisition_*` are
    now derived per-patch from ShopLineupParam/ItemLotParam. So a genuine change in
    `sold_by` between two snapshots must surface under changed_fields — the fields
    must NOT be in the diff skip set.
    """
    assert not (
        {"sold_by", "acquisition_sources", "acquisition_types", "dropped_by"}
        & _os._DIFF_SKIP_FIELDS
    ), "acquisition fields are per-patch now; they must remain diffable"

    docs = [
        {
            "entity_type": "weapon", "name": "__test_acq_diff__",
            "patch_version": "test-acq-a", "source": "ShopLineupParam",
            "description": "acq diff test", "text_content": "",
            "sold_by": ["Merchant Kale"], "acquisition_sources": ["Merchant Kale"],
            "acquisition_types": ["merchant"],
        },
        {
            "entity_type": "weapon", "name": "__test_acq_diff__",
            "patch_version": "test-acq-b", "source": "ShopLineupParam",
            "description": "acq diff test", "text_content": "",
            "sold_by": ["Twin Maiden Husks"],
            "acquisition_sources": ["Twin Maiden Husks"],
            "acquisition_types": ["merchant"],
        },
    ]
    ids = [
        "weapon::__test_acq_diff__::test-acq-a",
        "weapon::__test_acq_diff__::test-acq-b",
    ]
    try:
        for doc, doc_id in zip(docs, ids):
            client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        result = _os.diff_entities(
            client, "__test_acq_diff__", "test-acq-a", "test-acq-b"
        )
        assert "error" not in result, f"Unexpected error: {result}"
        assert result["changed"] is True
        assert "sold_by" in result["changed_fields"], result
        assert "sold_by" not in result["unchanged_fields"], result

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_search_literal_acquisition_fields_require_naming(client):
    """Acquisition keyword fields are opt-in, not in the default field set (#61).

    The default search field set is text-only by design; the structured keyword
    fields (sold_by, acquisition_sources, acquisition_types) are reachable only when
    named explicitly via fields=[...]. This locks the behavior the fields docstring
    now documents.
    """
    doc = {
        "entity_type": "weapon", "name": "__test_acq_named__",
        "patch_version": "test", "source": "test",
        "description": "acq naming test", "text_content": "",
        "sold_by": ["__Test Merchant ZZQ__"],
        "acquisition_sources": ["__Test Merchant ZZQ__"],
        "acquisition_types": ["merchant"],
    }
    doc_id = "weapon::__test_acq_named__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        default = _os.search_literal(
            client, "__Test Merchant ZZQ__", entity_type="weapon", source="test")
        default_names = {r["name"] for r in default["results"]}
        assert "__test_acq_named__" not in default_names, (
            "acquisition fields must NOT be searched by default (#61)")

        named = _os.search_literal(
            client, "__Test Merchant ZZQ__", entity_type="weapon", source="test",
            fields=["sold_by"])
        named_names = {r["name"] for r in named["results"]}
        assert "__test_acq_named__" in named_names, (
            "naming fields=['sold_by'] must reach the structured field (#61)")

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_enemy_entity_searchable(client):
    """Native enemy entity type is indexed, searchable, and carries name_ja (#25).

    Enemies come from the NpcName roster (bosses + named enemies) with EN + JP names;
    the humanoid subset also carries NpcParam combat stats. Verify a synthetic enemy
    doc is full-text and literal searchable, narrows by entity_type, resolves via
    get_entity with its name_ja and hp, and that 'enemy' shows in list_entity_types.
    """
    doc = {
        "entity_type": "enemy", "name": "__test_boss_zqx__",
        "name_ja": "テスト・ボスzqx", "npc_id": "905999000",
        "patch_version": "test", "source": "NpcName",
        "text_content": "__test_boss_zqx__", "tags": ["enemy"], "hp": 12345,
    }
    doc_id = "enemy::__test_boss_zqx__::test"
    try:
        client.index(index=_os.INDEX, id=doc_id, body=doc, refresh="wait_for")

        names = {r["name"] for r in _os.search(
            client, "__test_boss_zqx__", entity_type="enemy")}
        assert "__test_boss_zqx__" in names, "enemy must be full-text searchable"

        lit = _os.search_literal(
            client, "__test_boss_zqx__", entity_type="enemy", patch_version="test")
        assert any(r["name"] == "__test_boss_zqx__" for r in lit["results"])

        got = _os.get_entity(client, "__test_boss_zqx__", entity_type="enemy")
        assert got is not None
        assert got["name_ja"] == "テスト・ボスzqx"
        assert got["hp"] == 12345

        assert "enemy" in _os.list_entity_types(client)

    finally:
        client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)


def test_enemy_drops_and_dropped_by_searchable(client):
    """Boss/named-enemy drops round-trip on both sides of the edge (#68).

    The EMEVD drop parse adds a ``drops`` list to enemy docs and the reciprocal
    ``dropped_by`` to the dropped item's doc. Verify both fields persist through
    get_entity and that the item is findable by naming ``dropped_by`` in a literal
    search (it's an opt-in keyword field, like the other acquisition fields, #61).
    """
    enemy = {
        "entity_type": "enemy", "name": "__test_drop_boss__",
        "patch_version": "test", "source": "NpcName",
        "text_content": "__test_drop_boss__", "tags": ["enemy"],
        "drops": ["__test_drop_item__"],
    }
    item = {
        "entity_type": "weapon", "name": "__test_drop_item__",
        "patch_version": "test", "source": "EquipParamWeapon",
        "text_content": "__test_drop_item__",
        "acquisition_types": ["enemy_drop"],
        "acquisition_sources": ["__test_drop_boss__"],
        "dropped_by": ["__test_drop_boss__"],
    }
    enemy_id = "enemy::__test_drop_boss__::test"
    item_id = "weapon::__test_drop_item__::test"
    try:
        client.index(index=_os.INDEX, id=enemy_id, body=enemy, refresh="wait_for")
        client.index(index=_os.INDEX, id=item_id, body=item, refresh="wait_for")

        got_enemy = _os.get_entity(client, "__test_drop_boss__", entity_type="enemy")
        assert got_enemy["drops"] == ["__test_drop_item__"]

        got_item = _os.get_entity(client, "__test_drop_item__", entity_type="weapon")
        assert got_item["dropped_by"] == ["__test_drop_boss__"]

        # dropped_by is opt-in: naming it in a literal search finds the item by boss.
        lit = _os.search_literal(
            client, "__test_drop_boss__", fields=["dropped_by"],
            patch_version="test")
        assert any(r["name"] == "__test_drop_item__" for r in lit["results"])

    finally:
        client.delete(index=_os.INDEX, id=enemy_id, ignore=[404])
        client.delete(index=_os.INDEX, id=item_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)
