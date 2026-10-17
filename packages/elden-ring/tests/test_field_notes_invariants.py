"""#460: structural invariants of INDEX_MAPPING and _FIELD_NOTES.

These check the shape of the field catalog, not note wording, so they hold for
any field a later change adds: every note names a mapped path, every mapped leaf
is documented, no note uses controller button names, and the mapping stays well
under the index's field limit.
"""

import re

from elden_ring import _client
from elden_ring._client import (
    _FIELD_NOTES,
    INDEX_MAPPING,
    _mapping_paths,
    _queryable_paths,
    describe_index,
)

_PATHS = _mapping_paths(INDEX_MAPPING["mappings"]["properties"])

# Mapper budget: total_fields.limit is 3000, and each _SET_WHEN mount costs ~92,
# so stop well short of it (2299 at #460).
_MAPPER_BUDGET = 2700


def _has_note(path: str) -> bool:
    """The path or one of its ancestors (attack_power for attack_power.fire) has a note."""
    parts = path.split(".")
    return any(".".join(parts[:i]) in _FIELD_NOTES for i in range(1, len(parts) + 1))


def test_every_note_key_is_a_mapped_path():
    stale = sorted(set(_FIELD_NOTES) - _queryable_paths())
    assert not stale, stale


def test_every_mapped_leaf_has_an_own_or_ancestor_note():
    leaves = [p for p, spec in _PATHS.items() if "properties" not in spec]
    missing = [p for p in leaves if not _has_note(p)]
    assert not missing, missing


def test_every_disabled_object_has_its_own_note():
    # #469: an enabled: false object is a leaf of the mapping whose inner shape
    # describe_fields can't show, so its note sits on the path itself.
    missing = sorted(p for p in _client._disabled_paths() if p not in _FIELD_NOTES)
    assert not missing, missing


def test_every_shared_disabled_note_names_a_disabled_leaf():
    leaves = {p.rsplit(".", 1)[-1] for p in _client._disabled_paths()}
    stale = sorted(set(_client._DISABLED_LEAF_NOTES) - leaves)
    assert not stale, stale


def test_notes_are_non_empty_strings():
    bad = [
        k for k, v in _FIELD_NOTES.items() if not isinstance(v, str) or not v.strip()
    ]
    assert not bad, bad


def test_no_note_uses_controller_button_names():
    # Action words (light / heavy attack, guard, skill), never one controller's
    # button labels.
    button = re.compile(r"\b[LR][12]\b")
    bad = [k for k, v in _FIELD_NOTES.items() if button.search(v)]
    assert not bad, bad


def test_mapper_count_stays_under_budget():
    limit = INDEX_MAPPING["settings"]["mapping.total_fields.limit"]
    # OpenSearch counts every field and object mapper plus each multi-field.
    mappers = len(_PATHS) + sum(len(s.get("fields", {})) for s in _PATHS.values())
    assert _MAPPER_BUDGET < limit
    assert mappers < _MAPPER_BUDGET, mappers


class _StubClient:
    def __init__(self):
        self.calls = []

    def search(self, index, body):
        self.calls.append((index, body))
        return {
            "aggregations": {
                "types": {
                    "buckets": [
                        {"key": "weapon", "doc_count": 7},
                        {"key": "spell", "doc_count": 3},
                    ]
                },
                "sources": {"buckets": [{"key": "Magic", "doc_count": 3}]},
            }
        }


def test_describe_index_reports_types_subfields_and_notes():
    stub = _StubClient()
    out = describe_index(stub)
    assert stub.calls and stub.calls[0][0] == _client.INDEX
    assert out["entity_types"] == {"weapon": 7, "spell": 3}
    assert out["sources"] == {"Magic": 3}
    fields = out["fields"]
    assert set(fields) == set(_PATHS)
    name = fields["name"]
    assert name["type"] == "text"
    assert set(name["subfields"]) == {"keyword", "folded"}
    assert name["note"] == _FIELD_NOTES["name"]
    # Objects report as "object"; every note surfaces on its own path.
    for path, spec in _PATHS.items():
        assert fields[path]["type"] == spec.get("type", "object"), path
        assert fields[path].get("note") == _FIELD_NOTES.get(path), path


def test_list_entity_types_reads_the_type_buckets():
    assert _client.list_entity_types(_StubClient()) == ["weapon", "spell"]
