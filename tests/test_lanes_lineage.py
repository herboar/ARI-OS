"""Lane Board schema-2: lineage, night pending, nested render smoke."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs" / "lane-board-fixture.json"


def test_fixture_is_schema_2_with_lineage():
    data = json.loads(FIXTURE.read_text())
    assert data["schema"] == 2
    assert "night" in data
    assert data["night"]["pending_session_digests"] >= 0

    xf = next(r for r in data["repos"] if r["key"] == "xfactor")
    by_name = {l["name"]: l for l in xf["lanes"]}
    assert "main" in by_name
    assert by_name["main"]["depth"] == 0
    assert by_name["main"]["role"] == "main"

    # Ari shape: dp2 is a variant under broll, not a peer of main
    dp2 = by_name["dp2-2b2"]
    assert dp2["role"] == "variant"
    assert dp2["parent_id"] == by_name["broll-unit-01"]["id"]
    assert dp2["depth"] == 2

    dead = by_name["covers-bigtype"]
    assert dead["route_status"] == "dead_route"
    assert dead["integration"]["target_branch"] == by_name["reel-covers"]["branch"]


def test_render_nested_rail_and_night_banner():
    from ari_os.tools.lane_render import render_board
    from ari_os.tools.lane_fixtures import load_fixture

    snap = load_fixture()
    html = render_board(snap, view="rail", standalone=True)
    assert "session digest" in html
    assert "data-depth=" in html
    assert "DEAD" in html or "dead_route" in html
    assert "covers-bigtype" in html
    assert "lb-parent" in html or "←" in html


def test_render_dense_tree_default_sort():
    from ari_os.tools.lane_render import render_board
    from ari_os.tools.lane_fixtures import load_fixture

    snap = load_fixture()
    html = render_board(snap, view="dense", standalone=True)
    assert 'data-sort="tree"' in html
    assert "data-tree=" in html


def test_cli_render_text_shows_parent():
    from ari_os.tools.lanes import render_text
    from ari_os.tools.lane_fixtures import load_fixture

    snap = load_fixture()
    text = render_text(snap, color=False)
    assert "LANE BOARD" in text
    assert "digest" in text.lower()
    assert "←" in text or "broll" in text.lower()


def test_dead_route_commands_skip_main_merge():
    from ari_os.tools.lane_render import _commands

    dead = {
        "verdict": "MERGE",
        "route_status": "dead_route",
        "branch": "agent/covers-bigtype",
        "path": "/tmp/x",
        "kind": "worktree",
        "integration": {"target_branch": "agent/reel-covers"},
        "git": {"modified": 0, "staged": 0, "untracked": 0},
    }
    repo = {"path": "/tmp/repo", "base": "main"}
    html = _commands(dead, repo)
    assert "Dead route" in html
    assert "merge --no-ff" not in html


def test_variant_merge_targets_parent():
    from ari_os.tools.lane_render import _commands

    variant = {
        "verdict": "MERGE",
        "route_status": "open",
        "role": "variant",
        "branch": "agent/covers-dark",
        "path": "/tmp/x",
        "kind": "worktree",
        "integration": {"target_branch": "agent/reel-covers"},
        "git": {"modified": 0, "staged": 0, "untracked": 0},
    }
    repo = {"path": "/tmp/repo", "base": "main"}
    html = _commands(variant, repo)
    assert "agent/reel-covers" in html
    assert "checkout agent/reel-covers" in html


def test_rail_bakeoff_styles_all_emitted():
    from ari_os.tools.lane_render import render_board
    from ari_os.tools.lane_fixtures import load_fixture

    html = render_board(load_fixture(), view="rail", standalone=True, rail="elbow")
    assert 'data-rail="elbow"' in html
    assert 'data-rail-style="nested"' in html
    assert 'data-rail-style="elbow"' in html
    assert 'data-rail-style="graph"' in html
    assert "lb-minigraph" in html
    assert 'data-lb-set="rail:graph"' in html
