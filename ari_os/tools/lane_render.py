"""Lane Board renderer — turns a lane snapshot into HTML.

Two switchable views over the same snapshot:

* **Rail** — three bake-off styles (toggle `rail`): **nested** (depth columns),
  **elbow** (single main spine + parent badge), **graph** (mini DAG above list).
  Dead routes are dashed and never loop back to main.
* **Dense** — tree-indented rows with parent chips; divergence as a bar.

Views and rail styles are emitted/selectable via `data-view` / `data-rail` —
CSS + localStorage flip, no refetch.

Presentation rules that are not negotiable (see docs/lane-board-contract.md):
every liveness dot renders with its evidence string, `conflict: true` renders
as a visible contradiction we never resolve, `unknown` never folds into idle,
and a `SWEEP` verdict never emits a runnable removal command.
"""
from __future__ import annotations

import html as _html
import json
from datetime import datetime, timezone
from pathlib import Path

from .lane_assets import BOARD_CSS, BOARD_JS

# Verdict → CSS custom property, and the sort rank used by the "verdict" sort.
_VERDICT_COLOR = {
    "RESCUE": "var(--red)", "DIRTY": "var(--dirty)", "MERGE": "var(--amber)",
    "PARKED": "var(--blue)", "SWEEP": "var(--text2)", "CLEAN": "var(--text3)",
}
_VERDICT_RANK = {"RESCUE": 0, "DIRTY": 1, "MERGE": 2, "PARKED": 3, "SWEEP": 4, "CLEAN": 5}
_STALE_AFTER_S = 15


# ---------------------------------------------------------------- snapshot --

def _fixture_from_docs() -> dict:
    """Last-resort fallback: the frozen fixture committed under docs/."""
    p = Path(__file__).resolve().parents[2] / "docs" / "lane-board-fixture.json"
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


def _snapshot() -> dict:
    """Live snapshot if `lanes.build_snapshot` exists, else captured sample data.

    The import is lazy and defensive on purpose: `lanes.py` is owned by another
    lane and may not exist yet. Sample data is always flagged `_fixture` so the
    board can say so out loud — it must never masquerade as live.
    """
    try:
        from ari_os.tools.lanes import build_snapshot  # type: ignore
        return build_snapshot()
    except Exception as exc:  # noqa: BLE001 — the board must always render
        note = "live snapshot unavailable: %s" % exc
        try:
            from ari_os.tools.lane_fixtures import load_fixture  # type: ignore
            snap = load_fixture()
        except Exception:  # noqa: BLE001
            try:
                snap = _fixture_from_docs()
            except Exception as exc2:  # noqa: BLE001
                snap = {"schema": 1, "generated_at": None, "slept": False,
                        "repos": [], "errors": [{"scope": "fixture",
                                                 "message": "no fixture available: %s" % exc2}]}
        snap.setdefault("errors", [])
        snap["errors"] = list(snap["errors"]) + [{"scope": "snapshot", "message": note}]
        snap["_fixture"] = True
        return snap


# ------------------------------------------------------------------ helpers --

def _e(value) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def _age(seconds) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 90:
        return "%ds" % int(s)
    if s < 5400:
        return "%dm" % round(s / 60)
    if s < 172800:
        return "%dh" % round(s / 3600)
    return "%dd" % round(s / 86400)


def _bytes(n) -> str:
    if not n:
        return "size not measured"
    step = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return "%.0f %s" % (step, unit) if unit != "B" else "%d B" % int(step)
        step /= 1024
    return str(n)


def _snapshot_age_s(snap: dict):
    raw = snap.get("generated_at")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


def _uncommitted(git: dict) -> int:
    return sum(int(git.get(k) or 0) for k in ("modified", "staged", "untracked"))


def _drift_scale(snap: dict) -> int:
    """Largest ahead/behind in the snapshot — the shared scale for every bar."""
    top = 1
    for repo in snap.get("repos") or []:
        for lane in repo.get("lanes") or []:
            g = lane.get("git") or {}
            top = max(top, int(g.get("ahead") or 0), int(g.get("behind") or 0))
    return top


# -------------------------------------------------------------- components --

def _pill(lane: dict) -> str:
    verdict = lane.get("verdict") or "CLEAN"
    return ('<span class="lb-pill" data-v="%s" title="%s">%s</span>'
            % (_e(verdict), _e(lane.get("verdict_reason") or ""), _e(verdict)))


def _figs(git: dict) -> str:
    """Ahead/behind as a low-attention monospace figure — never the headline."""
    return ('<span class="lb-figs" title="%d ahead of base, %d behind base">'
            '&#8593;%d &#8595;%d</span>'
            % (int(git.get("ahead") or 0), int(git.get("behind") or 0),
               int(git.get("ahead") or 0), int(git.get("behind") or 0)))


def _divergence(git: dict, scale: int) -> str:
    ahead, behind = int(git.get("ahead") or 0), int(git.get("behind") or 0)
    bw = 0 if not behind else max(2, round(min(behind, scale) / scale * 44))
    aw = 0 if not ahead else max(2, round(min(ahead, scale) / scale * 44))
    return ('<div class="lb-drift"><span class="lb-div" aria-hidden="true">'
            '<span class="h l"><i style="width:%dpx"></i></span>'
            '<span class="tick"></span>'
            '<span class="h r"><i style="width:%dpx"></i></span></span>%s</div>'
            % (bw, aw, _figs(git)))


