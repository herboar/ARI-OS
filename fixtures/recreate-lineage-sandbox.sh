#!/usr/bin/env bash
# Recreates fixtures/lineage-sandbox with real parent→child worktrees (Ari model).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="$ROOT/fixtures/lineage-sandbox"
rm -rf "$SANDBOX"
mkdir -p "$SANDBOX" && cd "$SANDBOX"
git init -b main
git config user.email "lane-board@local"
git config user.name "Lane Board Demo"
echo "# lineage-sandbox" > README.md
git add README.md && git commit -m "main: seed lineage sandbox"
echo "base" > base.txt && git add base.txt && git commit -m "main: base stub"

git worktree add .claude/worktrees/broll-unit -b agent/broll-unit
( cd .claude/worktrees/broll-unit
  echo plan > broll-plan.md && git add broll-plan.md && git commit -m "broll: plan"
  echo assets > broll-assets.md && git add broll-assets.md && git commit -m "broll: assets"
  echo draft >> broll-plan.md && git add broll-plan.md && git commit -m "broll: draft"
)
git worktree add .claude/worktrees/broll-dp2 -b agent/broll-dp2 agent/broll-unit
( cd .claude/worktrees/broll-dp2
  echo gif > dp2-gif.md && git add dp2-gif.md && git commit -m "dp2: gif bg"
  echo gate >> dp2-gif.md && git add dp2-gif.md && git commit -m "dp2: gate3"
)
git worktree add .claude/worktrees/broll-editor -b agent/broll-editor agent/broll-unit
( cd .claude/worktrees/broll-editor
  echo ux > editor-ux.md && git add editor-ux.md && git commit -m "editor: unique-bg"
  echo del >> editor-ux.md && git add editor-ux.md && git commit -m "editor: delete slide"
  echo sync >> editor-ux.md && git add editor-ux.md && git commit -m "editor: sync"
)
git worktree add .claude/worktrees/reel-covers -b agent/reel-covers agent/broll-unit
( cd .claude/worktrees/reel-covers
  echo plate > covers.md && git add covers.md && git commit -m "covers: plate"
  echo type >> covers.md && git add covers.md && git commit -m "covers: typography"
)
git worktree add .claude/worktrees/covers-dark -b agent/covers-dark agent/reel-covers
( cd .claude/worktrees/covers-dark
  echo dark > covers-dark.md && git add covers-dark.md && git commit -m "covers: dark a/b"
  echo win >> covers-dark.md && git add covers-dark.md && git commit -m "covers: dark wins"
)
git worktree add .claude/worktrees/covers-bigtype -b agent/covers-bigtype agent/reel-covers
( cd .claude/worktrees/covers-bigtype
  echo big > covers-big.md && git add covers-big.md && git commit -m "covers: bigtype a/b"
)
git worktree add .claude/worktrees/script-parked -b agent/script-parked
( cd .claude/worktrees/script-parked
  echo "PARKED 2026-08-04" > script.md && git add script.md && git commit -m "script: PARKED draft"
)

mkdir -p .claude
cp "$ROOT/fixtures/lineage-sandbox-agent-lanes.json" .claude/agent-lanes.json 2>/dev/null || true
# if template missing, write inline
if [[ ! -f .claude/agent-lanes.json ]]; then
  cat > .claude/agent-lanes.json << 'EOF'
{"lanes":[
 {"branch":"main","note":"Demo main","role":"main"},
 {"branch":"agent/broll-unit","parent":"main","role":"feature","note":"FEATURE · open b-roll unit — linear home for related work"},
 {"branch":"agent/broll-dp2","parent":"agent/broll-unit","role":"variant","note":"VARIANT of broll · GIF backgrounds — merge to broll, not main"},
 {"branch":"agent/broll-editor","parent":"agent/broll-unit","role":"variant","note":"VARIANT of broll · slide editor UX — merge to broll, not main"},
 {"branch":"agent/reel-covers","parent":"agent/broll-unit","role":"feature","note":"WORKTREE under b-roll unit (not off main)"},
 {"branch":"agent/covers-dark","parent":"agent/reel-covers","role":"variant","note":"A/B of reel-covers · dark plates — merge to reel-covers"},
 {"branch":"agent/covers-bigtype","parent":"agent/reel-covers","role":"variant","route_status":"dead_route","note":"DEAD route · bigger type discarded — will not merge"},
 {"branch":"agent/script-parked","parent":"main","role":"feature","note":"PARKED 2026-08-04 · independent of broll line"}
]}
EOF
fi
echo "OK sandbox at $SANDBOX"
git -C "$SANDBOX" worktree list
