"""Cross-process exclusive lock for the injection path.

The stack serialises *every* call that stops the game, because two controllers
rewriting the same thread's registers garble RIP/RSP and kill the process
(2026-11-08, run 20).  POSIX uses flock(2); Windows has no flock, so this module
picks the right primitive and everything above keeps calling acquire/release.
"""
import os
import tempfile
import time


def _default_path():
    return os.path.join(tempfile.gettempdir(), "hoi4_ptrace.lock")


class FileLock:
    """Blocking exclusive lock on a file. Re-entrant, cross-process."""

    def __init__(self, path=None, timeout=None, poll=0.05):
        self.path = path or _default_path()
        self.timeout = timeout
        self.poll = poll
        self.fh = None
        self._depth = 0

    # ---- platform primitives ----
    def _lock(self):
        if os.name == "nt":
            import msvcrt
            self.fh.seek(0)
            if self.fh.seek(0, os.SEEK_END) == 0:
                self.fh.write(b"\0")
                self.fh.flush()
            self.fh.seek(0)
            deadline = None if self.timeout is None else time.time() + self.timeout
            while True:
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if deadline is not None and time.time() > deadline:
                        raise TimeoutError("lock %s busy" % self.path)
                    time.sleep(self.poll)
        else:
            import fcntl
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)

    def _unlock(self):
        if os.name == "nt":
            import msvcrt
            self.fh.seek(0)
            try:
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass

    # ---- public ----
    def acquire(self):
        if self._depth:
            self._depth += 1
            return
        self.fh = open(self.path, "a+b")
        try:
            self._lock()
        except Exception:
            try:
                self.fh.close()
            finally:
                self.fh = None
            raise
        self._depth = 1

    def release(self):
        if not self._depth:
            return
        self._depth -= 1
        if self._depth:
            return
        try:
            self._unlock()
        finally:
            try:
                if self.fh:
                    self.fh.close()
            finally:
                self.fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()
