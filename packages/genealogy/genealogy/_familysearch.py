"""FamilySearch API client and GedcomX -> Person mapping.

Auth: FamilySearch requires a registered app key (``FAMILYSEARCH_CLIENT_ID``)
and a user-authorized OAuth2 access token. The token is obtained locally via
``genealogy-fs-login`` (authorization-code + PKCE) and stored either in an SSM
SecureString (``FAMILYSEARCH_TOKEN_SSM_PATH``, used in Lambda) or a local JSON
file (``FAMILYSEARCH_TOKEN_FILE``). ``FAMILYSEARCH_ACCESS_TOKEN`` overrides both.

Endpoints used (Accept ``application/x-fs-v1+json`` unless noted):
- GET /platform/users/current
- GET /platform/tree/search  (Accept ``application/x-gedcomx-atom+json``)
- GET /platform/tree/persons/{pid}
- GET /platform/tree/ancestry?person=&generations=&personDetails=
"""

import calendar
import json
import os
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import httpx

from genealogy._models import EVENT_TAGS, Citation, Event, Person, Tree

FS_JSON = "application/x-fs-v1+json"
FS_ATOM = "application/x-gedcomx-atom+json"
SYSTEM = "familysearch"


@dataclass(frozen=True)
class FsEnvironment:
    api: str
    ident: str

    @property
    def authorize_url(self) -> str:
        return f"{self.ident}/cis-web/oauth2/v3/authorization"

    @property
    def token_url(self) -> str:
        return f"{self.ident}/cis-web/oauth2/v3/token"


ENVIRONMENTS = {
    "production": FsEnvironment(
        "https://api.familysearch.org", "https://ident.familysearch.org"
    ),
    "beta": FsEnvironment(
        "https://apibeta.familysearch.org", "https://identbeta.familysearch.org"
    ),
    "integration": FsEnvironment(
        "https://api-integ.familysearch.org", "https://identint.familysearch.org"
    ),
}


def environment() -> FsEnvironment:
    name = os.environ.get("FAMILYSEARCH_ENV", "production")
    if name not in ENVIRONMENTS:
        raise ValueError(f"FAMILYSEARCH_ENV must be one of {sorted(ENVIRONMENTS)}")
    return ENVIRONMENTS[name]


class NotAuthenticated(RuntimeError):
    pass


def token_request(http: httpx.Client, env: FsEnvironment, data: dict) -> httpx.Response:
    """POST to the OAuth token endpoint (code exchange or refresh)."""
    return http.post(env.token_url, data=data, headers={"Accept": "application/json"})


def person_url(pid: str | None) -> str:
    return f"https://www.familysearch.org/tree/person/details/{pid}"


# -- token storage -------------------------------------------------------------


@cache
def _ssm():
    import boto3

    return boto3.client("ssm")


class TokenStore:
    """Reads/writes the token JSON blob from env, SSM, or a local file."""

    def load(self) -> dict[str, Any] | None:
        if tok := os.environ.get("FAMILYSEARCH_ACCESS_TOKEN"):
            return {"access_token": tok}
        if path := os.environ.get("FAMILYSEARCH_TOKEN_SSM_PATH"):
            try:
                param = _ssm().get_parameter(Name=path, WithDecryption=True)
            except _ssm().exceptions.ParameterNotFound:
                return None
            return json.loads(param["Parameter"]["Value"])
        file = self._file()
        return json.loads(file.read_text()) if file.exists() else None

    def save(self, token: dict[str, Any]) -> str:
        token = {**token, "obtained_at": int(time.time())}
        if path := os.environ.get("FAMILYSEARCH_TOKEN_SSM_PATH"):
            _ssm().put_parameter(
                Name=path, Value=json.dumps(token), Type="SecureString", Overwrite=True
            )
            return f"ssm:{path}"
        file = self._file()
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(token))
        file.chmod(0o600)
        return str(file)

    @staticmethod
    def _file() -> Path:
        default = Path.home() / ".genealogy-mcp" / "familysearch-token.json"
        return Path(os.environ.get("FAMILYSEARCH_TOKEN_FILE", default))


# -- client ----------------------------------------------------------------------


