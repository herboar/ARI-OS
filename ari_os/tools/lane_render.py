"""Lane Board renderer — turns a lane snapshot into HTML.

Two switchable views over the same snapshot:

* **Rail** — main is a vertical spine; features fork from main, variants fork
  from their parent feature (nested indent). Dead routes are dashed and never
  loop back to main. Drift is behind-parent; node size is ahead-of-parent.
* **Dense** — tree-indented rows with parent chips; divergence as a bar.

Both views are always emitted; the container's `data-view` attribute decides
which one is visible, so switching is a CSS flip — no refetch, no scroll loss.

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


def _rail_svg(lane: dict, scale: int) -> str:
    """Nested branch curve: depth indents the origin; dead routes never rejoin main.

    Main spine is CSS on `.lb-railcell`. Variants start from a parent column, not
    always from main — Ari's linear/parent fork model made visual.
    """
    git = lane.get("git") or {}
    integ = lane.get("integration") or {}
    verdict = lane.get("verdict") or "CLEAN"
    color = _VERDICT_COLOR.get(verdict, "var(--text3)")
    title = "<title>%s</title>" % _e(lane.get("verdict_reason") or verdict)
    depth = int(lane.get("depth") or 0)
    route = lane.get("route_status") or "open"
    role = lane.get("role") or "unknown"
    # Column geometry: main at x=20; each depth steps +16px
    spine_x = 20
    origin_x = spine_x + max(0, depth - 1) * 16
    node_x = spine_x + depth * 16 + 28
    width = max(96, int(node_x + 36))

    if lane.get("kind") == "main":
        return ('<div class="lb-railcell" data-depth="0">'
                '<svg class="lb-rail" viewBox="0 0 %d 64" width="%d" height="64" '
                'role="img">%s<circle class="node" cx="%d" cy="32" r="5.5" '
                'fill="var(--text2)" stroke="var(--bg)"/>'
                '<text x="%d" y="35" font-size="9" fill="var(--text3)">main</text>'
                '</svg></div>' % (width, width, title, spine_x, spine_x + 14))

    behind = integ.get("behind_parent")
    ahead = integ.get("ahead_of_parent")
    if behind is None:
        behind = int(git.get("behind") or 0)
    if ahead is None:
        ahead = int(git.get("ahead") or 0)
    behind, ahead = int(behind or 0), int(ahead or 0)
    # Horizontal reach encodes drift from parent
    reach = min(behind, scale) / float(scale or 1) * 28
    apex = node_x + reach
    radius = 3 + min(ahead, 25) / 25.0 * 5
    merged_parent = bool(integ.get("merged_into_parent"))
    merged_base = bool(git.get("merged_into_base"))
    dead = route == "dead_route"
    dash = ""
    if dead or verdict == "PARKED":
        dash = ' stroke-dasharray="4 4"'
    if dead:
        color = "var(--text3)"

    # Curve from parent column down to node
    d = "M%d,6 C%d,18 %.1f,22 %.1f,32" % (origin_x, origin_x, apex - 10, apex)
    # Only feature lanes (depth 1, integrating to main) draw a merge-back to main spine
    # Variants merge to parent — short inward hook, not full spine rejoin
    if not dead:
        if role != "variant" and merged_base:
            d += " C%.1f,42 %d,46 %d,58" % (apex + 10, spine_x, spine_x)
        elif role == "variant" and merged_parent:
            d += " C%.1f,42 %d,46 %d,58" % (apex + 8, origin_x, origin_x)

    ring = ""
    if _uncommitted(git) and lane.get("kind") == "worktree":
        ring = ('<circle cx="%.1f" cy="32" r="%.1f" fill="none" stroke="var(--red)" '
                'stroke-width="1.25" opacity=".85"/>' % (apex, radius + 3.5))
    dead_mark = ""
    if dead:
        dead_mark = ('<text x="%.1f" y="48" font-size="8" fill="var(--text3)" '
                     'text-anchor="middle">DEAD</text>' % apex)

    return ('<div class="lb-railcell" data-depth="%d" data-role="%s" data-route="%s">'
            '<svg class="lb-rail" viewBox="0 0 %d 64" width="%d" height="64" role="img">'
            '%s<circle cx="%d" cy="6" r="2" fill="var(--line)"/>'
            '<path class="branch" d="%s" stroke="%s"%s/>'
            '<circle class="node" cx="%.1f" cy="32" r="%.1f" fill="%s" stroke="var(--bg)"/>%s%s'
            '</svg></div>'
            % (depth, _e(role), _e(route), width, width, title, origin_x, d, color, dash,
               apex, radius, color, ring, dead_mark))


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


def _row(lane: dict, repo: dict, mode: str, scale: int) -> str:
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
    kind_tag = '<span class="lb-kind">%s</span>' % _e(kind)
    namecell = ('<div class="lb-name" style="padding-left:%dpx"><div class="n">%s%s</div>%s</div>'
                % (max(0, depth) * 14, _e(name), kind_tag, sub))

    if mode == "rail":
        namecell = ('<div class="lb-name"><div class="n">%s%s</div>%s%s</div>'
                    % (_e(name), kind_tag, sub, _actors(lane)))
        cells = [_rail_svg(lane, scale), namecell, _figs(git), _pill(lane),
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


def _repo_section(repo: dict, mode: str, scale: int) -> str:
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
    lanes = "".join(_row(l, repo, mode, scale) for l in (repo.get("lanes") or []))
    if not lanes:
        lanes = '<div class="lb-hidden-note">no lanes discovered in this repo</div>'
    # <details>, so each repo folds. Open by default; the fold layer in
    # MONITOR_JS restores whatever Mati last chose from localStorage. The head
    # is the summary, so lane/unmerged/uncommitted counts stay visible folded.
    return ('<details class="lb-repo" open data-key="%s" data-lb-fold="repo:%s">'
            '<summary class="lb-repo-head">'
            '<div class="lb-repo-name"><span class="lb-accent"></span>%s</div>'
            '<span class="lb-figs">base %s</span>%s%s</summary>'
            '<div class="lb-lanes">%s</div><div class="lb-hidden-note"></div></details>'
            % (_e(repo.get("key")), _e(repo.get("key")), _e(repo.get("name")),
               _e(repo.get("base")), stats, sub, lanes))


# ---------------------------------------------------------------- chrome --

_CONTROLS = [
    ("view", [("rail", "Rail"), ("dense", "Dense")]),
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


def render_board(snapshot: dict, view: str = "dense", standalone: bool = True) -> str:
    """Render the board. `standalone` wraps it in a full previewable document.

    Both views are emitted every time; `view` only picks which one starts
    visible. In standalone mode the choice is pinned (`data-pin`) so a preview
    file shows the view it was asked for instead of whatever localStorage says.
    """
    snap = snapshot or {}
    view = view if view in ("rail", "dense") else "dense"
    scale = _drift_scale(snap)
    age_s = _snapshot_age_s(snap)
    repos = snap.get("repos") or []

    views = []
    for mode in ("rail", "dense"):
        sections = "".join(_repo_section(r, mode, scale) for r in repos)
        if not sections:
            sections = ('<section class="lb-repo"><div class="lb-repo-head">'
                        '<div class="lb-repo-name"><span class="lb-accent"></span>'
                        'No repos in this snapshot</div></div></section>')
        views.append('<div class="lb-view" data-v="%s">%s</div>' % (mode, sections))

    clock = ('<span class="lb-when" data-lb-clock>snapshot %s old</span>'
             % (_e(_age(age_s)) if age_s is not None else "age unknown"))
    generated = _e(snap.get("generated_at") or "unknown")
    header = ('<div class="lb-head"><h2 class="lb-h1">Lane Board%s'
              '<span class="lb-when">%s &middot; %sms</span></h2>%s</div>'
              % (clock, generated, _e(snap.get("duration_ms")), _controls()))

    board = ('<div class="lb" data-view="%s" data-density="comfortable" data-theme="dark" '
             'data-clean="show" data-sort="tree" data-age-s="%s"%s>%s%s%s%s</div>'
             % (view, _e("" if age_s is None else round(age_s, 1)),
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
