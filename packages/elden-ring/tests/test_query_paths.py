"""Offline coverage for query paths only the live suite used to reach (#455): the
literal patterns union, calculate_attack_rating's mid-name affinity fallback,
list_menu_categories / list_entity_types, and start_instance. diff_entities and
text_changed_between are covered in test_version_diff_tools.py."""

import asyncio
import json
import os

import boto3
import pytest
from botocore.stub import Stubber
from opensearchpy.exceptions import ConnectionError as SearchConnectionError
from opensearchpy.exceptions import ConnectionTimeout, TransportError
from test_filter_validation import _Client, _doc

from elden_ring import _client, mcp_server


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
        _QueryClient(_SCAVENGER), weapon, {}, affinity=affinity
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
        *_client._availability_filter(False),
        *_client._variant_filter(True),
    ]


def test_menu_categories_for_one_type():
    c = _AggClient(_MENU)
    out = _client.list_menu_categories(c, "weapon")
    assert list(out.items()) == [("Dagger", 20), ("Katana", 12)]
    # Same default filters as search(collapse_variants=True) (#465).
    assert c.bodies[0]["query"]["bool"]["filter"] == [
        {"term": {"entity_type": "weapon"}},
        *_client._availability_filter(False),
        *_client._variant_filter(True),
    ]


def test_menu_categories_can_count_unavailable_and_variants():
    c = _AggClient(_MENU)
    _client.list_menu_categories(
        c, "weapon", include_unavailable=True, collapse_variants=False
    )
    _client.list_menu_categories(c, include_unavailable=True, collapse_variants=False)
    assert c.bodies[0]["query"]["bool"]["filter"] == [
        {"term": {"entity_type": "weapon"}}
    ]
    assert c.bodies[1]["query"]["bool"]["filter"] == [
        {"exists": {"field": "menu_category"}}
    ]


def test_menu_categories_empty_checks_filter_values():
    c = _AggClient({})
    assert _client.list_menu_categories(c, "weapon") == {}
    out = _client.list_menu_categories(c, "wepon")
    assert "error" in out and "wepon" in out["error"]
    assert _client.list_menu_categories(c) == {}


def test_list_entity_types():
    assert _client.list_entity_types(_AggClient(_MENU)) == ["weapon", "armor"]


# ---------------------------------------------------------------------------
# start_instance (#440: non-blocking, at most ~20 s per call)
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
    """Fake EC2: each describe_instances pops the next (state, dns), keeping the last."""

    def __init__(self, states):
        self.states, self.started = list(states), []

    def start_instances(self, InstanceIds):
        self.started.append(InstanceIds)

    def describe_instances(self, InstanceIds):
        state, dns = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        inst = {"State": {"Name": state}, "PublicDnsName": dns}
        return {"Reservations": [{"Instances": [inst]}]}


class _Health:
    def __init__(self, answers):
        self.answers, self.timeouts = list(answers), []

    @property
    def cluster(self):
        return self

    def health(self, params):
        self.timeouts.append(params["request_timeout"])
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return {"status": answer}


@pytest.fixture
def instance(monkeypatch):
    monkeypatch.setenv("EC2_INSTANCE_ID", "i-123")
    clock = _Clock()
    monkeypatch.setattr(_client, "time", clock)
    _client._cache.endpoint = "stale-host"
    _client._cache.client = object()
    yield monkeypatch, clock
    _client.reset_cache()


def _wire(monkeypatch, ec2, health=None):
    monkeypatch.setattr(_client, "_ec2", lambda: ec2)
    if health is not None:
        monkeypatch.setattr(_client, "get_client", lambda: health)


def test_start_instance_boots_then_waits_out_read_timeouts(instance):
    monkeypatch, _ = instance
    ec2 = _EC2([("stopped", ""), ("pending", ""), ("running", "host-new")])
    health = _Health(
        [
            SearchConnectionError("N/A", "refused", None),
            ConnectionTimeout("TIMEOUT", "Read timed out", None),
            "red",
            "yellow",
        ]
    )
    _wire(monkeypatch, ec2, health)
    assert _client.start_instance(budget_seconds=60) == {"status": "ready"}
    assert ec2.started == [["i-123"]]
    assert len(health.timeouts) == 4
    assert max(health.timeouts) <= 5
    # The stale client is dropped and the new hostname cached.
    assert _client._cache.endpoint == "host-new"


