"""ARI-OS Cortex — click CLI: ingest, retrieve, dream, wander, distill, mode.

Public port + scrub of the heavy brain's CLI. Subcommands map 1:1 to the
retrieval spine:

- ``ingest`` — index a file or sweep default roots (writes chunks + vectors).
- ``retrieve`` — run the hybrid (FTS+vec, region rerank, divisive-norm,
  kg_expand) pipeline and print the assembled regioned block.
- ``dream`` — run deterministic sleep consolidation plus optional summaries.
- ``wander`` — surface a bounded associative tangent.
- ``distill`` — run a tiered consolidation pass.
- ``mode`` — list / get / set / auto the active cognitive mode for a cwd.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import click

from .config import brain_db_path, state_home
from .db import init_db


@click.group()
def main() -> None:
    """ARI-OS Cortex — heavy single-machine brain."""


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@main.command()
@click.option("--rebuild", is_flag=True, help="Wipe and recreate the brain DB before indexing.")
@click.option("--path", "path_filter", default=None, help="Ingest a single file (auto-detect extension).")
@click.option("--dry-run", is_flag=True, help="Print what would be indexed without writing.")
def ingest(rebuild: bool, path_filter: str | None, dry_run: bool) -> None:
    """Index transcripts (default sweep) or a single file."""
    from .embed import default_embed_client
    from .index import index_file, index_sweep, set_markdown_indexing

    db = brain_db_path()
    if dry_run:
        click.echo(f"[dry-run] would index to {db}")
        return

    # Auto-enable repo-file ingest for single-file invocations on a markdown
    # path; sweep mode stays transcript-only (no surprise repo sweeps).
    if path_filter and Path(path_filter).suffix != ".jsonl":
        set_markdown_indexing(True)

    if rebuild and db.exists():
        db.unlink()
    init_db(db)

    emb = default_embed_client()
    if path_filter:
        p = Path(path_filter)
        layer = "semantic"
        try:
            chunk_ids = index_file(db, p, layer, emb)
        except PermissionError as e:
            click.echo(f"error: {e}", err=True)
            sys.exit(2)
        click.echo(f"indexed: {p} ({len(chunk_ids)} chunks)")
        return

    stats = index_sweep(db, embed_client=emb)
    click.echo(json.dumps(stats))


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------


@main.command()
@click.option("--query", "-q", required=True)
@click.option("--cwd", default=None, help="Working directory (sets the workspace / posture gate).")
@click.option("--branch", default=None)
@click.option("--mode", default="default", show_default=True)
@click.option("--k", "k_vector", default=12, show_default=True, type=int)
@click.option("--token-budget", default=4000, show_default=True, type=int)
@click.option("--kg-expand", is_flag=True, help="Surface chunks that share kg_entities with vec hits.")
@click.option(
    "--harness",
    type=click.Choice(["claude", "codex", "kimi"]),
    default=None,
    help="Filter results to skills compatible with this harness.",
)
@click.option(
    "--posture",
    type=click.Choice(["tunnel", "global", "auto"]),
    default="auto",
    show_default=True,
    help="Retrieval posture: tunnel (workspace-local), global (full corpus), auto.",
)
@click.option("--session-id", default=None)
def retrieve(
    query: str,
    cwd: str | None,
    branch: str | None,
    mode: str,
    k_vector: int,
    token_budget: int,
    kg_expand: bool,
    harness: str | None,
    posture: str,
    session_id: str | None,
) -> None:
    """Run the hybrid retrieval pipeline and print the regioned context block."""
    from . import retrieve as _retrieve

    db = brain_db_path()
    if not db.exists():
        click.echo(f"## Brain — not yet initialised (db missing at {db})")
        return

    try:
        _posture_val: str | None = None
        if posture == "auto":
            from .mode_router import classify_posture
            from .mode_routing import load_routing_config
            try:
                kws = load_routing_config().keywords
            except Exception:
                kws = {}
            pd = classify_posture(query, kws)
            _posture_val = pd.posture
        else:
            _posture_val = posture

        result = _retrieve.retrieve(
            db,
            query=query,
            cwd=cwd,
            branch=branch,
            mode=mode,
            k_vector=k_vector,
            token_budget=token_budget,
            kg_expand=kg_expand,
            posture=_posture_val,
            session_id=session_id,
        )
        chunks = _retrieve.filter_by_harness(result.chunks, harness=harness)
        click.echo(_retrieve.format_for_harness(
            chunks, harness=harness, mode=mode,
            sufficiency=result.sufficiency, reason=result.reason,
            posture_offer=result.posture_offer))
    except Exception as e:  # never block the consumer
        click.echo(f"## Brain — retrieval failed: {e}", err=True)


# ---------------------------------------------------------------------------
# dream / wander / distill / predict / prefetch
# ---------------------------------------------------------------------------


def _db_or_none(label: str) -> Path | None:
    db = brain_db_path()
    if not db.exists():
        click.echo(f"{label}: brain not initialised at {db}")
        return None
    return db


def _consolidation_llm():
    try:
        from . import llm as llm_mod
        from .model_routing import model_for_stage

        return llm_mod.get_llm(model_for_stage("consolidation"))
    except Exception as exc:
        click.echo(f"llm unavailable: {exc}; continuing with llm=off")
        return None


@main.command()
def dream() -> None:
    """Run the local dream pass: distill, decay, queue, and mode hint."""
    db = _db_or_none("dream")
    if db is None:
        return

    from . import dream as dream_mod

    try:
        result = dream_mod.run_dream(
            db,
            llm=_consolidation_llm(),
            output_dir=state_home(),
        )
    except Exception as exc:
        click.echo(f"dream: skipped ({exc})")
        return

    summaries = (
        result.session_digests
        + result.daily_syntheses
        + result.weekly_arcs
    )
    click.echo(
        "dream: "
        f"summaries={summaries} "
        f"session={result.session_digests} "
        f"daily={result.daily_syntheses} "
        f"weekly={result.weekly_arcs} "
        f"queue={result.dream_queue_items} "
        f"mode={result.mode}"
    )


@main.command()
@click.option("--focus", required=True, help="Current focus text to wander away from.")
def wander(focus: str) -> None:
    """Surface one associative memory away from the current focus."""
    from . import config

    if not config.wander_enabled(True):
        click.echo("wander off: cortex.wander disabled")
        return

    db = _db_or_none("wander")
    if db is None:
        return

    try:
        from .wander import render_wander_block, wander as run_wander

        result = run_wander(db, focus)
        block = render_wander_block(result)
    except Exception as exc:
        click.echo(f"wander empty: {exc}")
        return

    if block:
        click.echo(block)
    else:
        click.echo("wander empty: no associative chunk surfaced")


@main.command()
@click.option(
    "--tier",
    type=click.Choice(["session", "daily", "weekly"]),
    default="session",
    show_default=True,
    help="Consolidation tier to run.",
)
def distill(tier: str) -> None:
    """Run one tiered distillation pass."""
    db = _db_or_none("distill")
    if db is None:
        return

    from . import distill as distill_mod

    llm = _consolidation_llm()
    try:
        if tier == "session":
            created = distill_mod.distill_session_to_digest(
                db, output_dir=state_home(), llm=llm
            )
        elif tier == "daily":
            created = distill_mod.distill_daily_synthesis(
                db, output_dir=state_home(), llm=llm
            )
        else:
            created = distill_mod.distill_weekly_arc(
                db, output_dir=state_home(), llm=llm
            )
    except Exception as exc:
        click.echo(f"distill: skipped ({exc})")
        return

    click.echo(f"distill: tier={tier} distilled={created}")


@main.command()
@click.option("--cluster", is_flag=True, help="Run one predictive clustering sweep.")
@click.option("--signals", is_flag=True, help="Detect and write predictive growth signals.")
@click.option("--cwd", default=None, help="Working directory to prefetch for after prediction.")
def predict(cluster: bool, signals: bool, cwd: str | None) -> None:
    """Run deterministic predictive maintenance: cluster sweep, signals, or cwd prefetch."""
    from . import config

    if not config.predictive_enabled(True):
        click.echo("predict off: cortex.predictive disabled")
        return

    db = _db_or_none("predict")
    if db is None:
        return

    ran = False
    if cluster:
        ran = True
        try:
            from .predictive.clusterer import run_cluster_sweep

            run_id = run_cluster_sweep(db)
        except Exception as exc:
            click.echo(f"predict: cluster skipped ({exc})")
        else:
            click.echo(f"predict: cluster run_id={run_id}")

    if signals:
        ran = True
        try:
            from .predictive.signal_writer import write_signals
            from .predictive.trend import detect_growth_signals

            found = detect_growth_signals(db)
            out = write_signals(found)
        except Exception as exc:
            click.echo(f"predict: signals skipped ({exc})")
        else:
            click.echo(f"predict: signals={len(found)} path={out}")

    if cwd:
        ran = True
        try:
            from .predictive.prefetch import prefetch_for_cwd

            block = prefetch_for_cwd(db, cwd)
        except Exception as exc:
            click.echo(f"predict: prefetch skipped ({exc})")
        else:
            click.echo(block or "")

    if not ran:
        click.echo("predict: nothing selected; use --cluster, --signals, or --cwd <dir>")


@main.command()
@click.option("--cwd", required=True, help="Working directory to prefetch memories for.")
def prefetch(cwd: str) -> None:
    """Print a bounded workspace pre-fetch block for a cwd."""
    from . import config

    if not config.predictive_enabled(True):
        click.echo("prefetch off: cortex.predictive disabled")
        return

    db = _db_or_none("prefetch")
    if db is None:
        return

    try:
        from .predictive.prefetch import prefetch_for_cwd

        block = prefetch_for_cwd(db, cwd)
    except Exception as exc:
        click.echo(f"prefetch empty: {exc}")
        return

    click.echo(block or "")


@main.command("tune")
@click.option("--cwd", default=None, help="cwd whose active mode to inspect (default: $PWD).")
def tune(cwd: str | None) -> None:
    """Inspect retrieval tuning: the active mode's rerank weights + how to adjust."""
    from .modes.loader import active_mode_for_cwd, list_modes, load_mode

    name = active_mode_for_cwd(cwd or os.getcwd(), home_dir=state_home())
    params = load_mode(name)
    click.echo(f"active mode: {name}")
    click.echo(f"  region_weights: {params.get('region_weights')}")
    click.echo(f"  tier_weights:   {params.get('tier_weights')}")
    click.echo(
        f"  k_vector={params.get('k_vector')} "
        f"kg_expand={params.get('kg_expand')} "
        f"token_budget={params.get('token_budget')}"
    )
    click.echo(f"available modes: {', '.join(list_modes())}")
    click.echo(
        "Tune by switching modes (`arios cortex mode set <name>`) or editing the "
        "mode YAMLs under modes/. See README -> 'Tuning your brain'."
    )


