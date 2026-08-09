"""ARI-OS background dispatcher.

Spawns detached `claude -p` headless workers, tracks them in
~/.ari-os/workers.json, and brokers a file-based question protocol. The
orchestrator session stays free while workers run.

Each spawned worker records its own identity (worktree, branch, pid start
time, read-only flag, purpose) so the lane board can attribute an actor to a
lane as a fact rather than by guessing from cwd prefixes and timestamps.

A `--read-only` worker is denied `Bash` as well as the write tools: a shell
is a write tool (`echo >`, `cp`, `mv`, `sed -i`). The consequence is that a
read-only worker cannot run scripts, tests, or build commands at all — it can
only read, search, and report. Dispatch anything that must execute something
as a normal (writing) worker in its own worktree.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
from .. import paths
from . import state

EXECUTORS = {"haiku", "sonnet", "opus", "fable"}
_READONLY_TOOLS = "Bash,Write,Edit,MultiEdit,NotebookEdit"
_FALSEY = {"0", "false", "no"}


def worker_id(label: str) -> str:
    return f"w-{uuid.uuid4().hex[:4]}-{paths.safe_segment(label)}"


def _run(argv: list[str], timeout: float = 5.0) -> str | None:
    """Run a short command and return stripped stdout, or None on any failure.

    Never `subprocess.run(timeout=)`: its expiry SIGKILLs, and a git killed
    mid-index-write strands index.lock inside a live agent's worktree.
    SIGTERM first, SIGKILL only as a last resort.
    """
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        return None
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = out.strip()
    return out or None


def _git(cwd: str, *args: str) -> str | None:
    """git in `cwd`, read-only. Returns stripped stdout, or None on failure."""
    return _run(["git", "--no-optional-locks", "-C", str(cwd), *args])


def is_repo_root(path: str) -> bool:
    top = _git(path, "rev-parse", "--show-toplevel")
    if not top:
        return False
    try:
        return Path(top).resolve() == Path(path).resolve()
    except Exception:
        return False


def is_main_tree(path: str) -> bool:
    """True when `path` sits inside a repo's MAIN working tree.

    A linked worktree has a `.git` *file* pointing at
    `<repo>/.git/worktrees/<name>`, so its git-dir differs from its
    git-common-dir. In a main tree the two are the same directory.
    Unknown (not a repo, git unavailable) is False — never block on doubt.
    """
    out = _git(path, "rev-parse", "--git-dir", "--git-common-dir")
    if not out:
        return False
    lines = out.splitlines()
    if len(lines) != 2:
        return False
    try:
        # Either line may be relative to `path`; absolute lines absorb the join.
        resolved = [(Path(path) / line.strip()).resolve() for line in lines]
    except Exception:
        return False
    return resolved[0] == resolved[1]


def worktree_of(cwd: str) -> str | None:
    return _git(cwd, "rev-parse", "--show-toplevel")


def branch_of(cwd: str) -> str | None:
    return _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def pid_start(pid) -> str | None:
    """Process start time, e.g. 'Sat Aug  8 11:39:34 2026'.

    Recorded so a later reader can detect PID reuse: measured PID wrap on this
    machine is ~10 hours, well inside a stale worker row's lifetime.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    return _run(["ps", "-o", "lstart=", "-p", str(pid)])


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


def worker_env(wid: str) -> dict:
    """Environment for a spawned worker.

    ARI_OS_WORKER is the only way the global PreToolUse git guard can tell a
    dispatched worker from Mati. A worker is a separate `claude -p` process, so
    its MAIN loop carries no `agent_id` in the hook payload — without this stamp
    the guard reads it as the interactive session and enforces nothing.
    """
    env = dict(os.environ)
    env["ARI_OS_WORKER"] = wid
    return env


