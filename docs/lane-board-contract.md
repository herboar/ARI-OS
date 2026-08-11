# Lane Board — interface contract

**Version:** 2 · 2026-08-11
**Status:** ACTIVE — additive lineage + night-pending.
**v1 note:** Schema 1 fields remain valid; v2 adds lineage + cockpit extras. Three lanes build against this simultaneously.
Do not change this file. If it is wrong, write a question file and stop — do not "fix" it locally,
because two other workers are compiling against it.

## Module ownership — do not touch another lane's files

| Lane | Owns (exclusively) | May read |
|---|---|---|
| `lb-core` | `ari_os/tools/lanes.py` (new), `ari_os/tools/lane_fixtures.py` (new) | everything |
| `lb-dispatch` | `ari_os/tools/dispatch.py`, `ari_os/tools/state.py` | everything |
| `lb-ui` | `ari_os/tools/monitor.py`, `ari_os/tools/lane_render.py` (new), `ari_os/tools/lane_assets.py` (new) | everything |

No lane edits a file owned by another. If you believe you must, that is a question-file event.

## Golden rule

**Git is the only load-bearing signal.** Everything else — pid, mtime, heartbeat, declaration — is
inference and must be rendered as such, always with its evidence string. A row that claims something
is fine when it is not is the worst possible outcome. `UNKNOWN` is a first-class state; never fold it
into `idle` or `ok`.

## Repos

Discovered from a constant in `lanes.py`, not from config:

```python
REPOS = [
    {"key": "animarek", "name": "Animarek",
     "path": "/Users/mmarek/Desktop/ClaudeCode_Projects/EA_Animarek", "base": "main"},
    {"key": "xfactor",  "name": "X-Factor",
     "path": "/Users/mmarek/Desktop/ClaudeCode_Projects/EA-xfactor",  "base": "main"},
]
```

A missing or non-repo path is not fatal: emit an entry in `errors[]` and skip it.

## `build_snapshot() -> dict`

Pure read. Never writes to any repo. Never mutates `workers.json`.

```jsonc
{
  "schema": 1,
  "generated_at": "2026-08-08T04:12:00+00:00",   // ISO8601 UTC
  "duration_ms": 84,
  "slept": false,          // wall-clock delta exceeded monotonic delta since last call
  "errors": [ {"scope": "repo:xfactor", "message": "git timed out after 5s"} ],
  "repos": [ Repo ]
}
```

### Repo

```jsonc
{
  "key": "xfactor",
  "name": "X-Factor",
  "path": "/Users/.../EA-xfactor",
  "base": "main",
  "head": { "sha": "5858067b", "subject": "docs: cut the Eden bug report", 
            "when": "2026-08-08T02:11:00+00:00", "author": "Matias Marek", "age_s": 4260 },
  "activity": { "commits_1h": 0, "commits_24h": 14, "actors_24h": 2 },  // from git log, S5.1
  "totals": { "lanes": 6, "unmerged_commits": 45, "uncommitted_files": 26 },
  "lanes": [ Lane ]
}
```

`activity` exists because "is anything stepping on anything" is a diff-over-time question, and
`git log` answers it as a fact with zero inference. `actors_24h` = distinct committer names.

### Lane

One per worktree. The repo's **main working tree is always a lane**, always first, `kind: "main"`.

```jsonc
{
  "id": "xfactor:reel-covers",         // "<repo key>:<name>", stable, used as a DOM key
  "kind": "main" | "worktree",
  "name": "reel-covers",               // basename of the worktree path, or "main"
  "purpose": "Reel covers for phone-drive + survival-mode",  // or null
  "purpose_source": "dispatch" | "lane-file" | "derived" | null,
  "path": "/Users/.../.claude/worktrees/reel-covers",
  "branch": "agent/reel-covers",
  "head": { "sha": "df46d671", "subject": "...", "when": "...", "age_s": 604800 },
  "git": {
    "ahead": 0, "behind": 62,
    "modified": 1, "staged": 0, "untracked": 14,
    "merged_into_base": true
  },
  "verdict": "RESCUE",
  "verdict_reason": "merged into main, but 14 untracked and 1 modified file remain in the worktree",
  "age_days": 7,                       // since the lane's newest commit
  "size_bytes": null,                  // optional, may be null; never block a snapshot on du
  "actors": [ Actor ],
  "overlaps": ["xfactor:dp2-2b2"],     // other lane ids sharing changed files
  "overlap_files": ["content-engine/tools/slide_editor/render.js"],  // capped at 12, then "+N more"
  "history": [ HistoryEntry ]
}
```

### Verdict — git-derived ONLY

Liveness must **never** influence the verdict. (Decision Q1=c: the git column is unconditional and
unsuppressable; liveness is an annotation.) This exists because in a liveness-gated design an
`rclone` backup touching a stranded lane keeps it "warm" and silences its RESCUE row forever.

