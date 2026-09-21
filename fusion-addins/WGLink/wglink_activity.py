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

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import functools
import threading
from typing import TypeVar


#: A user ran a WGLink command. This is the only cause that is not a finding.
#: A command names itself -- ``command:send`` -- through :func:`command_cause`,
#: so work is attributed to the command that asked for it, not merely to "a
#: command"; the bare spelling stays valid for a caller that cannot name one.
CAUSE_COMMAND = "command"
#: The periodic watcher tick.
CAUSE_TICK = "tick"
#: WG asked for something over the live channel.
CAUSE_LIVE = "live-dispatch"
#: Add-in start-up and shutdown, which are bounded and happen once.
CAUSE_STARTUP = "startup"
CAUSE_SHUTDOWN = "shutdown"
#: Settling the claims an interrupted session left behind: read-only, never a
#: replay, once per claim. Bounded like start-up, and counted apart from it so a
#: run can say exactly how much of its start-up work was this.
CAUSE_CLAIM_SETTLEMENT = "claim-settlement"
#: No cause was declared. Deliberately not a synonym for ``command``.
CAUSE_UNATTRIBUTED = "unattributed"

#: An entry point whose name could not be read. Counted, never dropped.
UNNAMEABLE = "<unnameable>"

#: The inspection entry points Gate AR names, and the paths that change or
#: export the document. One spelling each, shared by every module that records.
LINK_RESOLUTION = "link_resolution"
GEOMETRY_MEASUREMENT = "geometry_measurement"
SOURCE_INVENTORY = "source_inventory"
DOCUMENT_SIGNATURE = "document_signature"
STATUS_PUBLICATION = "status_publication"
MUTATION_INSERT = "mutation_insert"
MUTATION_UPDATE = "mutation_update"
MUTATION_DETACH = "mutation_detach"
MUTATION_SOURCE_IDENTITY = "mutation_source_identity"
MUTATION_BODY_DECLARATION = "mutation_body_declaration"
EXPORT_RETURN = "export_return"

#: Causes that are not a finding between commands besides a command itself:
#: the bounded, once-only handling at start-up and shutdown.
_BOUNDED = frozenset({CAUSE_STARTUP, CAUSE_SHUTDOWN, CAUSE_CLAIM_SETTLEMENT})


def command_cause(operation: object) -> str:
    """The cause for work a named WGLink command asked for: ``command:<name>``."""

    try:
        name = str(operation)
    except Exception:  # noqa: BLE001 - an unnameable command is still a command
        name = UNNAMEABLE
    return f"{CAUSE_COMMAND}:{name}"


def is_command_cause(cause: object) -> bool:
    return isinstance(cause, str) and (
        cause == CAUSE_COMMAND or cause.startswith(f"{CAUSE_COMMAND}:")
    )


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

        found: dict[str, int] = {}
        for name, causes in self.counts().items():
            total = sum(
                count
                for cause, count in causes.items()
                if not is_command_cause(cause) and cause not in _BOUNDED
            )
            if total:
                found[name] = total
        return found

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()

    def as_payload(self) -> Mapping[str, object]:
        """The shape the heartbeat publishes, when it publishes at all."""

        return {"counts": self.counts(), "betweenCommands": self.between_commands()}


#: This registration's log. Every WGLink module records into it; a second
#: registration loads its own copy of this module and so keeps its own counts.
LOG = ActivityLog()


def record(entry_point: object, cause: str | None = None) -> None:
    """Count one execution in :data:`LOG`. Never raises."""

    try:
        LOG.record(entry_point, cause)
    except Exception:  # noqa: BLE001 - measurement must not become a failure
        pass


_F = TypeVar("_F", bound=Callable[..., object])


def counted(entry_point: str) -> Callable[[_F], _F]:
    """Record ``entry_point`` on entry, before the function can refuse or raise.

    Counting before the body is the point: a path that ran and failed inside
    an advisory ``except`` still ran, and must say so.
    """

    def decorate(function: _F) -> _F:
        @functools.wraps(function)
        def wrapper(*args: object, **kwargs: object) -> object:
            record(entry_point)
            return function(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def carrying(function: Callable[..., object]) -> Callable[..., object]:
    """``function`` bound to the cause declared *here*, for a later hop.

    A thread, a timer or a custom event runs its work somewhere else, where
    the declared cause is not set. Binding it at the point of hand-off is what
    keeps a follow-up that a command scheduled counted under that command,
    instead of under whatever delivered it.
    """

    cause = current_cause()

    @functools.wraps(function)
    def wrapper(*args: object, **kwargs: object) -> object:
        with because(cause):
            return function(*args, **kwargs)

    wrapper.cause = cause  # type: ignore[attr-defined]
    return wrapper


__all__ = [
    "CAUSE_CLAIM_SETTLEMENT",
    "CAUSE_COMMAND",
    "CAUSE_LIVE",
    "CAUSE_SHUTDOWN",
    "CAUSE_STARTUP",
    "CAUSE_TICK",
    "CAUSE_UNATTRIBUTED",
    "UNNAMEABLE",
    "DOCUMENT_SIGNATURE",
    "EXPORT_RETURN",
    "GEOMETRY_MEASUREMENT",
    "LINK_RESOLUTION",
    "LOG",
    "MUTATION_BODY_DECLARATION",
    "MUTATION_DETACH",
    "MUTATION_INSERT",
    "MUTATION_SOURCE_IDENTITY",
    "MUTATION_UPDATE",
    "SOURCE_INVENTORY",
    "STATUS_PUBLICATION",
    "ActivityLog",
    "because",
    "carrying",
    "command_cause",
    "counted",
    "current_cause",
    "is_command_cause",
    "record",
]
