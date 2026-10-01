"""Unknown filter values error instead of returning silent zeros (#205), and
get_entity's patch_version (#206)."""

from elden_ring import _client

_TYPES = ["weapon", "npc_dialogue"]
_SOURCES = ["EquipParamWeapon", "TalkMsg"]
_VERSIONS = ["1.02.0", "1.10.0", "1.17.0"]


class _Client:
    """Fake OpenSearch: terms aggs return the live values, every query matches the
    docs in ``docs`` that satisfy its term filters."""

    def __init__(self, docs=(), carried=()):
        self.docs, self.carried, self.bodies = list(docs), set(carried), []

    def search(self, index, body):
        self.bodies.append(body)
        aggs = body.get("aggs", {})
        if "entity_type" in aggs and "source" in aggs:
            return {
                "aggregations": {
                    k: {"buckets": [{"key": v} for v in vals]}
                    for k, vals in (
                        ("entity_type", _TYPES),
                        ("source", _SOURCES),
                        ("patch_version", _VERSIONS),
                    )
                }
            }
        if "has" in aggs:
            names = aggs["has"]["filters"]["filters"]
            return {
                "aggregations": {
                    "has": {
                        "buckets": {
                            f: {"doc_count": 5 if f in self.carried else 0}
                            for f in names
                        }
                    }
                }
            }
        hits = [d for d in self.docs if self._matches(d, body.get("query", {}))]
        if "versions" in aggs:
            vs = sorted({d["patch_version"] for d in hits})
            return {"aggregations": {"versions": {"buckets": [{"key": v} for v in vs]}}}
        hits.sort(key=lambda d: _client._ver_key(d["patch_version"]), reverse=True)
        return {
            "hits": {
                "hits": [{"_source": d, "_score": 1.0} for d in hits],
                "total": {"value": len(hits)},
            },
            "aggregations": {
                "distinct_entities": {"value": len({d["name"] for d in hits})},
                "categories": {"buckets": []},
                "by_type": {"buckets": []},
            },
        }

    @staticmethod
    def _matches(doc, query):
        terms = [query] if "term" in query else query.get("bool", {}).get("filter", [])
        for f in terms:
            if "term" not in f:
                continue
            ((field, value),) = f["term"].items()
            if doc.get(field.removesuffix(".keyword")) != value:
                return False
        return True


def _doc(name, version, entity_type="weapon", **kw):
    return {"name": name, "entity_type": entity_type, "patch_version": version, **kw}


def test_retired_source_errors_before_any_query():
    c = _Client()
    out = _client.search_literal(c, pattern="象る", source="erdb")
    assert "retired" in out["error"]
    assert c.bodies == []
    assert "retired" in _client.list_menu_categories(c, "weapon", "fextralife")["error"]
    assert "retired" in _client.search(c, "x", source="erdb")["error"]


def test_unknown_source_entity_type_and_version_error():
    c = _Client()
    out = _client.search_literal(c, pattern="x", source="Bogus")
    assert "unknown source 'Bogus'" in out["error"] and "TalkMsg" in out["error"]
    out = _client.search_literal(c, pattern="x", entity_type="weapons", count_only=True)
    assert "unknown entity_type 'weapons'" in out["error"]
    out = _client.search_literal(c, patterns=["a", "b"], patch_version="1.10")
    assert "patch version '1.10' is not loaded" in out["error"]
    assert "unknown source" in _client.search(c, "x", source="Bogus")["error"]
    assert (
        "unknown source" in _client.search(c, "x", source="B", count_only=True)["error"]
    )
    assert "unknown source" in _client.list_menu_categories(c, "weapon", "B")["error"]


def test_valid_empty_results_stay_empty():
    c = _Client()
    assert _client.search_literal(c, pattern="x", source="TalkMsg") == {
        "total": 0,
        "results": [],
    }
    assert _client.search(c, "x", entity_type="weapon") == []
    assert _client.list_menu_categories(c, "weapon", "EquipParamWeapon") == {}


def test_non_empty_result_skips_the_value_check():
    c = _Client([_doc("Dagger", "1.17.0")])
    out = _client.search(c, "x", entity_type="weapon")
    assert [d["name"] for d in out] == ["Dagger"]
    assert len(c.bodies) == 1


def test_unknown_field_names_error():
    c = _Client()
    out = _client.search_literal(c, pattern="x", fields=["text_contnet_ja"])
    assert "unknown field(s) ['text_contnet_ja']" in out["error"]
    out = _client.search_literal(c, pattern="x", include_fields=["nmae"])
    assert "['nmae']" in out["error"]
    assert "unknown field" in _client.search(c, "x", include_fields=["nmae"])["error"]
    out = _client.text_changed_between(c, "weapon", "descripton", "1.02.0", "1.17.0")
    assert "['descripton']" in out["error"]
    assert c.bodies == []


def test_subfields_and_wildcards_are_known():
    assert (
        _client._unknown_fields(
            ["description_ja.lemma", "name.keyword", "stats.*", "attack_power.fire"]
        )
        == []
    )


