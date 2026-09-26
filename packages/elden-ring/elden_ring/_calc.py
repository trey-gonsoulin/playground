"""Stat-dependent weapon attack rating and status buildup (#120).

Pure functions over a weapon doc's non-indexed ``ar_inputs`` (per-level base attack,
scaling and buildup, the stats that scale each damage type, and the CalcCorrectGraph
curves), following the community calculator's model
(ThomasJClark/elden-ring-weapon-calculator):

- each type deals ``base x (1 + sum(scaling_stat x curve(stat)))``;
- an unmet requirement for any stat a type scales with makes it ``base x 0.6``;
- two-handing counts Str as floor(Str x 1.5) (not for paired weapons; bows and
  ballistae are always two-handed);
- poison, bleed, sleep and madness scale with Arcane (unadjusted stats); rot, frost
  and death blight never scale;
- catalysts get spell scaling = 100 x the multiplier per damage type;
- displayed values are floored per type.
"""

import math

STATS = ("str", "dex", "int", "fai", "arc")
DAMAGE_TYPES = ("physical", "magic", "fire", "lightning", "holy")
_ARC_STATUSES = frozenset({"poison", "bleed", "sleep", "madness"})
_EPS = 1e-9  # guards floor() against float error (114.0 stored as 113.99999…)


def graph_value(stages: list, stat: int) -> float:
    """CalcCorrectGraph correction (0..~1.1) at a stat value. ``stages`` are five
    [maxVal, maxGrowVal (percent), adjPt] rows; between two stages the growth is
    interpolated with exponent adjPt (negative: mirrored curve)."""
    for i in range(1, 5):
        if stat <= stages[i][0] or i == 4:
            break
    (lo, lo_grow, adj), (hi, hi_grow, _) = stages[i - 1], stages[i]
    ratio = min(1.0, max(0.0, (stat - lo) / (hi - lo))) if hi != lo else 1.0
    if adj > 0:
        ratio = ratio**adj
    elif adj < 0:
        ratio = 1 - (1 - ratio) ** -adj
    return (lo_grow + (hi_grow - lo_grow) * ratio) / 100


def effective_stats(inputs: dict, stats: dict, two_handed: bool) -> dict:
    """Stats with the two-handing Str bonus applied where the weapon gets it."""
    out = dict(stats)
    if inputs.get("always_two_handed") or (two_handed and not inputs.get("paired")):
        out["str"] = math.floor(out["str"] * 1.5)
    return out


def _multiplier(inputs, t, scaled_by, level, stats, unmet) -> tuple[float, bool]:
    """(1 + scaling bonus, penalized) for one damage type or status."""
    if any(s in unmet for s in scaled_by):
        return 0.6, True
    scaling = inputs.get("scaling") or {}
    overwrite = (inputs.get("overwrite") or {}).get(t, {})
    stages = (inputs.get("graphs") or {}).get(
        str((inputs.get("graph_ids") or {}).get(t))
    )
    mult = 1.0
    for s in scaled_by:
        per_level = scaling.get(s)
        if not per_level or not stages:
            continue
        rate = per_level[level] / 100
        if s in overwrite:
            rate = overwrite[s] * per_level[level] / per_level[0]
        mult += graph_value(stages, stats[s]) * rate
    return mult, False


def _breakdown(base: float, mult: float) -> dict:
    total = math.floor(base * mult + _EPS)
    shown = math.floor(base + _EPS)
    return {"base": shown, "scaling": total - shown, "total": total}


def attack_rating(
    inputs: dict, requirements: dict, stats: dict, level: int, two_handed: bool
) -> dict:
    """Attack power, status buildup and (catalysts) spell scaling at ``level`` for
    character ``stats`` ({str, dex, int, fai, arc})."""
    adjusted = effective_stats(inputs, stats, two_handed)
    unmet = [s for s in STATS if adjusted[s] < (requirements or {}).get(s, 0)]
    correct = inputs.get("correct") or {}
    attack: dict = {}
    penalized: list = []
    for t, per_level in (inputs.get("attack") or {}).items():
        mult, pen = _multiplier(inputs, t, correct.get(t, []), level, adjusted, unmet)
        attack[t] = _breakdown(per_level[level], mult)
        if pen:
            penalized.append(t)
    status: dict = {}
    for t, per_level in (inputs.get("status") or {}).items():
        if not per_level[level]:
            continue
        scaled_by = ["arc"] if t in _ARC_STATUSES else []
        mult, pen = _multiplier(inputs, t, scaled_by, level, stats, unmet)
        status[t] = _breakdown(per_level[level], mult)
        if pen:
            penalized.append(t)
    spell = None
    if inputs.get("spell_tool"):
        spell = {
            t: math.floor(
                100
                * _multiplier(inputs, t, correct.get(t, []), level, adjusted, unmet)[0]
                + _EPS
            )
            for t in DAMAGE_TYPES
        }
    return {
        "attack_power": attack,
        "total": sum(v["total"] for v in attack.values()),
        "status_buildup": status or None,
        "spell_scaling": spell,
        "unmet_requirements": unmet or None,
        "penalized": penalized or None,
        "effective_stats": adjusted,
    }
