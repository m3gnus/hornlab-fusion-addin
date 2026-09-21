"""Failure injection on the M1 shipping path, and what a restart must not lose.

A8 briefs 2 and 5, add-in side. Every other transfer test replaces
``write_wg_request`` whole; these inject *inside* the atomic write
(``wglink_watch._write_json_atomically``): a rename that raises, including a
Windows-style sharing violation on the retry, an fsync that fails, and a write
that stops half way. The invariants:

* no partial or garbage request is ever visible under its final name, and the
  staging file never outlives the write;
* a retry re-writes the same id and fields (contract C5);
* when every attempt fails and nothing landed, the user is refused visibly --
  and a write that did land is never reported as "not asked" (C6).

Then the one-time start-up conversion of old outbox items when its write
raises, when the outbox file is held, and when the process dies between the
two; and the start-up re-check of requests a restart left waiting.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
import types

import pytest

from test_wglink_addin_lifecycle import _Application, _Definitions, _Panels, _UI, _load_instance
from test_wglink_transfer import (
    FIELDS,
    SNAPSHOT,
    SOLVE,
    _advertise,
    _deliver_followups,
    _fire_timers,
    _inbox,
    _outbox,
    _queue_both,
    _queued,
    _run,
    _transfer,
)


ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGLink"
sys.path.insert(0, str(ADDIN))
import wglink_live  # noqa: E402
import wglink_watch  # noqa: E402


def _sharing_violation() -> PermissionError:
    """What Windows raises when another process holds the file (WinError 32)."""

    error = PermissionError(
        errno.EACCES,
        "The process cannot access the file because it is being used by another process",
    )
    error.winerror = 32  # type: ignore[attr-defined]
    return error


def _os_with(watch_module, **overrides) -> types.SimpleNamespace:
    """``os`` as ``watch_module`` sees it, with some calls replaced.

    Replaced on the module's own ``os`` name only, so the rest of the process
    -- pytest included -- keeps the real functions.
    """

    real = watch_module.os
    proxy = types.SimpleNamespace(
        **{name: getattr(real, name) for name in dir(real) if not name.startswith("__")}
    )
    for name, value in overrides.items():
        setattr(proxy, name, value)
    return proxy


def _json_with_partial_dump(watch_module, failures: list[int]) -> types.SimpleNamespace:
    """``json`` whose ``dump`` writes part of the document and then fails, while
    ``failures`` holds a token (one token per failing call)."""

    real = watch_module.json

    def dump(payload, stream, **kwargs):
        if failures:
            failures.pop()
            text = real.dumps(payload, **kwargs)
            stream.write(text[: len(text) // 2])
            stream.flush()
            raise OSError(errno.ENOSPC, "No space left on device")
        return real.dump(payload, stream, **kwargs)

    return types.SimpleNamespace(dump=dump, dumps=real.dumps, loads=real.loads)


def _names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def _staging(folder: Path) -> list[str]:
    return [name for name in _names(folder) if name.endswith(".tmp")]


# -- inside the atomic write ------------------------------------------------------


def _write(tmp_path: Path, command_id: str = "op-1") -> Path:
    return wglink_watch.write_wg_request(
        tmp_path, kind=SOLVE, command_id=command_id, return_id="wgr_1", **FIELDS
    )


def test_control_a_healthy_write_leaves_exactly_the_request(tmp_path: Path) -> None:
    """The positive control for every "nothing left" below."""

    _advertise(tmp_path, 4)

    path = _write(tmp_path)

    assert _names(path.parent) == ["op-1.json"]
    assert json.loads(path.read_text())["operationId"] == "op-1"


def test_a_rename_that_raises_leaves_no_request_and_no_staging_file(
    monkeypatch, tmp_path: Path
) -> None:
    _advertise(tmp_path, 4)
    renames: list[tuple[str, str]] = []

    def refuse(source, destination):
        renames.append((Path(source).name, Path(destination).name))
        raise _sharing_violation()

    monkeypatch.setattr(wglink_watch, "os", _os_with(wglink_watch, replace=refuse))

    with pytest.raises(PermissionError):
        _write(tmp_path)

    [(staged, final)] = renames
    assert staged.startswith(".") and staged.endswith(".tmp")
    assert final == "op-1.json"
    assert _names(tmp_path / ".wg-solve-requests") == []


def test_an_fsync_that_fails_is_never_renamed_into_place(monkeypatch, tmp_path: Path) -> None:
    _advertise(tmp_path, 4)
    seen: list[list[str]] = []

    def fail(_descriptor):
        seen.append(_names(tmp_path / ".wg-solve-requests"))
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(wglink_watch, "os", _os_with(wglink_watch, fsync=fail))

    with pytest.raises(OSError, match="Input/output"):
        _write(tmp_path)

    # While the bytes were unsynced, only the hidden staging name existed.
    [during] = seen
    assert len(during) == 1 and during[0].startswith(".") and during[0].endswith(".tmp")
    assert _names(tmp_path / ".wg-solve-requests") == []


def test_a_write_that_stops_half_way_leaves_no_partial_request(monkeypatch, tmp_path: Path) -> None:
    _advertise(tmp_path, 4)
    monkeypatch.setattr(wglink_watch, "json", _json_with_partial_dump(wglink_watch, [1]))

    with pytest.raises(OSError, match="No space"):
        _write(tmp_path)

    assert _names(tmp_path / ".wg-solve-requests") == []


# -- the command: retry and outcome ----------------------------------------------


def _watch(fixture):
    return fixture.module.wglink_watch


def _outcome(fixture) -> str:
    return fixture.module._request_trace["outcome"]


def test_a_sharing_violation_on_the_first_rename_is_retried_under_the_same_id(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_fail_rename_once")
    watch = _watch(fixture)
    real_replace = watch.os.replace
    destinations: list[str] = []

    def once(source, destination):
        destinations.append(Path(destination).name)
        if len(destinations) == 1:
            raise _sharing_violation()
        return real_replace(source, destination)

    monkeypatch.setattr(watch, "os", _os_with(watch, replace=once))

    _run(fixture.module, "send")

    assert len(destinations) == 2 and destinations[0] == destinations[1]
    inbox = fixture.ipc / ".wg-solve-requests"
    [payload] = _inbox(fixture.ipc)
    assert destinations[0] == f"{payload['operationId']}.json"
    assert _staging(inbox) == []
    assert "Sent to Waveguide Generator" in fixture.ui.messages[-1][1]
    assert _outcome(fixture) == "requested"
    assert len(fixture.timers) == 1


def test_a_rename_that_landed_and_raised_is_not_reported_as_not_asked(
    monkeypatch, tmp_path: Path
) -> None:
    """The first rename lands and reports an error; the retry's rename then hits a
    sharing violation (WG, or a scanner, has the file open). The request is in the
    inbox, so "not asked" would be false, and a second Send a second operation."""

    fixture = _transfer(monkeypatch, tmp_path, "WGLink_fail_landed")
    watch = _watch(fixture)
    real_replace = watch.os.replace
    calls: list[int] = []

    def landed_then_held(source, destination):
        calls.append(1)
        if len(calls) == 1:
            real_replace(source, destination)
            raise OSError(errno.EIO, "the rename reported an error after it landed")
        raise _sharing_violation()

    monkeypatch.setattr(watch, "os", _os_with(watch, replace=landed_then_held))

    _run(fixture.module, "solve")

    assert len(calls) == fixture.module.WG_REQUEST_WRITE_ATTEMPTS
    [payload] = _inbox(fixture.ipc)
    assert payload["kind"] == SOLVE and payload["returnId"] == "wgr_1"
    assert _staging(fixture.ipc / ".wg-solve-requests") == []
    title, text = fixture.ui.messages[-1]
    assert title == "WGLink" and f"request {payload['operationId'][:8]}" in text
    assert _outcome(fixture) == "requested"
    assert len(fixture.timers) == 1


def test_renames_that_never_land_are_a_visible_refusal_with_nothing_left(
    monkeypatch, tmp_path: Path
) -> None:
    """The control for the one above: the same two failures, nothing landed."""

    fixture = _transfer(monkeypatch, tmp_path, "WGLink_fail_never_landed")
    watch = _watch(fixture)
    calls: list[int] = []

    def refuse(_source, _destination):
        calls.append(1)
        raise _sharing_violation()

    monkeypatch.setattr(watch, "os", _os_with(watch, replace=refuse))

    _run(fixture.module, "send")

    assert len(calls) == fixture.module.WG_REQUEST_WRITE_ATTEMPTS
    assert _names(fixture.ipc / ".wg-solve-requests") == []
    title, text = fixture.ui.messages[-1]
    assert title == "WGLink refused"
    assert "was not asked to take it" in text and "could not write the request" in text
    assert _outcome(fixture) == "writeFailed"
    assert fixture.timers == []


def test_a_garbled_file_under_the_final_name_is_not_taken_for_a_landed_write(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_fail_garbled")
    module = fixture.module
    watch = _watch(fixture)
    fixed = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(module, "uuid", types.SimpleNamespace(uuid4=lambda: fixed))
    inbox = fixture.ipc / ".wg-solve-requests"
    inbox.mkdir()
    (inbox / f"{fixed}.json").write_text('{"schemaVersion": 4, "commandId": "')

    def refuse(_source, _destination):
        raise _sharing_violation()

    monkeypatch.setattr(watch, "os", _os_with(watch, replace=refuse))

    _run(module, "send")

    assert fixture.ui.messages[-1][0] == "WGLink refused"
    assert _outcome(fixture) == "writeFailed"


@pytest.mark.parametrize("fault", ["fsync", "partial"])
def test_a_write_that_fails_every_time_is_refused_and_leaves_nothing(
    monkeypatch, tmp_path: Path, fault: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_fail_every_{fault}")
    watch = _watch(fixture)
    if fault == "fsync":
        def fail(_descriptor):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(watch, "os", _os_with(watch, fsync=fail))
    else:
        monkeypatch.setattr(watch, "json", _json_with_partial_dump(watch, [1, 1]))

    _run(fixture.module, "solve")

    assert _names(fixture.ipc / ".wg-solve-requests") == []
    assert fixture.ui.messages[-1][0] == "WGLink refused"
    assert _outcome(fixture) == "writeFailed"


def test_a_write_that_stops_half_way_once_is_retried_whole(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_fail_partial_once")
    watch = _watch(fixture)
    monkeypatch.setattr(watch, "json", _json_with_partial_dump(watch, [1]))

    _run(fixture.module, "send")

    [payload] = _inbox(fixture.ipc)
    assert payload["kind"] == SNAPSHOT and payload["bundlePath"] == "wgreturn/speaker.wgreturn"
    assert _staging(fixture.ipc / ".wg-solve-requests") == []
    assert _outcome(fixture) == "requested"


# -- start-up conversion: the failed branch ---------------------------------------


class _WriterThatFails:
    """``wglink_watch`` as ``convert_outbox`` sees it, with the write refused."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        return getattr(wglink_watch, name)

    def write_wg_request(self, _ipc, **fields):
        self.calls.append(fields["command_id"])
        raise _sharing_violation()


