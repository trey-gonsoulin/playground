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
    """Search on an empty index returns an empty list (not an error)."""
    results = _os.search(client, "bleed katana")
    assert isinstance(results, list)
    assert results == []


def test_get_entity_missing_returns_none(client):
    """get_entity on an empty index returns None (not an error)."""
    result = _os.get_entity(client, "Rivers of Blood")
    assert result is None


def test_list_entity_types_empty(client):
    """list_entity_types on an empty index returns an empty list."""
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
        results = _os.search(client, "integration test placeholder", entity_type="weapon")
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

        result = _os.search(client, "count only test placeholder", entity_type="weapon", count_only=True)
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
            client, "fields test placeholder",
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

        result = _os.search_literal(client, "literal count only test unique xyz987", count_only=True)
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
        assert "__test_sortid_nomatch__" not in names, f"Expected no-match excluded, got: {names}"

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
        assert "__test_sortid_low__" not in names, f"Expected low excluded, got: {names}"

    finally:
        for doc_id in ids:
            client.delete(index=_os.INDEX, id=doc_id, ignore=[404])
        client.indices.refresh(index=_os.INDEX)