def test_field_the_type_lacks_warns_with_alternatives():
    c = _Client(carried={"name", "description", "name_ja", "description_ja"})
    out = _client.search_literal(
        c, pattern="象る", fields=["text_content_ja.lemma"], entity_type="weapon"
    )
    assert out["total"] == 0
    (warning,) = out["warnings"]
    assert warning.startswith("text_content_ja.lemma: no weapon documents carry")
    assert "description_ja" in warning
    assert {"term": {"entity_type": "weapon"}} == c.bodies[-1]["query"]


def test_carried_field_gives_no_warning():
    c = _Client(carried={"description_ja"})
    out = _client.search_literal(
        c, pattern="象る", fields=["description_ja"], entity_type="weapon"
    )
    assert "warnings" not in out


def test_get_entity_unknown_entity_type_errors():
    c = _Client()
    assert "unknown entity_type" in _client.get_entity(c, "X", "bosses")["error"]
    assert _client.get_entity(c, "Nobody", "weapon") is None


_RIVERS = [_doc("Rivers of Blood", v, attack=i) for i, v in enumerate(_VERSIONS)]


def test_get_entity_patch_version_exact():
    c = _Client(_RIVERS)
    assert _client.get_entity(c, "Rivers of Blood")["patch_version"] == "1.17.0"
    out = _client.get_entity(c, "Rivers of Blood", patch_version="1.10.0")
    assert (out["patch_version"], out["attack"]) == ("1.10.0", 1)
    assert "requested_patch_version" not in out


def test_get_entity_patch_version_as_of_sparse_type():
    lines = [
        _doc("Line", "1.02.0", "npc_dialogue"),
        _doc("Line", "1.17.0", "npc_dialogue"),
    ]
    c = _Client([*lines, *_RIVERS])
    out = _client.get_entity(c, "Line", patch_version="1.10.0")
    assert out["patch_version"] == "1.02.0"
    assert out["requested_patch_version"] == "1.10.0"


def test_get_entity_patch_version_errors():
    c = _Client([*_RIVERS, _doc("Milady", "1.17.0")])
    assert (
        "not loaded" in _client.get_entity(c, "Milady", patch_version="1.11")["error"]
    )
    out = _client.get_entity(c, "Milady", patch_version="1.10.0")
    assert out["error"] == "'Milady' not present in 1.10.0"
    c = _Client([*_RIVERS, _doc("Line", "1.17.0", "npc_dialogue")])
    out = _client.get_entity(c, "Line", patch_version="1.10.0")
    assert out["error"].startswith("no npc_dialogue data at or before '1.10.0'")


def test_get_entity_patch_version_keeps_historical_flags():
    docs = [
        _doc("New Name", "1.02.0", display_name="Old Name"),
        _doc("New Name", "1.17.0", display_name="New Name"),
    ]
    out = _client.get_entity(_Client(docs), "Old Name", patch_version="1.02.0")
    assert out["display_name"] == "Old Name"
    assert out["name_is_historical"] is True and out["queried_name"] == "Old Name"


def test_trailing_zero_spellings_share_a_key():
    k = _client._trimmed_key
    assert k("1.02.0") == k("1.02") and k("1.10") == k("1.10.0")
    assert k("1.02") != k("1.02.1") and k("1.10.0") != k("1.01")


def test_version_aliases_map_to_the_loaded_label():
    c = _Client([_doc("Dagger", "1.02"), _doc("Dagger", "1.10.0")])
    assert _client._canonical_version(c, "1.02.0") == "1.02"
    assert _client._canonical_version(c, "1.10") == "1.10.0"
    assert _client._canonical_version(c, "1.10.0") == "1.10.0"
    assert _client._canonical_version(c, "1.11") == "1.11"
    assert _client._canonical_version(c, None) is None


def test_ambiguous_alias_passes_through():
    c = _Client([_doc("Dagger", "1.10"), _doc("Dagger", "1.10.0")])
    assert _client._canonical_version(c, "1.10.0.0") == "1.10.0.0"


def test_tools_accept_version_aliases():
    c = _Client([_doc("Dagger", "1.02"), _doc("Dagger", "1.10.0")])
    out = _client.search_literal(c, entity_type="weapon", patch_version="1.02.0")
    assert out["total"] == 1
    assert (out["patch_version"], out["requested_patch_version"]) == ("1.02", "1.02.0")
    out = _client.get_entity(c, "Dagger", patch_version="1.10")
    assert (out["patch_version"], out["requested_patch_version"]) == ("1.10.0", "1.10")
    out = _client.search(c, "x", patch_version="1.02.0")
    assert [d["patch_version"] for d in out] == ["1.02"]
    out = _client.diff_entities(c, "Dagger", "1.02.0", "1.10")
    assert out["requested_patch_versions"] == {"1.02.0": "1.02", "1.10": "1.10.0"}
    assert "error" not in out