def _files(lane: dict) -> str:
    git = lane.get("git") or {}
    mod, staged, untracked = (int(git.get("modified") or 0), int(git.get("staged") or 0),
                              int(git.get("untracked") or 0))
    if not (mod or staged or untracked):
        return '<span class="lb-files muted">&mdash;</span>'
    bits = []
    if untracked:
        bits.append("%du" % untracked)
    if mod:
        bits.append("%dm" % mod)
    if staged:
        bits.append("%ds" % staged)
    is_main = lane.get("kind") == "main"
    cls = "muted" if is_main else ("hot" if lane.get("verdict") in ("RESCUE", "DIRTY") else "")
    tip = ("main tree: untracked files here are the permanent noise floor, not actionable"
           if is_main else "%d untracked, %d modified, %d staged" % (untracked, mod, staged))
    return '<span class="lb-files %s" title="%s">%s</span>' % (cls, _e(tip), _e(" ".join(bits)))


def _actor(actor: dict, full: bool = False) -> str:
    """A liveness dot never renders without its evidence string beside it."""
    liveness = actor.get("liveness") or "unknown"
    evidence = actor.get("evidence") or "NO EVIDENCE RECORDED"
    conflict = bool(actor.get("conflict"))
    parts = ['<span class="lb-dot" data-l="%s"></span>' % _e(liveness),
             '<span class="lb-alabel">%s</span>' % _e(actor.get("label") or actor.get("id") or "?"),
             '<span class="lb-lv" data-l="%s">%s</span>' % (_e(liveness), _e(liveness))]
    if conflict:
        parts.append('<span class="lb-flag">&#9888; signals disagree</span>')
    if actor.get("read_only"):
        parts.append('<span class="lb-ro">read-only</span>')
    parts.append('<span class="lb-ev">%s</span>' % _e(evidence))
    if full:
        parts.append('<span class="lb-ev">%s &middot; %s &middot; seen %s ago</span>'
                     % (_e(actor.get("kind") or "?"), _e(actor.get("source") or "?"),
                        _e(_age(actor.get("last_seen_age_s")))))
    return ('<span class="lb-actor" data-conflict="%s" title="%s">%s</span>'
            % ("1" if conflict else "0", _e(evidence), "".join(parts)))


def _actors(lane: dict, full: bool = False) -> str:
    actors = lane.get("actors") or []
    if not actors:
        return '<div class="lb-actors"><span class="lb-none">no actor attributed</span></div>'
    return ('<div class="lb-actors">%s</div>'
            % "".join(_actor(a, full) for a in actors))


def _lane_metrics(lane: dict, scale: int):
    """Shared git/lineage metrics for every rail style."""
    git = lane.get("git") or {}
    integ = lane.get("integration") or {}
    verdict = lane.get("verdict") or "CLEAN"
    color = _VERDICT_COLOR.get(verdict, "var(--text3)")
    depth = int(lane.get("depth") or 0)
    route = lane.get("route_status") or "open"
    role = lane.get("role") or "unknown"
    behind = integ.get("behind_parent")
    ahead = integ.get("ahead_of_parent")
    if behind is None:
        behind = int(git.get("behind") or 0)
    if ahead is None:
        ahead = int(git.get("ahead") or 0)
    behind, ahead = int(behind or 0), int(ahead or 0)
    dead = route == "dead_route"
    if dead:
        color = "var(--text3)"
    dash = ' stroke-dasharray="4 4"' if (dead or verdict == "PARKED") else ""
    radius = 3 + min(ahead, 25) / 25.0 * 5
    merged_parent = bool(integ.get("merged_into_parent"))
    merged_base = bool(git.get("merged_into_base"))
    title = "<title>%s</title>" % _e(lane.get("verdict_reason") or verdict)
    return {
        "git": git, "integ": integ, "verdict": verdict, "color": color,
        "depth": depth, "route": route, "role": role, "behind": behind,
        "ahead": ahead, "dead": dead, "dash": dash, "radius": radius,
        "merged_parent": merged_parent, "merged_base": merged_base, "title": title,
        "scale": scale or 1,
    }


def _rail_nested(lane: dict, scale: int) -> str:
    """A · Nested Rail — depth columns; curve origin at parent column."""
    m = _lane_metrics(lane, scale)
    # Deep horizontal steps so depth-2 is obviously not a main-fork
    spine_x = 18
    step = 34
    origin_x = spine_x + max(0, m["depth"] - 1) * step
    node_x = spine_x + m["depth"] * step + 22
    width = max(120, int(node_x + 48))
    if lane.get("kind") == "main":
        return ('<div class="lb-railcell" data-style="nested" data-depth="0">'
                '<svg class="lb-rail" viewBox="0 0 %d 64" width="%d" height="64" role="img">'
                '%s<circle class="node" cx="%d" cy="32" r="5.5" fill="var(--text2)" stroke="var(--bg)"/>'
                '<text x="%d" y="35" font-size="9" fill="var(--text3)">main</text></svg></div>'
                % (width, width, m["title"], spine_x, spine_x + 14))
    reach = min(m["behind"], m["scale"]) / float(m["scale"]) * 28
    apex = node_x + reach
    d = "M%d,6 C%d,18 %.1f,22 %.1f,32" % (origin_x, origin_x, apex - 10, apex)
    if not m["dead"]:
        if m["role"] != "variant" and m["merged_base"]:
            d += " C%.1f,42 %d,46 %d,58" % (apex + 10, spine_x, spine_x)
        elif m["role"] == "variant" and m["merged_parent"]:
            d += " C%.1f,42 %d,46 %d,58" % (apex + 8, origin_x, origin_x)
    ring = ""
    if _uncommitted(m["git"]) and lane.get("kind") == "worktree":
        ring = ('<circle cx="%.1f" cy="32" r="%.1f" fill="none" stroke="var(--red)" '
                'stroke-width="1.25" opacity=".85"/>' % (apex, m["radius"] + 3.5))
    dead_mark = ('<text x="%.1f" y="48" font-size="8" fill="var(--text3)" '
                 'text-anchor="middle">DEAD</text>' % apex) if m["dead"] else ""
    return ('<div class="lb-railcell" data-style="nested" data-depth="%d" data-role="%s" data-route="%s">'
            '<svg class="lb-rail" viewBox="0 0 %d 64" width="%d" height="64" role="img">'
            '%s<circle cx="%d" cy="6" r="2" fill="var(--line)"/>'
            '<path class="branch" d="%s" stroke="%s"%s/>'
            '<circle class="node" cx="%.1f" cy="32" r="%.1f" fill="%s" stroke="var(--bg)"/>%s%s'
            '</svg></div>'
            % (m["depth"], _e(m["role"]), _e(m["route"]), width, width, m["title"],
               origin_x, d, m["color"], m["dash"], apex, m["radius"], m["color"], ring, dead_mark))


