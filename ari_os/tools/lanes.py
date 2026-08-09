"""Lane Board data layer — a truthful snapshot of every worktree, branch and actor.

Contract: docs/lane-board-contract.md (schema version 1).

Golden rule: git is the only load-bearing signal. Verdicts are computed from git
alone and are never influenced by liveness. Liveness is an annotation and always
carries its evidence string. Contradictions are rendered, never resolved.

Pure read. Nothing here mutates a repo, and `build_snapshot()` never raises.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import state

SCHEMA = 1

REPOS = [
    {"key": "animarek", "name": "Animarek",
     "path": "/Users/mmarek/Desktop/ClaudeCode_Projects/EA_Animarek", "base": "main"},
    {"key": "xfactor", "name": "X-Factor",
     "path": "/Users/mmarek/Desktop/ClaudeCode_Projects/EA-xfactor", "base": "main"},
]

GIT_TIMEOUT_S = 5.0        # rider 5: per-call ceiling
BUDGET_S = 2.5             # rider 5: total snapshot budget
HISTORY_CAP = 25
OVERLAP_CAP = 12
LIVE_S = 120               # log/transcript mtime under this reads as live
CONFLICT_S = 30            # dead pid + log younger than this = rendered contradiction
SESSION_WARM_S = 30 * 60
ACTOR_WINDOW_S = 45 * 60   # older sessions/workers are history, not board rows
SUBAGENT_WINDOW_S = 300    # a swarm can leave 50 transcripts; only current ones are rows
SUBAGENT_ROWS = 6          # beyond this, one aggregate row (never a silent drop)
SLEEP_GAP_S = 60           # rider 7
MAX_PARALLEL = 12

PROJECTS_DIR = Path.home() / ".claude" / "projects"
_PARKED_RE = re.compile(r"\bPARKED\b", re.I)
_PARKED_DATE_RE = re.compile(r"\bPARKED\b[^0-9]{0,12}(\d{4}-\d{2}-\d{2})", re.I)
_SLUG_RE = re.compile(r"[^A-Za-z0-9]")

_clock: dict = {}          # sleep-detection cache, module-level by design


# ---------------------------------------------------------------- plumbing

class _Ctx:
    """Per-snapshot error sink and deadline."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.errors: list = []          # list.append is atomic under CPython

    def error(self, scope: str, message: str) -> None:
        self.errors.append({"scope": scope, "message": message})


