"""Attack-rating calculator checks (#120). No OpenSearch needed."""

import math

from elden_ring._calc import attack_rating, effective_stats, graph_value, spell_damage

# CalcCorrectGraph 0 (damage) and 6 (status) as the 1.17 regulation defines them.
_G0 = [[1, 0, 1.2], [18, 25, -1.2], [60, 75, 1], [80, 90, 1], [150, 110, 1]]
_G6 = [[1, 0, 1], [25, 10, 1], [45, 75, 1], [60, 90, 1], [99, 100, 1]]
_TENS = {s: 10 for s in ("str", "dex", "int", "fai", "arc")}


def _inputs(**kw) -> dict:
    return {
        "attack": {"physical": [100.0, 200.0]},
        "scaling": {"str": [50.0, 60.0], "dex": [50.0, 60.0]},
        "correct": {"physical": ["str", "dex"]},
        "graph_ids": {"physical": 0, "bleed": 6},
        "graphs": {"0": _G0, "6": _G6},
        **kw,
    }


def test_graph_stage_edges():
    assert graph_value(_G0, 1) == 0
    assert graph_value(_G0, 18) == 0.25
    assert graph_value(_G0, 60) == 0.75
    assert math.isclose(graph_value(_G0, 99), 0.9 + 0.2 * 19 / 70)
    assert graph_value(_G6, 45) == 0.75
    # adjPt -1.2 between 18 and 60 mirrors the curve (fast early growth)
    assert graph_value(_G0, 39) > 0.5
    # adjPt 1.2 below 18 is slow early growth
    assert graph_value(_G0, 9) < 0.25 * 8 / 17


def test_scaling_bonus_and_floor():
    stats = {**_TENS, "str": 60, "dex": 60}
    r = attack_rating(_inputs(), {}, stats, 1, False)
    # 200 x (1 + 0.6 x 0.75 x 2) = 380
    assert r["attack_power"]["physical"] == {"base": 200, "scaling": 180, "total": 380}
    assert r["total"] == 380
    assert r["penalized"] is None


def test_unmet_requirement_penalty():
    r = attack_rating(_inputs(), {"dex": 20}, _TENS, 0, False)
    assert r["attack_power"]["physical"] == {"base": 100, "scaling": -40, "total": 60}
    assert r["unmet_requirements"] == ["dex"]
    assert r["penalized"] == ["physical"]


def test_two_handing_str_bonus_rules():
    stats = {**_TENS, "str": 11}
    assert effective_stats({}, stats, True)["str"] == 16
    assert effective_stats({"paired": True}, stats, True)["str"] == 11
    assert effective_stats({"always_two_handed": True}, stats, False)["str"] == 16
    # meeting a Str requirement only when two-handed lifts the penalty
    r1 = attack_rating(_inputs(), {"str": 15}, stats, 0, False)
    r2 = attack_rating(_inputs(), {"str": 15}, stats, 0, True)
    assert r1["penalized"] == ["physical"] and r2["penalized"] is None


def test_status_scales_with_arcane_only_listed_statuses():
    inputs = _inputs(
        scaling={"arc": [50.0, 50.0]},
        correct={},
        status={"bleed": [50, 50], "frostbite": [40, 40]},
    )
    r = attack_rating(inputs, {}, {**_TENS, "arc": 45}, 0, True)
    # 50 x (1 + 0.5 x 0.75) = 68.75
    assert r["status_buildup"]["bleed"] == {"base": 50, "scaling": 18, "total": 68}
    assert r["status_buildup"]["frostbite"] == {"base": 40, "scaling": 0, "total": 40}


def test_overwrite_rate_keeps_level_growth():
    inputs = _inputs(overwrite={"physical": {"dex": 0.3}})
    stats = {**_TENS, "str": 1, "dex": 60}
    # dex rate = 0.3 x 60/50 = 0.36; 200 x (1 + 0.36 x 0.75) = 254
    r = attack_rating(inputs, {}, stats, 1, False)
    assert r["attack_power"]["physical"]["total"] == 254


