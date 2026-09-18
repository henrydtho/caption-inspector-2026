"""Cooperative cancellation for the long-running checks.

A Tier 2 pass can run for minutes on a feature, and until now the only way out
was to quit the app. The UI needs a Stop button, which means the worker has to
be asked to stop rather than killed - a killed thread would leave a half-written
transcript cache and a live ffmpeg child behind.

So: a token the UI owns and the worker checks. Every loop that can run long
calls `raise_if_cancelled()` at a point where abandoning the work is safe, and
subprocesses are registered so Stop can terminate them instead of waiting out a
30-minute ffmpeg decode.

The token is deliberately dumb - one flag, one lock, no callbacks into Tk. The
worker never touches a widget, and the UI never blocks on the worker.
"""

import threading


class OperationCancelled(Exception):
    """Raised inside a worker when the user has pressed Stop.

    Callers catch this and report "stopped", not "failed" - a cancelled run is
    not an error and must not be reported to a vendor as one.
    """

    def __init__(self, message="The operation was stopped."):
        super().__init__(message)


class CancelToken:
    """A one-way flag, plus the child processes that should die with it."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes = []

    # ------------------------------------------------------------------ state

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        """Ask the worker to stop, and kill anything it has already spawned."""
        self._event.set()
        with self._lock:
            processes = list(self._processes)
            self._processes.clear()

        for process in processes:
            _terminate(process)

    def reset(self):
        """Reuse the token for the next run."""
        self._event.clear()
        with self._lock:
            self._processes.clear()

    def raise_if_cancelled(self):
        if self._event.is_set():
            raise OperationCancelled()

    def wait(self, timeout=None):
        """Block until cancelled. Returns True if it was."""
        return self._event.wait(timeout)

    # -------------------------------------------------------------- processes

    def register_process(self, process):
        """Track a child process so `cancel()` can terminate it.

        If the token is already cancelled the process is killed immediately -
        otherwise a Stop pressed microseconds before the spawn would be lost and
        the run would continue to completion.
        """
        with self._lock:
            if self._event.is_set():
                _terminate(process)
                raise OperationCancelled()
            self._processes.append(process)

    def unregister_process(self, process):
        with self._lock:
            try:
                self._processes.remove(process)
            except ValueError:
                pass


def _terminate(process):
    """Terminate, then kill. A wedged ffmpeg ignores SIGTERM."""
    try:
        if process.poll() is not None:
            return
        process.terminate()
    except (OSError, ValueError):
        return

    try:
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
        except (OSError, ValueError):
            pass


def raise_if_cancelled(token):
    """Null-safe check, so callers do not need `if token is not None` everywhere."""
    if token is not None:
        token.raise_if_cancelled()


def is_cancelled(token):
    return token is not None and token.cancelled