def _assign_graph_columns(lanes: list) -> dict:
    """One column per worktree family: MAIN | WT1 | A/B… | WT2 | A/B… |

    Returns layout keyed by lane id:
      {col, parent_col, n_cols, family}  family = 'main'|'worktree'|'ab'
    """
    children: dict = {}
    main = None
    for lane in lanes:
        if lane.get("kind") == "main":
            main = lane
            continue
        pid = lane.get("parent_id") or "__main__"
        children.setdefault(pid, []).append(lane)

    def sort_kids(kids):
        return sorted(kids, key=lambda l: (
            l.get("_tree_ord") if l.get("_tree_ord") is not None else 99,
            l.get("name") or ""))

    layout = {}
    col = 0
    if main:
        layout[main["id"]] = {"col": 0, "parent_col": None, "family": "main"}
        col = 1

    for wt in sort_kids(children.get("__main__", [])):
        wt_col = col
        layout[wt["id"]] = {"col": wt_col, "parent_col": 0, "family": "worktree"}
        col += 1
        # A/B (and deeper) as subcolumns immediately after this worktree
        stack = [(wt["id"], wt_col)]
        while stack:
            pid, pcol = stack.pop(0)
            for ch in sort_kids(children.get(pid, [])):
                layout[ch["id"]] = {
                    "col": col,
                    "parent_col": pcol if pid == wt["id"] else layout[pid]["col"],
                    "family": "ab",
                }
                my = col
                col += 1
                stack.append((ch["id"], my))

    n_cols = max((v["col"] for v in layout.values()), default=0) + 1
    for v in layout.values():
        v["n_cols"] = n_cols
    # orphans
    for lane in lanes:
        if lane["id"] not in layout:
            layout[lane["id"]] = {
                "col": col, "parent_col": 0, "family": "worktree", "n_cols": col + 1
            }
            col += 1
            for v in layout.values():
                v["n_cols"] = col
    return layout


