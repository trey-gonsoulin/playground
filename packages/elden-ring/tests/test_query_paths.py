"""Offline coverage for query paths only the live suite used to reach (#455): the
literal patterns union, calculate_attack_rating's mid-name affinity fallback,
list_menu_categories / list_entity_types, and start_instance. diff_entities and
text_changed_between are covered in test_version_diff_tools.py."""

import pytest
from opensearchpy.exceptions import ConnectionError as SearchConnectionError
from test_filter_validation import _Client, _doc

from elden_ring import _client


def _words(s: str) -> set[str]:
    return set(s.lower().split())


class _QueryClient(_Client):
    """The filter fake plus what these paths send: phrase multi_match and name match
    clauses, collapse, size, _source, distinct-entity counts and ids queries."""

    def search(self, index, body):
        query = body.get("query", {})
        if "ids" in query:
            self.bodies.append(body)
            ids = query["ids"]["values"]
            hits = [
                {"_id": _id, "_source": self._project(d, body)}
                for d in self.docs
                if (_id := self._id(d)) in ids
            ]
            return {"hits": {"hits": hits, "total": {"value": len(hits)}}}
        resp = super().search(index, body)
        if "hits" not in resp:
            return resp
        hits = [
            h
            for h in resp["hits"]["hits"]
            if all(self._must(h["_source"], m) for m in self._musts(query))
        ]
        if collapse := body.get("collapse"):
            field = collapse["field"].removesuffix(".keyword")
            seen, kept = set(), []
            for h in hits:  # newest first, so each group keeps its newest patch
                if (k := self._field(h["_source"], field)) not in seen:
                    seen.add(k)
                    kept.append(h)
            hits = kept
        total = len(hits)
        distinct = len({self._key(h["_source"]) for h in hits})
        hits = [
            {**h, "_source": self._project(h["_source"], body)}
            for h in hits[: body.get("size", 10)]
        ]
        resp["hits"] = {"hits": hits, "total": {"value": total}}
        resp["aggregations"]["distinct_entities"] = {"value": distinct}
        return resp

    @staticmethod
    def _musts(query):
        return query.get("bool", {}).get("must", [])

    @staticmethod
    def _must(doc, clause):
        if "multi_match" in clause:
            phrase = clause["multi_match"]["query"]
            return any(isinstance(v, str) and phrase in v for v in doc.values())
        if "match" in clause:
            return _words(clause["match"]["name"]["query"]) <= _words(doc["name"])
        return True  # match_all

    @staticmethod
    def _key(doc):
        return f"{doc['entity_type']}::{doc['name']}"

    @classmethod
    def _field(cls, doc, field):
        return cls._key(doc) if field == "entity_key" else doc.get(field)

    @classmethod
    def _id(cls, doc):
        return f"{cls._key(doc)}::{doc['patch_version']}"

    @staticmethod
    def _project(doc, body):
        keep = body.get("_source")
        return doc if keep is None else {k: v for k, v in doc.items() if k in keep}


# ---------------------------------------------------------------------------
# search_literal patterns union
# ---------------------------------------------------------------------------

_TEXTS = [
    _doc("Dagger", "1.02.0", description="a short blade, sharp"),
    _doc("Dagger", "1.17.0", description="a short blade, keen and sharp"),
    _doc("Dagger", "1.17.0", "spell", description="a sharp spell blade"),
    _doc("Club", "1.17.0", description="a blunt club, not a blade"),
    _doc("Torch", "1.17.0", description="sheds light"),
]


def test_union_dedupes_names_across_patterns():
    c = _QueryClient(_TEXTS)
    out = _client.search_literal(c, pattern="blade", patterns=["sharp", "blade"])
    # Dagger weapon matches both patterns, the Dagger spell is a separate entity.
    assert out["total"] == 3
    got = [(d["entity_type"], d["name"], d["patch_version"]) for d in out["results"]]
    assert got == [
        ("weapon", "Dagger", "1.17.0"),
        ("spell", "Dagger", "1.17.0"),
        ("weapon", "Club", "1.17.0"),
    ]
    # Full docs come from one ids query, not the key-only union pass.
    assert out["results"][0]["description"] == "a short blade, keen and sharp"
    assert sum("ids" in b["query"] for b in c.bodies) == 1
    # Duplicate patterns ("blade" twice) run one sub-query (#447).
    union = [b for b in c.bodies if b.get("_source") == _client._UNION_KEY_FIELDS]
    assert len(union) == 2


