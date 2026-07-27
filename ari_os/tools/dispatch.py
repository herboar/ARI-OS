"""ARI-OS background dispatcher.

Spawns detached `claude -p` headless workers, tracks them in
~/.ari-os/workers.json, and brokers a file-based question protocol. The
orchestrator session stays free while workers run.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
from .. import paths
from . import state

EXECUTORS = {"haiku", "sonnet", "opus", "fable"}
_READONLY_TOOLS = "Write,Edit,MultiEdit,NotebookEdit"
_FALSEY = {"0", "false", "no"}


def worker_id(label: str) -> str:
    return f"w-{uuid.uuid4().hex[:4]}-{paths.safe_segment(label)}"


def is_repo_root(path: str) -> bool:
    try:
        top = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3)
        if top.returncode != 0:
            return False
        return Path(top.stdout.strip()).resolve() == Path(path).resolve()
    except Exception:
        return False


def build_claude_argv(executor: str, cwd: str,
                      add_dirs: list[str], read_only: bool) -> list[str]:
    argv = ["claude", "-p", "--model", executor]
    skip_permissions = os.environ.get("ARI_OS_WORKER_SKIP_PERMISSIONS", "1")
    if skip_permissions.strip().lower() not in _FALSEY:
        argv.append("--dangerously-skip-permissions")
    for d in add_dirs:
        argv += ["--add-dir", d]
    if read_only:
        argv += ["--disallowed-tools", _READONLY_TOOLS]
    return argv


def spawn(wid: str, argv: list[str], cwd: str, task: str) -> int:
    log = state.state_dir() / "logs" / f"{wid}.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    fh = os.fdopen(fd, "w")
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                            stdout=fh, stderr=subprocess.STDOUT,
                            start_new_session=True)
    if proc.stdin is not None:
        proc.stdin.write(task.encode())
        proc.stdin.close()
    fh.close()
    return proc.pid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_start(a) -> None:
    if a.executor not in EXECUTORS:
        sys.exit(f"Unknown executor: {a.executor}. One of {sorted(EXECUTORS)}")
    if is_repo_root(a.cwd):
        sys.exit("Refusing to run a worker at a git repo ROOT. "
                 "Use a worktree or subdirectory as --cwd.")
    for d in (a.add_dir or []):
        if d.startswith("-"):
            sys.exit(f"Refusing --add-dir value that looks like a flag: {d!r}")
    task = Path(a.task_file).read_text()
    wid = worker_id(a.label)
    argv = build_claude_argv(a.executor, a.cwd, a.add_dir or [], a.read_only)
    pid = spawn(wid, argv, a.cwd, task)
    with state.locked():
        workers = state.read_workers()
        workers.append({"id": wid, "label": a.label, "executor": a.executor,
                        "cwd": a.cwd, "status": "running", "pid": pid,
                        "started_at": _now()})
        state.write_workers(workers)
    print(wid)


def cmd_list(a) -> None:
    for w in state.reconcile():
        print(f"{w['id']:32} {w['status']:10} {w.get('label','')}")


def cmd_questions(a) -> None:
    qdir = state.state_dir() / "questions"
    found = sorted(qdir.glob("*.md"))
    if not found:
        print("(no unresolved questions)")
        return
    for q in found:
        print(f"--- {q.stem} ---")
        print(q.read_text())


def compose_answer_task(original_task: str, answer: str) -> str:
    return (f"{original_task}\n\n## Orchestrator answer to your question\n"
            f"{answer}\n\nProceed with this answer baked in.")


def cmd_answer(a) -> None:
    qdir = state.state_dir() / "questions"
    qfile = paths.resolve_within(qdir, f"{paths.safe_segment(a.worker_id)}.md")
    original = qfile.read_text() if qfile.exists() else ""
    new_task = compose_answer_task(original, a.answer)
    if qfile.exists():
        qfile.unlink()
    print(new_task[:200])


def main() -> None:
    p = argparse.ArgumentParser(prog="dispatch")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.set_defaults(fn=cmd_start)
    s.add_argument("--executor", required=True)
    s.add_argument("--task-file", required=True, dest="task_file")
    s.add_argument("--cwd", required=True)
    s.add_argument("--label", required=True)
    s.add_argument("--add-dir", action="append", dest="add_dir")
    s.add_argument("--read-only", action="store_true")
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("questions").set_defaults(fn=cmd_questions)
    an = sub.add_parser("answer"); an.set_defaults(fn=cmd_answer)
    an.add_argument("worker_id")
    an.add_argument("--answer", required=True)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