Precedence, first match wins:

| Verdict | Condition | Meaning |
|---|---|---|
| `RESCUE` | `untracked + modified + staged > 0` **and** (`merged_into_base` **or** `ahead == 0`) | Work exists here that git will never see. Highest priority. |
| `DIRTY` | `untracked + modified + staged > 0` and `ahead > 0` | Uncommitted work alongside unmerged commits. |
| `MERGE` | `ahead > 0`, nothing uncommitted | Finished work nobody integrated. |
| `PARKED` | `purpose` matches `/\bPARKED\b/i` | Deliberately on hold. Never nag. |
| `SWEEP` | `merged_into_base`, `ahead == 0`, nothing uncommitted, `kind == "worktree"` | Safe to remove. **Never rendered as a runnable command.** |
| `CLEAN` | anything else | Nothing to do. |

`kind == "main"` never gets `SWEEP`. For main, `RESCUE`/`DIRTY` are reported but a main tree with
untracked files is normal — see the `noise_floor` note below.

**Noise floor.** X-Factor's main tree carries ~1,352 untracked files and Animarek's ~160. An
untracked count on a MAIN lane is therefore permanently non-actionable: for `kind == "main"`, compute
`untracked` but **do not** let it produce `RESCUE`/`DIRTY`. Only worktree lanes escalate on untracked.

### Actor

```jsonc
{
  "id": "w-3f2a-editor-ux" | "session:8d19c0",
  "kind": "worker" | "session" | "subagent",
  "label": "editor-ux-batch",
  "read_only": false,           // from the recorded dispatch flag; null if unknown
  "liveness": "live" | "warm" | "cold" | "unknown",
  "evidence": "pid 41022 alive + log wrote 8s ago",   // MANDATORY, never empty, never null
  "last_seen_age_s": 8,
  "conflict": false,            // signals disagree; render as a contradiction, do not resolve
  "source": "ari-os-log" | "transcript" | "pid" | "recorded"
}
```

**`evidence` is mandatory.** A liveness dot may never render without its evidence string beside or
beneath it. This is the rule that makes the board auditable by eye: when it is wrong, Mati can see
*why* rather than merely losing trust in it.

### Liveness rules

Primary source for `kind == "worker"` is **`~/.ari-os/logs/<worker-id>.log`** — mtime and size.
This is first-party: ARI-OS writes it, nothing upstream can change it. Use transcripts only for
sessions and subagents, where nothing first-party exists.

| Condition | liveness |
|---|---|
| worker pid alive **and** log mtime < 120s | `live` |
| worker pid alive **and** log mtime ≥ 120s | `warm` (evidence must say "pid alive, log quiet 6m") |
| worker pid dead **and** log mtime ≥ 120s | `cold` |
| worker pid dead **but** log mtime < 30s | `cold` + `conflict: true` — render the contradiction verbatim, never pick a winner |
| session transcript mtime < 120s | `live` |
| session transcript mtime < 30min | `warm` |
| otherwise | `cold` |
| `snapshot.slept == true` | **every** actor forced to `unknown`, evidence suffixed `" (measured across a sleep)"` |

PID reuse: if a recorded `pid_start` exists, compare it to `ps -o lstart= -p <pid>`; on mismatch the
pid is not ours → `cold`, evidence `"pid 41022 belongs to a different process now"`.

### Mapping actors to lanes

- Workers with a **recorded** `worktree` field (new, from `lb-dispatch`): exact path match. Trust it.
- Everything else: forward-encode each known lane path into the `~/.claude/projects/` slug form
  (`/` → `-`) and prefix-match, **longest match wins**, applied uniformly to transcripts AND worker
  cwds. **Never reverse-decode a slug** — the encoding is many-to-one (`/`, `_`, `.` all collapse to
  `-`), so decoding is guessing.
- An actor that matches nothing goes into `snapshot.errors[]` as an `unattributed` note **and** is
  surfaced in the UI in an "Elsewhere" line. Never silently dropped.

### HistoryEntry — the plain-English action log

Mati's ask: *"a little collapsible report of all the actions that were taken on that worktree in
plain english."* Commit subjects already are that; use them.

```jsonc
{ "when": "2026-08-06T16:32:00+00:00", "age": "2d ago", "sha": "2a4b7e84",
  "who": "Matias Marek", "what": "merge: reel-cover decks + dopamine-page authoring sources" }
```

Source: `git log <base>..<branch>` for worktree lanes (newest first, cap 25). If `ahead == 0` because
the lane merged, fall back to `git log -25 <branch>`. Prepend a synthetic first entry when a purpose
is known: `{"what": "Lane opened: <purpose>", "who": "dispatch", ...}`.

### Lane naming and purpose