def test_catalyst_spell_scaling():
    inputs = _inputs(
        spell_tool="sorcery",
        scaling={"int": [100.0]},
        correct={"physical": [], "magic": ["int"]},
        attack={"physical": [25.0]},
        graph_ids={t: 0 for t in ("physical", "magic", "fire", "lightning", "holy")},
    )
    r = attack_rating(inputs, {"int": 10}, {**_TENS, "int": 60}, 0, False)
    assert r["spell_scaling"]["magic"] == 175
    assert r["spell_scaling"]["fire"] == 100
    r = attack_rating(inputs, {"int": 20}, _TENS, 0, False)
    assert r["spell_scaling"]["magic"] == 60


# CalcCorrectGraph 4 (throwable damage) and 10 (throwable status), 1.17.
_G4 = [[1, 0, 1], [20, 40, 1], [50, 80, 1], [80, 95, 1], [99, 100, 1]]
_G10 = [[1, 0, 1], [15, 10, 1], [30, 50, 1], [50, 60, 1], [99, 70, 1]]


def test_thrown_consumable_matches_wiki():
    """#178: a thrown item's flat power scales through its virtual weapon (Fire Pot:
    230 fire, Str 100 / Dex 25 on graph 4), matching the wiki's AR table."""
    fire_pot = {
        "attack": {"fire": [230.0]},
        "scaling": {"str": [100.0], "dex": [25.0]},
        "correct": {"fire": ["str", "dex"]},
        "graph_ids": {"fire": 4},
        "graphs": {"4": _G4},
    }
    for s, d, wiki in ((10, 10, 284), (20, 10, 332), (50, 10, 424), (99, 99, 517)):
        r = attack_rating(fire_pot, {}, {**_TENS, "str": s, "dex": d}, 0, False)
        assert r["attack_power"]["fire"]["total"] == wiki, (s, d, r)
    # Poison Spraymist: 26 poison, Arc 65 on graph 10 -> wiki 27 at 10, 36 at 60.
    spraymist = {
        "scaling": {"arc": [65.0]},
        "status": {"poison": [26]},
        "graph_ids": {"poison": 10},
        "graphs": {"10": _G10},
    }
    for arc, wiki in ((10, 27), (60, 36)):
        r = attack_rating(spraymist, {}, {**_TENS, "arc": arc}, 0, False)
        assert r["status_buildup"]["poison"]["total"] == wiki, (arc, r)


def _defense_multiplier(ratio: float) -> float:
    """Elden Ring's attack/defense damage curve (share of AR that lands)."""
    if ratio > 8:
        return 0.9
    if ratio > 2.5:
        return 0.9 - 0.2 * ((8 - ratio) / 5.5) ** 2
    if ratio > 1:
        return 0.7 - 0.3 * ((2.5 - ratio) / 1.5) ** 2
    if ratio > 0.125:
        return 0.1 + 0.3 * ((ratio - 0.125) / 0.875) ** 2
    return 0.1


def test_thrown_motion_percent_is_not_applied():
    """#182: Throwing Dagger's AtkParam_Pc row reads physical motion 120 %, but the
    game deals its flat 67 x stat scaling only. The wiki's damage tests on Land
    Squirts (physical defense 100 x area SpEffect 7060's 1.079, pierce taken 1.0)
    match the unmultiplied AR to within 1 point; x 1.2 overshoots by 15+."""
    dagger = {
        "attack": {"physical": [67.0]},
        "scaling": {"str": [95.0], "dex": [145.0]},
        "correct": {"physical": ["str", "dex"]},
        "graph_ids": {"physical": 3},
        "graphs": {
            "3": [[1, 0, 1], [20, 30, 1], [30, 62, 1], [50, 82, 1], [99, 100, 1]]
        },
    }
    wiki = {(10, 10): 26, (20, 20): 49, (80, 20): 85, (20, 80): 105, (40, 40): 112}
    defense = 100 * 1.079
    for (s, d), dealt in {**wiki, (50, 50): 127}.items():
        ar = attack_rating(dagger, {}, {**_TENS, "str": s, "dex": d}, 0, False)
        ar = ar["attack_power"]["physical"]["total"]
        assert abs(ar * _defense_multiplier(ar / defense) - dealt) < 1, (s, d, ar)
        boosted = ar * 1.2
        assert boosted * _defense_multiplier(boosted / defense) - dealt > 15, (s, d)


