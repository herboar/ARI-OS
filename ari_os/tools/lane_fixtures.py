"""The frozen Lane Board fixture — one shared source for tests and the UI lane.

`docs/lane-board-fixture.json` is real captured state, committed so that the
renderer can be built and tested without touching a live machine. It contains
every hard case on purpose: a `conflict: true` actor, an `unknown` liveness, a
RESCUE lane, two overlapping lanes, and a PARKED lane.

Usage:

    from ari_os.tools.lane_fixtures import load_fixture
    snapshot = load_fixture()
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FIXTURE_NAME = "lane-board-fixture.json"

# ari_os/tools/lane_fixtures.py -> repo root -> docs/
_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "docs" / FIXTURE_NAME,
    Path(__file__).resolve().parents[1] / "docs" / FIXTURE_NAME,
    Path.cwd() / "docs" / FIXTURE_NAME,
]


def fixture_path() -> Path:
    """Locate the committed fixture. $ARI_OS_LANE_FIXTURE overrides."""
    override = os.environ.get("ARI_OS_LANE_FIXTURE")
    if override:
        return Path(override)
    for candidate in _CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "lane-board fixture not found; looked in %s (set $ARI_OS_LANE_FIXTURE to override)"
        % ", ".join(str(c) for c in _CANDIDATES))


def load_fixture() -> dict:
    """Return the committed snapshot as a plain dict (a fresh copy each call)."""
    return json.loads(fixture_path().read_text())


if __name__ == "__main__":
    snap = load_fixture()
    print("%s  schema %s  %d repos  %d lanes" % (
        fixture_path(), snap.get("schema"), len(snap.get("repos", [])),
        sum(len(r.get("lanes", [])) for r in snap.get("repos", []))))