def test_a_conversion_whose_write_fails_keeps_the_item_and_the_next_start_rewrites_its_id(
    tmp_path: Path,
) -> None:
    _advertise(tmp_path, 4)
    _queue_both(tmp_path)
    refusing = _WriterThatFails()

    first = wglink_live.convert_outbox(tmp_path, solve_files=refusing, live_will_run=False)

    assert sorted(first["failed"]) == ["snap-1", "solve-1"]
    assert first["converted"] == []
    assert _inbox(tmp_path) == []
    assert sorted(item["operationId"] for item in _outbox(tmp_path)) == ["snap-1", "solve-1"]

    second = wglink_live.convert_outbox(tmp_path, solve_files=wglink_watch, live_will_run=False)

    assert sorted(second["converted"]) == ["snap-1", "solve-1"]
    assert sorted(payload["operationId"] for payload in _inbox(tmp_path)) == ["snap-1", "solve-1"]
    assert _outbox(tmp_path) == []


@pytest.mark.parametrize("wg_took_the_first", [False, True])
def test_a_conversion_whose_outbox_file_is_held_is_kept_and_rewritten_under_the_same_id(
    monkeypatch, tmp_path: Path, wg_took_the_first: bool
) -> None:
    """``outbox.delete`` returns False (Windows: the item file is held open)."""

    _advertise(tmp_path, 4)
    _queued(tmp_path, wglink_live.KIND_SOLVE, "solve-9")
    monkeypatch.setattr(wglink_live.Outbox, "delete", lambda _self, _operation_id: False)

    first = wglink_live.convert_outbox(tmp_path, solve_files=wglink_watch, live_will_run=False)

    assert first["failed"] == ["solve-9"] and first["converted"] == []
    [written] = _inbox(tmp_path)
    assert written["operationId"] == "solve-9"
    assert [item["operationId"] for item in _outbox(tmp_path)] == ["solve-9"]
    if wg_took_the_first:
        (tmp_path / ".wg-solve-requests" / "solve-9.json").unlink()
    monkeypatch.undo()

    second = wglink_live.convert_outbox(tmp_path, solve_files=wglink_watch, live_will_run=False)

    assert second["converted"] == ["solve-9"]
    # One file, the same id and fields: WG recovers a second delivery of it as
    # the same operation, and nothing new was invented.
    assert _inbox(tmp_path) == [written]
    assert _outbox(tmp_path) == []


