import io
import os
import stat
import subprocess

import pytest

from ari_os.tools import dispatch


def test_build_argv_does_not_include_task_prompt(monkeypatch):
    monkeypatch.delenv("ARI_OS_WORKER_SKIP_PERMISSIONS", raising=False)

    argv = dispatch.build_claude_argv(
        executor="sonnet",
        cwd="/work/proj",
        add_dirs=["/work/extra"],
        read_only=False,
    )

    assert "secret prompt" not in argv
    assert argv[:2] == ["claude", "-p"]


def test_spawn_sends_task_on_stdin_and_private_log(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_OS_HOME", str(tmp_path))
    captured = {}

    class FakeStdin:
        def __init__(self):
            self.bytes = b""
            self.closed = False

        def write(self, data):
            self.bytes += data

        def close(self):
            self.closed = True

    class FakeProcess:
        pid = 123

        def __init__(self, argv, cwd, stdin, stdout, stderr, env,
                     start_new_session):
            captured["argv"] = argv
            captured["cwd"] = cwd
            captured["stdin_arg"] = stdin
            captured["stdout"] = stdout
            captured["stderr"] = stderr
            captured["env"] = env
            captured["start_new_session"] = start_new_session
            self.stdin = FakeStdin()
            captured["stdin"] = self.stdin

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)

    pid = dispatch.spawn("w-ab12-test", ["claude", "-p"], str(tmp_path), "secret prompt")

    assert pid == 123
    assert captured["stdin_arg"] is subprocess.PIPE
    assert captured["stdin"].bytes == b"secret prompt"
    assert captured["stdin"].closed is True
    # The global git lane guard cannot see a dispatched worker without this:
    # a worker is its own `claude -p` process, so hook payloads carry no
    # agent_id and it would otherwise read as Mati's interactive session.
    assert captured["env"]["ARI_OS_WORKER"] == "w-ab12-test"
    log = tmp_path / "logs" / "w-ab12-test.log"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_cmd_start_rejects_flag_like_add_dir(tmp_path):
    task_file = tmp_path / "task.md"
    task_file.write_text("do it")

    class A:
        pass

    (tmp_path / "work").mkdir()
    A.executor = "sonnet"
    A.task_file = str(task_file)
    A.cwd = str(tmp_path / "work")
    A.label = "x"
    A.add_dir = ["--dangerous"]
    A.read_only = False
    with pytest.raises(SystemExit):
        dispatch.cmd_start(A())


def test_cmd_start_updates_worker_state_under_lock(tmp_path, monkeypatch):
    task_file = tmp_path / "task.md"
    task_file.write_text("do it")
    work = tmp_path / "work"
    work.mkdir()
    events = []

    class Lock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, *_):
            events.append("lock-exit")
            return False

    class A:
        pass

    A.executor = "sonnet"
    A.task_file = str(task_file)
    A.cwd = str(work)
    A.label = "x"
    A.add_dir = []
    A.read_only = False

    monkeypatch.setattr(dispatch, "is_repo_root", lambda _path: False)
    monkeypatch.setattr(dispatch, "worker_id", lambda _label: "w-ab12-x")
    monkeypatch.setattr(dispatch, "spawn", lambda *_args: 123)
    monkeypatch.setattr(dispatch.state, "locked", lambda: Lock())
    monkeypatch.setattr(dispatch.state, "read_workers", lambda: [])

    def write_workers(workers):
        events.append(("write", list(workers)))

    monkeypatch.setattr(dispatch.state, "write_workers", write_workers)

    dispatch.cmd_start(A())

    assert events[0] == "lock-enter"
    assert events[-1] == "lock-exit"
    assert events[1][0] == "write"
    assert events[1][1][0]["id"] == "w-ab12-x"