def test_calculate_attack_rating_falls_back_to_consumable(monkeypatch):
    """A name that isn't a weapon resolves as a thrown consumable (#178)."""
    from elden_ring import _client

    seen = {}

    def resolve(client, name, entity_type=None):
        return "Fire Pot" if entity_type == "consumable" else None

    class Client:
        def search(self, index, body):
            seen["filter"] = body["query"]["bool"]["filter"]
            doc = {"name": "Fire Pot", "ar_inputs": {"attack": {"fire": [230.0]}}}
            return {"hits": {"hits": [{"_source": doc}]}}

    monkeypatch.setattr(_client, "_resolve_entity_name", resolve)
    monkeypatch.setattr(_client, "_affinity_variant", lambda *a: None)
    monkeypatch.setattr(_client, "_entity_versions", lambda c, t: ["1.17.0"])
    r = _client.calculate_attack_rating(Client(), "Fire Pot", dict(_TENS))
    assert {"term": {"entity_type": "consumable"}} in seen["filter"]
    assert r["weapon"] == "Fire Pot" and r["max_level"] == 0, r
    assert r["attack_power"]["fire"]["total"] == 230, r


def test_calculate_attack_rating_ignores_two_handed_for_consumable(monkeypatch):
    """Consumables aren't wielded: two_handed=True must not apply Str x1.5 (#178)."""
    from elden_ring import _client

    fire_pot = {
        "attack": {"fire": [230.0]},
        "scaling": {"str": [100.0], "dex": [25.0]},
        "correct": {"fire": ["str", "dex"]},
        "graph_ids": {"fire": 4},
        "graphs": {"4": _G4},
    }

    class Client:
        def search(self, index, body):
            doc = {"name": "Fire Pot", "ar_inputs": fire_pot}
            return {"hits": {"hits": [{"_source": doc}]}}

    monkeypatch.setattr(
        _client,
        "_resolve_entity_name",
        lambda c, n, t=None: "Fire Pot" if t == "consumable" else None,
    )
    monkeypatch.setattr(_client, "_affinity_variant", lambda *a: None)
    monkeypatch.setattr(_client, "_entity_versions", lambda c, t: ["1.17.0"])
    stats = {**_TENS, "str": 20}
    one = _client.calculate_attack_rating(Client(), "Fire Pot", dict(stats))
    two = _client.calculate_attack_rating(
        Client(), "Fire Pot", dict(stats), two_handed=True
    )
    assert two["attack_power"] == one["attack_power"], (one, two)
    assert two["attack_power"]["fire"]["total"] == 332, two  # wiki, 20 Str / 10 Dex
    assert two["two_handed"] is False and "notes" not in one, two
    assert any("two_handed ignored" in n for n in two["notes"]), two


def test_spell_damage_wiki_pebble():
    """Glintstone Pebble (magic 152, the wiki's x1.52) from a Meteorite Staff at
    80 Int, whose spell scaling the wiki gives as 272: 413 (#130)."""
    scaling = dict.fromkeys(("physical", "magic", "fire", "lightning", "holy"), 272)
    assert spell_damage({"magic": 152}, scaling) == {
        "magic": {"base": 152, "total": 413}
    }


