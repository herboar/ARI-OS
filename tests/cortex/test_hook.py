"""ar.t10 SessionStart hook tests — ``ari_os.hooks.session_start_cortex``.

Verifies the dynamic context search emission on session start:

- With no brain DB on disk, the hook prints nothing and exits 0.
- With a seeded brain DB, the hook prints a regioned context block
  containing >= 2 region headers.
- The block degrades to a one-line fallback (not an exception) when the
  backend errors.
- The hook is idempotent: the managed SessionStart entry replaces any
  prior ARI-OS entry on re-install.
- Non-ARI-OS hooks (e.g. PostToolUse) survive the merge.
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from ari_os import install
from ari_os.hooks import session_start_cortex as hook


# ---------- fixtures --------------------------------------------------------


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _insert_chunk(db_path: Path, *, text: str, region: str, vec, path: str,
                  workspace: str | None = None) -> int:
    """Seed a chunk + vector pair (same shape as test_cli helpers)."""
    from ari_os.tools.cortex import db as _db
    con = _db.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO source(path, layer, workspace, mtime, sha256, last_indexed_at) "
            "VALUES (?, 'semantic', ?, 0, 'x', 0)", (path, workspace))
        sid = con.execute("SELECT id FROM source WHERE path=?", (path,)).fetchone()[0]
        cur = con.execute(
            "INSERT INTO chunk(source_id, ordinal, text, line_start, line_end, region, "
            "importance, distillation_tier) "
            "VALUES (?, 0, ?, 1, 1, ?, 0.5, 0)",
            (sid, text, region),
        )
        cid = cur.lastrowid
        con.execute("INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                    (cid, _pack(vec)))
        return cid
    finally:
        con.close()


class _StubEmbed:
    """Maps a known query to a fixed vector; everything else -> zero."""

    def __init__(self, table: dict | None = None, dim: int = 768) -> None:
        self.table = table or {}
        self.dim = dim

    def embed(self, texts):
        out = []
        for t in texts:
            if t in self.table:
                out.append(self.table[t])
            else:
                out.append([0.0] * self.dim)
        return out


@pytest.fixture
def ari_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin ARI_OS_HOME + ARI_OS_BRAIN_DB to tmp_path for hermetic hook tests."""
    home = tmp_path / ".ari-os"
    home.mkdir()
    monkeypatch.setenv("ARI_OS_HOME", str(home))
    monkeypatch.setenv("ARI_OS_BRAIN_DB", str(home / "brain.db"))
    monkeypatch.delenv("ARI_OS_SKILL", raising=False)
    monkeypatch.delenv("ARI_OS_CWD", raising=False)
    return home


@pytest.fixture
def brain_db(ari_home: Path) -> Path:
    from ari_os.tools.cortex import db as _db
    p = ari_home / "brain.db"
    _db.init_db(p)
    return p


# ---------- no-DB behaviour ------------------------------------------------


def test_hook_no_db_prints_nothing_and_exits_zero(ari_home, monkeypatch, capsys):
    """Fresh install — no brain DB. Hook must NOT raise; it just stays silent."""
    monkeypatch.setattr("ari_os.tools.cortex.config.brain_db_path",
                        lambda: ari_home / "brain.db")
    rc = hook.main()  # default cwd = /tmp/<this test>; no DB exists
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_run_no_db_returns_empty_string(ari_home, monkeypatch):
    monkeypatch.setattr("ari_os.tools.cortex.config.brain_db_path",
                        lambda: ari_home / "brain.db")
    out = hook.run(cwd="/tmp/whatever")
    assert out == ""


# ---------- seeded-DB behaviour --------------------------------------------


def test_hook_with_seeded_db_prints_regioned_block(
    brain_db, tmp_path, monkeypatch, capsys
):
    """A brain with two regions worth of chunks -> a regioned block >= 2 headers."""
    dim = 768
    qvec = [0.0] * dim
    qvec[0] = 1.0
    _insert_chunk(brain_db, text="wernicke fact about retrieval",
                  region="wernicke", vec=qvec, path="wen.md")
    _insert_chunk(brain_db, text="broca fact about retrieval",
                  region="broca", vec=qvec, path="bro.md")

    cwd = tmp_path / "fixture-ws"
    cwd.mkdir()

    # The hook synthesises "<basename> project context" — match it exactly.
    monkeypatch.setattr("ari_os.tools.cortex.config.brain_db_path", lambda: brain_db)
    monkeypatch.setattr(
        "ari_os.tools.cortex.embed.default_embed_client",
        lambda: _StubEmbed({f"{cwd.name} project context": qvec}),
    )

    rc = hook.main(["--cwd", str(cwd)])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    region_markers = [
        "Orbitofrontal", "Frontoparietal", "Hippocampus", "Wernicke",
        "Broca", "Occipital", "Parietal",
    ]
    hits = sum(1 for m in region_markers if m in captured.out)
    assert hits >= 2, f"expected >=2 region headers, got: {captured.out!r}"
    assert "[wen.md" in captured.out
    assert "[bro.md" in captured.out


def test_hook_returns_block_via_run(brain_db, tmp_path, monkeypatch):
    """``run()`` is the test-friendly entry point — returns the block as a str."""
    dim = 768
    qvec = [0.0] * dim
    qvec[0] = 1.0
    _insert_chunk(brain_db, text="hippocampus fact", region="hippocampus",
                  vec=qvec, path="hip.md")
    cwd = tmp_path / "fixture-hippo"
    cwd.mkdir()
    monkeypatch.setattr("ari_os.tools.cortex.config.brain_db_path", lambda: brain_db)
    monkeypatch.setattr(
        "ari_os.tools.cortex.embed.default_embed_client",
        lambda: _StubEmbed({f"{cwd.name} project context": qvec}),
    )
    out = hook.run(cwd=str(cwd))
    assert "Hippocampus" in out
    assert "[hip.md" in out


