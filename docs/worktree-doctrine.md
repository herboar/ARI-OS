# Worktree doctrine (Ari Leavesley, 2026-08-09)

Source: call notes in EA_Animarek `transcripts/meetings/2026-08-09-ari-leavesley-gemini-notes.md`.
This is the rule set the Lane Board visualizes.

## Rules

1. **Linear feature lanes.** Related work continues on the open feature branch /
   worktree. Do not open three siblings off `main` that each need a separate merge.
2. **Variants fork the feature, not main.** A/B/C experiments:
   `git worktree add .claude/worktrees/<slug>-a -b agent/<slug>-a agent/<parent>`.
   Only one path merges toward main; losers are **dead routes**.
3. **One integration edge to main.** Variants merge to the parent feature first.
   Only the feature merges to main.
4. **Session stickiness.** One worktree for the day on a feature. Reopening from
   main while the feature is ahead wastes tokens and loses context.
5. **Worktrees are for code.** Not for data extraction or pure output. Use
   `--read-only` dispatch (or no worktree) for audits and pulls.
6. **Document per repo.** Keep a short copy of these rules in project CONTEXT
   (or this file) so agents stop inventing parallel main-forks.

## Create / close cheatsheet

```bash
# Feature from main
git worktree add .claude/worktrees/<slug> -b agent/<slug> main

# Variant of an open feature (Ari model)
git worktree add .claude/worktrees/<slug>-a -b agent/<slug>-a agent/<parent-slug>

# Dispatch with lineage recorded
python -m ari_os.tools.dispatch start \
  --executor sonnet --cwd .claude/worktrees/<slug>-a --label <slug>-a \
  --purpose "..." --from agent/<parent-slug> --role variant

# Keep winner: merge into parent (or use close --into parent)
# Discard loser:
python -m ari_os.tools.dispatch close <label> --mode dead
```

## What the Lane Board shows

- Nested Rail / tree Dense: parent arrows, depth, DEAD chips.
- MERGE actions for variants target the **parent**, not main.
- Dead routes never offer merge-to-main.
- `/night` strip: pending session digests + recent convs.