@main.command("llm")
@click.argument("backend", required=False, type=click.Choice(["ollama", "api", "off"]))
def llm_cmd(backend: str | None) -> None:
    """Get or set the cortex LLM backend (ollama | api | off)."""
    from . import config

    if backend is None:
        click.echo(config._config_value("cortex.llm") or config.DEFAULT_LLM)
        return
    config.set_config_value("cortex.llm", backend)
    click.echo(f"cortex.llm set: {backend}")


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------


@main.command("see")
@click.argument("image", type=click.Path(dir_okay=False, path_type=Path))
def see_command(image: Path) -> None:
    """Describe an image with the local vision bridge when enabled."""
    from . import mcp_tools

    result = mcp_tools.see_image(brain_db_path(), str(image))
    click.echo(json.dumps(result, indent=2))


@main.command("lens")
@click.argument("slug")
def lens_command(slug: str) -> None:
    """Retrieve a local LENS card by slug when enabled."""
    from . import mcp_tools

    click.echo(mcp_tools.lens(brain_db_path(), slug).rstrip())


@main.command("ears")
@click.argument("audio_or_url")
def ears_command(audio_or_url: str) -> None:
    """Transcribe local audio or fetch a YouTube transcript when enabled."""
    from . import config
    from .media.media_engines import MediaUnavailable, transcribe_audio, youtube_text
    from .mcp_tools import _cortex_llm_enabled

    if not config.ears_enabled():
        click.echo(
            "cortex.ears is off. Enable it with `ari-os cortex ears on` "
            "after installing local media backends."
        )
        return
    if not _cortex_llm_enabled():
        click.echo(
            "cortex.ears is off because cortex.llm is off. Enable a local "
            "LLM backend before using EARS media ingest."
        )
        return

    try:
        if audio_or_url.startswith(("http://", "https://")):
            text = youtube_text(audio_or_url)
        else:
            text = transcribe_audio(Path(audio_or_url))
    except (MediaUnavailable, ValueError) as exc:
        click.echo(f"cortex.ears unavailable: {exc}")
        return

    if text:
        click.echo(text)
    else:
        click.echo("cortex.ears returned no transcript.")