def test_start_instance_returns_starting_within_budget(instance):
    monkeypatch, clock = instance
    ec2 = _EC2([("stopped", ""), ("pending", "")])
    _wire(monkeypatch, ec2)
    out = _client.start_instance()
    assert out["status"] == "starting" and out["state"] == "pending"
    assert "~30 s" in out["message"]
    assert ec2.started == [["i-123"]]
    assert clock.now <= 20
    # The stop invalidated the old hostname (#456).
    assert _client._cache.endpoint is None and _client._cache.client is None


def test_start_instance_returns_starting_while_opensearch_initialises(instance):
    monkeypatch, clock = instance
    health = _Health(
        [
            ConnectionTimeout("TIMEOUT", "Read timed out", None),
            TransportError(503, "unavailable", None),  # security index loading
        ]
    )
    _wire(monkeypatch, _EC2([("running", "host-new")]), health)
    out = _client.start_instance()
    assert out == {
        "status": "starting",
        "state": "running",
        "message": "OpenSearch is still initialising; "
        "call start_search_service() again in ~30 s",
    }
    assert clock.now <= 20
    assert all(t <= 5 for t in health.timeouts)


def test_start_instance_already_running_keeps_cached_client(instance):
    monkeypatch, clock = instance
    ec2 = _EC2([("running", "stale-host")])
    health = _Health(["green"])
    _wire(monkeypatch, ec2, health)
    cached = _client._cache.client
    assert _client.start_instance() == {"status": "ready"}
    assert ec2.started == []
    assert _client._cache.client is cached
    assert clock.now == 0


def test_start_instance_waits_out_stopping(instance):
    monkeypatch, _ = instance
    ec2 = _EC2([("stopping", ""), ("stopping", ""), ("stopped", ""), ("running", "h")])
    _wire(monkeypatch, ec2, _Health(["green"]))
    assert _client.start_instance() == {"status": "ready"}
    assert ec2.started == [["i-123"]]


def test_start_instance_still_stopping_does_not_start(instance):
    monkeypatch, clock = instance
    ec2 = _EC2([("stopping", "")])
    _wire(monkeypatch, ec2)
    out = _client.start_instance()
    assert out["status"] == "starting" and out["state"] == "stopping"
    assert ec2.started == []
    assert clock.now <= 20


def test_start_instance_terminated_is_an_error(instance):
    monkeypatch, _ = instance
    ec2 = _EC2([("terminated", "")])
    _wire(monkeypatch, ec2)
    assert _client.start_instance()["status"] == "error"
    assert ec2.started == []


def test_start_search_service_wraps_client_error(instance):
    """A real boto3 client under Stubber: StartInstances raising
    IncorrectInstanceState comes back as {"status": "error"}, not an exception."""
    monkeypatch, _ = instance
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)  # no profile lookups
    ec2 = boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106
    )
    stopped = {"Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]}
    with Stubber(ec2) as stub:
        stub.add_response("describe_instances", stopped, {"InstanceIds": ["i-123"]})
        stub.add_client_error(
            "start_instances",
            service_error_code="IncorrectInstanceState",
            service_message="The instance is not in a state from which it can be started.",
        )
        _wire(monkeypatch, ec2)
        out = asyncio.run(mcp_server.mcp.call_tool("start_search_service", {}))
        stub.assert_no_pending_responses()
    blocks = out[0] if isinstance(out, tuple) else out
    result = json.loads(blocks[0].text)
    assert result["status"] == "error"
    assert "IncorrectInstanceState" in result["message"]


def test_start_search_service_annotations():
    tool = mcp_server.mcp._tool_manager.get_tool("start_search_service")
    assert tool is not None and tool.annotations is not None
    ann = tool.annotations
    assert (ann.readOnlyHint, ann.destructiveHint, ann.idempotentHint) == (
        False,
        False,
        True,
    )
