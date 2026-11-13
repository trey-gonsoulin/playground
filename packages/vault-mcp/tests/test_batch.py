"""Batch tools: create_notes, update_notes, move_notes and import_archive."""

import io
import socket
import tarfile
import zipfile

import pytest
from conftest import audit
from mcp.server.mcpserver.exceptions import ToolError
from vault_mcp import _archive
from vault_mcp import mcp_server as t

# --- create_notes -----------------------------------------------------------------


def test_create_notes_small_batch_applies(vault, tmp_path):
    out = t.create_notes(
        [
            {"title": "Pasta", "body": "boil", "folder": "Home", "type": "recipe"},
            {"title": "Idea"},
        ]
    )
    assert out["dry_run"] is False
    assert out["paths"] == ["Home/Pasta.md", "_Inbox/Idea.md"]
    pasta = (vault / "Home/Pasta.md").read_text()
    assert "type: recipe" in pasta and 'up: "[[Home]]"' in pasta
    assert "# Idea" in (vault / "_Inbox/Idea.md").read_text()
    [entry] = audit(tmp_path)
    assert entry["tool"] == "create_notes" and len(entry["paths"]) == 2


def test_create_notes_checks_everything_before_writing(vault):
    out = t.create_notes(
        [
            {"title": "Fine"},
            {"title": "Rug Options", "folder": "Home"},  # exists
            {"title": "Bad", "type": "nonsense"},
            {"title": "fine"},  # duplicate in the batch
        ]
    )
    assert out["dry_run"] is True
    assert [e["index"] for e in out["errors"]] == [1, 2, 3]
    assert not (vault / "_Inbox").exists()


def test_create_notes_large_batch_plans_first(vault):
    notes = [{"title": f"Note {i}"} for i in range(12)]
    plan = t.create_notes(notes)
    assert plan["dry_run"] is True and plan["to_create"] == 12
    assert not (vault / "_Inbox").exists()
    t.create_notes(notes, dry_run=False)
    assert len(list((vault / "_Inbox").iterdir())) == 12


def test_batch_size_limit(vault):
    with pytest.raises(ToolError, match="at most"):
        t.create_notes([{"title": f"n{i}"} for i in range(t.MAX_BATCH + 1)])
    with pytest.raises(ToolError, match="empty"):
        t.update_notes([])


# --- update_notes -----------------------------------------------------------------


def test_update_notes_dry_run_then_apply(vault, tmp_path):
    rug = (vault / "Home/Rug Options.md").read_text()
    updates = [
        {"note": "Rug Options", "patch": [{"find": "- wool", "replace": "- silk"}]},
        {"note": "Old list", "properties": {"status": "archive"}},
        {"note": "Vex", "properties": {"type": "campaign"}},  # already so
    ]
    plan = t.update_notes(updates)
    assert plan["dry_run"] is True and plan["to_change"] == 2
    assert "-- wool" in plan["plan"][0]["diff"] and "+- silk" in plan["plan"][0]["diff"]
    assert plan["unchanged"] == ["TTRPG/Murder on the Eberron Express/NPCs/Vex.md"]
    assert (vault / "Home/Rug Options.md").read_text() == rug
    t.update_notes(updates, dry_run=False)
    assert (vault / "Home/Rug Options.md").read_text() == rug.replace(
        "- wool", "- silk"
    )
    assert "status: archive" in (vault / "Home/Old list.md").read_text()
    assert audit(tmp_path)[-1]["tool"] == "update_notes"


def test_update_notes_refuses_when_any_item_fails(vault):
    rug = (vault / "Home/Rug Options.md").read_text()
    out = t.update_notes(
        [
            {"note": "Rug Options", "body": "replaced"},
            {"note": "Shopping", "patch": [{"find": "not there", "replace": "x"}]},
            {"note": "Nope"},
        ],
        dry_run=False,
    )
    assert out["dry_run"] is True
    assert [e["index"] for e in out["errors"]] == [1, 2]
    assert (vault / "Home/Rug Options.md").read_text() == rug


def test_update_notes_one_edit_per_note(vault):
    out = t.update_notes(
        [
            {"note": "Rug Options", "body": "a"},
            {"note": "Home/Rug Options.md", "body": "b"},
        ]
    )
    assert "twice" in out["errors"][0]["error"]


# --- move_notes -------------------------------------------------------------------


def test_move_notes_plan_then_apply(vault):
    moves = [
        {"note": "Rug Options", "to": "Living Room Rugs"},
        {"note": "Old list", "to": "Archive/"},
    ]
    plan = t.move_notes(moves)
    assert plan["dry_run"] is True and plan["to_move"] == 2
    assert plan["plan"][0]["notes_with_links_to_update"] == ["Home/Shopping.md"]
    assert (vault / "Home/Rug Options.md").exists()
    out = t.move_notes(moves, dry_run=False)
    assert [m["to"] for m in out["moved"]] == [
        "Home/Living Room Rugs.md",
        "Archive/Old list.md",
    ]
    assert "[[Living Room Rugs|a rug]]" in (vault / "Home/Shopping.md").read_text()
    assert "Rug Options" in (vault / "Home/Living Room Rugs.md").read_text()  # alias
    assert (vault / "Archive/Old list.md").exists()


def test_move_notes_refuses_clashing_targets(vault):
    out = t.move_notes(
        [{"note": "Rug Options", "to": "Same"}, {"note": "Shopping", "to": "Same"}],
        dry_run=False,
    )
    assert out["dry_run"] is True and "two notes" in out["errors"][0]["error"]
    assert (vault / "Home/Rug Options.md").exists()