# ---------------------------------------------------------------------------
# kg
# ---------------------------------------------------------------------------


@main.group()
def kg() -> None:
    """Knowledge graph extraction and read-only inspection."""


@kg.command("extract")
@click.option("--limit", default=None, type=int, help="Maximum chunks to extract this run.")
def kg_extract(limit: int | None) -> None:
    """Populate KG tables from eligible chunks."""
    from . import config
    from .kg.sweep import LLMUnavailable, populate_kg_incremental

    if not config.kg_enabled(False):
        click.echo("kg off: cortex.kg disabled")
        return

    db = _db_or_none("kg extract")
    if db is None:
        return

    try:
        stats = populate_kg_incremental(db, limit=limit)
    except LLMUnavailable as exc:
        click.echo(f"kg extract: skipped ({exc})")
        return
    except Exception as exc:
        click.echo(f"kg extract: skipped ({exc})")
        return

    click.echo(
        "kg extract: "
        f"chunks={stats.chunks_processed} "
        f"entities={stats.entities_upserted} "
        f"relations={stats.relations_upserted} "
        f"skipped={stats.skipped} "
        f"failed={stats.failed}"
    )


@kg.command("list")
@click.option("--kind", default=None, help="Filter entities by kind.")
@click.option("-k", "limit", default=50, show_default=True, type=int)
def kg_list(kind: str | None, limit: int) -> None:
    """List KG entities."""
    db = _db_or_none("kg list")
    if db is None:
        return

    from .mcp_tools import entities

    rows = entities(db, kind=kind, k=limit)
    if not rows:
        click.echo("kg list: empty")
        return
    for row in rows:
        click.echo(
            f"{row['id']}\t{row['kind']}\t{row['name']}\t"
            f"mentions={row['mention_count']}\tconfidence={row['confidence']}"
        )


