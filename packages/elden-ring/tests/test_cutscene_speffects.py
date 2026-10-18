"""Cutscene SpEffect triggers (#280): the new field is mapped (strict index) and
documented. No OpenSearch needed."""

from elden_ring._client import INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# A 1.17 build's doc, trimmed.
_SCENE = {
    "entity_type": "cutscene",
    "name": "Cutscene 10000030",
    "trigger_kind": "boss_defeat",
    "boss": "Godrick the Grafted",
    "trigger_flags": [10000800],
    "trigger_speffects": [
        {
            "speffect_id": 14785,
            "entity_id": 10000800,
            "character": "Godrick the Grafted",
            "bosses": ["Godrick the Grafted"],
        }
    ],
}


def _leaves(doc, props, path=""):
    for k, v in doc.items():
        assert k in props, path + k
        items = v if isinstance(v, list) else [v]
        for item in items:
            if isinstance(item, dict):
                _leaves(item, props[k]["properties"], path + k + ".")


def test_field_mapped():
    _leaves(_SCENE, _PROPS)
    sub = _PROPS["trigger_speffects"]["properties"]
    assert sub["speffect_id"] == {"type": "long"}
    assert sub["entity_id"] == {"type": "long"}
    assert sub["character"] == {"type": "keyword"}
    assert sub["bosses"] == {"type": "keyword"}