def spawn(wid: str, argv: list[str], cwd: str, task: str) -> int:
    log = state.state_dir() / "logs" / f"{wid}.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    fh = os.fdopen(fd, "w")
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                            stdout=fh, stderr=subprocess.STDOUT,
                            env=worker_env(wid),
                            start_new_session=True)
    if proc.stdin is not None:
        proc.stdin.write(task.encode())
        proc.stdin.close()
    fh.close()
    return proc.pid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_start(a) -> None:
    read_only = bool(getattr(a, "read_only", False))
    purpose = getattr(a, "purpose", None)
    allow_main_tree = bool(getattr(a, "allow_main_tree", False))
    if a.executor not in EXECUTORS:
        sys.exit(f"Unknown executor: {a.executor}. One of {sorted(EXECUTORS)}")
    if is_repo_root(a.cwd):
        sys.exit("Refusing to run a worker at a git repo ROOT. "
                 "Use a worktree or subdirectory as --cwd.")
    if not read_only and not allow_main_tree and is_main_tree(a.cwd):
        sys.exit(
            f"Refusing to run a writing worker inside a MAIN working tree: {a.cwd}\n"
            "One working tree = one committer, and the main tree belongs to the "
            "interactive session.\n"
            "Fix: git worktree add .claude/worktrees/<slug> -b agent/<slug>\n"
            "     then pass that worktree as --cwd.\n"
            "Or pass --read-only (read-only workers may audit a live tree), "
            "or --allow-main-tree if you truly mean it.")
    for d in (a.add_dir or []):
        if d.startswith("-"):
            sys.exit(f"Refusing --add-dir value that looks like a flag: {d!r}")
    if not purpose:
        print("dispatch: warning: no --purpose given. The lane board will show "
              "this worker's lane as unnamed; pass --purpose \"...\" next time.",
              file=sys.stderr)
    task = Path(a.task_file).read_text()
    wid = worker_id(a.label)
    argv = build_claude_argv(a.executor, a.cwd, a.add_dir or [], read_only)
    # Identity is resolved at spawn, while the worktree provably still exists.
    # Any git failure stores null; it never blocks a dispatch.
    worktree = worktree_of(a.cwd)
    branch = branch_of(a.cwd)
    pid = spawn(wid, argv, a.cwd, task)
    started = pid_start(pid)
    with state.locked():
        workers = state.read_workers()
        workers.append({"id": wid, "label": a.label, "executor": a.executor,
                        "cwd": a.cwd, "status": "running", "pid": pid,
                        "started_at": _now(),
                        "worktree": worktree, "branch": branch,
                        "pid_start": started, "read_only": read_only,
                        "purpose": purpose})
        state.write_workers(workers)
    print(wid)


def cmd_list(a) -> None:
    for w in state.reconcile():
        print(f"{w['id']:32} {w['status']:10} {w.get('label','')}")


# ------------------------------------------------------------------ close

def _git_rc(cwd, *args: str) -> tuple[int, str, str]:
    """git in `cwd`, returning (returncode, stdout, stderr).

    Unlike `_git`, this distinguishes "succeeded with empty output" from
    "failed" — `status --porcelain` on a clean tree is exactly that case, and
    the whole refusal hinges on telling those two apart.
    """
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def find_worker(label: str) -> dict | None:
    """Newest worker row matching `label` by label or id."""
    hits = [w for w in state.read_workers()
            if w.get("label") == label or w.get("id") == label]
    return hits[-1] if hits else None


def main_tree_of(worktree: str) -> str | None:
    """The repo's MAIN working tree, given any linked worktree inside it."""
    rc, out, _ = _git_rc(worktree, "rev-parse", "--git-common-dir")
    if rc != 0 or not out:
        return None
    # git may answer relatively; resolve against the worktree it answered for.
    common = Path(worktree, out.splitlines()[0].strip()).resolve()
    return str(common.parent) if common.name == ".git" else None


def clear_lane_entry(repo: str, branch: str) -> bool:
    """Drop `branch` from <repo>/.claude/agent-lanes.json. True if it changed."""
    p = Path(repo) / ".claude" / "agent-lanes.json"
    try:
        data = json.loads(p.read_text())
    except Exception:
        return False
    rows = data.get("lanes") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return False
    kept = [r for r in rows
            if not (isinstance(r, dict) and r.get("branch") == branch)]
    if len(kept) == len(rows):
        return False
    if isinstance(data, dict):
        data["lanes"] = kept
    else:
        data = kept
    p.write_text(json.dumps(data, indent=2) + "\n")
    return True


