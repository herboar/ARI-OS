"""ARI-OS SessionStart hook — auto-emits a regioned brain context block.

Invoked by Claude Code (or any harness) on session start. Behaviour:

1. Resolve the working directory (priority: ``$ARI_OS_CWD`` > ``$PWD``
   > ``os.getcwd()``). The cwd feeds the tunnel posture so the block
   stays local to the user's current project.
2. If no brain DB exists at the resolved path, emit nothing and exit 0.
   A missing DB is a fresh install, not a failure — never block the
   consumer.
3. Otherwise, run the hybrid retrieval pipeline (``cortex retrieve``)
   against a synthesised SessionStart query — derived from the cwd
   basename and any active skill hint in ``$ARI_OS_SKILL``. Print the
   rendered regioned block to stdout.

This module is the public, scrubbed port of the dynamic context search
emission that powers the private engine's auto-context on session open.

Exit codes:
- 0 — block printed, or nothing to print (missing DB / empty corpus).
- non-zero — reserved for unrecoverable internal errors; printed to
  stderr so the harness can log without breaking the session.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _cwd() -> str:
    """Resolve the session cwd from env or process state."""
    return (
        os.environ.get("ARI_OS_CWD")
        or os.environ.get("PWD")
        or os.getcwd()
    )


def _brain_query(cwd: str, skill: str | None) -> str:
    """Build the SessionStart retrieval query from the cwd + active skill.

    The query is a *narrow, project-local* probe — we want whatever the
    brain already knows about this workspace, not a global sweep. Skill
    hints (e.g. "systematic-debugging") bias the query toward the kind
    of context that skill would surface.
    """
    base = Path(cwd).name or "workspace"
    if skill:
        # Strip namespacing (e.g. "superpowers:brainstorming" -> "brainstorming").
        skill = skill.split(":")[-1]
        return f"{base} {skill} context"
    return f"{base} project context"


def _emit_block(cwd: str, query: str, *, harness: str | None = None) -> str:
    """Run the cortex retrieve pipeline for *query* against *cwd*'s brain.

    Returns the rendered regioned block (possibly empty if the brain has
    no matching chunks). Never raises — backend errors yield a one-line
    fallback so the consumer's session is never blocked.
    """
    from ari_os.tools.cortex import config as _config
    from ari_os.tools.cortex import retrieve as _retrieve
    from ari_os.tools.cortex.embed import EmbedClient, default_embed_client

    db = _config.brain_db_path()
    if not db.exists():
        return ""

    try:
        result = _retrieve.retrieve(
            db,
            query=query,
            embed_client=default_embed_client(),
            cwd=cwd,
            mode="default",
            posture="tunnel",
        )
    except Exception as e:  # never block the session
        return f"## Brain — retrieval failed: {e}"

    chunks = _retrieve.filter_by_harness(result.chunks, harness=harness)
    if not chunks:
        return ""
    return _retrieve.format_for_harness(
        chunks,
        harness=harness,
        mode="default",
        sufficiency=result.sufficiency,
        reason=result.reason,
        posture_offer=result.posture_offer,
    )


def run(cwd: str | None = None, *, skill: str | None = None,
        harness: str | None = None) -> str:
    """Entry point used by both the CLI body and the test suite.

    Pure function (modulo env): takes a cwd + optional skill/harness,
    returns the rendered block (possibly empty). Never raises.
    """
    target_cwd = cwd or _cwd()
    query = _brain_query(target_cwd, skill or os.environ.get("ARI_OS_SKILL"))
    return _emit_block(target_cwd, query, harness=harness)


def main(argv: list[str] | None = None) -> int:
    """CLI body — invoked as ``python -m ari_os.hooks.session_start_cortex``.

    Honours ``--cwd``, ``--skill``, ``--harness``, and ``--json`` for
    machine-readable output (used by tests + tooling). ``argv``
    defaults to ``sys.argv[1:]`` at call time; pass a list explicitly
    for hermetic tests.
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="session_start_cortex",
        description="Emit the ARI-OS regioned brain context block for a session start.",
    )
    ap.add_argument("--cwd", default=None, help="Working directory (overrides $PWD).")
    ap.add_argument("--skill", default=None, help="Active skill hint (biases the query).")
    ap.add_argument(
        "--harness",
        default=None,
        choices=("claude", "codex", "kimi"),
        help="Filter to skills compatible with the given harness.",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON envelope {cwd, query, block} on a single line.",
    )
    import sys as _sys
    # When no argv is passed, fall back to ``sys.argv[1:]`` only if it
    # looks like the program was actually invoked through a CLI (i.e. the
    # first arg ends with our module name). Otherwise (test harnesses,
    # library callers) treat the call as arg-less.
    if argv is not None:
        args = ap.parse_args(argv)
    else:
        try:
            invoked_as = Path(_sys.argv[0]).name
        except (IndexError, AttributeError):
            invoked_as = ""
        if "session_start_cortex" in invoked_as:
            args = ap.parse_args(_sys.argv[1:])
        else:
            args = ap.parse_args([])

    cwd = args.cwd or _cwd()
    skill = args.skill or os.environ.get("ARI_OS_SKILL")
    try:
        block = _emit_block(
            cwd,
            _brain_query(cwd, skill),
            harness=args.harness,
        )
    except Exception as e:
        print(f"session_start_cortex: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "cwd": cwd,
            "query": _brain_query(cwd, skill),
            "block": block,
        }))
        return 0

    if block:
        print(block)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
