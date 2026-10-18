"""Mapping checks for spirit-ash enemy labels (#104). No OpenSearch needed."""

from elden_ring._field_notes import _FIELD_NOTES
from elden_ring._schema import INDEX_MAPPING


def test_name_source_fields_mapped_as_keyword():
    # Declared before the reindex so the dynamic text+keyword default never locks in.
    props = INDEX_MAPPING["mappings"]["properties"]
    assert props["name_source"] == {"type": "keyword"}
    assert props["chr_models"] == {"type": "keyword"}


def test_name_source_fields_documented():
    assert "spirit_ash" in _FIELD_NOTES["name_source"]
    assert "chr_models" in _FIELD_NOTES
