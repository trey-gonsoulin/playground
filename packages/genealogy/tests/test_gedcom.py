from genealogy._gedcom import from_gedcom, to_gedcom
from genealogy._models import Citation, Event, Person, Tree


def _sample_tree() -> Tree:
    t = Tree(name="t")
    for pid, given, sex in (
        ("I1", "Ann", "F"),
        ("I2", "John", "M"),
        ("I3", "Mary", "F"),
    ):
        t.people[pid] = Person(id=pid, given=given, surname="Walsh", sex=sex)
    t.people["I1"].events.append(
        Event(
            type="BIRT",
            date="12 MAR 1890",
            place="Cork, Ireland",
            citations=[
                Citation(title="Baptism register", url="https://example.org/r/1")
            ],
        )
    )
    t.people["I1"].notes.append("line one\nline two " + "x" * 300)
    t.people["I1"].external_ids["familysearch"] = "KWQ7-ABC"
    t.add_child("I1", "I2", "I3")
    t.next_person = 4
    return t


def test_export_structure():
    ged = to_gedcom(_sample_tree())
    lines = ged.splitlines()
    assert lines[0] == "0 HEAD"
    assert lines[-1] == "0 TRLR"
    assert "1 NAME Ann /Walsh/" in lines
    assert "1 _FSFTID KWQ7-ABC" in lines
    assert "1 HUSB @I2@" in lines and "1 WIFE @I3@" in lines and "1 CHIL @I1@" in lines
    assert all(len(line) <= 255 for line in lines)
    assert any(line.startswith("2 CONT line two") for line in lines)
    assert any(line.startswith("2 CONC ") for line in lines)


def test_round_trip_preserves_people_links_and_text():
    original = _sample_tree()
    parsed = from_gedcom(to_gedcom(original), name="t")

    assert len(parsed.people) == 3
    ann = next(p for p in parsed.people.values() if p.given == "Ann")
    assert ann.surname == "Walsh" and ann.sex == "F"
    assert ann.external_ids == {"familysearch": "KWQ7-ABC"}
    birth = ann.event("BIRT")
    assert birth and birth.date == "12 MAR 1890" and birth.place == "Cork, Ireland"
    assert birth.citations[0].url == "https://example.org/r/1"
    assert ann.notes == original.people["I1"].notes

    parents = parsed.relatives(ann.id)["parents"]
    assert sorted(p["name"] for p in parents) == ["John Walsh", "Mary Walsh"]


def test_import_tolerates_foreign_gedcom():
    ged = "﻿0 HEAD\n1 CHAR UTF-8\n0 @P7@ INDI\n1 NAME Seán /Ó Briain/\n1 SEX M\n1 DEAT Y\n2 DATE ABT 1901\n1 _UID abc\n0 @X1@ FAM\n1 HUSB @P7@\n1 MARR\n2 PLAC Clare\n0 TRLR\n"
    t = from_gedcom(ged)
    (p,) = t.people.values()
    assert (p.given, p.surname, p.sex) == ("Seán", "Ó Briain", "M")
    death = p.event("DEAT")
    assert death and death.date == "ABT 1901" and death.value is None
    (fam,) = t.families.values()
    assert fam.partner_ids == [p.id] and fam.events[0].place == "Clare"


def test_long_text_survives_round_trip_at_space_boundaries():
    notes = [
        "a" * 239 + " word",  # space would land at the 240-char cut
        "b" * 240 + " tail",  # space would start the next piece
        "x " * 300,  # many candidate boundaries, trailing space
        " " * 600 + "end",  # no clean boundary at all
    ]
    t = Tree()
    t.people["I1"] = Person(id="I1", given="A", notes=notes)
    ged = to_gedcom(t)
    assert all(len(line) <= 255 for line in ged.splitlines())
    (p,) = from_gedcom(ged).people.values()
    assert p.notes == notes