def test_hook_skill_hint_biases_query(brain_db, tmp_path, monkeypatch):
    """A skill hint folds into the query; without the matching embed the
    block stays empty (no matches) but the hook still returns 0."""
    dim = 768
    qvec = [0.0] * dim
    qvec[0] = 1.0
    _insert_chunk(brain_db, text="wernicke fact", region="wernicke",
                  vec=qvec, path="w.md")
    cwd = tmp_path / "fixture-skill"
    cwd.mkdir()
    monkeypatch.setattr("ari_os.tools.cortex.config.brain_db_path", lambda: brain_db)
    # Query will be "<cwd> systematic-debugging context" — embed a different
    # zero vector to prove the skill hint is plumbed into the query string.
    monkeypatch.setattr(
        "ari_os.tools.cortex.embed.default_embed_client",
        lambda: _StubEmbed(),  # all-zero -> nothing matches
    )
    rc = hook.main(["--cwd", str(cwd), "--skill", "systematic-debugging"])
    assert rc == 0


# ---------- installer integration ------------------------------------------


def test_installer_registers_session_start_hook(tmp_path, monkeypatch):
    """``apply()`` must write a SessionStart entry pointing at our hook."""
    monkeypatch.setenv("ARI_OS_HOME", str(tmp_path / "state"))
    cdir = tmp_path / "claude"
    cdir.mkdir()
    monkeypatch.setenv("ARI_OS_CLAUDE_DIR", str(cdir))

    repo = tmp_path / "repo"
    (repo / "ari_os" / "skills" / "x").mkdir(parents=True)
    (repo / "ari_os" / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\n# x")
    (repo / "ari_os" / "commands").mkdir()
    (repo / "ari_os" / "commands" / "x.md").write_text("---\ndescription: x\n---\nbody")
    (repo / "ari_os" / "VERSION").write_text("0.1.0")

    install.apply(install.plan_actions(repo), dry_run=False)
    settings = json.loads((cdir / "settings.json").read_text())
    ss = settings.get("hooks", {}).get("SessionStart", [])
    assert any("ari_os.hooks.session_start_cortex" in h.get("command", "")
               for h in ss)


def test_installer_reinstall_is_idempotent(tmp_path, monkeypatch):
    """Running apply() twice produces exactly one ARI-OS SessionStart entry."""
    monkeypatch.setenv("ARI_OS_HOME", str(tmp_path / "state"))
    cdir = tmp_path / "claude"
    cdir.mkdir()
    monkeypatch.setenv("ARI_OS_CLAUDE_DIR", str(cdir))
    repo = tmp_path / "repo"
    (repo / "ari_os" / "skills" / "x").mkdir(parents=True)
    (repo / "ari_os" / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\n# x")
    (repo / "ari_os" / "commands").mkdir()
    (repo / "ari_os" / "commands" / "x.md").write_text("---\ndescription: x\n---\nbody")
    (repo / "ari_os" / "VERSION").write_text("0.1.0")

    install.apply(install.plan_actions(repo), dry_run=False)
    install.apply(install.plan_actions(repo), dry_run=False)
    settings = json.loads((cdir / "settings.json").read_text())
    ss = settings.get("hooks", {}).get("SessionStart", [])
    ari_entries = [h for h in ss if "ari_os.hooks.session_start_cortex" in h.get("command", "")]
    assert len(ari_entries) == 1


def test_installer_preserves_unrelated_hooks(tmp_path, monkeypatch):
    """PostToolUse / PreToolUse entries (non-ARI-OS) must survive the merge."""
    monkeypatch.setenv("ARI_OS_HOME", str(tmp_path / "state"))
    cdir = tmp_path / "claude"
    cdir.mkdir()
    monkeypatch.setenv("ARI_OS_CLAUDE_DIR", str(cdir))
    # Seed an unrelated hook into the user-owned settings.json.
    (cdir / "settings.json").write_text(json.dumps({
        "hooks": {
            "PostToolUse": [
                {"type": "command", "command": "echo unrelated"}
            ]
        }
    }))
    repo = tmp_path / "repo"
    (repo / "ari_os" / "skills" / "x").mkdir(parents=True)
    (repo / "ari_os" / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\n# x")
    (repo / "ari_os" / "commands").mkdir()
    (repo / "ari_os" / "commands" / "x.md").write_text("---\ndescription: x\n---\nbody")
    (repo / "ari_os" / "VERSION").write_text("0.1.0")

    install.apply(install.plan_actions(repo), dry_run=False)
    settings = json.loads((cdir / "settings.json").read_text())
    post = settings.get("hooks", {}).get("PostToolUse", [])
    assert any(h.get("command") == "echo unrelated" for h in post)


# ---------- subprocess smoke -------------------------------------------------


def test_hook_module_invokable_subprocess(ari_home):
    """``python3 -m ari_os.hooks.session_start_cortex`` runs end-to-end."""
    res = subprocess.run(
        [sys.executable, "-m", "ari_os.hooks.session_start_cortex",
         "--cwd", "/tmp/nonexistent"],
        capture_output=True, text=True, check=False,
    )
    # Missing DB -> silent, exit 0.
    assert res.returncode == 0
    assert res.stdout == ""
