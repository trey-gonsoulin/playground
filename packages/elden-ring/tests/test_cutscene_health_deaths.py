"""Cutscene HP-ratio and death triggers (#394): the new fields are mapped (strict
index) and documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# 1.17 build docs, trimmed.
_SCENES = [
    {
        "entity_type": "cutscene",
        "name": "Cutscene 16000020",
        "trigger_health": [
            {
                "entity_id": 16000801,
                "comparison": "<=",
                "ratio": 0.01,
                "character": "God-Devouring Serpent",
                "bosses": ["Rykard, Lord of Blasphemy"],
            }
        ],
    },
    {
        "entity_type": "cutscene",
        "name": "Cutscene 14000010",
        "trigger_deaths": [
            {
                "entity_id": 14000801,
                "character": "Rennala, Queen of the Full Moon",
                "bosses": ["Rennala, Queen of the Full Moon"],
            }
        ],
    },
]


def _leaves(doc, props, path=""):
    for k, v in doc.items():
        assert k in props, path + k
        items = v if isinstance(v, list) else [v]
        for item in items:
            if isinstance(item, dict):
                _leaves(item, props[k]["properties"], path + k + ".")


def test_fields_mapped():
    for doc in _SCENES:
        _leaves(doc, _PROPS)
    health = _PROPS["trigger_health"]["properties"]
    assert health["comparison"] == {"type": "keyword"}
    assert health["ratio"] == {"type": "float"}
    assert _PROPS["trigger_deaths"]["properties"]["entity_id"] == {"type": "long"}


def test_fields_documented():
    assert "16000020" in _FIELD_NOTES["trigger_health"]
    assert "14000010" in _FIELD_NOTES["trigger_deaths"]
