"""Mapping checks for spirit-ash enemy labels (#104). No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING


def test_name_source_fields_mapped_as_keyword():
    # Declared before the reindex so the dynamic text+keyword default never locks in.
    props = INDEX_MAPPING["mappings"]["properties"]
    assert props["name_source"] == {"type": "keyword"}
    assert props["chr_models"] == {"type": "keyword"}


def test_name_source_fields_documented():
    assert "spirit_ash" in _FIELD_NOTES["name_source"]
    assert "chr_models" in _FIELD_NOTES
