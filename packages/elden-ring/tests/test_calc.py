"""Attack-rating calculator checks (#120). No OpenSearch needed."""

import math

from elden_ring._calc import attack_rating, effective_stats, graph_value

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