@kg.command("stats")
def kg_stats() -> None:
    """Print KG entity, relation, and chunk-link counts."""
    db = _db_or_none("kg stats")
    if db is None:
        return

    from .db import connect

    con = connect(db)
    try:
        entity_count = con.execute("SELECT COUNT(*) FROM kg_entity").fetchone()[0]
        relation_count = con.execute("SELECT COUNT(*) FROM kg_relation").fetchone()[0]
        link_count = con.execute("SELECT COUNT(*) FROM kg_entity_chunk").fetchone()[0]
    finally:
        con.close()
    click.echo(f"kg stats: entities={entity_count} relations={relation_count} links={link_count}")


# ---------------------------------------------------------------------------
# mode
# ---------------------------------------------------------------------------


@main.group()
def mode() -> None:
    """Cognitive modes — overlay retrieval parameter sets."""


@mode.command("list")
def mode_list() -> None:
    """List available modes."""
    from .modes.loader import list_modes as _list
    for m in _list():
        click.echo(m)


@mode.command("get")
@click.option("--cwd", default=None, help="cwd to inspect (default: $PWD).")
def mode_get(cwd: str | None) -> None:
    """Print the active mode for the given cwd (or $PWD)."""
    from .modes.loader import active_mode_for_cwd
    click.echo(active_mode_for_cwd(cwd or os.getcwd(), home_dir=state_home()))


@mode.command("set")
@click.argument("name")
@click.option("--cwd", default=None, help="cwd to set the mode for (default: $PWD).")
def mode_set(name: str, cwd: str | None) -> None:
    """Set the active mode for the current cwd (manual; locks out auto-shift)."""
    import time as _time
    from .mode_state import write_manual_lock
    from .modes.loader import list_modes, set_active_mode

    if name not in list_modes():
        raise click.UsageError(f"unknown mode '{name}'. Available: {', '.join(list_modes())}")
    target_cwd = cwd or os.getcwd()
    set_active_mode(target_cwd, name, home_dir=state_home())
    write_manual_lock(state_home(), target_cwd, int(_time.time()) + 1200)  # 20-min manual lock
    click.echo(f"mode set: {name} (cwd={target_cwd})")


@mode.command("auto")
@click.option("--prompt", default=None)
@click.option("--cwd", default=None, help="cwd to decide for (default: $PWD).")
@click.option("--skill", default=None)
@click.option("--session-id", default=None)
def mode_auto(prompt: str | None, cwd: str | None, skill: str | None, session_id: str | None) -> None:
    """Auto-shift cognitive mode from situation signals (universal entry point)."""
    import time as _time
    from .mode_router import Signals, ThrashPolicy, decide_mode, should_switch
    from .mode_routing import load_routing_config
    from .mode_state import load_state, read_manual_lock, save_state
    from .modes.loader import active_mode_for_cwd, list_modes, set_active_mode

    target_cwd = cwd or os.getcwd()
    home = state_home()
    sid = session_id or hashlib.sha1(str(target_cwd).encode()).hexdigest()
    try:
        cfg = load_routing_config()
    except Exception:
        return  # no-op on missing/broken config — never block a prompt

    state = load_state(home)
    sess = dict(state.get(sid) or {})
    now = int(_time.time())
    if skill:
        # Namespaced skills (e.g. "superpowers:brainstorming") — strip the prefix.
        skill = skill.split(":")[-1]
        sess["last_skill"] = skill
        sess["last_skill_at"] = now

    signals = Signals(skill=sess.get("last_skill"), cwd=target_cwd, prompt=prompt)
    current = active_mode_for_cwd(target_cwd, home_dir=home)
    decision = decide_mode(signals, current, cfg)

    if decision.mode not in list_modes():
        state[sid] = sess
        save_state(home, state)
        return

    lock = read_manual_lock(home, target_cwd)
    switch, sess = should_switch(decision, current, sess, now, ThrashPolicy(), manual_lock_until=lock)
    if switch:
        set_active_mode(target_cwd, decision.mode, home_dir=home)
        click.echo(f"🧠 mode → {decision.mode} ({decision.reason})")
    state[sid] = sess
    save_state(home, state)


if __name__ == "__main__":  # pragma: no cover
    main()
