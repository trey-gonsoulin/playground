"""Schema v2 checks (#115): grouped stat objects, keyword cleanup, strict mapping.
No OpenSearch needed."""

from elden_ring._client import _flatten, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def _mapped(path: str) -> dict | None:
    props, spec = _PROPS, None
    for part in path.split("."):
        spec = props.get(part)
        if spec is None:
            return None
        props = spec.get("properties", {})
    return spec


def test_mapping_is_strict():
    assert INDEX_MAPPING["mappings"]["dynamic"] == "strict"


def test_closed_vocabularies_are_plain_keyword():
    for f in ("affinity", "availability", "drops", "spell_role"):
        assert _PROPS[f] == {"type": "keyword"}, f


def test_flat_stat_fields_removed():
    for f in (
        "attack_physical",
        "scaling_str",
        "req_dex",
        "defense_fire",
        "negation_slash",
        "hp",
        "magic_defense",
        "achievement_set",
    ):
        assert f not in _PROPS, f


def test_grouped_leaves_mapped():
    expected = {
        "attack_power.holy": "integer",
        "scaling.arc.grade": "keyword",
        "requirements.fai": "integer",
        "negation.pierce": "float",
        "negation.lightning": "float",
        "stats.hp": "integer",
        "stats.poise": "float",
        "defense.holy": "float",
        "resistances.bleed": "integer",
    }
    for path, type_ in expected.items():
        assert (_mapped(path) or {}).get("type") == type_, path
    assert _mapped("defense.physical") is None  # NpcParam has no physical defense


def test_builder_doc_shapes_fully_mapped():
    # One doc per grouped shape the builders emit; strict mapping rejects any stray leaf.
    docs = [
        {
            "attack_power": {"physical": 96},
            "scaling": {"str": {"grade": "D"}},
            "requirements": {"str": 14, "dex": 12},
            "depicted_in_talisman": "Dagger Talisman",
        },
        {"negation": {"physical": 10.0, "strike": 12.0, "holy": 4.0}},
        {"requirements": {"int": 18}, "fp_cost": 12, "spell_role": "Offensive"},
        {
            "stats": {"hp": 3186, "stamina": 150, "poise": 80.0},
            "defense": {"magic": 100, "fire": 100, "lightning": 100, "holy": 100},
            "resistances": {"poison": 154},
        },
    ]
    for doc in docs:
        for path in _flatten(doc):
            assert _mapped(path) is not None, path


def test_flatten_to_dotted_leaves():
    doc = {"name": "Halberd", "attack_power": {"physical": 134}, "tags": ["Halberd"]}
    assert _flatten(doc) == {
        "name": "Halberd",
        "attack_power.physical": 134,
        "tags": ["Halberd"],
    }
    assert _flatten({"scaling": {"str": {"grade": "D"}}}) == {"scaling.str.grade": "D"}