def _rail_elbow(lane: dict, scale: int, layout: dict | None = None) -> str:
    """Classic column git graph: one column per worktree; A/B as subcolumns.

    Horizontal lines only go parent_col ↔ this_col (never cross the board).
    A/B runs vertically in its subcolumn, then MERGE back to parent or DEAD stop.
    """
    m = _lane_metrics(lane, scale)
    layout = layout or {}
    info = layout.get(lane.get("id") or "", {})
    col = int(info.get("col") or 0)
    parent_col = info.get("parent_col")
    n_cols = int(info.get("n_cols") or max(col + 1, 1))
    family = info.get("family") or ("main" if lane.get("kind") == "main" else "worktree")

    col_w = 22
    x0 = 12
    def X(c):
        return x0 + int(c) * col_w

    width = max(96, x0 + n_cols * col_w + 16)
    h = 64
    mid = 32
    color = m["color"]
    dead = m["dead"]
    if dead:
        color = "var(--text3)"

    parts = [
        '<div class="lb-railcell lb-rail-cols" data-style="elbow" data-depth="%d" '
        'data-family="%s" data-col="%d">'
        '<svg class="lb-rail" viewBox="0 0 %d %d" width="%d" height="%d" role="img">'
        % (m["depth"], _e(family), col, width, h, width, h),
        m["title"],
    ]

    # Faint guides for every column
    for c in range(n_cols):
        parts.append(
            '<line x1="%d" y1="0" x2="%d" y2="%d" stroke="var(--line)" '
            'stroke-width="1" opacity=".35"/>' % (X(c), X(c), h)
        )

    # Solid verticals: MAIN always; parent worktree column; this column
    solid = {0, col}
    if parent_col is not None:
        solid.add(int(parent_col))
    for c in sorted(solid):
        if c == 0:
            stroke, sw = "var(--text2)", 2.75
        elif c == col:
            stroke, sw = color, (2.5 if family == "worktree" else 2)
        else:
            stroke, sw = "var(--text3)", 2
        if dead and c == col:
            parts.append(
                '<line x1="%d" y1="0" x2="%d" y2="%d" stroke="%s" stroke-width="%s" '
                'stroke-dasharray="4 3"/>' % (X(c), X(c), mid, color, sw)
            )
        else:
            parts.append(
                '<line x1="%d" y1="0" x2="%d" y2="%d" stroke="%s" stroke-width="%s"/>'
                % (X(c), X(c), h, stroke, sw)
            )

    # MAIN node
    if family == "main" or lane.get("kind") == "main":
        parts.append(
            '<circle class="node" cx="%d" cy="%d" r="6" fill="var(--text2)" '
            'stroke="var(--bg)" stroke-width="1.5"/>' % (X(0), mid)
        )
        parts.append(
            '<text x="%d" y="%d" font-size="8" fill="var(--text3)" text-anchor="middle">'
            'MAIN</text>' % (X(0), mid + 16)
        )
        parts.append("</svg></div>")
        return "".join(parts)

    # Horizontal fork from parent column → this column (only one hop)
    if parent_col is not None:
        px, cx = X(int(parent_col)), X(col)
        y = mid if family == "worktree" else (mid - 10 if not dead else mid - 6)
        # worktree: fork at mid; A/B: slightly higher so merge can sit at mid
        if family == "ab":
            y_fork = 18
            parts.append(
                '<path d="M%d,%d H%d" fill="none" stroke="%s" stroke-width="1.75"%s/>'
                % (px, y_fork, cx, color,
                   ' stroke-dasharray="4 3"' if dead else "")
            )
            # vertical run in subcolumn from fork to mid (or to dead)
            y_end = mid if not (
                dead
            ) else mid
            parts.append(
                '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1.85"%s/>'
                % (cx, y_fork, cx, y_end, color,
                   ' stroke-dasharray="4 3"' if dead else "")
            )
            if dead:
                # DEAD stop — X, no line below mid
                parts.append(
                    '<circle cx="%d" cy="%d" r="6" fill="none" stroke="var(--red)" '
                    'stroke-width="1.5"/>' % (cx, mid)
                )
                parts.append(
                    '<path d="M%d,%d L%d,%d M%d,%d L%d,%d" stroke="var(--red)" '
                    'stroke-width="1.5"/>'
                    % (cx - 4, mid - 4, cx + 4, mid + 4, cx + 4, mid - 4, cx - 4, mid + 4)
                )
                parts.append(
                    '<text x="%d" y="%d" font-size="8" fill="var(--red)" '
                    'text-anchor="middle">DEAD</text>' % (cx, mid + 16)
                )
            else:
                merged = bool(m.get("merged_parent")) or (
                    (lane.get("route_status") or "") in ("merged", "winner")
                )
                parts.append(
                    '<circle cx="%d" cy="%d" r="4.5" fill="%s" '
                    'stroke="var(--bg)" stroke-width="1.5"/>' % (cx, mid, color)
                )
                if merged:
                    # Winner: rejoin parent worktree column
                    parts.append(
                        '<path d="M%d,%d H%d" fill="none" stroke="var(--olive)" '
                        'stroke-width="1.85"/>' % (cx, mid, px)
                    )
                    parts.append(
                        '<circle cx="%d" cy="%d" r="5.5" fill="var(--olive)" '
                        'stroke="var(--bg)" stroke-width="1.5"/>' % (px, mid)
                    )
                    parts.append(
                        '<text x="%d" y="%d" font-size="8" fill="var(--olive)" '
                        'text-anchor="middle">MERGE</text>' % (px, mid + 16)
                    )
                else:
                    # Still open: A/B column keeps running vertically
                    parts.append(
                        '<text x="%d" y="%d" font-size="8" fill="%s" '
                        'text-anchor="middle">A/B</text>' % (cx, mid + 16, color)
                    )
        else:
            # worktree fork from main
            parts.append(
                '<path d="M%d,%d H%d" fill="none" stroke="%s" stroke-width="2"/>'
                % (px, mid, cx, color)
            )
            parts.append(
                '<circle class="node" cx="%d" cy="%d" r="6" fill="%s" '
                'stroke="var(--bg)" stroke-width="1.5"/>' % (cx, mid, color)
            )
            if _uncommitted(m["git"]):
                parts.append(
                    '<circle cx="%d" cy="%d" r="9" fill="none" stroke="var(--red)" '
                    'stroke-width="1.25"/>' % (cx, mid)
                )
            parts.append(
                '<text x="%d" y="%d" font-size="8" fill="var(--text3)" '
                'text-anchor="middle">WT</text>' % (cx, mid + 16)
            )
    else:
        parts.append(
            '<circle class="node" cx="%d" cy="%d" r="6" fill="%s" '
            'stroke="var(--bg)" stroke-width="1.5"/>' % (X(col), mid, color)
        )

    parts.append("</svg></div>")
    return "".join(parts)


def _rail_graph_dot(lane: dict, scale: int) -> str:
    """C · row marker only — the real topology lives in the mini-graph above."""
    m = _lane_metrics(lane, scale)
    if lane.get("kind") == "main":
        return ('<div class="lb-railcell" data-style="graph" data-depth="0">'
                '<svg class="lb-rail" viewBox="0 0 48 64" width="48" height="64" role="img">'
                '%s<circle class="node" cx="24" cy="32" r="5" fill="var(--text2)" stroke="var(--bg)"/>'
                '</svg></div>' % m["title"])
    opacity = ".45" if m["dead"] else "1"
    ring = ""
    if _uncommitted(m["git"]) and lane.get("kind") == "worktree":
        ring = ('<circle cx="24" cy="32" r="%.1f" fill="none" stroke="var(--red)" '
                'stroke-width="1.25"/>' % (m["radius"] + 3))
    mark = ""
    if m["dead"]:
        mark = '<text x="24" y="48" font-size="7" fill="var(--text3)" text-anchor="middle">✕</text>'
    return ('<div class="lb-railcell" data-style="graph" data-depth="%d" data-role="%s" data-route="%s" style="opacity:%s">'
            '<svg class="lb-rail" viewBox="0 0 48 64" width="48" height="64" role="img">'
            '%s<circle class="node" cx="24" cy="32" r="%.1f" fill="%s" stroke="var(--bg)"/>%s%s'
            '</svg></div>'
            % (m["depth"], _e(m["role"]), _e(m["route"]), opacity, m["title"],
               m["radius"], m["color"], ring, mark))


