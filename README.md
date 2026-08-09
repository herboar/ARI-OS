<p align="center">
  <img src="docs/icon.png" alt="ARI-OS" width="360">
</p>

```
┌──────────────────────────────────────────────────────────┐
│ ▢  ░░░░░░░░░░░░░░░░░░░  A R I · O S  ░░░░░░░░░░░░░░░░░░░░│
├──────────────────────────────────────────────────────────┤
│                                                          │
│   An orchestrator-first operating layer for              │
│   Claude Code. You stop typing tasks one at a time       │
│   and start running a background crew you watch.         │
│                                                          │
│   brainstorm → plan → dispatch → watch → ship            │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

Batteries-included: a few declared dependencies, one `install.py`, an optional local model — and Claude Code gains a background crew and a live brain.

## What is this, in plain English?

Out of the box, Claude Code does **one thing at a time**. You give it a task, you
watch it work, you wait, you give it the next task. You are the bottleneck. Your
session fills up with the mess of doing the work, and when it ends, the context
is gone.

ARI-OS changes the shape of the work. Instead of doing tasks yourself in one
chat, you **hand whole tasks to background workers** (cheaper, faster models),
keep your own session free to think and steer, and **watch every worker in a
little dashboard** until it is done. You stop being the typist and become the
director.

Four ideas make that work, and ARI-OS ships all four as installable pieces:

1. **Spec-first thinking.** Talk an idea into a short written spec before any code
   exists, so the work has a target.
2. **Background-first dispatch.** Fire focused work off to a worker and keep
   advising. The worker runs on its own; you review the result.
3. **Continuity.** A handoff format lets a fresh session pick up cold with zero
   loss, so a long job survives across days.
4. **A live memory.** A local brain (Cortex) the workflow writes to and reads
   from — semantically — so work builds on what past sessions decided instead of
   starting cold.

## The shift

```
        BEFORE                          AFTER
   ┌──────────────┐               ┌──────────────┐
   │ you type a   │               │ you define   │
   │ task         │               │ an outcome   │
   │ you wait     │               │ workers run  │
   │ you babysit  │               │ in parallel  │
   │ one at a time│               │ you review   │
   └──────────────┘               └──────────────┘
   bottleneck = you           bottleneck = removed
```

You move from *doing* to *directing*. The grunt work runs on cheap models in the
background. Your attention is spent where humans actually add value: judgment,
taste, and deciding what "done" means.

```
        ┌───────────┐     ┌───────────┐
        │ BRAINSTORM│ ──→ │   PLAN    │
        └───────────┘     └─────┬─────┘
                                │
        ┌───────────┐     ┌─────▼─────┐
        │  REVIEW   │ ←── │ DISPATCH  │
        └─────┬─────┘     └─────┬─────┘
              │                 │ background workers
              │           ┌─────▼─────┐
              │           │  MONITOR  │  localhost:7777
              ▼           └───────────┘
        ┌───────────┐
        │   SHIP    │
        └───────────┘
