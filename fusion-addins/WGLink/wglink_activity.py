"""What the add-in inspected or changed, and why.

The question this answers is not "how long did a tick take" but "did anything
run between the user's commands at all". Those are different questions, and
the first cannot answer the second:

* ``resolve_links_ms = 0.1`` on an unlinked document and a function that
  returned before doing any work produce the same number;
* a timing field that is absent because the phase was skipped is
  indistinguishable from one absent because nothing wrote it;
* the heartbeat's own measurement sits inside an advisory
  ``except Exception`` that turns a refusal into empty strings, so a path that
  ran and failed reports exactly what a path that never ran reports.

A counter incremented at the entry point itself, carrying the cause that
reached it, is a statement that no path which did not execute can produce.

**The default cause accuses.** Work recorded with no declared cause counts as
``unattributed`` and appears in :meth:`ActivityLog.between_commands`. An
uninstrumented path must show up as a problem, never disappear because nobody
labelled it -- otherwise the measurement comes out clean exactly where the code
is least understood.

**Causes are per-thread.** The live client runs worker threads beside Fusion's
main thread. A process-wide cause would let a command on the main thread
launder a worker's inspection into ``command``.

Nothing here touches ``adsk``, so it is importable and testable headless.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import threading


#: A user ran a WGLink command. This is the only cause that is not a finding.
CAUSE_COMMAND = "command"
#: The periodic watcher tick.
CAUSE_TICK = "tick"
#: WG asked for something over the live channel.
CAUSE_LIVE = "live-dispatch"
#: Add-in start-up and shutdown, which are bounded and happen once.
CAUSE_STARTUP = "startup"
CAUSE_SHUTDOWN = "shutdown"
#: No cause was declared. Deliberately not a synonym for ``command``.
CAUSE_UNATTRIBUTED = "unattributed"

#: An entry point whose name could not be read. Counted, never dropped.
UNNAMEABLE = "<unnameable>"


_local = threading.local()


def current_cause() -> str:
    """Why work on this thread is running, right now."""

    return getattr(_local, "cause", CAUSE_UNATTRIBUTED)


@contextmanager
def because(cause: str) -> Iterator[None]:
    """Declare why the work inside this block is running.

    Restores the previous cause on the way out, including when the block
    raises: a tick that fails must not leave every later record attributed to
    it.
    """

    previous = current_cause()
    _local.cause = cause
    try:
        yield
    finally:
        _local.cause = previous


class ActivityLog:
    """Counts of ``entry point x cause``, for one add-in session."""

    def __init__(self) -> None:
        self._counts: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def record(self, entry_point: object, cause: str | None = None) -> None:
        """Count one execution of an entry point.

        ``cause`` defaults to the calling thread's declared cause. This must
        never raise into its caller: it is measurement, not work.
        """

        try:
            name = str(entry_point)
        except Exception:  # noqa: BLE001 - an unnameable entry point still ran
            name = UNNAMEABLE
        reason = cause if cause is not None else current_cause()
        with self._lock:
            self._counts.setdefault(name, {})
            self._counts[name][reason] = self._counts[name].get(reason, 0) + 1

    def counts(self) -> dict[str, dict[str, int]]:
        """A copy, so a reader cannot be changed underneath by a later record."""

        with self._lock:
            return {name: dict(causes) for name, causes in self._counts.items()}

    def between_commands(self) -> dict[str, int]:
        """Everything a user command did not ask for, per entry point.

        This is the acceptance criterion in one call: it must be empty between
        commands. Start-up and shutdown are bounded and happen once, so they
        are excluded; a tick, a live dispatch or an unattributed path is not.
        """

        excluded = {CAUSE_COMMAND, CAUSE_STARTUP, CAUSE_SHUTDOWN}
        found: dict[str, int] = {}
        for name, causes in self.counts().items():
            total = sum(count for cause, count in causes.items() if cause not in excluded)
            if total:
                found[name] = total
        return found

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()

    def as_payload(self) -> Mapping[str, object]:
        """The shape the heartbeat publishes, when it publishes at all."""

        return {"counts": self.counts(), "betweenCommands": self.between_commands()}


__all__ = [
    "CAUSE_COMMAND",
    "CAUSE_LIVE",
    "CAUSE_SHUTDOWN",
    "CAUSE_STARTUP",
    "CAUSE_TICK",
    "CAUSE_UNATTRIBUTED",
    "UNNAMEABLE",
    "ActivityLog",
    "because",
    "current_cause",
]
