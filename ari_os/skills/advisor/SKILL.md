---
name: advisor
description: Use whenever a task will take real focused work (many files, long builds, deep debugging) — dispatch a background worker instead of doing it inline, and keep the main session as an advisor.
---

# Advisor (background-first dispatch)

Your main session is an **advisor seat**, not a doer. Focused execution belongs in a background worker. You spend orchestrator tokens on judgment only.

## Dispatch by default when a task will

- read more than ~5 files, or
- run more than one long build/test, or
- involve deep debugging on one component, or
- consume an uncertain amount of context.

## How to dispatch

1. Write a self-contained brief to a file — use the `/handoff` brief template (the worker has none of this conversation).
2. Create the worker's git lane (~/.claude/CLAUDE.md "Multi-agent git lanes"): `git -C <repo> worktree add .claude/worktrees/<slug> -b agent/<slug>`, and record it in `<repo>/.claude/agent-lanes.json`. File-writing workers NEVER run in the main working tree; `--read-only` workers may.
3. Start a detached background worker:
   `/Users/mmarek/.ari-os/venv/bin/python -m ari_os.tools.dispatch start --executor sonnet --task-file BRIEF.md --cwd <repo>/.claude/worktrees/<slug> --label <slug>`
   (executors: `haiku` | `sonnet` | `opus` | `fable`; `--read-only` for review-only tasks; never at a repo root — worktrees/subdirs only.)
4. Keep advising in the main session. Do NOT also do the work yourself. When the worker reports DONE and passes review, the orchestrator merges `agent/<slug>` back and removes the worktree; workers never merge or push.

Workers run headless with permissions pre-granted — no prompts, fully autonomous inside their `--cwd`.

## advisor vs swarm

In-session fan-out on the Claude subscription with no worktree/monitor needed → `/swarm`. Headless background worker with monitor + escalation → this skill's dispatch.

## What stays with the orchestrator vs. goes where

| Situation | Route | Why |
|---|---|---|
| Architecture, design, tradeoff calls, debugging dead ends | **You (orchestrator)** | Judgment — never delegate planning or final review |
| Tiny edits (< 5 lines, obvious) | **You** | Dispatch overhead > savings |
| Multi-file builds, refactors, implementation | `sonnet` worker | Default workhorse coder, detached |
| Single-file, clear spec, mechanical | `haiku` worker | Cheapest with file access |
| Long heavy work that would bloat this session (hard refactors, big builds, deep reviews) | `opus` worker | Near-frontier capability, far cheaper quota than Fable — the default ceiling |
| Frontier judgment IS the deliverable AND it must run detached (adversarial audit where the verdict is the artifact, cross-system reasoning no single component owns) | `fable` worker | Exception, not a difficulty tier — if Opus would produce the same artifact, use Opus. See Fable quota policy |
| Quick fan-out reads/searches (results needed THIS turn) | in-session Agent tool — **set `model:` explicitly** (`haiku`/`sonnet`) | Subagents inherit the session model by default; an unset model in a Fable session silently burns Fable quota on lookups |
| Second opinion, structured analysis, video analysis | `ask.py -m gemini` | Different model family catches different things |
| Drafts, bulk text, cheap research | `ask.py -m kimi` / `-m minimax` | Text-only, cheaper than Claude (requires keys — see Keys) |

## Fable quota policy

Fable quota is scarce and runs out fast; Opus is right next to it in capability and is the **default ceiling** for delegated work. Since 2026-07-24 the `opus` alias resolves to **Opus 5** (Claude Code ≥2.1.219; older versions get Opus 4.8, still fine) — near-Fable on coding/agentic benchmarks (within ~0.5% on CursorBench, beats Fable on OSWorld) at **half Fable's price**, which makes the down-ladder default even stronger. Escalation ladder:

1. `haiku` — mechanical, single-file, clear spec.
2. `sonnet` — normal implementation. Most dispatches stop here.
3. `opus` — hard refactors, deep debugging, thorough reviews, anything "this needs a strong model." Reach for Opus freely; it is the top tier of normal dispatch and now near-frontier (Opus 5).
4. `fable` — exception, not a tier you climb to by difficulty alone. Use only when frontier judgment itself is the deliverable (adversarial audit, architecture calls baked into execution, cross-system reasoning) AND it must run detached. If Opus would likely produce the same artifact — with Opus 5 that is almost always — use Opus.

**Opus 5 worker briefs:** don't include "verify/double-check your work" scaffolding (Opus 5 self-verifies; the instruction causes redundant loops); it spawns subagents more readily, so keep the don't-nest rule explicit in briefs; outputs run longer by default and lowering effort doesn't shorten them — state length limits explicitly when they matter.

Two standing rules:

- **Don't dispatch Fable because the session is Fable.** The orchestrator seat being Fable is already the judgment spend; workers default down the ladder.
- **In-session Agent calls always set `model:` explicitly.** They inherit the session model when unset — in a Fable session that means every casual search subagent burns Fable quota. This inheritance leak, not deliberate dispatches, is where quota usually evaporates.

## Surfacing worker questions

- `/Users/mmarek/.ari-os/venv/bin/python -m ari_os.tools.dispatch questions` lists open questions.
- `... dispatch answer <label> --answer "..."` composes the answer into the task; re-dispatch with `start`.
- The monitor (`/monitor`, http://localhost:7777) shows running / blocked / done at a glance.

## Worker report statuses (bake into every brief)

- **DONE** — review it (spot-check files, run acceptance).
- **DONE_WITH_CONCERNS** — read concerns; correctness issues get fixed before review.
- **NEEDS_GUIDANCE** — think the decision through yourself, re-dispatch with guidance baked in, same tier.
- **BLOCKED** — more context → re-dispatch; more reasoning → one tier up, **capped at `opus`** (a blocked Opus worker means re-scope, split, or do it yourself — not escalate to Fable); too large → split; plan wrong → revise plan.

## ask.py (text-in/text-out, any provider — no file access; you place results)

```bash
/Users/mmarek/.ari-os/venv/bin/python -m ari_os.tools.ask -m gemini "prompt"
echo "long prompt" | /Users/mmarek/.ari-os/venv/bin/python -m ari_os.tools.ask -m kimi --stdin
```

**Live:** `gemini` (structured output, video, second opinions).
**Pending keys:** `kimi` (long-context, swarm research), `minimax` (cheap multilingual) — activate by adding MOONSHOT_API_KEY / MINIMAX_API_KEY to the `com.ari-os.keys` keychain service.
**Deferred to Froggy:** `gemma` local routes need Ollama; this Mac (8GB M3) can't run it. Froggy (4090, 64GB) can serve Ollama over LAN later.

## When NOT to dispatch

- 2-line edits — just do them.
- Deeply coupled work where every step depends on the last — do it yourself.
- Exploration/debugging before there's a plan — investigate first, dispatch after.
- Already inside a subagent — don't nest.

## Worktree lineage (Ari model)

- Features branch from `main` (or the integration base).
- **Variants and A/B tests branch from the parent feature tip**, not from main:
  `git worktree add .claude/worktrees/<slug> -b agent/<slug> agent/<parent-slug>`.
- Record parent at dispatch: `--from agent/<parent-slug> --role variant`.
- Only one path merges to main; discard losers with `dispatch close --mode dead`.
- Stay on the open feature worktree for the day (session stickiness).
- Worktrees are for code changes, not pure data extraction.
