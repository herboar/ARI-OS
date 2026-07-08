"""P4 CLI tests for dream, wander, and distill commands."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from ari_os.tools.cortex import db
from ari_os.tools.cortex.cortex import main


class _StubEmbed:
    def __init__(self, table: dict[str, list[float]] | None = None, dim: int = 768):
        self.table = table or {}
        self.dim = dim
        self.calls: list[str] = []

    def embed(self, texts):
        out = []
        for text in texts:
            self.calls.append(text)
            out.append(self.table.get(text, [0.0] * self.dim))
        return out


class _StubLLM:
    def consolidate(self, texts: list[str]) -> str:
        return "CLI digest: " + " | ".join(texts)

    def distill(self, text: str, tier: int) -> str:
        return f"CLI tier {tier + 1}: {text}"


@pytest.fixture
def ari_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".ari-os"
    home.mkdir()
    monkeypatch.setenv("ARI_OS_HOME", str(home))
    monkeypatch.setenv("ARI_OS_BRAIN_DB", str(home / "brain.db"))
    return home


@pytest.fixture
def brain_db(ari_home: Path) -> Path:
    path = ari_home / "brain.db"
    db.init_db(path)
    return path


def _write_transcript(path: Path) -> None:
    records = [
        {
            "type": "user",
            "sessionId": "sess-p4",
            "timestamp": "2026-06-13T12:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Alpha memory about morning planning."}],
            },
        },
        {
            "type": "assistant",
            "sessionId": "sess-p4",
            "timestamp": "2026-06-13T12:00:05Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Bravo memory about night review."}],
            },
        },
    ]
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _vec(dim: int = 768, *, hot_index: int = 0) -> list[float]:
    out = [0.0] * dim
    out[hot_index] = 1.0
    return out


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _insert_chunk(
    db_path: Path,
    *,
    text: str,
    path: str,
    workspace: str = "test",
    region: str = "wernicke",
    vec: list[float] | None = None,
) -> int:
    con = db.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO source(path, layer, workspace, mtime, sha256, last_indexed_at) "
            "VALUES (?, 'semantic', ?, 0, 'x', 0)",
            (path, workspace),
        )
        source_id = con.execute("SELECT id FROM source WHERE path = ?", (path,)).fetchone()[0]
        ordinal = con.execute(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM chunk WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]
        cur = con.execute(
            "INSERT INTO chunk(source_id, ordinal, text, line_start, line_end, region, "
            "importance, distillation_tier) VALUES (?, ?, ?, 1, 2, ?, 0.5, 0)",
            (source_id, ordinal, text, region),
        )
        chunk_id = int(cur.lastrowid)
        if vec is not None:
            con.execute(
                "INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                (chunk_id, _pack(vec)),
            )
        return chunk_id
    finally:
        con.close()


def test_dream_reports_summary_count_with_stub_llm_after_ingest(
    ari_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr("ari_os.tools.cortex.embed.EmbedClient", lambda: _StubEmbed())
    monkeypatch.setattr("ari_os.tools.cortex.llm.get_llm", lambda spec=None: _StubLLM())

    ingest = CliRunner().invoke(main, ["ingest", "--path", str(transcript)])
    assert ingest.exit_code == 0, ingest.output

    result = CliRunner().invoke(main, ["dream"])

    assert result.exit_code == 0, result.output
    assert "dream:" in result.output
    assert "summaries=1" in result.output


def test_wander_focus_prints_block_over_populated_brain(
    brain_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    focus_vec = _vec(hot_index=0)
    _insert_chunk(
        brain_db,
        text="database migration rollback checklist",
        path="focus.md",
        workspace="project-a",
        region="frontoparietal",
        vec=focus_vec,
    )
    for i in range(6):
        _insert_chunk(
            brain_db,
            text=f"associative memory {i}",
            path=f"assoc-{i}.md",
            workspace="project-b",
            region="hippocampus",
        )
    monkeypatch.setattr(
        "ari_os.tools.cortex.wander.default_embed_client",
        lambda: _StubEmbed({"database migration": focus_vec}),
    )

    result = CliRunner().invoke(main, ["wander", "--focus", "database migration"])

    assert result.exit_code == 0, result.output
    assert "MIND-WANDER" in result.output


def test_wander_off_prints_clean_line_without_embedding(
    ari_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ARI_OS_WANDER", "0")

    def fail_embed():
        raise AssertionError("wander off must not instantiate embeddings")

    monkeypatch.setattr("ari_os.tools.cortex.embed.EmbedClient", fail_embed)

    result = CliRunner().invoke(main, ["wander", "--focus", "anything"])

    assert result.exit_code == 0, result.output
    assert "wander off" in result.output


def test_distill_session_exits_zero_and_reports_count(
    brain_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _insert_chunk(brain_db, text="alpha memory", path="session.md")
    _insert_chunk(brain_db, text="bravo memory", path="session.md")
    monkeypatch.setattr("ari_os.tools.cortex.llm.get_llm", lambda spec=None: _StubLLM())

    result = CliRunner().invoke(main, ["distill", "--tier", "session"])

    assert result.exit_code == 0, result.output
    assert "distill:" in result.output
    assert "distilled=1" in result.output


def test_p4_commands_missing_db_exit_zero_with_informative_lines(ari_home: Path):
    missing_db = ari_home / "brain.db"
    if missing_db.exists():
        missing_db.unlink()

    for args, expected in [
        (["dream"], "brain not initialised"),
        (["wander", "--focus", "anything"], "brain not initialised"),
        (["distill", "--tier", "session"], "brain not initialised"),
    ]:
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        assert expected in result.output
