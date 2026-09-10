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