def _repo_mini_graph(repo: dict, scale: int) -> str:
    """C · pure-SVG tree/DAG for one repo. No dagre — layout by depth + tree order."""
    lanes = list(repo.get("lanes") or [])
    if not lanes:
        return ""
    # Group by depth
    by_depth = {}
    for l in lanes:
        d = 0 if l.get("kind") == "main" else int(l.get("depth") or 1)
        by_depth.setdefault(d, []).append(l)
    max_depth = max(by_depth) if by_depth else 0
    col_w = 110
    row_h = 28
    max_rows = max(len(v) for v in by_depth.values()) if by_depth else 1
    width = max(280, (max_depth + 1) * col_w + 40)
    height = max(80, max_rows * row_h + 36)

    # Assign positions: (id -> x,y)
    pos = {}
    for d, group in by_depth.items():
        group = sorted(group, key=lambda l: (l.get("_tree_ord") if l.get("_tree_ord") is not None else 99,
                                             l.get("name") or ""))
        for i, l in enumerate(group):
            x = 30 + d * col_w
            y = 24 + i * row_h + (max_rows - len(group)) * row_h / 4
            pos[l["id"]] = (x, y, l)

    # Edges parent -> child
    edges = []
    for l in lanes:
        if l.get("kind") == "main":
            continue
        pid = l.get("parent_id")
        # parent_id null ⇒ main
        if not pid:
            main = next((x for x in lanes if x.get("kind") == "main"), None)
            pid = main["id"] if main else None
        if pid and pid in pos and l["id"] in pos:
            edges.append((pid, l["id"], l))

    parts = ['<svg class="lb-minigraph" viewBox="0 0 %d %d" width="100%%" height="%d" role="img" aria-label="lane lineage graph">'
             % (width, height, height)]
    parts.append('<title>%s lineage</title>' % _e(repo.get("name") or "repo"))
    for src, dst, child in edges:
        x1, y1, _ = pos[src]
        x2, y2, _ = pos[dst]
        dead = (child.get("route_status") == "dead_route")
        color = "var(--text3)" if dead else _VERDICT_COLOR.get(child.get("verdict") or "CLEAN", "var(--text3)")
        dash = ' stroke-dasharray="3 3"' if dead else ""
        # cubic elbow
        mx = (x1 + x2) / 2
        parts.append('<path d="M%.1f,%.1f C%.1f,%.1f %.1f,%.1f %.1f,%.1f" fill="none" stroke="%s" '
                     'stroke-width="1.5"%s opacity=".85"/>'
                     % (x1 + 8, y1, mx, y1, mx, y2, x2 - 10, y2, color, dash))

    for lid, (x, y, l) in pos.items():
        verdict = l.get("verdict") or "CLEAN"
        color = _VERDICT_COLOR.get(verdict, "var(--text3)")
        dead = l.get("route_status") == "dead_route"
        if l.get("kind") == "main":
            color = "var(--text2)"
        if dead:
            color = "var(--text3)"
        name = l.get("name") or "?"
        if len(name) > 12:
            name = name[:11] + "…"
        r = 5.5 if l.get("kind") == "main" else 4.5
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" stroke="var(--bg)" stroke-width="1.5">'
                     '<title>%s — %s</title></circle>'
                     % (x, y, r, color, _e(l.get("branch") or name), _e(verdict)))
        if dead:
            parts.append('<text x="%.1f" y="%.1f" font-size="8" fill="var(--text3)" text-anchor="middle">✕</text>'
                         % (x, y + 3))
        parts.append('<text x="%.1f" y="%.1f" font-size="10" fill="var(--text2)">%s</text>'
                     % (x + 10, y + 3.5, _e(name)))

    parts.append("</svg>")
    return ('<div class="lb-minigraph-wrap" data-rail-only="graph">'
            '<div class="lb-minigraph-label">lineage graph · click a row below for detail</div>'
            '%s</div>' % "".join(parts))


def _rail_svg(lane: dict, scale: int, rail_style: str = "nested",
              layout: dict | None = None) -> str:
    """Dispatch to the active bake-off rail style."""
    style = rail_style if rail_style in ("nested", "elbow", "graph") else "elbow"
    if style == "elbow":
        return _rail_elbow(lane, scale, layout)
    if style == "graph":
        return _rail_graph_dot(lane, scale)
    return _rail_nested(lane, scale)


# ------------------------------------------------------------- expansions --

def _commands(lane: dict, repo: dict) -> str:
    """Actions. SWEEP is deliberately not runnable from here."""
    verdict = lane.get("verdict")
    path = lane.get("path") or ""
    branch = lane.get("branch") or ""
    out = []
    if verdict in ("RESCUE", "DIRTY"):
        inspect = "git --no-optional-locks -C %s status --porcelain -unormal" % path
        out.append('<div class="lb-cmd"><code>%s</code>'
                   '<button type="button" data-lb-copy="%s">copy</button></div>'
                   % (_e(inspect), _e(inspect)))
        out.append('<div class="lb-manual">Read-only. Stage the files you want by exact path '
                   '&mdash; never a directory-level add.</div>')
    if verdict in ("MERGE", "DIRTY") and branch and (lane.get("route_status") != "dead_route"):
        integ = lane.get("integration") or {}
        into = integ.get("target_branch") or repo.get("base") or "main"
        # Merge into the correct parent (feature for variants; main for features)
        merge = "git --no-optional-locks -C %s merge --no-ff %s" % (
            # merge must run with target branch checked out — document via checkout+merge
            repo.get("path") or "", branch)
        # Prefer explicit: checkout target then merge branch
        seq = ("git --no-optional-locks -C %s checkout %s && "
               "git --no-optional-locks -C %s merge --no-ff %s" % (
                   repo.get("path") or "", into, repo.get("path") or "", branch))
        tip = "merge into %s (parent/integration target)" % into
        out.append('<div class="lb-cmd"><span class="lb-cmd-tip">%s</span><code>%s</code>'
                   '<button type="button" data-lb-copy="%s">copy</button></div>'
                   % (_e(tip), _e(seq), _e(seq)))
    if lane.get("route_status") == "dead_route":
        out.append('<div class="lb-manual">Dead route — will not merge to main. '
                   'Reclaim the worktree only when clean (no SWEEP command offered here).</div>')
    if verdict == "SWEEP":
        out.append('<div class="lb-manual">Safe to reclaim &mdash; <b>%s</b>. '
                   'No command is offered here on purpose: reclaiming a lane is destructive '
                   'and one paste away from the wrong path. Type it yourself against:'
                   '<span class="lb-path">%s</span></div>'
                   % (_e(_bytes(lane.get("size_bytes"))), _e(path)))
    if not out:
        return ""
    return '<div class="lb-sec"><h4>Action</h4>%s</div>' % "".join(out)


