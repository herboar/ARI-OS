"""ar.t3 ingest tests — chunker, index, transcript_filter.

Verifies the public, scrubbed port of the heavy brain's ingest spine:
- A fixture CC .jsonl transcript is parsed by ``transcript_filter`` into
  clean user/assistant text segments (tool_use/tool_result noise dropped).
- ``index_file`` writes at least one ``chunk`` row with a region and a
  non-null ``distillation_tier`` (the ar.t3 "tier" surface).
- The default ingest roots target ``~/.claude/projects/**/*.jsonl``.
- Markdown (repo-file) indexing is **off by default** — only a .jsonl
  pass is allowed without an explicit opt-in.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from ari_os.tools.cortex import chunker, config, db, index, transcript_filter


# ---------- fixtures --------------------------------------------------------

class _StubEmbed:
    """Deterministic stand-in for the Ollama EmbedClient.

    Maps each text to a stable, distinct 768-d vector so chunk_id lookups
    are unique per text. Real-shape bytes keep sqlite-vec happy.
    """
    def __init__(self, dim: int = 768) -> None:
        self.dim = dim
        self.calls: list[str] = []

    def embed(self, texts):
        out = []
        for t in texts:
            self.calls.append(t)
            # Stable per-text vector: seed with hash, then expand to dim.
            h = hash(t)
            vec = [0.0] * self.dim
            vec[0] = float(h & 0xFFFF) / 65535.0
            vec[1] = float((h >> 16) & 0xFFFF) / 65535.0
            out.append(vec)
        return out


def _write_cc_transcript(path: Path) -> None:
    """Write a small CC .jsonl with user/assistant text + tool noise.

    Mirrors a real CC transcript shape: each line is a JSON record with
    a top-level ``type`` of "user" or "assistant" and a ``message.content``
    that may be a list of typed blocks (text, tool_use, tool_result).
    """
    records = [
        # Header / non-message records — must be skipped.
        {"type": "file-history-snapshot", "messageId": "ignored"},
        {"type": "summary", "summary": "ignored summary"},
        # Real user turn.
        {
            "type": "user",
            "sessionId": "sess-1",
            "timestamp": "2026-06-13T12:00:00Z",
            "cwd": "/tmp/example",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello cortex, this is a clean user prompt."},
                    # tool_result block — must be dropped.
                    {"type": "tool_result", "tool_use_id": "x", "content": "ignored tool output"},
                ],
            },
        },
        # Real assistant turn.
        {
            "type": "assistant",
            "sessionId": "sess-1",
            "timestamp": "2026-06-13T12:00:05Z",
            "cwd": "/tmp/example",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hi — I am a clean assistant reply with a short code fence."},
                    # tool_use block — must be dropped.
                    {"type": "tool_use", "id": "y", "name": "Bash", "input": {"cmd": "ls"}},
                ],
            },
        },
        # A user turn that is **entirely** tool noise — must yield zero segments.
        {
            "type": "user",
            "sessionId": "sess-1",
            "timestamp": "2026-06-13T12:00:10Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "y", "content": "file listing here"},
                ],
            },
        },
    ]
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def transcript_path(tmp_path: Path) -> Path:
    p = tmp_path / "transcript.jsonl"
    _write_cc_transcript(p)
    return p


@pytest.fixture
def brain_db(tmp_path: Path) -> Path:
    p = tmp_path / "brain.db"
    db.init_db(p)
    return p


@pytest.fixture
def stub_embed() -> _StubEmbed:
    return _StubEmbed()


# ---------- transcript_filter ----------------------------------------------

def test_transcript_filter_yields_only_user_assistant_text(transcript_path):
    segs = transcript_filter.filter_jsonl(transcript_path)
    assert segs, "expected at least one clean segment"
    assert all(s.kind in ("user", "assistant") for s in segs)
    # The two real text messages (one user, one assistant) survived; the
    # all-tool-noise user turn was dropped.
    assert len(segs) == 2
    assert segs[0].kind == "user"
    assert "clean user prompt" in segs[0].text
    assert "tool output" not in segs[0].text  # tool_result text was stripped
    assert segs[1].kind == "assistant"
    assert "clean assistant reply" in segs[1].text
    # tool_use input JSON must not leak into the assistant text.
    assert '"Bash"' not in segs[1].text


def test_transcript_filter_handles_string_content(tmp_path):
    """Some CC records carry ``content`` as a bare string, not a list."""
    p = tmp_path / "string.jsonl"
    p.write_text(json.dumps({
        "type": "user",
        "sessionId": "s2",
        "timestamp": "2026-06-13T00:00:00Z",
        "message": {"role": "user", "content": "Just a string body."},
    }) + "\n")
    segs = transcript_filter.filter_jsonl(p)
    assert len(segs) == 1
    assert segs[0].text == "Just a string body."


def test_transcript_filter_handles_malformed_lines(tmp_path):
    p = tmp_path / "mixed.jsonl"
    p.write_text(
        "not-json\n"
        + json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": "real text"},
        ]}}) + "\n"
    )
    segs = transcript_filter.filter_jsonl(p)
    assert [s.text for s in segs] == ["real text"]


def test_transcript_filter_elides_long_code_fences(tmp_path):
    long_code = "```\n" + "\n".join(f"line {i}" for i in range(200)) + "\n```"
    p = tmp_path / "code.jsonl"
    p.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": f"before\n{long_code}\nafter"},
        ]},
    }) + "\n")
    segs = transcript_filter.filter_jsonl(p)
    assert segs
    assert "[code block elided" in segs[0].text
    assert "after" in segs[0].text


# ---------- index_file (.jsonl path) ---------------------------------------

def test_index_file_creates_chunk_rows_with_region_and_tier(
    brain_db, transcript_path, stub_embed
):
    chunk_ids = index.index_file(
        brain_db, transcript_path, layer="episodic", embed_client=stub_embed
    )
    assert len(chunk_ids) >= 1

    con = db.connect(brain_db)
    try:
        rows = con.execute(
            "SELECT id, region, distillation_tier, session_id, ordinal, text "
            "FROM chunk ORDER BY ordinal"
        ).fetchall()
    finally:
        con.close()

    assert rows, "no chunk rows written"
    for _id, region, tier, _sess, _ord, _text in rows:
        assert region in index.REGIONS, f"unexpected region: {region}"
        assert tier is not None, "distillation_tier must not be NULL"
    # Transcript heuristic: user → wernicke, assistant → broca.
    regions = [r[1] for r in rows]
    assert "wernicke" in regions
    assert "broca" in regions


def test_index_file_persists_session_id_and_created_ts(
    brain_db, transcript_path, stub_embed
):
    index.index_file(brain_db, transcript_path, layer="episodic", embed_client=stub_embed)
    con = db.connect(brain_db)
    try:
        sess_rows = con.execute(
            "SELECT DISTINCT session_id FROM chunk WHERE session_id IS NOT NULL"
        ).fetchall()
        cts_rows = con.execute(
            "SELECT DISTINCT created_ts FROM chunk WHERE created_ts IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    assert sess_rows and sess_rows[0][0] == "sess-1"
    assert cts_rows, "created_ts should be populated from transcript timestamps"


def test_index_file_persists_vectors(
    brain_db, transcript_path, stub_embed
):
    chunk_ids = index.index_file(
        brain_db, transcript_path, layer="episodic", embed_client=stub_embed
    )
    assert chunk_ids
    con = db.connect(brain_db)
    try:
        for cid in chunk_ids:
            row = con.execute(
                "SELECT length(embedding) FROM chunk_vec WHERE rowid = ?", (cid,)
            ).fetchone()
            assert row is not None
            assert row[0] == 768 * 4  # 768 floats × 4 bytes
    finally:
        con.close()


def test_index_file_is_idempotent(
    brain_db, transcript_path, stub_embed
):
    """A second ingest of the same file must not duplicate chunks."""
    index.index_file(brain_db, transcript_path, layer="episodic", embed_client=stub_embed)
    first = brain_db
    index.index_file(brain_db, transcript_path, layer="episodic", embed_client=stub_embed)
    con = db.connect(brain_db)
    try:
        n = con.execute("SELECT count(*) FROM chunk").fetchone()[0]
    finally:
        con.close()
    # 2 clean segments (one user, one assistant); noise-only turn was dropped.
    assert n == 2


# ---------- workspace tagging -----------------------------------------------

@pytest.fixture
def ws_root(tmp_path, monkeypatch) -> Path:
    """Isolated $ARI_OS_HOME plus one configured workspace root with a repo."""
    home = tmp_path / "arios-home"
    home.mkdir()
    monkeypatch.setenv("ARI_OS_HOME", str(home))
    root = tmp_path / "Projects"
    (root / "EA_xfactor").mkdir(parents=True)
    (home / "config.json").write_text(
        json.dumps({"cortex.workspace_roots": [str(root)]})
    )
    return root


def _write_transcript_with_cwds(path: Path, cwds: list[str | None]) -> None:
    """Minimal CC transcript: one clean user record per cwd (None = no cwd)."""
    with path.open("w") as f:
        for i, cwd in enumerate(cwds):
            rec = {
                "type": "user",
                "sessionId": "sess-ws",
                "timestamp": f"2026-08-01T12:00:{i:02d}Z",
                "message": {"role": "user", "content": [
                    {"type": "text", "text": f"turn {i} with enough text to embed"},
                ]},
            }
            if cwd is not None:
                rec["cwd"] = cwd
            f.write(json.dumps(rec) + "\n")


def _source_workspace(brain_db: Path, path: Path) -> str | None:
    con = db.connect(brain_db)
    try:
        row = con.execute(
            "SELECT workspace FROM source WHERE path = ?", (str(path),)
        ).fetchone()
    finally:
        con.close()
    assert row is not None, "source row missing"
    return row[0]


def test_transcript_ingest_tags_workspace_from_record_cwds(
    brain_db, tmp_path, stub_embed, ws_root
):
    p = tmp_path / "ws.jsonl"
    _write_transcript_with_cwds(p, [str(ws_root / "EA_xfactor" / "content-engine")] * 2)
    index.index_file(brain_db, p, layer="episodic", embed_client=stub_embed)
    assert _source_workspace(brain_db, p) == "ea-xfactor"


def test_transcript_ingest_majority_cwd_wins(brain_db, tmp_path, stub_embed, ws_root):
    """One stray record (subagent hop / --cwd override) must not retag the file."""
    p = tmp_path / "mixed.jsonl"
    _write_transcript_with_cwds(p, [
        str(ws_root / "EA_xfactor"),
        str(ws_root / "Other_Repo"),
        str(ws_root / "EA_xfactor"),
    ])
    index.index_file(brain_db, p, layer="episodic", embed_client=stub_embed)
    assert _source_workspace(brain_db, p) == "ea-xfactor"


def test_transcript_ingest_falls_back_to_encoded_dirname(
    brain_db, tmp_path, stub_embed, ws_root
):
    """No record carries a cwd → match the lossy CC project dirname."""
    from ari_os.tools.cortex.workspace_map import encode_cc_project_dirname

    proj_dir = tmp_path / f"{encode_cc_project_dirname(str(ws_root))}-EA-xfactor"
    proj_dir.mkdir()
    p = proj_dir / "nocwd.jsonl"
    _write_transcript_with_cwds(p, [None, None])
    index.index_file(brain_db, p, layer="episodic", embed_client=stub_embed)
    assert _source_workspace(brain_db, p) == "ea-xfactor"


def test_transcript_ingest_outside_roots_stays_untagged(
    brain_db, tmp_path, stub_embed, ws_root
):
    p = tmp_path / "outside.jsonl"
    _write_transcript_with_cwds(p, ["/somewhere/else"])
    index.index_file(brain_db, p, layer="episodic", embed_client=stub_embed)
    assert _source_workspace(brain_db, p) is None


def test_memory_file_ingest_tags_workspace_folder(brain_db, stub_embed, ws_root):
    """$ARI_OS_HOME/memories/<ws>/*.md tags as <ws> (matches --path re-ingest)."""
    mem = config.state_home() / "memories" / "ea-xfactor"
    mem.mkdir(parents=True)
    md = mem / "note.md"
    md.write_text("## Note\na memory worth keeping\n")
    index.set_markdown_indexing(True)
    try:
        index.index_file(brain_db, md, layer="semantic", embed_client=stub_embed)
    finally:
        index.set_markdown_indexing(False)
    assert _source_workspace(brain_db, md) == "ea-xfactor"


def test_markdown_ingest_under_root_tags_workspace(brain_db, stub_embed, ws_root):
    md = ws_root / "EA_xfactor" / "doc.md"
    md.write_text("## A\nhello world\n")
    index.set_markdown_indexing(True)
    try:
        index.index_file(brain_db, md, layer="semantic", embed_client=stub_embed)
    finally:
        index.set_markdown_indexing(False)
    assert _source_workspace(brain_db, md) == "ea-xfactor"


# ---------- index_text (/remember path) -------------------------------------

def test_index_text_writes_a_single_chunk(brain_db, stub_embed):
    cid = index.index_text(
        brain_db,
        "remember this: chunker, index, transcript_filter.",
        source="remember:test-1",
        layer="semantic",
        embed_client=stub_embed,
    )
    assert cid > 0
    con = db.connect(brain_db)
    try:
        row = con.execute(
            "SELECT text, region, importance, distillation_tier, ordinal "
            "FROM chunk WHERE id = ?", (cid,)
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    text, region, importance, tier, ordinal = row
    assert "remember this" in text
    assert region in index.REGIONS
    assert tier is not None
    assert ordinal == 0
    assert importance > 0


# ---------- defaults & opt-ins ---------------------------------------------

def test_default_ingest_roots_target_cc_transcripts():
    roots = index.default_ingest_roots()
    assert roots, "default roots must be populated"
    pattern, layer = roots[0]
    assert ".claude" in pattern
    assert pattern.endswith(".jsonl")
    assert layer == "episodic"


def test_markdown_indexing_is_off_by_default(tmp_path, brain_db, stub_embed):
    """Personal installs must not silently sweep repo markdown."""
    assert index.markdown_indexing_enabled() is False
    md = tmp_path / "doc.md"
    md.write_text("## A\nhello world\n")
    with pytest.raises(PermissionError):
        index.index_file(brain_db, md, layer="semantic", embed_client=stub_embed)


def test_markdown_indexing_can_be_opted_in(tmp_path, brain_db, stub_embed):
    md = tmp_path / "doc.md"
    md.write_text("## A\nhello world\n")
    index.set_markdown_indexing(True)
    try:
        chunk_ids = index.index_file(brain_db, md, layer="semantic", embed_client=stub_embed)
        assert chunk_ids
    finally:
        index.set_markdown_indexing(False)


# ---------- chunker regression (size-cap) ----------------------------------

def test_chunker_size_cap_holds_on_dense_content():
    """Smoke test — the chunker char cap guards the embed call."""
    dense = "## Data\n" + "\n".join(
        f"key_{i}: value_{i}_payload_xyz_qrs" for i in range(2000)
    )
    chunks = chunker.chunk_markdown(dense)
    assert chunks
    assert all(len(c.text) <= chunker.MAX_CHUNK_CHARS for c in chunks)
