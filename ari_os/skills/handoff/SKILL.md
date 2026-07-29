---
name: handoff
description: Use when a session is ending, getting long (~30+ tool calls / ~50k tokens / ~50% context budget), branching, or work must continue later — writes a HANDOFF.md and/or a worker brief so work resumes with zero loss. Mati prefers handoff over compaction. Default exit is dispatching a background worker; interactive pickup is the fallback.
---

# Handoff (ICM temporal continuity, ARI-OS edition)

A long task outlives one session. This skill writes the bridge. Downstream engine: ARI-OS dispatch + monitor.

## When to trigger (proactively — Claude suggests, doesn't wait)

- ~30+ tool calls or ~50k tokens accumulated, or ~50% of context budget spent
- The conversation has branched onto something materially different
- A task is wrapping up and a logical next phase exists
- Auto-compaction feels close — never compact silently; Mati explicitly prefers handoffs over auto-compaction
- Mati says "hand this off" / "park this" / "sleep on it"

Suggested phrasing:

> This chat is getting heavy — a clean handoff beats auto-compaction. Want me to hand off `<the next thing>`?

## What a HANDOFF.md contains (multi-phase / parked work)

Write to `<workspace>/HANDOFF.md`:

1. **Why parked** — one line.
2. **What this is** — the goal, 2-3 sentences.
3. **Where everything lives** — exact paths to specs, plans, briefs, the repo.
4. **Build status** — a table: each unit + state (done / building / blocked / not started).
5. **Locked decisions** — so the next session does not relitigate them.
6. **What to do on resume** — numbered, ordered.

For a single next slice, skip HANDOFF.md and write just a brief.

## The brief (what a worker actually consumes)

Nothing in the engine defines a brief — this template is the fuel format. Write to `<workspace>/.briefs/<YYYY-MM-DD>-<slug>.md`:

```markdown
# <one-line name>

## Boot
cd <absolute workspace path>

## Lane (multi-agent git rule — ~/.claude/CLAUDE.md "Multi-agent git lanes")
Work in your own worktree, never the main tree: `git worktree add .claude/worktrees/<slug> -b agent/<slug>` (or state explicitly that Mati approved main-tree work for this brief). Declare your lane in `.claude/agent-lanes.json` before the first commit. Stage exact paths only — `git add -A` / `git add .` are forbidden.

## Task
<one line, imperative, no hedging>

## Context
- <non-obvious constraint the worker can't see from the code>
- <recent decision that shapes what "good" means>
- Relevant spec: <path>, sections §N, §M. Locked decisions in §K — do not relitigate.

## Files
- <absolute path>: read | modify | create — <one line on what>

## Acceptance
- <binary, runnable checks — command exits 0, file contains X>

## If blocked
If you hit a decision this brief does not cover, do NOT guess. Write the
question to ~/.ari-os/questions/<label>.md (label = your --label slug):
decision point, options considered, your default if no answer. Then stop.

## When done
Report: what was done, files changed, decisions made, anything surprising.
```

## Dispatch (default exit)

Executor tier: haiku = mechanical (extract, format, boilerplate) · sonnet = standard build (implement, refactor, wire up) · opus = judgment-heavy. When in doubt, one tier up.

Create the worker's lane FIRST (never dispatch into the main working tree — ~/.claude/CLAUDE.md "Multi-agent git lanes"):

```bash
git -C <repo> worktree add .claude/worktrees/<slug> -b agent/<slug>
/Users/mmarek/.ari-os/venv/bin/python -m ari_os.tools.dispatch start \
  --executor sonnet --task-file <brief> --cwd <repo>/.claude/worktrees/<slug> --label <slug>
```

After review the orchestrator merges `agent/<slug>` back and `git worktree remove`s the lane. The worker never merges or pushes.

Watch on the monitor (`/monitor`, http://localhost:7777). Blocked workers surface via
`/Users/mmarek/.ari-os/venv/bin/python -m ari_os.tools.dispatch questions`; answer with
`... dispatch answer <label> --answer "..."` and re-dispatch.

**Interactive fallback** (Mati will pick it up himself): instead of dispatching, print:
"Open a fresh session in `<workspace>`, first message: *Read .briefs/<file> and execute it.*"

## Decisions

Log non-trivial decisions where the repo convention exists (`decisions/log.md`, `D-<scope>-<NN>`), and `/remember` the durable ones into the brain so future sessions recall them.

## The first-turn rule (MANDATORY footer on every handoff/brief)

Every handoff MUST end with this line, verbatim:

> FIRST TURN: read the referenced docs ONE AT A TIME (no parallel tool calls, no batched reads). Parallel reads on the first turn can wedge a new session; sequential reads avoid it.
