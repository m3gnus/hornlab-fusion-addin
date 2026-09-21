"""What the add-in inspected or changed, and why.

The acceptance criterion this serves is about what happens *between* user
commands: the add-in must not repeatedly inspect CAD geometry, resolve managed
entities, inventory sources, calculate document signatures or publish
synchronization status unless a command asked for it.

Timings cannot answer that. ``resolve_links_ms = 0.1`` on an unlinked document
is indistinguishable from a function that returned early, an absent field is
indistinguishable from one never written, and the heartbeat's own measurement
is wrapped in an advisory ``except Exception`` that turns a refusal into empty
strings. All three read as "nothing happened".

A counter incremented at the entry point itself, carrying the cause that
reached it, is a statement no path that did not execute can produce.
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_activity  # noqa: E402


@pytest.fixture
def log():
    return wglink_activity.ActivityLog()


def test_an_entry_point_records_the_cause_that_reached_it(log):
    with wglink_activity.because(wglink_activity.CAUSE_COMMAND):
        log.record("resolve_links")

    assert log.counts() == {"resolve_links": {wglink_activity.CAUSE_COMMAND: 1}}


def test_the_same_entry_point_under_two_causes_is_two_rows(log):
    with wglink_activity.because(wglink_activity.CAUSE_COMMAND):
        log.record("resolve_links")
    with wglink_activity.because(wglink_activity.CAUSE_TICK):
        log.record("resolve_links")
        log.record("resolve_links")

    assert log.counts() == {
        "resolve_links": {wglink_activity.CAUSE_COMMAND: 1, wglink_activity.CAUSE_TICK: 2}
    }


def test_work_with_no_declared_cause_is_not_silently_attributed_to_a_command(log):
    """The default must be the accusing one.

    If an uninstrumented path defaulted to ``command`` it would vanish from
    ``between_commands`` -- the measurement would be clean because the code was
    unlabelled, which is the failure this whole file exists to prevent.
    """

    log.record("resolve_links")

    assert log.counts() == {"resolve_links": {wglink_activity.CAUSE_UNATTRIBUTED: 1}}
    assert log.between_commands() == {"resolve_links": 1}


def test_between_commands_excludes_command_work_and_keeps_everything_else(log):
    with wglink_activity.because(wglink_activity.CAUSE_COMMAND):
        log.record("resolve_links")
        log.record("measure_geometry")
    with wglink_activity.because(wglink_activity.CAUSE_TICK):
        log.record("resolve_links")
    with wglink_activity.because(wglink_activity.CAUSE_LIVE):
        log.record("publish_status")

    assert log.between_commands() == {"resolve_links": 1, "publish_status": 1}


def test_a_quiet_log_reports_nothing_between_commands(log):
    with wglink_activity.because(wglink_activity.CAUSE_COMMAND):
        log.record("resolve_links")

    assert log.between_commands() == {}


def test_the_cause_is_restored_after_the_block_including_on_an_exception(log):
    with pytest.raises(RuntimeError):
        with wglink_activity.because(wglink_activity.CAUSE_TICK):
            raise RuntimeError("the tick failed")
    log.record("resolve_links")

    assert log.counts() == {"resolve_links": {wglink_activity.CAUSE_UNATTRIBUTED: 1}}


def test_nesting_restores_the_outer_cause(log):
    with wglink_activity.because(wglink_activity.CAUSE_TICK):
        with wglink_activity.because(wglink_activity.CAUSE_LIVE):
            log.record("publish_status")
        log.record("resolve_links")

    assert log.counts() == {
        "publish_status": {wglink_activity.CAUSE_LIVE: 1},
        "resolve_links": {wglink_activity.CAUSE_TICK: 1},
    }


def test_a_cause_on_one_thread_does_not_attribute_another_thread_s_work(log):
    """The live client's threads run beside the main thread and touch no adsk.

    A process-wide cause would let a command on the main thread launder a
    worker thread's inspection into ``command``.
    """

    started = threading.Event()
    release = threading.Event()

    def worker() -> None:
        started.wait(timeout=5)
        log.record("publish_status")
        release.set()

    thread = threading.Thread(target=worker)
    thread.start()
    with wglink_activity.because(wglink_activity.CAUSE_COMMAND):
        started.set()
        release.wait(timeout=5)
    thread.join(timeout=5)

    assert log.counts() == {"publish_status": {wglink_activity.CAUSE_UNATTRIBUTED: 1}}
    assert log.between_commands() == {"publish_status": 1}


def test_an_unnameable_entry_point_is_counted_rather_than_dropped(log):
    """Measurement must not become a new way for the add-in to fail.

    It must not become a new way to hide work either: swallowing the record
    would mean the one tool built to prove nothing ran could itself under-report,
    which is the whole defect it exists to detect.
    """

    class Hostile:
        def __str__(self) -> str:
            raise ValueError("no name")

        def __hash__(self) -> int:
            raise ValueError("no hash")

    log.record(Hostile())  # must not raise

    assert log.between_commands() == {wglink_activity.UNNAMEABLE: 1}


def test_a_snapshot_is_a_copy_not_the_live_mapping(log):
    with wglink_activity.because(wglink_activity.CAUSE_TICK):
        log.record("resolve_links")
    snapshot = log.counts()
    with wglink_activity.because(wglink_activity.CAUSE_TICK):
        log.record("resolve_links")

    assert snapshot == {"resolve_links": {wglink_activity.CAUSE_TICK: 1}}


def test_a_named_command_is_a_command_and_bounded_work_is_not_a_finding(log):
    with wglink_activity.because(wglink_activity.command_cause("send")):
        log.record("resolve_links")
    for cause in (
        wglink_activity.CAUSE_STARTUP,
        wglink_activity.CAUSE_SHUTDOWN,
        wglink_activity.CAUSE_CLAIM_SETTLEMENT,
    ):
        with wglink_activity.because(cause):
            log.record("resolve_links")

    assert log.counts()["resolve_links"]["command:send"] == 1
    assert log.between_commands() == {}


def test_a_cause_that_merely_starts_with_the_word_command_is_not_one(log):
    with wglink_activity.because("commander"):
        log.record("resolve_links")

    assert log.between_commands() == {"resolve_links": 1}


def test_carrying_binds_the_cause_where_the_work_was_handed_off(log):
    """A follow-up runs later, on a thread or event with no cause of its own."""

    with wglink_activity.because(wglink_activity.command_cause("solve")):
        job = wglink_activity.carrying(lambda: log.record("publish_status"))

    ran = threading.Thread(target=job)
    ran.start()
    ran.join(timeout=5)
    job()

    assert log.counts() == {"publish_status": {"command:solve": 2}}
    assert wglink_activity.current_cause() == wglink_activity.CAUSE_UNATTRIBUTED


def test_counted_records_before_the_body_can_raise(monkeypatch):
    fresh = wglink_activity.ActivityLog()
    monkeypatch.setattr(wglink_activity, "LOG", fresh)

    @wglink_activity.counted("resolve_links")
    def refuses():
        raise RuntimeError("refused")

    with pytest.raises(RuntimeError):
        refuses()

    assert fresh.counts() == {"resolve_links": {wglink_activity.CAUSE_UNATTRIBUTED: 1}}
