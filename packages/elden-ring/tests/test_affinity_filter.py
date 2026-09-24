"""Unit tests for the collapse_affinity filter clause (#21). No OpenSearch needed."""

from elden_ring._client import _affinity_filter


def test_affinity_filter_off_adds_nothing():
    assert _affinity_filter(False) == []


def test_affinity_filter_drops_only_non_standard_affinity_docs():
    [clause] = _affinity_filter(True)
    [variant] = clause["bool"]["must_not"]
    # Excluded = has an affinity AND it isn't Standard; docs without the field
    # (non-weapons, non-infusable weapons) and Standard rows pass through.
    assert variant["bool"]["filter"] == [{"exists": {"field": "affinity"}}]
    assert variant["bool"]["must_not"] == [{"term": {"affinity.keyword": "Standard"}}]