def test_union_include_fields_without_name():
    c = _QueryClient(_TEXTS)
    out = _client.search_literal(
        c, patterns=["sharp", "blunt"], include_fields=["description"]
    )
    assert out["total"] == 3
    assert out["results"] == [
        {"description": "a short blade, keen and sharp"},
        {"description": "a sharp spell blade"},
        {"description": "a blunt club, not a blade"},
    ]


def test_union_respects_limit_but_counts_everything():
    out = _client.search_literal(
        _QueryClient(_TEXTS), patterns=["sharp", "blunt"], limit=1
    )
    assert out["total"] == 3
    assert [d["name"] for d in out["results"]] == ["Dagger"]


def test_union_count_only_skips_the_doc_fetch():
    c = _QueryClient(_TEXTS)
    out = _client.search_literal(c, patterns=["sharp", "light"], count_only=True)
    assert out == {"total": 3}
    assert not any("ids" in b["query"] for b in c.bodies)


def test_union_lower_bound_when_a_pattern_overflows_its_page(monkeypatch):
    monkeypatch.setattr(_client, "_UNION_FETCH_SIZE", 1)
    out = _client.search_literal(
        _QueryClient(_TEXTS), patterns=["blade", "light"], count_only=True
    )
    assert out == {"total": 2, "total_is_lower_bound": True}


def test_union_with_a_patch_version():
    out = _client.search_literal(
        _QueryClient(_TEXTS), patterns=["sharp", "blunt"], patch_version="1.02.0"
    )
    assert out["total"] == 1
    assert out["results"][0]["description"] == "a short blade, sharp"


def test_too_many_patterns_errors_before_any_query():
    c = _QueryClient(_TEXTS)
    pats = [f"p{i}" for i in range(_client._MAX_PATTERNS + 1)]
    assert "at most" in _client.search_literal(c, patterns=pats)["error"]
    assert c.bodies == []


# ---------------------------------------------------------------------------
# calculate_attack_rating: affinity variants named mid-name
# ---------------------------------------------------------------------------

_AR = {"attack": {"physical": [100, 110]}}
_SCAVENGER = [
    _doc("Scavenger's Curved Sword", "1.17.0", affinity="Standard", ar_inputs=_AR),
    _doc(
        "Scavenger's Heavy Curved Sword",
        "1.17.0",
        affinity="Heavy",
        ar_inputs={"attack": {"physical": [120, 130]}},
    ),
    _doc("Heavy Dagger", "1.17.0", affinity="Heavy", ar_inputs=_AR),
    _doc("Dagger", "1.17.0", affinity="Standard", ar_inputs=_AR),
]


def _ar(weapon, affinity=None):
    return _client.calculate_attack_rating(
        _QueryClient(_SCAVENGER), weapon, {}, None, False, affinity
    )


def test_affinity_argument_finds_a_mid_name_variant():
    out = _ar("Scavenger's Curved Sword", "Heavy")
    assert out["weapon"] == "Scavenger's Heavy Curved Sword"
    assert out["level"] == out["max_level"] == 1
    assert out["total"] == 130


def test_affinity_prefixed_name_finds_a_mid_name_variant():
    out = _ar("Heavy Scavenger's Curved Sword")
    assert out["weapon"] == "Scavenger's Heavy Curved Sword"


def test_prefix_affinity_still_resolves_directly():
    assert _ar("Dagger", "Heavy")["weapon"] == "Heavy Dagger"
    assert _ar("Heavy Dagger")["weapon"] == "Heavy Dagger"


def test_missing_affinity_variant_errors():
    assert _ar("Scavenger's Curved Sword", "Fire") == {
        "error": "weapon 'Fire Scavenger's Curved Sword' not found"
    }
    assert _ar("Keen Scavenger's Curved Sword") == {
        "error": "weapon 'Keen Scavenger's Curved Sword' not found"
    }


# ---------------------------------------------------------------------------
# list_menu_categories / list_entity_types
# ---------------------------------------------------------------------------


def _buckets(counts: dict[str, int]) -> list[dict]:
    return [{"key": k, "entity_count": {"value": v}} for k, v in counts.items()]