def _overlaps(lane: dict) -> str:
    others = lane.get("overlaps") or []
    files = lane.get("overlap_files") or []
    if not (others or files):
        return ""
    shown, extra = files[:12], max(0, len(files) - 12)
    items = "".join("<li>%s</li>" % _e(f) for f in shown)
    if extra:
        items += '<li>+%d more</li>' % extra
    return ('<div class="lb-sec"><h4>Overlap</h4><div class="lb-overlap">'
            'shares changed files with <b>%s</b><ul>%s</ul></div></div>'
            % (_e(", ".join(others)) or "another lane", items))


def _history(lane: dict) -> str:
    entries = lane.get("history") or []
    if not entries:
        return ('<div class="lb-sec"><h4>Action log</h4>'
                '<span class="lb-none">no commits recorded for this lane</span></div>')
    rows = "".join(
        '<li><span class="a">%s</span><span class="w">%s</span>'
        '<span class="t">%s</span><span class="s">%s</span></li>'
        % (_e(h.get("age") or ""), _e(h.get("who") or ""),
           _e(h.get("what") or ""), _e(h.get("sha") or ""))
        for h in entries)
    return ('<div class="lb-sec"><h4>Action log &mdash; %d entr%s</h4>'
            '<ul class="lb-hist">%s</ul></div>'
            % (len(entries), "y" if len(entries) == 1 else "ies", rows))


def _lane_body(lane: dict, repo: dict) -> str:
    git = lane.get("git") or {}
    head = lane.get("head") or {}
    purpose = lane.get("purpose")
    if purpose:
        title = '<div class="lb-purpose">%s</div>' % _e(purpose)
    else:
        title = ('<div class="lb-purpose">%s</div>'
                 '<div class="lb-purpose none">no purpose recorded</div>'
                 % _e(lane.get("branch") or lane.get("name") or "?"))
    kv = [("branch", lane.get("branch")), ("verdict", lane.get("verdict_reason")),
          ("head", "%s %s" % (head.get("sha") or "?", head.get("subject") or "")),
          ("purpose src", lane.get("purpose_source") or "none"),
          ("git", "%d ahead / %d behind / merged=%s"
           % (int(git.get("ahead") or 0), int(git.get("behind") or 0),
              "yes" if git.get("merged_into_base") else "no")),
          ("age", "%s days" % lane.get("age_days"))]
    meta = "".join("<span><b>%s</b> %s</span>" % (_e(k), _e(v)) for k, v in kv)
    return ('<div class="lb-body">%s<div class="lb-kv">%s</div>'
            '<div class="lb-path">%s</div>'
            '<div class="lb-sec"><h4>Actors &amp; evidence</h4>%s</div>%s%s%s</div>'
            % (title, meta, _e(lane.get("path") or ""), _actors(lane, full=True),
               _overlaps(lane), _commands(lane, repo), _history(lane)))


# ------------------------------------------------------------------- rows --

def _parent_chip(lane: dict) -> str:
    if lane.get("kind") == "main":
        return ""
    route = lane.get("route_status") or "open"
    role = lane.get("role") or ""
    parent = lane.get("parent_branch") or "main"
    bits = ['<span class="lb-parent" title="parent branch">← %s</span>' % _e(parent)]
    if role and role not in ("feature", "main", "unknown"):
        bits.append('<span class="lb-role" data-role="%s">%s</span>' % (_e(role), _e(role)))
    if route == "dead_route":
        bits.append('<span class="lb-dead">DEAD</span>')
    conf = lane.get("lineage_confidence")
    src = lane.get("lineage_source")
    if src and src != "none":
        bits.append('<span class="lb-lineage" title="lineage %s / %s">%s</span>'
                    % (_e(src), _e(conf), _e(src)))
    return '<div class="lb-lineage-row">%s</div>' % "".join(bits)