_CRASH_BETWEEN_WRITE_AND_DELETE = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import wglink_live, wglink_watch

    def die(_self, _operation_id):
        os._exit(17)  # the process ends here: no cleanup, no finally

    wglink_live.Outbox.delete = die
    wglink_live.convert_outbox(Path(sys.argv[2]), solve_files=wglink_watch, live_will_run=False)
    sys.exit(0)
    """
)


def test_a_crash_between_the_conversion_write_and_the_delete_is_finished_by_the_next_start(
    tmp_path: Path,
) -> None:
    _advertise(tmp_path, 4)
    _queued(tmp_path, wglink_live.KIND_SNAPSHOT, "snap-7")
    script = tmp_path / "crash.py"
    script.write_text(_CRASH_BETWEEN_WRITE_AND_DELETE)

    crashed = subprocess.run(
        [sys.executable, str(script), str(ADDIN), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert crashed.returncode == 17, crashed.stderr
    [written] = _inbox(tmp_path)
    assert written["operationId"] == "snap-7"
    assert [item["operationId"] for item in _outbox(tmp_path)] == ["snap-7"]
    assert _staging(tmp_path / ".wg-solve-requests") == []

    result = wglink_live.convert_outbox(tmp_path, solve_files=wglink_watch, live_will_run=False)

    assert result["converted"] == ["snap-7"]
    assert _inbox(tmp_path) == [written]
    assert _outbox(tmp_path) == []


# -- start-up: the wrapper, and the report ------------------------------------------


def _start(monkeypatch, tmp_path: Path, name: str, *, coordination: bool | None = None):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    app = _Application(ui)
    module = _load_instance(monkeypatch, name, ui, app)
    if coordination is not None:
        module.SETTINGS_PATH.write_text(json.dumps({"automatic_coordination": coordination}))
    ipc = tmp_path / "ipc"
    ipc.mkdir(exist_ok=True)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: ipc)
    timers: list[tuple[float, object]] = []
    monkeypatch.setattr(module, "_start_timer", lambda delay, function: timers.append((delay, function)))
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    return types.SimpleNamespace(module=module, ui=ui, app=app, ipc=ipc, timers=timers)


_EARLIER = "WGLink earlier requests waiting"


@pytest.mark.parametrize("coordination", [False, None], ids=["off", "on"])
def test_a_conversion_that_raises_is_shown_and_start_up_carries_on(
    monkeypatch, tmp_path: Path, coordination: bool | None
) -> None:
    fixture = _start(monkeypatch, tmp_path, f"WGLink_convert_raises_{coordination}", coordination=coordination)
    module = fixture.module
    _advertise(fixture.ipc, 4)
    _queue_both(fixture.ipc)

    def explode(*_args, **_kwargs):
        raise RuntimeError("the outbox index is unreadable")

    monkeypatch.setattr(module.wglink_live, "convert_outbox", explode)
    started: list[str] = []
    real_start = module._start_live
    monkeypatch.setattr(module, "_start_live", lambda: (started.append("live"), real_start())[1])

    module.run(None)
    try:
        titles = [title for title, _text in fixture.ui.messages]
        assert _EARLIER in titles
        text = dict(fixture.ui.messages)[_EARLIER]
        assert "the outbox index is unreadable" in text and "Nothing was dropped" in text
        # Start-up went on: the panel is built and owned, and the live session
        # starts exactly when the gate says it should.
        assert module._owned is True
        assert "WGLink start error" not in titles
        assert started == ([] if coordination is False else ["live"])
        assert sorted(item["operationId"] for item in _outbox(fixture.ipc)) == ["snap-1", "solve-1"]
    finally:
        module.stop(None)


def test_control_a_clean_conversion_shows_no_earlier_requests_notice(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _start(monkeypatch, tmp_path, "WGLink_convert_clean", coordination=False)
    _advertise(fixture.ipc, 4)
    _queue_both(fixture.ipc)

    fixture.module.run(None)
    try:
        assert _EARLIER not in [title for title, _text in fixture.ui.messages]
        assert len(_inbox(fixture.ipc)) == 2
    finally:
        fixture.module.stop(None)


def test_items_start_up_could_not_convert_are_reported_and_converted_next_start_under_their_ids(
    monkeypatch, tmp_path: Path
) -> None:
    first = _start(monkeypatch, tmp_path, "WGLink_convert_failed_1", coordination=False)
    _advertise(first.ipc, 4)
    _queue_both(first.ipc)

    def refuse(_ipc, **_fields):
        raise _sharing_violation()

    monkeypatch.setattr(first.module.wglink_watch, "write_wg_request", refuse)
    first.module.run(None)
    outcomes = {(o["requestId"], o["outcome"]) for o in first.module._recent_outcomes}
    first.module.stop(None)

    text = dict(first.ui.messages)[_EARLIER]
    assert text.startswith("2 request(s)") and "same request ids" in text
    assert outcomes >= {("snap-1", "conversionFailed"), ("solve-1", "conversionFailed")}
    assert _inbox(first.ipc) == []
    assert len(_outbox(first.ipc)) == 2

    second = _start(monkeypatch, tmp_path, "WGLink_convert_failed_2", coordination=False)
    second.module.run(None)
    try:
        assert sorted(payload["operationId"] for payload in _inbox(second.ipc)) == ["snap-1", "solve-1"]
        assert _outbox(second.ipc) == []
        assert _EARLIER not in [title for title, _text in second.ui.messages]
    finally:
        second.module.stop(None)


# -- start-up: requests a restart left waiting (brief 5) ------------------------------


_WAITING = "WGLink request waiting"
_CAUSES = ("closed", "older than this WGLink add-in", "not collecting requests")


def _leave_request(ipc: Path, command_id: str, *, kind: str = SNAPSHOT, age: float = 3600.0) -> Path:
    _advertise(ipc, 4)
    path = wglink_watch.write_wg_request(
        ipc, kind=kind, command_id=command_id, return_id="wgr_1", **FIELDS
    )
    moment = time.time() - age
    os.utime(path, (moment, moment))
    return path


def _startup_checks(module) -> dict[str, int]:
    return module.wglink_activity.LOG.counts().get("pickup_check", {})


@pytest.mark.parametrize("coordination", [False, None], ids=["off", "on"])
def test_a_request_left_waiting_across_a_restart_is_reported_once_at_start_up(
    monkeypatch, tmp_path: Path, coordination: bool | None
) -> None:
    fixture = _start(monkeypatch, tmp_path, f"WGLink_restart_old_{coordination}", coordination=coordination)
    _leave_request(fixture.ipc, "left-1")
    _leave_request(fixture.ipc, "left-2", kind=SOLVE)

    fixture.module.run(None)
    try:
        waiting = [text for title, text in fixture.ui.messages if title == _WAITING]
        assert len(waiting) == 1
        text = waiting[0]
        assert text.startswith("2 request(s)")
        for cause in _CAUSES:
            assert cause in text
        assert "solve" not in text.lower() and "send" not in text.lower()
        assert _startup_checks(fixture.module) == {"startup": 1}
        # Checked at once, because both were already past the window: no timer.
        assert fixture.timers == []
        # Nothing was moved or dropped.
        assert len(_inbox(fixture.ipc)) == 2
    finally:
        fixture.module.stop(None)


def test_the_start_up_re_check_reads_the_inbox_and_nothing_in_fusion(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _start(monkeypatch, tmp_path, "WGLink_restart_no_cad")
    module = fixture.module
    _leave_request(fixture.ipc, "left-3")
    reads: list[int] = []
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: reads.append(1) or {})
    before = module.wglink_activity.LOG.counts()

    with module.wglink_activity.because(module.wglink_activity.CAUSE_STARTUP):
        module._recheck_requests_at_startup()

    after = module.wglink_activity.LOG.counts()
    added = {
        name: {cause: count - before.get(name, {}).get(cause, 0) for cause, count in causes.items()}
        for name, causes in after.items()
    }
    added = {name: {c: n for c, n in causes.items() if n} for name, causes in added.items()}
    assert {name: causes for name, causes in added.items() if causes} == {"pickup_check": {"startup": 1}}
    assert reads == []
    assert [title for title, _text in fixture.ui.messages] == [_WAITING]


def test_a_request_younger_than_the_window_gets_the_rest_of_its_minute(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _start(monkeypatch, tmp_path, "WGLink_restart_young", coordination=False)
    module = fixture.module
    _leave_request(fixture.ipc, "old-1")
    _leave_request(fixture.ipc, "young-1", age=20.0)

    module.run(None)
    try:
        assert _WAITING not in [title for title, _text in fixture.ui.messages]
        [(delay, _function)] = fixture.timers
        window = module.wglink_watch.SOLVE_PICKUP_NOTICE_SECONDS
        assert window - 25.0 < delay <= window - 20.0 + 1.0
        _fire_timers(fixture)
        _deliver_followups(module, fixture.app)
        waiting = [text for title, text in fixture.ui.messages if title == _WAITING]
        assert len(waiting) == 1 and waiting[0].startswith("2 request(s)")
        assert _startup_checks(module) == {"startup": 1}
    finally:
        module.stop(None)


def test_control_a_request_wg_takes_before_the_check_is_not_reported(
    monkeypatch, tmp_path: Path
) -> None:
    """Positive control for the notice: the same measurement, and WG took it."""

    fixture = _start(monkeypatch, tmp_path, "WGLink_restart_taken", coordination=False)
    module = fixture.module
    path = _leave_request(fixture.ipc, "young-2", age=5.0)

    module.run(None)
    try:
        path.unlink()  # WG claims it by rename, then deletes it
        _fire_timers(fixture)
        _deliver_followups(module, fixture.app)
        assert _WAITING not in [title for title, _text in fixture.ui.messages]
        assert {"channel": "startup", "requestId": "young-2", "outcome": "taken"} in module._recent_outcomes
        assert _startup_checks(module) == {"startup": 1}
    finally:
        module.stop(None)


def test_nothing_waiting_means_no_check_no_timer_and_no_notice(monkeypatch, tmp_path: Path) -> None:
    fixture = _start(monkeypatch, tmp_path, "WGLink_restart_nothing", coordination=False)
    inbox = fixture.ipc / ".wg-solve-requests"
    inbox.mkdir()
    # Not requests waiting to be taken: a claim WG holds, a staging file, a file
    # for another target, and a file whose id is not its name.
    stale = time.time() - 3600
    for name, body in {
        ".wg-solve-claim-abc.json": {"schemaVersion": 4, "target": "waveguide-generator", "commandId": "x"},
        ".left-9.json.abc.tmp": {"schemaVersion": 4, "target": "waveguide-generator", "commandId": "left-9"},
        "other.json": {"schemaVersion": 4, "target": "fusion", "commandId": "other"},
        "renamed.json": {"schemaVersion": 4, "target": "waveguide-generator", "commandId": "elsewhere"},
    }.items():
        (inbox / name).write_text(json.dumps(body))
        os.utime(inbox / name, (stale, stale))

    fixture.module.run(None)
    try:
        assert _WAITING not in [title for title, _text in fixture.ui.messages]
        assert fixture.timers == []
        assert _startup_checks(fixture.module) == {}
    finally:
        fixture.module.stop(None)
