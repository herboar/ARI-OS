"""Shared state directory for ARI-OS dispatch + monitor.

Default ~/.ari-os, overridable with $ARI_OS_HOME (used by tests and by users
who want a custom location). Creates logs/ and questions/ on first access.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json, os
from pathlib import Path
from .. import paths


def state_dir() -> Path:
    base = os.environ.get("ARI_OS_HOME") or os.path.expanduser("~/.ari-os")
    d = paths.ensure_private_dir(base)
    paths.ensure_private_dir(d / "logs")
    paths.ensure_private_dir(d / "questions")
    return d


def workers_path() -> Path:
    return state_dir() / "workers.json"


def read_workers() -> list[dict]:
    p = workers_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def write_workers(workers: list[dict]) -> None:
    paths.write_private(workers_path(), json.dumps(workers, indent=2))


@contextmanager
def locked():
    """Exclusive lock around workers.json read-modify-write sections."""
    lock = state_dir() / "workers.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        try:
            lock.chmod(0o600)
        except OSError:
            pass
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except (ValueError, TypeError, OSError):
        return False
    return True


def _open_questions() -> set[str]:
    return {p.stem for p in (state_dir() / "questions").glob("*.md")}


def reconcile(workers: list[dict] | None = None) -> list[dict]:
    """Update worker statuses from reality: an open question -> blocked,
    a dead PID -> done, otherwise running. Persists if anything changed."""
    if workers is None:
        with locked():
            workers = read_workers()
            return _reconcile_unlocked(workers, persist=True)
    with locked():
        return _reconcile_unlocked(workers, persist=True)


def _reconcile_unlocked(workers: list[dict], persist: bool) -> list[dict]:
    open_q = _open_questions()
    changed = False
    for w in workers:
        if w.get("status") == "done":
            continue
        # Question files are named by worker id OR by label: worker_id() makes
        # "w-<hex4>-<label>", but every brief tells workers to write
        # questions/<label>.md. Matching only on id meant no worker ever
        # displayed as blocked. Accept both conventions.
        if w.get("id", "") in open_q or w.get("label", "") in open_q:
            new = "blocked"
        elif w.get("pid") is not None and not pid_alive(w["pid"]):
            new = "done"
        else:
            new = "running"
        if new != w.get("status"):
            w["status"] = new
            changed = True
    if changed and persist:
        write_workers(workers)
    return workers
