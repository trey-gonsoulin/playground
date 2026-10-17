"""Keepsake entity type (#475): the doc shape is covered by the strict mapping and
the type is documented. No OpenSearch needed."""

from elden_ring import mcp_server
from elden_ring._client import _FIELD_NOTES, _flatten, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# As the 1.17.0 build emits it.
_DOC = {
    "entity_type": "keepsake",
    "name": "Stonesword Key",
    "name_ja": "石剣の鍵",
    "patch_version": "1.17.0",
    "source": "CharaInitParam",
    "param_id": 2406,
    "description": "Two stone keys shaped like swords.\nBreaks the seal on imp "
    "statues,\nbut can only be used once.",
    "description_ja": "剣を模した石の鍵（２個）\nインプ像の封印を解き、壊れるという",
    "grants": [{"item": "Stonesword Key", "entity_type": "key_item", "quantity": 2}],
    "is_dlc": False,
}


def _mapped(path: str) -> dict | None:
    props, spec = _PROPS, None
    for part in path.split("."):
        spec = props.get(part)
        if spec is None:
            return None
        props = spec.get("properties", {})
    return spec


def test_keepsake_doc_fully_mapped():
    for path in _flatten(_DOC):
        assert _mapped(path) is not None, path
    assert _mapped("grants.quantity") == {"type": "integer"}
    assert _mapped("grants.entity_type") == {"type": "keyword"}


def test_keepsake_type_documented():
    assert (
        "keepsake (one doc per character-creation keepsake, #475)"
        in (_FIELD_NOTES["entity_type"])
    )
    assert "keepsake doc (#475)" in _FIELD_NOTES["grants"]
    assert "keepsake doc (#475)" in _FIELD_NOTES["description"]
    assert "Keepsakes (#475)" in _FIELD_NOTES["param_id"]
    assert "keepsake     —" in mcp_server.search_entities.__doc__
