"""Pause or restore index refresh around a bulk reindex (#406).

``pause`` sets ``refresh_interval: -1`` and async translog durability on the
concrete index behind ``ELDEN_RING_INDEX`` (default alias ``elden-ring-entities``).
``restore`` resets both to the defaults and forces a refresh. er_native/reindex_all.sh
runs ``restore`` at startup (a killed run can't leave refresh off), then ``pause``,
then ``restore`` again from its EXIT trap.

Knobs: ER_LOAD_PAUSE_REFRESH=0 skips the refresh pause, ER_LOAD_ASYNC_TRANSLOG=0
keeps per-request translog fsync. ``restore`` always resets both.

    OPENSEARCH_ENDPOINT=... OPENSEARCH_PASSWORD=... \
        python index_settings.py {pause,restore}
"""

from __future__ import annotations

import argparse

from load_data import _get_client, pause_refresh, restore_refresh


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pause or restore index refresh around a bulk reindex."
    )
    ap.add_argument("action", choices=("pause", "restore"))
    args = ap.parse_args()
    client = _get_client()
    if args.action == "pause":
        pause_refresh(client)
    else:
        restore_refresh(client)


if __name__ == "__main__":
    main()