Real lane names on this machine include `agent-a8d67571bf14b1fd2` and `recursing-lumiere-4c7011` —
neither tells Mati anything. `purpose` resolution order:

1. The `purpose` recorded by `dispatch start --purpose "..."` (new, `lb-dispatch`).
2. A `purpose`/`note` field in the repo's `.claude/agent-lanes.json`, if present and matching by branch.
3. **Derived**: the subject of the lane's oldest commit ahead of base, truncated to 72 chars.
4. `null` → the UI shows the branch name and a muted `no purpose recorded`.

## Engineering riders — mandatory, non-negotiable

These come from five independent adversarial reviews. Violating any is a build failure.

1. **`--no-optional-locks` on EVERY git invocation.** Plain `git status` rewrites `.git/index` and
   takes `index.lock` — against a tree where an agent is mid-commit, that is a real hazard.
   `git --no-optional-locks -C <path> status ...`
2. **`-uall` is forbidden on main; use `--porcelain -unormal`** and count untracked directories as
   one entry, or you will walk 1,352 files on every snapshot.
3. **Never `subprocess.run(timeout=)` alone** — its expiry sends SIGKILL, and a git killed
   mid-index-write strands `index.lock` inside a live agent's worktree. Use `Popen`, `terminate()`
   (SIGTERM), wait up to 2s, then `kill()` only as a last resort.
4. **Read `workers.json` under `state.locked()`.** `read_workers()` swallows every exception and
   returns `[]`, so an unlocked torn read renders as "quiet machine" — a silent false negative.
5. **Total snapshot budget 2.5s.** Per-git-call timeout 5s. On timeout, that field becomes `null`
   and an `errors[]` entry appears; the snapshot still returns. Never raise out of `build_snapshot()`.
6. **The board never executes anything against a repo.** No endpoint mutates git. Ever.
7. **Sleep detection:** persist `time.monotonic()` and `time.time()` between calls in a module-level
   cache. If `wall_delta - monotonic_delta > 60`, set `slept: true` for that snapshot.
8. **stdlib only.** No new dependencies, in any lane.

## Testing without a server

Do **not** start an HTTP server to test. Import and call the functions, write output to a file, and
inspect it:

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from ari_os.tools.lanes import build_snapshot
import json; print(json.dumps(build_snapshot(), indent=1)[:4000])
"
```

`lb-ui` must build against `ari_os/tools/lane_fixtures.py`, NOT against a live `build_snapshot()`.
A frozen fixture is committed at `docs/lane-board-fixture.json` — load that. It is real captured
state and is the contract made concrete. Your renderer must handle every field in it, including the
`conflict: true` actor and the `unknown` liveness case.


## Schema version 2 — lineage + night pending (additive)

### Lane lineage fields

```jsonc
{
  "parent_id": "xfactor:reel-covers" | null,  // null ⇒ parent is main spine
  "parent_branch": "agent/reel-covers" | null,
  "fork_point": { "sha": "abc1234", "age_s": 3600 } | null,
  "depth": 0,                                  // main=0
  "role": "main" | "feature" | "variant" | "review" | "unknown",
  "route_status": "open" | "winner" | "dead_route" | "merged",
  "integration": {
    "target_branch": "main" | "agent/reel-covers",
    "target_lane_id": "xfactor:main" | "xfactor:reel-covers" | null,
    "ahead_of_parent": 3,
    "behind_parent": 0,
    "merged_into_parent": false
  },
  "lineage_source": "declared" | "inferred" | "none",
  "lineage_confidence": "high" | "medium" | "low"
}
```

**Parent resolution order:** (1) declared in workers.json / agent-lanes.json
`parent`/`parent_branch`/`forked_from`; (2) git inference: among live lane tips, prefer
P where `merge-base(C,P) == tip(P)` (C forked from P), deepest such P wins;
(3) fallback parent = main (`parent_id: null`, depth 1). Always expose source + confidence.

**Actions:** MERGE/RESCUE copyable commands for `role: variant` target the **parent**
branch, not main. Only `role: feature` (or parent_id null) suggest merge to main.
`route_status: dead_route` never offers merge-to-main.

**Sorting:** default tree order is DFS by depth under main (features, then variants),
with verdict severity as a secondary key within siblings. UI may still sort by verdict/age/name.

### Snapshot cockpit extras

```jsonc
{
  "schema": 2,
  "night": {
    "pending_session_digests": 4,   // cortex sources /night distill would chew
    "recent_convs": 7,              // session transcripts touched in last 48h
    "evidence": "4 sources ready for tier-0→1; 7 convs active in 48h"
  }
}
```

`night` is read-only inference from `~/.ari-os/brain.db` + `~/.claude/projects` transcript
mtimes. Never blocks the snapshot; failures become `night: null` + `errors[]`.