def _git(ctx: _Ctx, cwd, args: list, scope: str):
    """Run one git command. Returns stdout, or None (with an errors[] entry).

    Rider 1: --no-optional-locks on every invocation.
    Rider 3: never subprocess.run(timeout=) — SIGTERM first, SIGKILL last.
    """
    if time.monotonic() > ctx.deadline:
        ctx.error(scope, "snapshot budget exhausted before `git %s`" % args[0])
        return None
    argv = ["git", "--no-optional-locks", "-C", str(cwd)] + args
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        ctx.error(scope, "git could not start: %s" % exc)
        return None
    try:
        out, err = p.communicate(timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
        ctx.error(scope, "git %s timed out after %ds" % (args[0], GIT_TIMEOUT_S))
        return None
    if p.returncode != 0:
        msg = err.decode("utf-8", "replace").strip().splitlines()
        ctx.error(scope, "git %s failed: %s" % (args[0], msg[0][:160] if msg else "rc=%d" % p.returncode))
        return None
    return out.decode("utf-8", "replace")


def _utc(raw: str):
    try:
        return datetime.fromisoformat(raw.strip()).astimezone(timezone.utc)
    except Exception:
        return None


def _age_of(dt, now: datetime):
    return None if dt is None else max(0, int((now - dt).total_seconds()))


def _fmt_age(sec) -> str:
    if sec is None:
        return "never"
    if sec < 90:
        return "%ds" % sec
    if sec < 5400:
        return "%dm" % (sec // 60)
    if sec < 172800:
        return "%dh" % (sec // 3600)
    return "%dd" % (sec // 86400)


def _day_label(days) -> str:
    if days is None:
        return "unknown"
    return "today" if days == 0 else "%dd ago" % days


def _slug(path: str) -> str:
    """Forward-encode a real path into ~/.claude/projects slug form.

    Never the reverse: the encoding is many-to-one, so decoding is guessing.
    """
    return _SLUG_RE.sub("-", str(path))


def _match_lane(slug: str, lane_slugs: list):
    """Longest-prefix match of an encoded path against encoded lane paths."""
    best = None
    for lane_slug, lane_id in lane_slugs:
        if slug == lane_slug or slug.startswith(lane_slug + "-"):
            if best is None or len(lane_slug) > best[0]:
                best = (len(lane_slug), lane_id)
    return best[1] if best else None


def _mtime_size(path: Path):
    try:
        st = path.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return None, None


def _slept() -> bool:
    """Rider 7: wall clock ran away from the monotonic clock => the laptop slept."""
    now_w, now_m = time.time(), time.monotonic()
    prev = _clock.get("t")
    _clock["t"] = (now_w, now_m)
    if not prev:
        return False
    return (now_w - prev[0]) - (now_m - prev[1]) > SLEEP_GAP_S


# ---------------------------------------------------------------- git facts

def _log_entries(ctx: _Ctx, repo_path: str, rev: str, scope: str, now: datetime) -> list:
    out = _git(ctx, repo_path, ["log", "-%d" % HISTORY_CAP,
                                "--format=%h%x00%s%x00%cI%x00%an", rev], scope)
    entries = []
    for line in (out or "").splitlines():
        parts = line.split("\0")
        if len(parts) != 4:
            continue
        when = _utc(parts[2])
        age = _age_of(when, now)
        entries.append({
            "when": when.isoformat() if when else None,
            "age": _day_label(None if age is None else age // 86400),
            "sha": parts[0], "who": parts[3], "what": parts[1],
        })
    return entries


def _status_counts(ctx: _Ctx, lane_path: str, scope: str):
    """Rider 2: --porcelain (implicitly -unormal). Never -uall."""
    out = _git(ctx, lane_path, ["status", "--porcelain"], scope)
    if out is None:
        return None
    modified = staged = untracked = 0
    for line in out.splitlines():
        if not line:
            continue
        if line.startswith("??"):
            untracked += 1
            continue
        x, y = line[0], line[1]
        if x not in " ?":
            staged += 1
        if y not in " ?":
            modified += 1
    return {"modified": modified, "staged": staged, "untracked": untracked}


def _ahead_behind(ctx: _Ctx, repo_path: str, base: str, branch: str, scope: str):
    out = _git(ctx, repo_path, ["rev-list", "--left-right", "--count",
                                "%s...%s" % (base, branch)], scope)
    if out is None:
        return None, None
    parts = out.split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[1]), int(parts[0])      # ahead, behind
    except ValueError:
        return None, None


def _worktrees(ctx: _Ctx, repo: dict) -> list:
    out = _git(ctx, repo["path"], ["worktree", "list", "--porcelain"], "repo:" + repo["key"])
    if out is None:
        return []
    lanes, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur.get("path"):
                lanes.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[9:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line.startswith("detached"):
            cur["branch"] = None
    root = os.path.realpath(repo["path"])
    for w in lanes:
        w["kind"] = "main" if os.path.realpath(w["path"]) == root else "worktree"
        w["name"] = "main" if w["kind"] == "main" else os.path.basename(w["path"].rstrip("/"))
    lanes.sort(key=lambda w: (w["kind"] != "main", w["name"]))
    return lanes


# ---------------------------------------------------------------- purpose

def _lane_file_purposes(repo_path: str) -> dict:
    """branch -> (purpose, raw note) from .claude/agent-lanes.json, if present."""
    p = Path(repo_path) / ".claude" / "agent-lanes.json"
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    rows = data.get("lanes", []) if isinstance(data, dict) else data
    out = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict) or not row.get("branch"):
            continue
        raw = row.get("purpose") or row.get("note") or ""
        if raw:
            out[row["branch"]] = (_trim_purpose(raw), raw)
    return out


def _trim_purpose(raw: str) -> str:
    text = re.split(r"\s+Worktree:", raw)[0].strip()
    if len(text) > 100:
        cut = text[:100].rsplit(" ", 1)[0]
        text = cut + "…"
    return text


# ---------------------------------------------------------------- verdicts

def _verdict(lane: dict, parked_raw: str):
    g = lane["git"]
    if g.get("ahead") is None or g.get("modified") is None:
        return "UNKNOWN", "git data unavailable — see errors[]"
    if lane["kind"] == "main":
        # Noise floor: main carries ~1.3k untracked files and Mati's live edits.
        return "CLEAN", "main working tree"
    dirt = g["modified"] + g["staged"] + g["untracked"]
    merged = bool(g["merged_into_base"])
    if dirt > 0 and (merged or g["ahead"] == 0):
        return "RESCUE", "merged or 0 ahead, but %d untracked and %d modified remain" % (
            g["untracked"], g["modified"] + g["staged"])
    if dirt > 0 and g["ahead"] > 0:
        return "DIRTY", "%d unmerged commits plus %d uncommitted files" % (g["ahead"], dirt)
    if parked_raw and _PARKED_RE.search(parked_raw):
        m = _PARKED_DATE_RE.search(parked_raw)
        return "PARKED", ("parked by Mati %s" % m.group(1)) if m else "parked deliberately — never nag"
    if g["ahead"] > 0:
        return "MERGE", "%d commits nobody has integrated" % g["ahead"]
    if merged and g["ahead"] == 0:
        return "SWEEP", "merged, nothing uncommitted — the worktree can be removed"
    return "CLEAN", "nothing to do"


# ---------------------------------------------------------------- actors

def _worker_actor(w: dict, now_ts: float, slept: bool):
    wid = w.get("id") or "w-?"
    log = state.state_dir() / "logs" / ("%s.log" % wid)
    mtime, size = _mtime_size(log)
    log_age = None if mtime is None else max(0, int(now_ts - mtime))
    pid = w.get("pid")
    alive = state.pid_alive(pid) if pid is not None else False
    conflict = False

    if pid is not None and alive and w.get("pid_start"):
        if _pid_start(pid) not in (None, w["pid_start"]):
            alive = False
            liveness, evidence = "cold", "pid %s belongs to a different process now" % pid
            return _actor(wid, "worker", w, liveness, evidence, log_age, False, "pid", slept)

    empty = " (log still empty)" if size == 0 else ""
    if pid is None:
        liveness = "unknown"
        evidence = "no pid recorded; log %s" % (
            "wrote %s ago%s" % (_fmt_age(log_age), empty) if log_age is not None else "missing")
    elif log_age is None:
        liveness = "warm" if alive else "cold"
        evidence = "pid %s %s, no log file" % (pid, "alive" if alive else "dead")
    elif alive and log_age < LIVE_S:
        liveness = "live"
        evidence = "pid %s alive + log wrote %s ago%s" % (pid, _fmt_age(log_age), empty)
    elif alive:
        liveness = "warm"
        evidence = "pid %s alive, log quiet %s%s" % (pid, _fmt_age(log_age), empty)
    elif log_age < CONFLICT_S:
        liveness, conflict = "cold", True
        evidence = "pid %s dead, but log wrote %s ago" % (pid, _fmt_age(log_age))
    else:
        liveness = "cold"
        evidence = "pid %s dead, log quiet %s" % (pid, _fmt_age(log_age))
    return _actor(wid, "worker", w, liveness, evidence, log_age, conflict, "ari-os-log", slept)


def _pid_start(pid):
    try:
        p = subprocess.Popen(["ps", "-o", "lstart=", "-p", str(pid)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = p.communicate(timeout=2)
        return out.decode().strip() or None
    except Exception:
        return None


def _actor(aid, kind, w, liveness, evidence, age, conflict, source, slept):
    if slept:                                      # rider: nothing survives a sleep
        liveness = "unknown"
        evidence = evidence + " (measured across a sleep)"
    return {
        "id": aid, "kind": kind,
        "label": (w or {}).get("label") or aid,
        "read_only": (w or {}).get("read_only"),
        "liveness": liveness, "evidence": evidence,
        "last_seen_age_s": age, "conflict": conflict, "source": source,
    }


def _session_actors(lane_slugs: list, now_ts: float, slept: bool) -> list:
    """Transcripts are the only signal for sessions and subagents."""
    found = []
    try:
        dirs = list(os.scandir(PROJECTS_DIR))
    except OSError:
        return found
    for entry in dirs:
        if not entry.is_dir():
            continue
        lane_id = _match_lane(entry.name, lane_slugs)
        if lane_id is None:
            continue
        for path, kind in _transcripts(Path(entry.path)):
            mtime, _size = _mtime_size(path)
            if mtime is None:
                continue
            age = max(0, int(now_ts - mtime))
            if age > (SUBAGENT_WINDOW_S if kind == "subagent" else ACTOR_WINDOW_S):
                continue
            if age < LIVE_S:
                liveness, word = "live", "wrote"
            elif age < SESSION_WARM_S:
                liveness, word = "warm", "last wrote"
            else:
                liveness, word = "cold", "quiet for"
            # subagent stems all start "agent-", so key them from the tail
            sid = path.stem[:8] if kind == "session" else path.stem[-10:]
            evidence = "transcript %s %s%s" % (
                word, _fmt_age(age), " ago" if liveness != "cold" else "")
            actor = _actor("%s:%s" % (kind, sid), kind,
                           {"label": "%s %s" % (kind, sid)},
                           liveness, evidence, age, False, "transcript", slept)
            found.append((lane_id, actor))
    return found


def _transcripts(root: Path):
    try:
        for f in root.glob("*.jsonl"):
            yield f, "session"
        for f in root.glob("*/subagents/*.jsonl"):
            yield f, "subagent"
        for f in root.glob("*/subagents/*/*.jsonl"):
            yield f, "subagent"
    except OSError:
        return


# ---------------------------------------------------------------- snapshot

def _lane_jobs(pool, ctx, repo, wt, now):
    """Every git call a lane needs, submitted as independent parallel jobs.

    Nothing here depends on another call's result — a serial chain would put
    Animarek's 1.4s main-tree `status` on the critical path of everything else.
    """
    scope = "lane:%s:%s" % (repo["key"], wt["name"])
    branch = wt.get("branch") or wt.get("path")
    base = repo["base"]
    jobs = {"status": pool.submit(_status_counts, ctx, wt["path"], scope),
            "branchlog": pool.submit(_log_entries, ctx, repo["path"], branch, scope, now)}
    if wt["kind"] == "worktree":
        jobs["ab"] = pool.submit(_ahead_behind, ctx, repo["path"], base, branch, scope)
        jobs["range"] = pool.submit(_log_entries, ctx, repo["path"],
                                    "%s..%s" % (base, branch), scope, now)
        jobs["diff"] = pool.submit(_git, ctx, repo["path"],
                                   ["diff", "--name-only", "%s...%s" % (base, branch)], scope)
    return jobs


def _result(jobs, key, default=None):
    fut = jobs.get(key)
    if fut is None:
        return default
    try:
        return fut.result()
    except Exception:
        return default


def _assemble_lane(ctx, repo, wt, jobs, purposes, now):
    branch = wt.get("branch") or wt.get("path")
    ahead, behind = (0, 0) if wt["kind"] == "main" else _result(jobs, "ab", (None, None))
    st = _result(jobs, "status") or {}
    merged = None if ahead is None else ahead == 0   # ancestor of base <=> 0 ahead

    history = _result(jobs, "range", []) if ahead else []
    branchlog = _result(jobs, "branchlog", []) or []
    if not history:
        history = branchlog

    head = None
    if branchlog:
        h = branchlog[0]
        age = _age_of(_utc(h["when"]), now) if h["when"] else None
        head = {"sha": h["sha"], "subject": h["what"], "when": h["when"], "age_s": age}

    changed = [x for x in (_result(jobs, "diff") or "").splitlines() if x]

    purpose = purpose_src = None
    raw_purpose = ""
    if branch in purposes:
        purpose, raw_purpose = purposes[branch]
        purpose_src = "lane-file"
    elif ahead and history:
        purpose = history[-1]["what"][:72]
        purpose_src = "derived"

    lane = {
        "id": "%s:%s" % (repo["key"], wt["name"]),
        "kind": wt["kind"], "name": wt["name"],
        "purpose": purpose, "purpose_source": purpose_src,
        "path": wt["path"], "branch": branch,
        "head": head,
        "git": {"ahead": ahead, "behind": behind,
                "modified": st.get("modified"), "staged": st.get("staged"),
                "untracked": st.get("untracked"), "merged_into_base": merged},
        "age_days": None if not head or head["age_s"] is None else head["age_s"] // 86400,
        "size_bytes": None,
        "actors": [], "overlaps": [], "overlap_files": [],
        "history": history[:HISTORY_CAP],
    }
    lane["verdict"], lane["verdict_reason"] = _verdict(lane, raw_purpose)
    if purpose:
        lane["history"].insert(0, {
            "when": None, "age": "lane opened", "sha": None,
            "who": "dispatch" if purpose_src == "dispatch" else purpose_src or "git",
            "what": "Lane opened: %s" % purpose})
    lane["_changed"] = changed
    return lane


def _overlaps(lanes: list) -> None:
    for i, a in enumerate(lanes):
        for b in lanes[i + 1:]:
            shared = sorted(set(a.get("_changed") or []) & set(b.get("_changed") or []))
            if not shared:
                continue
            for one, other in ((a, b), (b, a)):
                one["overlaps"].append(other["id"])
                merged = sorted(set(one["overlap_files"]) | set(shared))
                one["overlap_files"] = merged[:OVERLAP_CAP]
                if len(merged) > OVERLAP_CAP:
                    one["overlap_files"].append("+%d more" % (len(merged) - OVERLAP_CAP))


def _repo_head_activity(ctx, repo, now):
    scope = "repo:" + repo["key"]
    head = None
    entries = _log_entries(ctx, repo["path"], repo["base"], scope, now)
    if entries:
        h = entries[0]
        head = {"sha": h["sha"], "subject": h["what"], "when": h["when"],
                "author": h["who"], "age_s": _age_of(_utc(h["when"]), now) if h["when"] else None}
    out = _git(ctx, repo["path"], ["log", repo["base"], "--since=24.hours",
                                   "--format=%cI%x00%an"], scope)
    c1 = c24 = 0
    actors = set()
    for line in (out or "").splitlines():
        parts = line.split("\0")
        when = _utc(parts[0]) if parts else None
        if when is None:
            continue
        c24 += 1
        actors.add(parts[1] if len(parts) > 1 else "?")
        if (now - when).total_seconds() < 3600:
            c1 += 1
    return head, {"commits_1h": c1, "commits_24h": c24, "actors_24h": len(actors)}


def build_snapshot() -> dict:
    """Contract schema 1. Never raises: failures become errors[] and null fields."""
    t0 = time.monotonic()
    ctx = _Ctx(deadline=t0 + BUDGET_S)
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    slept = _slept()
    repos_out = []
    try:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            plans = []
            for repo in REPOS:
                if not os.path.isdir(repo["path"]):
                    ctx.error("repo:" + repo["key"], "path does not exist: %s" % repo["path"])
                    continue
                wts = _worktrees(ctx, repo)
                if not wts:
                    ctx.error("repo:" + repo["key"], "not a git repo (or worktree list failed)")
                    continue
                purposes = _lane_file_purposes(repo["path"])
                meta = pool.submit(_repo_head_activity, ctx, repo, now)
                jobs = [(wt, _lane_jobs(pool, ctx, repo, wt, now)) for wt in wts]
                plans.append((repo, purposes, meta, jobs))
            for repo, purposes, meta, jobs in plans:
                lanes = []
                for wt, job in jobs:
                    try:
                        lanes.append(_assemble_lane(ctx, repo, wt, job, purposes, now))
                    except Exception as exc:              # never raise out of here
                        ctx.error("repo:" + repo["key"], "lane build failed: %r" % exc)
                try:
                    head, activity = meta.result()
                except Exception as exc:
                    head, activity = None, {"commits_1h": None, "commits_24h": None,
                                            "actors_24h": None}
                    ctx.error("repo:" + repo["key"], "head/activity failed: %r" % exc)
                _overlaps([l for l in lanes if l["kind"] == "worktree"])
                work = [l for l in lanes if l["kind"] == "worktree"]
                repos_out.append({
                    "key": repo["key"], "name": repo["name"], "path": repo["path"],
                    "base": repo["base"], "head": head, "activity": activity,
                    "totals": {
                        "lanes": len(lanes),
                        "unmerged_commits": sum(l["git"]["ahead"] or 0 for l in work),
                        "uncommitted_files": sum((l["git"]["modified"] or 0)
                                                 + (l["git"]["staged"] or 0)
                                                 + (l["git"]["untracked"] or 0) for l in work),
                    },
                    "lanes": lanes,
                })
        _attach_actors(ctx, repos_out, now_ts, slept)
    except Exception as exc:
        ctx.error("snapshot", "unexpected failure: %r" % exc)
    for r in repos_out:
        for lane in r["lanes"]:
            lane.pop("_changed", None)
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "slept": slept,
        "errors": ctx.errors,
        "repos": repos_out,
    }


def _attach_actors(ctx, repos_out, now_ts, slept) -> None:
    index, lane_slugs = {}, []
    for r in repos_out:
        for lane in r["lanes"]:
            index[lane["id"]] = lane
            lane_slugs.append((_slug(lane["path"]), lane["id"]))
    by_path = {os.path.realpath(l["path"]): lid for lid, l in index.items()}

    try:
        with state.locked():                       # rider 4
            workers = state.read_workers()
    except Exception as exc:
        workers = []
        ctx.error("actors", "workers.json unreadable: %r" % exc)

    orphans = []
    for w in workers:
        if not isinstance(w, dict):
            continue
        log_mtime, _ = _mtime_size(state.state_dir() / "logs" / ("%s.log" % w.get("id", "")))
        recent = log_mtime is not None and (now_ts - log_mtime) <= ACTOR_WINDOW_S
        if w.get("status") == "done" and not recent:
            continue
        lane_id = None
        if w.get("worktree"):                      # recorded by dispatch — trust it
            lane_id = by_path.get(os.path.realpath(w["worktree"]))
        if lane_id is None and w.get("cwd"):
            lane_id = _match_lane(_slug(w["cwd"]), lane_slugs)
        actor = _worker_actor(w, now_ts, slept)
        if lane_id is None:
            orphans.append("%s (cwd %s)" % (w.get("id"), w.get("cwd")))
            continue
        index[lane_id]["actors"].append(actor)
        lane = index[lane_id]
        if w.get("purpose") and lane["purpose_source"] in (None, "derived"):
            lane["purpose"] = _trim_purpose(w["purpose"])
            lane["purpose_source"] = "dispatch"

    swarm: dict = {}
    for lane_id, actor in _session_actors(lane_slugs, now_ts, slept):
        if actor["kind"] == "subagent":
            swarm.setdefault(lane_id, []).append(actor)
        else:
            index[lane_id]["actors"].append(actor)
    for lane_id, subs in swarm.items():
        subs.sort(key=lambda a: a["last_seen_age_s"])
        index[lane_id]["actors"].extend(subs[:SUBAGENT_ROWS])
        rest = subs[SUBAGENT_ROWS:]
        if rest:                                   # rolled up, never silently dropped
            index[lane_id]["actors"].append(_actor(
                "subagents:%s" % lane_id, "subagent",
                {"label": "+%d more subagents" % len(rest)},
                rest[0]["liveness"],
                "%d further subagent transcripts, newest wrote %s ago" % (
                    len(rest), _fmt_age(rest[0]["last_seen_age_s"])),
                rest[0]["last_seen_age_s"], False, "transcript", slept))

    if orphans:
        ctx.error("actors", "%d unattributed worker%s: %s" % (
            len(orphans), "" if len(orphans) == 1 else "s", "; ".join(orphans[:3])))


# ---------------------------------------------------------------- renderer

_C = {"RESCUE": "31;1", "DIRTY": "33;1", "MERGE": "36;1", "PARKED": "34",
      "SWEEP": "35", "CLEAN": "32", "UNKNOWN": "37;1",
      "live": "32;1", "warm": "33", "cold": "90", "unknown": "37",
      "dim": "90", "head": "1"}


def _paint(text, key, color=True):
    return "\033[%sm%s\033[0m" % (_C[key], text) if color and key in _C else text


def render_text(snapshot: dict, color: bool = True, repo_key=None, quiet: bool = False) -> str:
    out = []
    out.append("%s  schema %s  %s  %dms%s" % (
        _paint("LANE BOARD", "head", color), snapshot.get("schema"),
        snapshot.get("generated_at", "")[:19].replace("T", " ") + "Z",
        snapshot.get("duration_ms", -1),
        _paint("  SLEPT — liveness unknown", "UNKNOWN", color) if snapshot.get("slept") else ""))
    for e in snapshot.get("errors", []):
        out.append(_paint("  ! %s — %s" % (e.get("scope"), e.get("message")), "RESCUE", color))
    for repo in snapshot.get("repos", []):
        if repo_key and repo["key"] != repo_key:
            continue
        head = repo.get("head") or {}
        act, tot = repo.get("activity") or {}, repo.get("totals") or {}
        out.append("")
        out.append("%s  %s" % (_paint(repo["name"].upper(), "head", color),
                               _paint(repo["path"], "dim", color)))
        out.append(_paint("  %s @ %s  %s  (%s ago)" % (
            repo.get("base"), head.get("sha"), (head.get("subject") or "")[:64],
            _fmt_age(head.get("age_s"))), "dim", color))
        out.append(_paint("  %s lanes · %s unmerged commits · %s uncommitted files · "
                          "%s commits/24h by %s" % (
                              tot.get("lanes"), tot.get("unmerged_commits"),
                              tot.get("uncommitted_files"), act.get("commits_24h"),
                              act.get("actors_24h")), "dim", color))
        for lane in repo.get("lanes", []):
            if quiet and lane.get("verdict") == "CLEAN":
                continue
            g = lane.get("git") or {}
            out.append("")
            out.append("  %s  %-22s %-24s %s" % (
                _paint("%-7s" % lane.get("verdict"), lane.get("verdict"), color),
                lane.get("name"), lane.get("branch") or "-",
                _paint("+%s/-%s  %sM %sS %s??  %s" % (
                    g.get("ahead"), g.get("behind"), g.get("modified"), g.get("staged"),
                    g.get("untracked"), _day_label(lane.get("age_days"))), "dim", color)))
            out.append("      %s" % _paint(lane.get("verdict_reason") or "", "dim", color))
            if lane.get("purpose"):
                out.append("      %s" % _paint("purpose: %s  [%s]" % (
                    lane["purpose"], lane.get("purpose_source")), "dim", color))
            else:
                out.append("      %s" % _paint("no purpose recorded", "dim", color))
            for a in lane.get("actors", []):
                out.append("      %s %s  %s" % (
                    _paint("●", a.get("liveness", "unknown"), color),
                    _paint("%-9s" % a.get("liveness"), a.get("liveness", "unknown"), color),
                    "%s — %s%s" % (a.get("label"), a.get("evidence"),
                                   _paint("  [CONFLICT]", "RESCUE", color)
                                   if a.get("conflict") else "")))
            if lane.get("overlaps"):
                out.append("      %s" % _paint("overlaps %s on %d file(s): %s" % (
                    ", ".join(lane["overlaps"]), len(lane["overlap_files"]),
                    ", ".join(os.path.basename(f) for f in lane["overlap_files"][:4])),
                    "DIRTY", color))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(prog="lanes", description="Lane Board — one-shot snapshot")
    ap.add_argument("--json", action="store_true", help="print the raw snapshot")
    ap.add_argument("--repo", help="only this repo key")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="only lanes whose verdict is not CLEAN")
    a = ap.parse_args()
    snap = build_snapshot()
    if a.json:
        print(json.dumps(snap, indent=1))
        return
    color = not a.no_color and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    print(render_text(snap, color=color, repo_key=a.repo, quiet=a.quiet))


if __name__ == "__main__":
    main()