def test_move_notes_hands_back_the_rest_when_out_of_time(vault, monkeypatch):
    moves = [{"note": "Rug Options", "to": "Rugs"}, {"note": "Old list", "to": "Older"}]
    monkeypatch.setattr(t, "_BATCH_SECONDS", -1.0)
    out = t.move_notes(moves, dry_run=False)
    assert out["moved"] == [] and len(out["remaining"]) == 2
    monkeypatch.setattr(t, "_BATCH_SECONDS", 15.0)
    out = t.move_notes(out["remaining"], dry_run=False)
    assert len(out["moved"]) == 2 and "remaining" not in out


# --- import_archive ---------------------------------------------------------------


def _zip(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


ARCHIVE = {
    "export/Recipes/Soup.md": b"# Soup\nhot\n",
    "export/Recipes/Bread.md": b"---\ntype: recipe\ncreated: 2020-01-01\n---\n# Bread\n",
    "export/Recipes/bread.png": b"\x89PNG fake",
    "export/.obsidian/app.json": b"{}",
    "export/__MACOSX/._Soup.md": b"junk",
    "export/../escape.md": b"nope",
}


@pytest.fixture
def served(monkeypatch):
    """import_archive's download, served from memory."""
    box: dict[str, bytes] = {}
    monkeypatch.setattr(_archive, "fetch", lambda url, timeout=0: box["data"])
    return box


def test_import_archive_plan_then_apply(vault, served, tmp_path):
    served["data"] = _zip(ARCHIVE, symlink="export/link.md")
    plan = t.import_archive("https://example.com/x.zip", folder="_Inbox/Import")
    assert plan["dry_run"] is True and plan["dropped_top_folder"] == "export"
    assert sorted(plan["paths"]) == [
        "_Inbox/Import/Recipes/Bread.md",
        "_Inbox/Import/Recipes/Soup.md",
        "_Inbox/Import/Recipes/bread.png",
    ]
    assert plan["notes"] == 2 and plan["attachments"] == 1
    reasons = {s["path"]: s["reason"] for s in plan["skipped"]}
    assert reasons == {
        "export/link.md": "symlink",
        "export/../escape.md": "unsafe path",
    }
    assert not (vault / "_Inbox").exists()

    with pytest.raises(ToolError, match="sha256"):
        t.import_archive("https://example.com/x.zip", "_Inbox/Import", dry_run=False)
    out = t.import_archive(
        "https://example.com/x.zip",
        "_Inbox/Import",
        sha256=plan["sha256"],
        dry_run=False,
    )
    assert out["to_write"] == 3 and out["backend"] == "filesystem"
    soup = (vault / "_Inbox/Import/Recipes/Soup.md").read_text()
    assert soup.startswith("---\ntype: note\nstatus: active\ncreated: ")
    assert soup.endswith("# Soup\nhot\n")
    bread = (vault / "_Inbox/Import/Recipes/Bread.md").read_text()
    assert "type: recipe" in bread and "created: 2020-01-01" in bread
    assert "status: active" in bread
    assert (vault / "_Inbox/Import/Recipes/bread.png").read_bytes() == b"\x89PNG fake"
    assert not (vault / "_Inbox/Import/.obsidian").exists()
    assert audit(tmp_path)[-1]["tool"] == "import_archive"


def test_import_archive_skips_existing_and_checks_sha(vault, served):
    served["data"] = _zip({"Home/Rug Options.md": b"theirs", "Home/New.md": b"new"})
    plan = t.import_archive(
        "https://example.com/x.zip", folder="", strip_top_folder=False
    )
    assert plan["conflicts_skipped"] == ["Home/Rug Options.md"]
    assert plan["paths"] == ["Home/New.md"]
    served["data"] = _zip({"Home/Other.md": b"changed"})
    with pytest.raises(ToolError, match="changed since"):
        t.import_archive(
            "https://example.com/x.zip",
            "",
            strip_top_folder=False,
            sha256=plan["sha256"],
            dry_run=False,
        )
    assert "theirs" not in (vault / "Home/Rug Options.md").read_text()


def test_import_tar_gz(vault, served):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"# Tarred\n"
        info = tarfile.TarInfo("notes/Tarred.md")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("notes/link.md")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(link)
    served["data"] = buf.getvalue()
    plan = t.import_archive("https://example.com/x.tgz", "_Inbox")
    assert plan["paths"] == ["_Inbox/Tarred.md"]
    assert plan["skipped"] == [
        {"path": "notes/link.md", "reason": "link or special file"}
    ]


def test_archive_limits(monkeypatch):
    monkeypatch.setattr(_archive, "MAX_FILE", 4)
    files, skipped = _archive.members(_zip({"a.md": b"tiny", "b.md": b"too big"}))
    assert [f.path for f in files] == ["a.md"]
    assert skipped[0]["path"] == "b.md" and "over" in skipped[0]["reason"]
    monkeypatch.setattr(_archive, "MAX_FILES", 1)
    with pytest.raises(_archive.ArchiveError, match="limit"):
        _archive.members(_zip({"a.md": b"1", "b.md": b"2"}))
    with pytest.raises(_archive.ArchiveError, match="not a zip or tar"):
        _archive.members(b"plain text")


@pytest.mark.parametrize(
    ("url", "addr", "match"),
    [
        ("http://example.com/x.zip", "93.184.216.34", "only https"),
        ("https://metadata.local/x.zip", "169.254.169.254", "not a public host"),
        ("https://intranet/x.zip", "10.0.0.5", "not a public host"),
        ("https://localhost/x.zip", "127.0.0.1", "not a public host"),
    ],
)
def test_fetch_refuses_non_public_urls(monkeypatch, url, addr, match):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443))],
    )
    with pytest.raises(_archive.ArchiveError, match=match):
        _archive.fetch(url)
