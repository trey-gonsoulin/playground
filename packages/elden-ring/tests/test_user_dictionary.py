"""Kuromoji user dictionary (#53): rule shape and analyzer wiring. The live check
builds a throwaway index and needs the integration env vars (see test_integration)."""

import os

import pytest

from elden_ring._client import INDEX_MAPPING, KUROMOJI_USER_DICTIONARY

_ANALYSIS = INDEX_MAPPING["settings"]["analysis"]


def test_rules_well_formed():
    surfaces = []
    for rule in KUROMOJI_USER_DICTIONARY:
        surface, segmentation, reading, pos = rule.split(",")
        # Lucene rejects a segmentation that doesn't spell the surface, and
        # needs one reading per segment
        assert segmentation.replace(" ", "") == surface, rule
        assert len(reading.split(" ")) == len(segmentation.split(" ")), rule
        assert pos.startswith("カスタム"), rule
        surfaces.append(surface)
    assert len(surfaces) == len(set(surfaces))
    for term in ("褪せ人", "黄金樹", "二本指", "結晶人", "しろがね人"):
        assert term in surfaces


def test_both_kuromoji_tokenizers_use_the_dictionary():
    tokenizers = _ANALYSIS["tokenizer"]
    analyzers = _ANALYSIS["analyzer"]
    assert tokenizers["kuromoji_normal"]["mode"] == "normal"
    assert tokenizers["kuromoji_search"]["mode"] == "search"
    for name in ("kuromoji_normal", "kuromoji_search"):
        assert tokenizers[name]["user_dictionary_rules"] is KUROMOJI_USER_DICTIONARY
    assert analyzers["kuromoji_analyzer"]["tokenizer"] == "kuromoji_search"
    assert analyzers["kuromoji_segmenter"]["tokenizer"] == "kuromoji_normal"
    assert analyzers["kuromoji_lemmatizer"]["tokenizer"] == "kuromoji_normal"


_LIVE = all(
    os.environ.get(v)
    for v in ("EC2_INSTANCE_ID", "OPENSEARCH_USER", "OPENSEARCH_PASSWORD_SSM_PATH")
)

# text -> (.morph tokens, .lemma tokens, .ja tokens) under the new settings
_EXPECTED = {
    "褪せ人": (["褪せ人"], ["褪せ人"], ["褪せ人"]),
    "しろがね人": (["しろがね人"], ["しろがね人"], ["しろがね人"]),
    "上質なしろがね盾": (
        ["上質", "な", "しろがね", "盾"],
        ["上質", "だ", "しろがね", "盾"],
        ["上質", "しろがね", "盾"],
    ),
    "小黄金樹": (["小", "黄金樹"], ["小", "黄金樹"], ["小", "黄金樹"]),
    "朱い腐敗": (["朱い", "腐敗"], ["朱い", "腐敗"], ["朱い", "腐敗"]),
    "輝石頭": (["輝石頭"], ["輝石頭"], ["輝石頭"]),
}


@pytest.mark.skipif(not _LIVE, reason="integration env vars not set")
def test_tokens_on_throwaway_index():
    import elden_ring._client as _os

    client = _os.get_client()
    index = "elden-ring-kuromoji-test-53"
    if client.indices.exists(index=index):
        client.indices.delete(index=index)
    client.indices.create(index=index, body=INDEX_MAPPING)
    try:

        def tokens(field, text):
            body = {"field": f"description_ja.{field}", "text": text}
            resp = client.indices.analyze(index=index, body=body)
            return [t["token"] for t in resp["tokens"]]

        for text, (morph, lemma, ja) in _EXPECTED.items():
            assert tokens("morph", text) == morph, text
            assert tokens("lemma", text) == lemma, text
            assert tokens("ja", text) == ja, text
    finally:
        client.indices.delete(index=index)