def test_calculate_attack_rating_with_spell(monkeypatch):
    """spell= scales the spell doc's spell_attacks base by the catalyst (#130)."""
    from elden_ring import _client

    staff = {
        "name": "Test Staff",
        "menu_category": "Glintstone Staff",
        "requirements": {"int": 10},
        "ar_inputs": _inputs(
            spell_tool="sorcery",
            scaling={"int": [100.0]},
            correct={"physical": [], "magic": ["int"]},
            attack={"physical": [25.0]},
            graph_ids={
                t: 0 for t in ("physical", "magic", "fire", "lightning", "holy")
            },
        ),
    }
    spells = {
        "Glintstone Pebble": {
            "name": "Glintstone Pebble",
            "menu_category": "Sorcery",
            "spell_attacks": [{"spell": "x", "attack_power": {"magic": 152}}],
        },
        "Lightning Spear": {
            "name": "Lightning Spear",
            "menu_category": "Incantation",
            "spell_attacks": [
                {
                    "spell": "x",
                    "attack_power": {"lightning": 293},
                    "hit_count": 1,
                    "attack_power_per_cast": {"lightning": 293},
                    "uncharged": {
                        "attack_power": {"lightning": 234},
                        "hit_count": 2,
                        "attack_power_per_cast": {"lightning": 284},
                    },
                    "charged": {
                        "attack_power": {"lightning": 293},
                        "hit_count": 1,
                        "attack_power_per_cast": {"lightning": 293},
                    },
                }
            ],
        },
        "Glintstone Stars": {
            "name": "Glintstone Stars",
            "menu_category": "Sorcery",
            "spell_attacks": [
                {
                    "spell": "x",
                    "attack_power": {"magic": 87},
                    "hit_count": 3,
                    "attack_power_per_cast": {"magic": 87 + 78 + 68},
                }
            ],
        },
        "Comet Azur": {
            "name": "Comet Azur",
            "menu_category": "Sorcery",
            "spell_attacks": [
                {"spell": "x", "attack_power": {"magic": 100}, "channeled": True}
            ],
        },
        "Golden Vow": {"name": "Golden Vow", "menu_category": "Incantation"},
    }

    class Client:
        def search(self, index, body):
            terms = {
                k: v
                for f in body["query"]["bool"]["filter"]
                for k, v in f["term"].items()
            }
            if terms["entity_type"] == "spell":
                doc = spells[terms["name.keyword"]]
            else:
                doc = staff
            return {"hits": {"hits": [{"_source": doc}]}}

    monkeypatch.setattr(
        _client, "_resolve_entity_name", lambda c, n, t=None: None if n == "Nope" else n
    )
    monkeypatch.setattr(_client, "_entity_versions", lambda c, t: ["1.17.0"])
    stats = {**_TENS, "int": 60}
    r = _client.calculate_attack_rating(
        Client(), "Test Staff", dict(stats), spell="Glintstone Pebble"
    )
    assert r["spell_scaling"]["magic"] == 175, r
    assert r["spell"] == "Glintstone Pebble", r
    assert r["spell_attack_power"] == {"magic": {"base": 152, "total": 266}}, r
    assert r["spell_total"] == 266 and "notes" not in r, r
    assert "spell_hit_count" not in r and "spell_charged" not in r, r
    # Hits per cast (#270): every star's power summed, then scaled (233 x 1.75).
    r = _client.calculate_attack_rating(
        Client(), "Test Staff", dict(stats), spell="Glintstone Stars"
    )
    assert r["spell_total"] == 152, r  # the strongest star, 87 x 1.75
    assert r["spell_hit_count"] == 3 and r["spell_total_per_cast"] == 407, r
    r = _client.calculate_attack_rating(
        Client(), "Test Staff", dict(stats), spell="Comet Azur"
    )
    assert r["spell_channeled"] is True and "spell_hit_count" not in r, r
    # A staff given an incantation still computes, with a note.
    r = _client.calculate_attack_rating(
        Client(), "Test Staff", dict(stats), spell="Lightning Spear"
    )
    # (this staff's lightning spell scaling is the unscaled 100)
    assert r["spell_attack_power"]["lightning"]["total"] == 293, r
    # The uncharged / charged casts of a chargeable spell (#269).
    assert r["spell_uncharged"] == {
        "attack_power": {"lightning": {"base": 234, "total": 234}},
        "total": 234,
        "hit_count": 2,
        "total_per_cast": 284,
    }, r
    assert r["spell_charged"]["total"] == 293, r
    assert any("can't cast incantations" in n for n in r["notes"]), r
    for name, msg in (("Golden Vow", "deals no damage"), ("Nope", "not found")):
        r = _client.calculate_attack_rating(Client(), "Test Staff", stats, spell=name)
        assert msg in r["error"], r
