from datetime import datetime

import pytest
from vault_mcp import _notes

BOM = "\ufeff"


def test_set_properties_never_reinterprets_other_values():
    text = (
        "---\ntime: 12:30\nversion: 1.10\nzip: 0755\nflag: yes\nn: 1_000\n"
        'quoted: "x"   # note\nlist:\n- a\n- b\n---\nbody\n'
    )
    out = _notes.set_properties(text, {"status": "archive"})
    assert out == text.replace("---\nbody", "status: archive\n---\nbody")
    fm, _, _ = _notes.split(out)
    assert fm["time"] == "12:30" and fm["zip"] == "0755" and fm["flag"] == "yes"


def test_set_properties_replaces_and_removes_only_named_keys():
    text = "---\na: 1\nb:\n  - x\n  - y\nc: 3\n---\nbody"
    out = _notes.set_properties(text, {"b": ["z"], "c": None, "d": True})
    assert out == "---\na: 1\nb:\n  - z\nd: true\n---\nbody"


def test_non_mapping_block_is_not_frontmatter():
    text = "---\nSome intro paragraph\n---\nrest"
    _fm, body, ok = _notes.split(text)
    assert not ok and body == text
    with pytest.raises(_notes.FrontmatterError):
        _notes.set_properties(text, {"status": "archive"})


def test_bom_note_keeps_single_frontmatter():
    text = BOM + "---\ntype: note\n---\nbody"
    fm, body, ok = _notes.split(text)
    assert ok and fm == {"type": "note"} and body == "body"
    out = _notes.set_properties(text, {"status": "active"})
    assert out == BOM + "---\ntype: note\nstatus: active\n---\nbody"


def test_frontmatter_round_trip_keeps_dates_and_links():
    text = '---\ntype: note\ncreated: 2026-11-10\nup: "[[Dashboard]]"\ntags: []\nup2:\n---\nbody\n'
    fm, body, ok = _notes.split(text)
    assert ok and fm["created"] == "2026-11-10" and body == "body\n"
    assert _notes.join(fm, body) == text


def test_lists_indent_like_obsidian():
    out = _notes.set_properties("---\ntype: note\n---\nx", {"aliases": ["Old"]})
    assert out == "---\ntype: note\naliases:\n  - Old\n---\nx"


def test_invalid_yaml_is_reported():
    _fm, _body, ok = _notes.split("---\nkey: [unclosed\n---\nx")
    assert not ok


def test_wikilinks_and_tags():
    body = "See [[A]], [[B|bee]], ![[C.png]], [[D#Head]] and [[A]]. #Tag #x/y `#no`\n```\n#fenced\n```"
    assert _notes.wikilinks(body) == ["A", "B", "C.png", "D"]
    assert _notes.tags({"tags": ["daily"]}, body) == ["daily", "tag", "x/y"]


def test_rewrite_links_keeps_alias_heading_embed():
    text = "[[Old]] [[Old|o]] ![[Old#h]] [[Other]]"
    new, n = _notes.rewrite_links(text, lambda t: t == "Old", "New")
    assert (new, n) == ("[[New]] [[New|o]] ![[New#h]] [[Other]]", 3)


def test_moment_format_and_template():
    when = datetime(2026, 11, 1, 9, 5)
    assert _notes.moment_format(when, "dddd, MMMM D, YYYY [at] HH:mm") == (
        "Sunday, November 1, 2026 at 09:05"
    )
    out = _notes.render_template("{{title}} {{date}} {{time}}", title="T", when=when)
    assert out == "T 2026-11-01 09:05"


def test_append_under_heading_nested_sections():
    body = "# Day\n\n## Capture\n- a\n### Sub\nx\n\n## Journal\n"
    out = _notes.append_under_heading(body, "- b", "Capture")
    assert out == "# Day\n\n## Capture\n- a\n### Sub\nx\n- b\n\n## Journal\n"