```

## What you actually get

```
┌─ WHAT YOU GET ───────────────────────────────────────────┐
│ skills    brainstorm · handoff · advisor · teach         │
│ commands  /brainstorm /handoff /dispatch /monitor        │
│ dispatch  detached background workers                    │
│ monitor   System 7 dashboard + color picker              │
│ status    ctx% · model · branch · workers · clock        │
└──────────────────────────────────────────────────────────┘
```

- **Skills** teach your assistant the workflow: `brainstorm` (turn loose thinking
  into a spec), `handoff` (write a resume doc so a new session continues cold),
  `advisor` (dispatch instead of doing it yourself), `teach` (optional one-line
  tips while you learn).
- **Dispatch** spawns a detached worker from a brief and tracks it. It refuses to
  run at a repo root and has a read-only mode, so a worker cannot wander.
  By default, those Claude workers include `--dangerously-skip-permissions` so
  long-running background jobs do not stall on interactive permission prompts.
  This is powerful and should only be used with briefs and worktrees you trust.
  Set `ARI_OS_WORKER_SKIP_PERMISSIONS=0` to omit that flag.
- **Monitor** is a tiny local web dashboard styled like classic Mac OS. It shows
  every worker as running, blocked, or done, surfaces any questions they have,
  and lets you recolor the background.
- **Format** keeps replies skimmable: clear action and decision markers, plus
  best-effort boxes for status.
- **Status line** puts context %, model, branch, worker count, and the clock in
  your footer.

## Memory (Cortex, a live brain)

ARI-OS ships a local brain called **Cortex**. It lives on your own machine as a
single SQLite file (`~/.ari-os/brain.db`), and it makes the workflow build on what
you have already decided instead of starting every session cold.

- **`/remember`** saves a decision, fact, or open thread.
- **`/recall`** pulls the relevant past context before you answer. It **tunnels to
  the project you are in** by default and only widens when you say so, so it is not
  dredging your whole history on every question.
- **`/dream`** consolidates the brain: it dedups and decays old notes and suggests
  tidy-ups. It never deletes a real memory without you.
- **`/morning`** opens a session with your recent threads, open loops, and a
  suggested focus.
- **`/night`** consolidates, reviews what you captured, and writes a carry-forward
  so tomorrow resumes cleanly.

Cognitive **modes** (`default`, `focus`, `wide`, `deep`, `creative`, `recall`,
`synthesis`, and more) tune how recall reranks — region weights, breadth, token
budget — and every so often the brain surfaces a tangential memory from outside
your current focus, a deliberate re-orientation that is off in `focus`. See
[Tuning your brain](#tuning-your-brain).

**Recall is semantic by default.** With a local [Ollama](https://ollama.com) (the
recommended backend) the brain embeds your memories with `nomic-embed-text` and
reranks them by meaning; a small local model (`gemma3:4b`) also consolidates them
between sessions. No Ollama? Recall falls back to ranked keyword search — still
useful, and the brain stays a single file you own. Pick the backend with
`python3 -m ari_os.tools.cortex llm <ollama|api|off>`.

### Optional: audio and video (EARS / LENS)

Off by default. Enable them at install or in the control panel, and Cortex can
take in media as memories: **EARS** turns an audio file into a transcript, **LENS**
turns a video into frame captions. Both use a tool or key you already have, and
the core brain never depends on them.

```bash
python3 -m ari_os.tools.cortex tune             # inspect the active mode + weights
python3 -m ari_os.tools.cortex llm ollama       # set the LLM backend (ollama|api|off)
python3 -m ari_os.tools.arios cortex status     # quick memory settings
python3 -m ari_os.tools.arios cortex ears on    # enable audio ingest (optional)
```

## A typical loop

```bash
# 1. Think it through. The brainstorm skill writes a spec with you.
/brainstorm a rate limiter for the API

# 2. Turn the spec into a plan, then dispatch the build to a worker:
python3 -m ari_os.tools.dispatch start --executor sonnet --effort medium \
    --task-file BRIEF.md --cwd ./worktree --label rate-limit

# 3. Watch it (and any others) in the dashboard:
python3 -m ari_os.tools.monitor          # http://localhost:7777

# 4. If a worker has a question, answer it and let it carry on:
python3 -m ari_os.tools.dispatch questions
python3 -m ari_os.tools.dispatch answer w-1a2b-rate-limit --answer "use a token bucket"