def _row(lane: dict, repo: dict, mode: str, scale: int, rail_style: str = "elbow",
         layout: dict | None = None) -> str:
    git = lane.get("git") or {}
    head = lane.get("head") or {}
    verdict = lane.get("verdict") or "CLEAN"
    name = lane.get("name") or lane.get("branch") or "?"
    purpose = lane.get("purpose")
    kind = lane.get("kind") or "worktree"
    depth = int(lane.get("depth") or 0)
    tree_ord = lane.get("_tree_ord")
    if tree_ord is None:
        tree_ord = 0 if kind == "main" else 100
    age_s = head.get("age_s")
    if age_s is None:
        age_s = (lane.get("age_days") or 0) * 86400
    alarm_id = "%s|%s|%s" % (lane.get("id"), verdict, lane.get("verdict_reason") or "")
    attrs = ('class="lb-row" data-verdict="%s" data-rank="%d" data-age="%s" data-name="%s" '
             'data-first="%d" data-depth="%d" data-tree="%s" data-route="%s" '
             'data-role="%s" data-alarm="%s" data-key="%s:%s"'
             % (_e(verdict), _VERDICT_RANK.get(verdict, 9), _e(age_s), _e(name),
                1 if kind == "main" else 0, depth, _e(tree_ord),
                _e(lane.get("route_status") or "open"),
                _e(lane.get("role") or ""), _e(alarm_id),
                _e(mode), _e(lane.get("id") or name)))
    if purpose:
        sub = '<div class="p">%s</div>' % _e(purpose)
    else:
        sub = '<div class="p none">%s &middot; no purpose recorded</div>' % _e(lane.get("branch"))
    sub += _parent_chip(lane)
    # Hierarchy labels (what Mati scans for): MAIN / WORKTREE / A/B
    role = lane.get("role") or ""
    route = lane.get("route_status") or ""
    if kind == "main":
        hier = "MAIN"
        hier_cls = "main"
    elif depth >= 2 or role == "variant":
        hier = "DEAD" if route == "dead_route" else "A/B"
        hier_cls = "dead" if route == "dead_route" else "ab"
    else:
        hier = "WORKTREE"
        hier_cls = "wt"
    kind_tag = (
        '<span class="lb-kind">%s</span>'
        '<span class="lb-hier" data-h="%s">%s</span>'
        % (_e(kind), hier_cls, hier)
    )
    # Indent list text by depth for every rail style (multiple worktrees + A/B under them)
    pad = max(0, depth) * (22 if rail_style == "elbow" else 14)
    namecell = ('<div class="lb-name" style="padding-left:%dpx"><div class="n">%s%s</div>%s</div>'
                % (pad, _e(name), kind_tag, sub))

    if mode == "rail":
        namecell = ('<div class="lb-name" style="padding-left:%dpx"><div class="n">%s%s</div>%s%s</div>'
                    % (pad, _e(name), kind_tag, sub, _actors(lane)))
        cells = [_rail_svg(lane, scale, rail_style, layout), namecell, _figs(git), _pill(lane),
                 '<span class="lb-chev">&#8250;</span>']
    else:
        cells = [_pill(lane), namecell, _divergence(git, scale), _files(lane),
                 '<span class="lb-age">%s</span>' % _e(_age(age_s)),
                 _actors(lane), '<span class="lb-chev">&#8250;</span>']
    # Alarm ack button for RESCUE/DIRTY/MERGE
    ack = ""
    if verdict in ("RESCUE", "DIRTY", "MERGE"):
        ack = ('<button type="button" class="lb-ack" data-lb-ack="%s" '
               'title="Acknowledge until the fact changes">ack</button>' % _e(alarm_id))
    return ('<details %s><summary>%s%s</summary>%s</details>'
            % (attrs, "".join(cells), ack, _lane_body(lane, repo)))


def _repo_section(repo: dict, mode: str, scale: int, rail_style: str = "elbow") -> str:
    totals = repo.get("totals") or {}
    activity = repo.get("activity") or {}
    head = repo.get("head") or {}
    stats = ('<div class="lb-repo-stats">'
             '<span><b>%s</b> lanes</span><span><b>%s</b> unmerged</span>'
             '<span><b>%s</b> uncommitted</span>'
             '<span>%s commits/1h &middot; %s/24h &middot; %s actor(s)</span></div>'
             % (_e(totals.get("lanes")), _e(totals.get("unmerged_commits")),
                _e(totals.get("uncommitted_files")), _e(activity.get("commits_1h")),
                _e(activity.get("commits_24h")), _e(activity.get("actors_24h"))))
    sub = ('<div class="lb-repo-sub">%s &middot; head %s %s &middot; %s ago</div>'
           % (_e(repo.get("path")), _e(head.get("sha")), _e(head.get("subject")),
              _e(_age(head.get("age_s")))))
    graph = _repo_mini_graph(repo, scale) if (mode == "rail" and rail_style == "graph") else ""
    lane_list = list(repo.get("lanes") or [])
    # Parent before children always (tree_ord from snapshot, fallback stable)
    lane_list.sort(key=lambda l: (
        l.get("_tree_ord") if l.get("_tree_ord") is not None else (
            0 if l.get("kind") == "main" else 100),
        l.get("name") or ""))
    layout = _assign_graph_columns(lane_list) if mode == "rail" else {}
    lanes = "".join(_row(l, repo, mode, scale, rail_style, layout) for l in lane_list)
    if not lanes:
        lanes = '<div class="lb-hidden-note">no lanes discovered in this repo</div>'
    # <details>, so each repo folds. Open by default; the fold layer in
    # MONITOR_JS restores whatever Mati last chose from localStorage. The head
    # is the summary, so lane/unmerged/uncommitted counts stay visible folded.
    return ('<details class="lb-repo" open data-key="%s" data-lb-fold="repo:%s">'
            '<summary class="lb-repo-head">'
            '<div class="lb-repo-name"><span class="lb-accent"></span>%s</div>'
            '<span class="lb-figs">base %s</span>%s%s</summary>'
            '%s<div class="lb-lanes">%s</div><div class="lb-hidden-note"></div></details>'
            % (_e(repo.get("key")), _e(repo.get("key")), _e(repo.get("name")),
               _e(repo.get("base")), stats, sub, graph, lanes))


# ---------------------------------------------------------------- chrome --

_CONTROLS = [
    ("view", [("rail", "Rail"), ("dense", "Dense")]),
    ("rail", [("elbow", "Elbow"), ("nested", "Nested"), ("graph", "Graph")]),
    ("density", [("comfortable", "Comfortable"), ("compact", "Compact")]),
    ("sort", [("tree", "Tree"), ("verdict", "Verdict"), ("age", "Age"), ("name", "Name")]),
    ("clean", [("show", "Show clean"), ("hide", "Hide clean")]),
    ("theme", [("dark", "Dark"), ("light", "Light")]),
]


