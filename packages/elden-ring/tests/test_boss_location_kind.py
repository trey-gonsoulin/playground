"""Boss location area fallback (#329): location_kind mapped and documented.
No OpenSearch needed."""

from elden_ring import mcp_server
from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_location_kind_mapped():
    assert _PROPS["location_kind"] == {"type": "keyword"}


def test_location_notes_describe_area_fallback():
    kind = _FIELD_NOTES["location_kind"]
    assert "'landmark'" in kind and "'area'" in kind and "#329" in kind
    loc = _FIELD_NOTES["location"]
    assert "location_kind 'area'" in loc and "region map" in loc


def test_search_docstring_mentions_location_kind():
    assert "location_kind" in (mcp_server.search_entities.__doc__ or "")