# 5. Review the result, then ship.
```

While that worker builds, your own session stays free. Dispatch two more. You are
running a crew, not waiting on a queue of one.

`--effort` (`low`|`medium`|`high`|`xhigh`|`max`) sets how hard the worker thinks,
independently of which model runs it. On current models this is the sharper of
the two dials: `low` for mechanical work, `medium` for a normal build, `xhigh`
for agentic coding and hard reviews, `max` when correctness outranks cost. Omit
it and the worker inherits Claude Code's default for that model. An unrecognised
value is refused up front rather than silently downgraded.

## Why this is a more powerful way to work

- **Parallelism.** One assistant becomes many workers. Three tasks move at once
  instead of in a line.
- **You stay in judgment mode.** The advisor seat is reserved for the decisions
  only you can make. The typing is delegated.
- **Cheaper.** Grunt work runs on small, fast models. You spend the expensive
  model on thinking, not boilerplate.
- **Reviewable and reversible.** Workers commit per step; the installer backs up
  every change and can be reverted or uninstalled cleanly.
- **It survives time.** The handoff format means a job that outlives one session
  is resumed without losing the thread.

## Install

> **Note on dependencies.** The current heavy generation runs on a small set of
> runtime dependencies (`mcp`, `sqlite-vec`, `scikit-learn`, `httpx`, `pyyaml`,
> `click`, `requests`) installed via `uv pip install -e .`. This supersedes the
> earlier light brain's "pure-stdlib, zero-dependency" design.

```
┌─ INSTALL ────────────────────────────────────────────────┐
│ python3 -m ari_os.install easy wizard                    │
│ read SETUP.md             advanced / by hand             │
│                                                          │
│ backed up · reversible · idempotent                      │
└──────────────────────────────────────────────────────────┘
```

```bash
uv pip install -e .         # install dependencies (or: pip install -e .)
python3 -m ari_os.install   # wire into Claude Code + stand up the brain
```

It copies the skills and commands into your Claude Code directory, registers a
status line and a SessionStart hook, adds a short managed block to your
`CLAUDE.md`, registers the Cortex MCP server, and **stands up an empty local
brain** (pick the recall backend with `--llm ollama|api|off`, default `ollama`;
opt into media with `--ears` / `--lens`). Every change is **backed up and
recorded**, so it is fully reversible. Your existing settings and keys are parsed
and merged, never overwritten. See **[SETUP.md](SETUP.md)** for the manual path.

## Control panel

```
┌─ CONTROL PANEL  (arios) ─────────────────────────────────┐
│ arios keys              which provider keys resolve      │
│ arios theme stipple     monitor background               │
│ arios toggle teach on   flip a feature                   │
│ arios update            in-place update                  │
└──────────────────────────────────────────────────────────┘
```

```bash
python3 -m ari_os.tools.arios keys
python3 -m ari_os.tools.arios theme stipple
python3 -m ari_os.tools.arios cortex status
python3 -m ari_os.tools.arios cortex embeddings auto
python3 -m ari_os.tools.arios cortex mode default
python3 -m ari_os.tools.arios cortex wander on
python3 -m ari_os.tools.arios cortex ears off
python3 -m ari_os.tools.arios cortex lens off
```

`arios cortex status` prints the saved Cortex settings. `arios cortex
embeddings <auto|google|ollama|off>` selects the embedding provider preference.
`arios cortex mode <name>` sets the active cognitive mode. `arios cortex wander
<on|off>` controls tangential recall, while `arios cortex ears <on|off>` and
`arios cortex lens <on|off>` toggle optional audio and video ingest.

## Tuning your brain

The brain reranks recall by cognitive mode. Each mode is a small parameter set:
region weights, tier weights, how many vector results to pull, graph expansion,
and the token budget for the final context block.

```bash
python3 -m ari_os.tools.cortex tune
python3 -m ari_os.tools.cortex mode list
python3 -m ari_os.tools.cortex mode get
python3 -m ari_os.tools.cortex mode set wide
```

Use `python3 -m ari_os.tools.cortex tune` to inspect the active mode. Switch modes
with `... cortex mode set <name>`, or customize the bundled YAML files in
`ari_os/tools/cortex/modes/`.

## Lifecycle

```
┌─ LIFECYCLE ──────────────────────────────────────────────┐
│ python3 -m ari_os.install --update     refresh           │
│ python3 -m ari_os.install --revert     undo last change  │
│ python3 -m ari_os.install --uninstall  remove everything │
└──────────────────────────────────────────────────────────┘
```

## Keys

Keys resolve from environment variables first (for example
`ANTHROPIC_API_KEY`), then the macOS Keychain service `com.ari-os.keys`. Key
values are never printed.

## Single home

ARI-OS is the home for these patterns. The earlier `icm-handoff-protocol` and
`advisor-driven-dev` repos now point here.