class _AggClient(_Client):
    """Canned menu_category / entity_type aggregations."""

    def __init__(self, by_type: dict[str, dict[str, int]]):
        super().__init__()
        self.by_type = by_type

    def search(self, index, body):
        aggs = body.get("aggs", {})
        if "by_type" in aggs:
            self.bodies.append(body)
            buckets = [
                {"key": t, "categories": {"buckets": _buckets(c)}}
                for t, c in self.by_type.items()
            ]
            return {"aggregations": {"by_type": {"buckets": buckets}}}
        if "categories" in aggs:
            self.bodies.append(body)
            ((t,),) = [
                f["term"].values()
                for f in body["query"]["bool"]["filter"]
                if "entity_type" in f.get("term", {})
            ]
            cats = self.by_type.get(t, {})
            return {"aggregations": {"categories": {"buckets": _buckets(cats)}}}
        if "types" in aggs:
            self.bodies.append(body)
            types = [{"key": t} for t in self.by_type]
            return {"aggregations": {"types": {"buckets": types}}}
        return super().search(index, body)


_MENU = {
    "weapon": {"Katana": 12, "Dagger": 20},
    "armor": {"Head": 300},
}


def test_menu_categories_by_type_without_entity_type():
    c = _AggClient(_MENU)
    out = _client.list_menu_categories(c, source="EquipParamWeapon")
    assert out == {"weapon": {"Dagger": 20, "Katana": 12}, "armor": {"Head": 300}}
    assert list(out["weapon"]) == ["Dagger", "Katana"]  # sorted by category
    assert c.bodies[0]["query"]["bool"]["filter"] == [
        {"exists": {"field": "menu_category"}},
        {"term": {"source": "EquipParamWeapon"}},
    ]


def test_menu_categories_for_one_type():
    out = _client.list_menu_categories(_AggClient(_MENU), "weapon")
    assert list(out.items()) == [("Dagger", 20), ("Katana", 12)]


def test_menu_categories_empty_checks_filter_values():
    c = _AggClient({})
    assert _client.list_menu_categories(c, "weapon") == {}
    out = _client.list_menu_categories(c, "wepon")
    assert "error" in out and "wepon" in out["error"]
    assert _client.list_menu_categories(c) == {}


def test_list_entity_types():
    assert _client.list_entity_types(_AggClient(_MENU)) == ["weapon", "armor"]


# ---------------------------------------------------------------------------
# start_instance
# ---------------------------------------------------------------------------


class _Clock:
    """time stand-in: sleep advances monotonic instead of waiting."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _EC2:
    def __init__(self, states):
        self.states, self.started = list(states), []

    def start_instances(self, InstanceIds):  # noqa: N803 — boto3 kwarg
        self.started.append(InstanceIds)

    def describe_instances(self, InstanceIds):  # noqa: N803 — boto3 kwarg
        state, dns = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        inst = {"State": {"Name": state}, "PublicDnsName": dns}
        return {"Reservations": [{"Instances": [inst]}]}


class _Health:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    @property
    def cluster(self):
        return self

    def health(self, request_timeout):
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return {"status": answer}


@pytest.fixture
def instance(monkeypatch):
    monkeypatch.setenv("EC2_INSTANCE_ID", "i-123")
    monkeypatch.setattr(_client, "time", _Clock())
    _client._cached_endpoint = "stale-host"
    _client._cached_client = object()
    yield monkeypatch
    _client.reset_cache()


def test_start_instance_waits_for_running_then_health(instance):
    ec2 = _EC2([("pending", ""), ("pending", "host-new"), ("running", "host-new")])
    health = _Health([SearchConnectionError("N/A", "refused", None), "red", "yellow"])
    instance.setattr(_client, "_ec2", lambda: ec2)
    instance.setattr(_client, "get_client", lambda: health)
    assert _client.start_instance() == "host-new"
    assert ec2.started == [["i-123"]]
    assert health.calls == 3
    # The stale client is dropped and the new hostname cached.
    assert _client._cached_endpoint == "host-new"
    assert _client._cached_client is None


def test_start_instance_times_out_waiting_for_running(instance):
    instance.setattr(_client, "_ec2", lambda: _EC2([("pending", "")]))
    with pytest.raises(TimeoutError, match="running state"):
        _client.start_instance(timeout_seconds=20)
    assert _client._cached_endpoint == "stale-host"


def test_start_instance_times_out_waiting_for_health(instance):
    instance.setattr(_client, "_ec2", lambda: _EC2([("running", "host-new")]))
    instance.setattr(_client, "get_client", lambda: _Health(["red"]))
    with pytest.raises(TimeoutError, match="healthy"):
        _client.start_instance(timeout_seconds=30)