class FamilySearchClient:
    """Long-lived client: keeps one connection pool and caches the token, so a
    warm Lambda container doesn't re-handshake or re-read SSM per tool call."""

    def __init__(
        self,
        env: FsEnvironment | None = None,
        tokens: TokenStore | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.env = env or environment()
        self.tokens = tokens or TokenStore()
        # follow_redirects: a merged FamilySearch person answers 301 to the
        # surviving person. Timeout stays under API Gateway's 30 s limit.
        self._http = httpx.Client(
            base_url=self.env.api,
            timeout=20,
            follow_redirects=True,
            transport=transport,
        )
        self._token: dict[str, Any] | None = None

    def _access_token(self) -> str:
        if self._token is None:
            self._token = self.tokens.load()
        if not self._token or not self._token.get("access_token"):
            raise NotAuthenticated(
                "No FamilySearch token. Run `uv run --package genealogy genealogy-fs-login` "
                "locally to authorize."
            )
        return self._token["access_token"]

    def _recover(self) -> bool:
        """After a 401: pick up a token another process saved, else refresh ours."""
        stale = (self._token or {}).get("access_token")
        stored = self.tokens.load()
        if stored and stored.get("access_token") != stale:
            self._token = stored
            return True
        refresh = (self._token or {}).get("refresh_token")
        client_id = os.environ.get("FAMILYSEARCH_CLIENT_ID")
        if not refresh or not client_id:
            return False
        resp = token_request(
            self._http,
            self.env,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
        )
        if resp.status_code != 200:
            return False
        self._token = {**(self._token or {}), **resp.json()}
        self.tokens.save(self._token)
        return True

    def get(
        self, path: str, params: dict | None = None, accept: str = FS_JSON
    ) -> dict | None:
        def _send() -> httpx.Response:
            return self._http.get(
                path,
                params=params,
                headers={
                    "Accept": accept,
                    "Authorization": f"Bearer {self._access_token()}",
                },
            )

        resp = _send()
        if resp.status_code == 401 and self._recover():
            resp = _send()
        if resp.status_code == 401:
            raise NotAuthenticated(
                "FamilySearch token expired or revoked. Re-run `genealogy-fs-login`."
            )
        if resp.status_code in (204, 404):
            return None
        resp.raise_for_status()
        return resp.json() if resp.content else None

    # -- endpoints ---------------------------------------------------------------

    def current_user(self) -> dict:
        data = self.get("/platform/users/current") or {}
        return (data.get("users") or [{}])[0]

    def search(self, count: int = 20, **criteria: str | int | None) -> dict:
        """Tree person search. Criteria keys are FamilySearch q-terms, e.g.
        givenName, surname, birthLikeDate, birthLikePlace, deathLikeDate,
        fatherSurname, motherGivenName, spouseSurname, sex."""
        terms = [f'{k}:"{v}"' for k, v in criteria.items() if v not in (None, "")]
        if not terms:
            raise ValueError("Provide at least one search criterion")
        data = self.get(
            "/platform/tree/search",
            params={"q": " ".join(terms), "count": min(count, 100)},
            accept=FS_ATOM,
        )
        return data or {"entries": [], "results": 0}

    def person(self, pid: str) -> dict | None:
        return self.get(f"/platform/tree/persons/{pid}")

    def ancestry(self, pid: str, generations: int = 4) -> dict | None:
        return self.get(
            "/platform/tree/ancestry",
            params={
                "person": pid,
                "generations": max(1, min(generations, 8)),
                "personDetails": "",
            },
        )


# -- GedcomX mapping ---------------------------------------------------------------

_GX = "http://gedcomx.org/"
_FACT_TO_TAG = {fact: tag for tag, fact in EVENT_TAGS.items()}


def _simple_date(formal: str) -> str | None:
    """'+1850-03-12' -> '12 MAR 1850'. Returns None if not a plain date."""
    parts = formal.lstrip("+").split("-")
    if not parts[0].isdigit():
        return None
    year = str(int(parts[0]))
    if len(parts) == 1:
        return year
    month = calendar.month_abbr[int(parts[1])].upper()
    if len(parts) == 2:
        return f"{month} {year}"
    return f"{int(parts[2])} {month} {year}"


def formal_to_gedcom(formal: str | None) -> str | None:
    """Convert a GedcomX formal date to a GEDCOM date phrase (best effort)."""
    if not formal:
        return None
    approx = formal.startswith("A")
    body = formal[1:] if approx else formal
    if "/" in body:
        start, _, end = body.partition("/")
        s, e = _simple_date(start), _simple_date(end)
        if s and e:
            return f"BET {s} AND {e}"
        if s:
            return f"AFT {s}"
        return f"BEF {e}" if e else None
    out = _simple_date(body)
    return f"ABT {out}" if out and approx else out


def _fact_to_event(fact: dict) -> Event | None:
    tag = _FACT_TO_TAG.get(fact.get("type", "").removeprefix(_GX))
    if tag is None:
        return None
    date = fact.get("date") or {}
    place = fact.get("place") or {}
    return Event(
        type=tag,
        date=formal_to_gedcom((date.get("formal") or "").strip())
        or date.get("original"),
        place=place.get("original")
        or (place.get("normalized") or [{}])[0].get("value"),
        value=fact.get("value"),
    )


def _preferred_name(gx_person: dict) -> tuple[str | None, str | None]:
    names = gx_person.get("names") or []
    name = next((n for n in names if n.get("preferred")), names[0] if names else None)
    form = ((name or {}).get("nameForms") or [{}])[0]
    parts = {
        p.get("type", "").removeprefix(_GX): p.get("value")
        for p in form.get("parts", [])
    }
    if parts.get("Given") or parts.get("Surname"):
        return parts.get("Given"), parts.get("Surname")
    full = form.get("fullText") or (gx_person.get("display") or {}).get("name")
    if not full:
        return None, None
    given, _, surname = full.rpartition(" ")
    return (given or None), (surname or None)


def ahnentafel(gx_person: dict) -> int | None:
    """The pedigree position from an ancestry read (1 = root, 2n/2n+1 = parents of n)."""
    number = str((gx_person.get("display") or {}).get("ascendancyNumber", ""))
    return int(number) if number.isdigit() else None


def person_summary(gx_person: dict) -> dict:
    """Compact view of a GedcomX person for tool output."""
    d = gx_person.get("display") or {}
    return {
        "pid": gx_person.get("id"),
        "name": d.get("name"),
        "gender": d.get("gender"),
        "lifespan": d.get("lifespan"),
        "birth": ", ".join(x for x in (d.get("birthDate"), d.get("birthPlace")) if x)
        or None,
        "death": ", ".join(x for x in (d.get("deathDate"), d.get("deathPlace")) if x)
        or None,
        "living": gx_person.get("living"),
        "url": person_url(gx_person.get("id")),
    }


def search_results(data: dict) -> list[dict]:
    """Summaries (with match score) from a /platform/tree/search atom feed."""
    results = []
    for entry in data.get("entries", []):
        persons = ((entry.get("content") or {}).get("gedcomx") or {}).get("persons", [])
        main = next((p for p in persons if p.get("id") == entry.get("id")), None)
        if main:
            results.append({**person_summary(main), "score": entry.get("score")})
    return results


def person_detail(doc: dict, pid: str) -> dict | None:
    """Main person (with facts) plus parents/spouses/children from a person read."""
    persons = {p["id"]: p for p in doc.get("persons", []) if "id" in p}
    main = persons.get(pid)
    if main is None:
        return None
    rel: dict[str, list[str]] = {"parents": [], "spouses": [], "children": []}
    for r in doc.get("relationships", []):
        if r.get("type", "").removeprefix(_GX) != "Couple":
            continue
        ids = [(r.get(k) or {}).get("resourceId") for k in ("person1", "person2")]
        if pid in ids:
            rel["spouses"] += [i for i in ids if i and i != pid]
    for r in doc.get("childAndParentsRelationships", []):
        child = (r.get("child") or {}).get("resourceId")
        parents = [(r.get(k) or {}).get("resourceId") for k in ("parent1", "parent2")]
        if child == pid:
            rel["parents"] += [p for p in parents if p]
        elif pid in parents and child:
            rel["children"].append(child)

    def _summaries(ids: list[str]) -> list[dict]:
        return [person_summary(persons.get(i, {"id": i})) for i in dict.fromkeys(ids)]

    facts = [
        e.model_dump(exclude_none=True, exclude={"citations"})
        for f in main.get("facts", [])
        if (e := _fact_to_event(f))
    ]
    return {
        **person_summary(main),
        "facts": facts,
        **{k: _summaries(v) for k, v in rel.items()},
    }


def gx_to_person(gx_person: dict) -> Person:
    """Map a GedcomX person to a Person. The id is a placeholder until merged."""
    given, surname = _preferred_name(gx_person)
    pid = gx_person["id"]
    return Person(
        id=f"{SYSTEM}:{pid}",
        given=given,
        surname=surname,
        sex=(gx_person.get("gender") or {}).get("type", "").removeprefix(_GX),
        living=bool(gx_person.get("living")),
        events=[e for f in gx_person.get("facts", []) if (e := _fact_to_event(f))],
        external_ids={SYSTEM: pid},
        citations=[
            Citation(
                title=f"FamilySearch Family Tree person {pid}",
                url=person_url(pid),
                origin=SYSTEM,
            )
        ],
    )


def import_ancestry(tree: Tree, ancestry: dict, overwrite: bool = False) -> dict:
    """Merge a /platform/tree/ancestry response into the tree.

    Each person carries an Ahnentafel number (see ``ahnentafel``), which gives
    the parent links without needing the relationship resources.
    """
    gx_people = ancestry.get("persons", [])
    merged, created = tree.merge_people(
        [gx_to_person(gx) for gx in gx_people], SYSTEM, overwrite=overwrite
    )
    by_number = {
        n: person
        for gx, person in zip(gx_people, merged)
        if (n := ahnentafel(gx)) is not None
    }
    links = 0
    for n, child in by_number.items():
        father, mother = by_number.get(2 * n), by_number.get(2 * n + 1)
        if father or mother:
            tree.add_child(
                child.id, father.id if father else None, mother.id if mother else None
            )
            links += 1
    return {"created": created, "updated": len(merged) - created, "parent_links": links}
