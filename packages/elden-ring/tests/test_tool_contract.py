"""Tool contract cleanup (#463): sort_id_mod validation, one terms-agg cap with a
truncation warning, and analyze_text covering all four JP analyzers."""

import pytest

from elden_ring import _client


class _NoSearch:
    """Fails if a validation error is not returned before any query."""

    def search(self, *a, **k):
        raise AssertionError("queried despite an invalid sort_id_mod")


@pytest.mark.parametrize(
    ("mod", "remainder"),
    [(0, 0), (-3, 0), (1000, 1000), (1000, -1), (10, 25)],
)
def test_invalid_sort_id_mod_is_an_error(mod, remainder):
    out = _client.search_literal(
        _NoSearch(), pattern="x", sort_id_mod=mod, sort_id_remainder=remainder
    )
    assert "sort_id_mod" in out["error"]


class _AggClient:
    def __init__(self, other=0):
        self.other, self.bodies = other, []

    def search(self, index, body):
        self.bodies.append(body)
        return {
            "aggregations": {
                name: {
                    "buckets": [{"key": "weapon", "doc_count": 3}],
                    "sum_other_doc_count": self.other if name == "sources" else 0,
                }
                for name in body["aggs"]
            }
        }


def test_describe_index_uses_one_cap_and_omits_warnings_when_complete():
    c = _AggClient()
    out = _client.describe_index(c)
    sizes = {a["terms"]["size"] for a in c.bodies[0]["aggs"].values()}
    assert sizes == {_client._CATALOG_AGG_SIZE}
    assert "warnings" not in out


def test_describe_index_warns_when_a_terms_agg_is_truncated():
    out = _client.describe_index(_AggClient(other=7))
    assert len(out["warnings"]) == 1
    assert out["warnings"][0].startswith("sources:")
    assert "7 docs" in out["warnings"][0]


def test_list_entity_types_uses_the_shared_cap():
    c = _AggClient()
    assert _client.list_entity_types(c) == ["weapon"]
    assert c.bodies[0]["aggs"]["types"]["terms"]["size"] == _client._CATALOG_AGG_SIZE


class _Indices:
    def __init__(self):
        self.fields = []

    def analyze(self, index, body):
        self.fields.append(body["field"])
        return {"tokens": [{"token": body["field"]}]}


class _AnalyzeClient:
    def __init__(self):
        self.indices = _Indices()


def test_analyze_text_reports_four_analyzers_by_field_path():
    c = _AnalyzeClient()
    out = _client.analyze_text(c, "褪せ人")
    assert out == {
        "standard": ["description_ja"],
        "kuromoji_analyzer": ["description_ja.ja"],
        "kuromoji_segmenter": ["description_ja.morph"],
        "kuromoji_lemmatizer": ["description_ja.lemma"],
    }
