"""Lane Board renderer — turns a lane snapshot into HTML.

Two switchable views over the same snapshot:

* **Rail** — main is a vertical spine, each lane branches off it with an SVG
  curve. Drift (`behind`) is the curve's reach, commit count (`ahead`) is the
  node's size, so you see divergence before you read a number.
* **Dense** — one tight row per lane, aligned columns, divergence as a bar.

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
    """The branch curve off the spine. Merged lanes loop back into it.

    The spine itself is a CSS rule on `.lb-railcell`, so it runs unbroken
    through rows of any height; only the branch is drawn here.
    """
    git = lane.get("git") or {}
    verdict = lane.get("verdict") or "CLEAN"
    color = _VERDICT_COLOR.get(verdict, "var(--text3)")
    title = "<title>%s</title>" % _e(lane.get("verdict_reason") or verdict)
    if lane.get("kind") == "main":
        return ('<div class="lb-railcell">'
                '<svg class="lb-rail" viewBox="0 0 96 64" width="96" height="64" '
                'role="img">%s<circle class="node" cx="24" cy="32" r="5.5" '
                'fill="var(--text2)" stroke="var(--bg)"/>'
                '<text x="38" y="35" font-size="9" fill="var(--text3)">main</text>'
                '</svg></div>' % title)

    behind, ahead = int(git.get("behind") or 0), int(git.get("ahead") or 0)
    apex = 40 + min(behind, scale) / float(scale) * 44          # reach == drift
    radius = 3 + min(ahead, 25) / 25.0 * 5                       # size == commits
    merged = bool(git.get("merged_into_base"))
    dash = ' stroke-dasharray="4 4"' if verdict == "PARKED" else ""
    d = "M24,6 C24,18 %.1f,22 %.1f,32" % (apex - 14, apex)
    if merged:
        d += " C%.1f,42 24,46 24,58" % (apex + 14)
    ring = ""
    if _uncommitted(git) and lane.get("kind") == "worktree":
        # The contradiction we must keep visible: a lane can be merged back into
        # the spine and still be holding work git will never see.
        ring = ('<circle cx="%.1f" cy="32" r="%.1f" fill="none" stroke="var(--red)" '
                'stroke-width="1.25" opacity=".85"/>' % (apex, radius + 3.5))
    return ('<div class="lb-railcell">'
            '<svg class="lb-rail" viewBox="0 0 96 64" width="96" height="64" role="img">'
            '%s<circle cx="24" cy="6" r="2" fill="var(--line)"/>'
            '<path class="branch" d="%s" stroke="%s"%s/>'
            '<circle class="node" cx="%.1f" cy="32" r="%.1f" fill="%s" stroke="var(--bg)"/>%s'
            '</svg></div>'
            % (title, d, color, dash, apex, radius, color, ring))


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
    if verdict in ("MERGE", "DIRTY") and branch:
        merge = "git --no-optional-locks -C %s merge --no-ff %s" % (repo.get("path") or "", branch)
        out.append('<div class="lb-cmd"><code>%s</code>'
                   '<button type="button" data-lb-copy="%s">copy</button></div>'
                   % (_e(merge), _e(merge)))
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

def _row(lane: dict, repo: dict, mode: str, scale: int) -> str:
    git = lane.get("git") or {}
    head = lane.get("head") or {}
    verdict = lane.get("verdict") or "CLEAN"
    name = lane.get("name") or lane.get("branch") or "?"
    purpose = lane.get("purpose")
    kind = lane.get("kind") or "worktree"
    age_s = head.get("age_s")
    if age_s is None:
        age_s = (lane.get("age_days") or 0) * 86400
    attrs = ('class="lb-row" data-verdict="%s" data-rank="%d" data-age="%s" data-name="%s" '
             'data-first="%d" data-key="%s:%s"'
             % (_e(verdict), _VERDICT_RANK.get(verdict, 9), _e(age_s), _e(name),
                1 if kind == "main" else 0, _e(mode), _e(lane.get("id") or name)))
    if purpose:
        sub = '<div class="p">%s</div>' % _e(purpose)
    else:
        sub = '<div class="p none">%s &middot; no purpose recorded</div>' % _e(lane.get("branch"))
    kind_tag = '<span class="lb-kind">%s</span>' % _e(kind)
    namecell = ('<div class="lb-name"><div class="n">%s%s</div>%s</div>'
                % (_e(name), kind_tag, sub))

    if mode == "rail":
        namecell = ('<div class="lb-name"><div class="n">%s%s</div>%s%s</div>'
                    % (_e(name), kind_tag, sub, _actors(lane)))
        cells = [_rail_svg(lane, scale), namecell, _figs(git), _pill(lane),
                 '<span class="lb-chev">&#8250;</span>']
    else:
        cells = [_pill(lane), namecell, _divergence(git, scale), _files(lane),
                 '<span class="lb-age">%s</span>' % _e(_age(age_s)),
                 _actors(lane), '<span class="lb-chev">&#8250;</span>']
    return ('<details %s><summary>%s</summary>%s</details>'
            % (attrs, "".join(cells), _lane_body(lane, repo)))


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
    ("sort", [("verdict", "Verdict"), ("age", "Age"), ("name", "Name")]),
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
             'data-clean="show" data-sort="verdict" data-age-s="%s"%s>%s%s%s%s</div>'
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