def cmd_close(a) -> None:
    """Retire a finished lane: refuse if dirty, merge, remove, delete, unlist.

    The refusal is the point. Stale worktrees accumulate because closing a lane
    is three manual commands nobody owns, and finished work has been stranded
    inside a gitignored worktree one `worktree remove` from gone.
    """
    worktree, branch, worker = a.worktree, a.branch, None
    if not worktree:
        worker = find_worker(a.label)
        if not worker:
            sys.exit(f"No worker matches label {a.label!r}. "
                     "Pass --worktree/--branch explicitly, or `dispatch list`.")
        worktree = worker.get("worktree")
        branch = branch or worker.get("branch")
        if not worktree:
            sys.exit(f"Worker {worker['id']} recorded no worktree "
                     "(spawned before instrumentation). Pass --worktree.")
        if worker.get("pid") is not None and state.pid_alive(worker["pid"]):
            sys.exit(f"Worker {worker['id']} is still RUNNING (pid "
                     f"{worker['pid']}). A lane closes after its worker exits.")

    worktree = str(Path(worktree).resolve())
    if not Path(worktree).is_dir():
        sys.exit(f"Not a directory: {worktree}")
    if is_main_tree(worktree) and not a.allow_main_tree:
        sys.exit(f"Refusing to close a MAIN working tree: {worktree}")

    if not branch:
        rc, branch, _ = _git_rc(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0 or not branch:
            sys.exit(f"Cannot determine the branch of {worktree}.")

    # 1. REFUSE on anything uncommitted. Untracked counts: two finished Reel
    #    covers sat untracked in a worktree, one `worktree remove` from gone.
    rc, dirty, err = _git_rc(worktree, "status", "--porcelain", "-unormal")
    if rc != 0:
        sys.exit(f"git status failed in {worktree}: {err}")
    if dirty:
        print(f"REFUSED: {worktree} has uncommitted work:\n", file=sys.stderr)
        print(dirty, file=sys.stderr)
        sys.exit("\nCommit it on %s, or move it out, then close the lane again."
                 % branch)

    repo = main_tree_of(worktree)
    if not repo:
        sys.exit(f"Cannot locate the main working tree for {worktree}.")
    into = a.into
    if not into:
        rc, into, _ = _git_rc(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0 or not into:
            sys.exit(f"Cannot determine the integration branch in {repo}.")

    merged = _git_rc(repo, "merge-base", "--is-ancestor", branch, into)[0] == 0
    plan = [f"repo       {repo}",
            f"worktree   {worktree}",
            f"branch     {branch} -> {into}" + ("  (already merged)" if merged else ""),
            "clean      yes (no tracked or untracked changes)"]
    print("\n".join(plan))
    if a.dry_run:
        print("\n(dry run: nothing done)")
        return

    # 2. Merge, unless the branch is already an ancestor of the target.
    if not merged:
        msg = f"merge({branch}): {worker.get('purpose') or a.label}" if worker \
              else f"merge({branch})"
        rc, out, err = _git_rc(repo, "merge", "--no-ff", branch, "-m", msg)
        if rc != 0:
            print(out, file=sys.stderr)
            sys.exit(f"MERGE FAILED, nothing removed. Resolve in {repo}, then "
                     f"re-run close.\n{err}")
        print(f"merged     {branch} into {into}")

    # 3. Remove the worktree. No --force: git refuses a dirty tree too.
    rc, out, err = _git_rc(repo, "worktree", "remove", worktree)
    if rc != 0:
        sys.exit(f"worktree remove failed (branch is merged, nothing lost):\n{err}")
    print(f"removed    {worktree}")

    # 4. Safe branch delete. -d, never -D: it refuses anything unmerged.
    if not a.keep_branch:
        rc, out, err = _git_rc(repo, "branch", "-d", branch)
        print(f"deleted    {branch}" if rc == 0
              else f"kept       {branch} ({err.splitlines()[0] if err else 'not deleted'})")

    # 5. Unlist the lane and stamp the worker row.
    if clear_lane_entry(repo, branch):
        print(f"unlisted   {branch} from .claude/agent-lanes.json")
    if worker:
        with state.locked():
            workers = state.read_workers()
            for w in workers:
                if w.get("id") == worker.get("id"):
                    w["closed_at"] = _now()
            state.write_workers(workers)
        print(f"closed     {worker['id']}")


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
    s.add_argument("--purpose", default=None,
                   help="one line on why this lane exists; shown on the lane board")
    s.add_argument("--allow-main-tree", action="store_true", dest="allow_main_tree",
                   help="permit a writing worker inside a MAIN working tree")
    sub.add_parser("list").set_defaults(fn=cmd_list)
    c = sub.add_parser("close", help="retire a finished lane")
    c.set_defaults(fn=cmd_close)
    c.add_argument("label", nargs="?", default=None,
                   help="worker label or id; omit when passing --worktree")
    c.add_argument("--worktree", default=None,
                   help="close this worktree directly, without a worker row")
    c.add_argument("--branch", default=None, help="override the lane branch")
    c.add_argument("--into", default=None,
                   help="integration branch (default: main tree's current HEAD)")
    c.add_argument("--keep-branch", action="store_true", dest="keep_branch")
    c.add_argument("--allow-main-tree", action="store_true", dest="allow_main_tree")
    c.add_argument("--dry-run", action="store_true", dest="dry_run")
    sub.add_parser("questions").set_defaults(fn=cmd_questions)
    an = sub.add_parser("answer"); an.set_defaults(fn=cmd_answer)
    an.add_argument("worker_id")
    an.add_argument("--answer", required=True)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
