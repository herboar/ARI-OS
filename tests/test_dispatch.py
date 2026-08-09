import argparse, re, subprocess
import pytest
from ari_os.tools import dispatch
from ari_os.tools import state as _state


def test_worker_id_shape():
    wid = dispatch.worker_id("impl-auth")
    assert re.match(r"^w-[0-9a-f]{4}-impl-auth$", wid)


def test_build_argv_basic():
    argv = dispatch.build_claude_argv(
        executor="sonnet",
        cwd="/work/proj", add_dirs=["/work/extra"], read_only=False)
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--model" in argv and "sonnet" in argv
    assert "--add-dir" in argv and "/work/extra" in argv
    assert "--dangerously-skip-permissions" in argv


def test_build_argv_omits_effort_when_unset():
    """No --effort means the worker keeps Claude Code's own default."""
    argv = dispatch.build_claude_argv(
        executor="sonnet", cwd="/p", add_dirs=[], read_only=False)
    assert "--effort" not in argv


def test_build_argv_passes_effort_when_set():
    argv = dispatch.build_claude_argv(
        executor="opus", cwd="/p", add_dirs=[], read_only=False,
        effort="xhigh")
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_start_refuses_unknown_effort():
    """`claude` only warns on a bad --effort and silently uses the default.

    In a detached worker that warning lands in a log nobody reads, so the
    operator would believe a job ran at the effort they named. Refuse instead.
    """
    a = argparse.Namespace(
        executor="sonnet", effort="hihg", cwd="/p", label="x",
        task_file="/dev/null", add_dir=None, read_only=False, purpose=None,
        allow_main_tree=False)
    with pytest.raises(SystemExit) as exc:
        dispatch.cmd_start(a)
    assert "hihg" in str(exc.value)


def test_build_argv_read_only_strips_write_tools():
    argv = dispatch.build_claude_argv(
        executor="haiku", cwd="/p", add_dirs=[], read_only=True)
    joined = " ".join(argv)
    assert "--disallowed-tools" in argv
    assert "Write" in joined and "Edit" in joined


def test_is_repo_root(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert dispatch.is_repo_root(str(tmp_path)) is True
    sub = tmp_path / "sub"; sub.mkdir()
    assert dispatch.is_repo_root(str(sub)) is False


def test_questions_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARI_OS_HOME", str(tmp_path))
    (tmp_path / "questions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "questions" / "w-ab12-foo.md").write_text(
        "Decision: jwt vs hex?\nDefault: hex")
    class A: pass
    dispatch.cmd_questions(A())
    out = capsys.readouterr().out
    assert "w-ab12-foo" in out and "jwt vs hex" in out


def test_compose_answer_bakes_in():
    t = dispatch.compose_answer_task("orig task", "use hex")
    assert "orig task" in t and "use hex" in t and "baked in" in t
