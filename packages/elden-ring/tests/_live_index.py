"""Opt-in gate and throwaway index for the live OpenSearch tests (#445).

Live tests carry the ``live`` marker, which ``addopts`` deselects by default, and
also skip unless ``ER_LIVE_TESTS=1`` and the stack env vars are set. They never
write to the production alias: :func:`throwaway_index` creates a fresh
``elden-ring-it-<uuid>`` index from ``INDEX_MAPPING``, points the modules' ``INDEX``
at it, and deletes it at teardown.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest

OPT_IN_ENV = "ER_LIVE_TESTS"
STACK_ENV = ("EC2_INSTANCE_ID", "OPENSEARCH_USER", "OPENSEARCH_PASSWORD_SSM_PATH")
LIVE_ALIAS = "elden-ring-entities"
THROWAWAY_PREFIX = "elden-ring-it-"


def live_enabled() -> bool:
    """True only with the explicit opt-in and every stack env var set."""
    return os.environ.get(OPT_IN_ENV) == "1" and all(
        os.environ.get(v) for v in STACK_ENV
    )


SKIP_REASON = (
    f"live suite: set {OPT_IN_ENV}=1 and {', '.join(STACK_ENV)}, run with -m live"
)


def throwaway_index_name() -> str:
    return f"{THROWAWAY_PREFIX}{uuid.uuid4().hex[:12]}"


def check_throwaway(client, index: str) -> None:
    """Raise unless ``index`` is a fresh throwaway name, never the live alias or
    one of its concrete indices."""
    if not index.startswith(THROWAWAY_PREFIX):
        raise RuntimeError(f"refusing to use non-throwaway index {index!r}")
    if index.startswith(LIVE_ALIAS):
        raise RuntimeError(f"refusing to use the live index {index!r}")
    if client.indices.exists_alias(name=index):
        raise RuntimeError(f"refusing to use alias {index!r}")
    if client.indices.exists(index=index):
        raise RuntimeError(f"throwaway index {index!r} already exists")


@contextmanager
def throwaway_index(client, mapping: dict, modules):
    """Create a throwaway index, set ``INDEX`` on each module to it, yield its name,
    then restore ``INDEX`` and delete the index (also when the body raises)."""
    index = throwaway_index_name()
    check_throwaway(client, index)
    client.indices.create(index=index, body=mapping)
    patch = pytest.MonkeyPatch()
    try:
        for module in modules:
            patch.setattr(module, "INDEX", index)
        yield index
    finally:
        patch.undo()
        client.indices.delete(index=index, ignore=[404])