def _controls() -> str:
    groups = []
    for key, options in _CONTROLS:
        buttons = "".join(
            '<button type="button" data-lb-set="%s:%s" aria-pressed="false">%s</button>'
            % (key, value, _e(label)) for value, label in options)
        groups.append('<div class="lb-grp"><span>%s</span><div class="lb-seg">%s</div></div>'
                      % (_e(key), buttons))
    return '<div class="lb-ctrls">%s</div>' % "".join(groups)


def _banners(snap: dict, age_s) -> str:
    out = []
    night = snap.get("night") or {}
    pd = night.get("pending_session_digests")
    rc = night.get("recent_convs")
    if pd is not None or rc is not None:
        cls = "warn" if (pd or 0) > 0 else "info"
        out.append(
            '<div class="lb-banner %s" data-lb-night="1"><b>/NIGHT</b>'
            '<span><b class="num">%s</b> session digest(s) pending · '
            '<b class="num">%s</b> recent conv(s) '
            '<span class="lb-ev">%s</span> — run <code>/night</code> to chew them.</span></div>'
            % (cls, _e(pd if pd is not None else "?"),
               _e(rc if rc is not None else "?"),
               _e(night.get("evidence") or "")))
    if snap.get("_fixture"):
        out.append('<div class="lb-banner alarm"><b>SAMPLE DATA</b>'
                   '<span>This board is rendering a captured fixture, not live state. '
                   'Nothing here reflects your repos right now.</span></div>')
    if snap.get("slept"):
        out.append('<div class="lb-banner warn"><b>SLEPT</b><span>Ages were measured across a '
                   'sleep &mdash; every actor is reported as <code>unknown</code>.</span></div>')
    out.append('<div class="lb-banner alarm lb-offline"><b>OFFLINE</b>'
               '<span>The monitor stopped answering. What you are looking at is frozen.</span></div>')
    out.append('<div class="lb-banner warn lb-stale"><b>STALE</b>'
               '<span>Snapshot is <span data-lb-staleage>%s</span> old &mdash; older than the '
               '%ds refresh, so the board may be behind.</span></div>'
               % (_e(_age(age_s)) if age_s is not None else "?", _STALE_AFTER_S))
    return "".join(out)


def _footer(snap: dict) -> str:
    rows = []
    for err in snap.get("errors") or []:
        rows.append('<div class="lb-err"><span class="sc">%s</span><span>%s</span></div>'
                    % (_e(err.get("scope") or "?"), _e(err.get("message") or "")))
    for actor in snap.get("unattributed") or []:
        rows.append('<div class="lb-err"><span class="sc">elsewhere</span>%s</div>'
                    % _actor(actor, full=True))
    if not rows:
        rows.append('<div class="lb-err"><span class="lb-none">no snapshot errors, '
                    'no unattributed actors</span></div>')
    return ('<div class="lb-foot"><h4>Elsewhere &amp; snapshot notes</h4>%s</div>'
            % "".join(rows))


def render_board(snapshot: dict, view: str = "dense", standalone: bool = True,
                 rail: str = "elbow") -> str:
    """Render the board. `standalone` wraps it in a full previewable document.

    Both views are emitted every time; `view` only picks which one starts
    visible. In standalone mode the choice is pinned (`data-pin`) so a preview
    file shows the view it was asked for instead of whatever localStorage says.
    """
    snap = snapshot or {}
    view = view if view in ("rail", "dense") else "dense"
    rail = rail if rail in ("nested", "elbow", "graph") else "elbow"
    scale = _drift_scale(snap)
    age_s = _snapshot_age_s(snap)
    repos = snap.get("repos") or []

    views = []
    # Dense once; rail three ways so Nested/Elbow/Graph flip with zero refetch.
    dense_sections = "".join(_repo_section(r, "dense", scale, "nested") for r in repos)
    if not dense_sections:
        dense_sections = ('<section class="lb-repo"><div class="lb-repo-head">'
                          '<div class="lb-repo-name"><span class="lb-accent"></span>'
                          'No repos in this snapshot</div></div></section>')
    views.append('<div class="lb-view" data-v="dense">%s</div>' % dense_sections)
    for style in ("elbow", "nested", "graph"):
        sections = "".join(_repo_section(r, "rail", scale, style) for r in repos)
        if not sections:
            sections = ('<section class="lb-repo"><div class="lb-repo-head">'
                        '<div class="lb-repo-name"><span class="lb-accent"></span>'
                        'No repos in this snapshot</div></div></section>')
        views.append('<div class="lb-view" data-v="rail" data-rail-style="%s">%s</div>'
                     % (style, sections))

    clock = ('<span class="lb-when" data-lb-clock>snapshot %s old</span>'
             % (_e(_age(age_s)) if age_s is not None else "age unknown"))
    generated = _e(snap.get("generated_at") or "unknown")
    header = ('<div class="lb-head"><h2 class="lb-h1">Lane Board%s'
              '<span class="lb-when">%s &middot; %sms</span></h2>%s</div>'
              % (clock, generated, _e(snap.get("duration_ms")), _controls()))

    rail_default = rail
    board = ('<div class="lb" data-view="%s" data-rail="%s" data-density="comfortable" '
             'data-theme="dark" data-clean="show" data-sort="tree" data-age-s="%s"%s>'
             '%s%s%s%s</div>'
             % (view, rail_default, _e("" if age_s is None else round(age_s, 1)),
                ' data-pin="1"' if standalone else "",
                header, _banners(snap, age_s), "".join(views), _footer(snap)))

    if not standalone:
        return board
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Lane Board &mdash; %s</title><style>"
            "html,body{margin:0;background:#0E0E0D;}%s</style></head><body>%s"
            "<script>%s</script></body></html>" % (view, BOARD_CSS, board, BOARD_JS))


def render_board_fragment(snapshot: dict, view: str = "dense") -> str:
    """The board only — for embedding in the monitor page."""
    return render_board(snapshot, view=view, standalone=False)
