"""Cross-process advisory file locks (fcntl on POSIX, msvcrt on Windows).

Used where several processes — one per Claude session, plus the daemon — must
not run the same critical section at once. The lock is held on an open file
handle, so the OS releases it automatically if the holding process dies.
"""

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


def _try_lock(handle: IO[bytes]) -> bool:
    """Take an exclusive lock on *handle* without blocking; True on success."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def file_lock(
    path: Path,
    *,
    timeout: float,
    poll_interval: float = 0.05,
    best_effort: bool = False,
) -> Iterator[bool]:
    """Hold an exclusive lock on *path* for the duration of the block.

    Waits up to *timeout* seconds for another holder to release it. On timeout
    a ``TimeoutError`` is raised — unless *best_effort*, in which case the block
    runs unlocked (yielding False). Also best-effort when the lock file can't be
    opened at all.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError:
        if not best_effort:
            raise
        yield False
        return

    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            if _try_lock(handle):
                locked = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval)
        if not locked and not best_effort:
            raise TimeoutError(
                f"Timed out after {timeout:.0f}s waiting for {path} — another "
                "process is holding it. If none is running, the lock is stale "
                "only while that process lives; it is released when it exits."
            )
        yield locked
    finally:
        if locked:
            _unlock(handle)
        handle.close()
