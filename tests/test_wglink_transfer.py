"""The M1 transfer path: one request file for Send and Solve (contract C1-C6, C8).

Send and Solve write one file each into WG's request inbox,
``<ipc>/.wg-solve-requests/<operationId>.json``, and differ only in ``kind``.
There is no second path: no live outbox item is made for new work in either
gate state. The file's schema follows what WG advertises, so a schema-3
reader -- which ignores ``kind`` -- can never be handed a Send.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import types

import pytest

from test_wglink_addin_lifecycle import (
    _Application,
    _Definitions,
    _Panels,
    _UI,
    _dialog_inputs,
    _load_instance,
    _per_request_folders,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_live  # noqa: E402
import wglink_watch  # noqa: E402


SNAPSHOT = wglink_watch.KIND_RECEIVE_SNAPSHOT
SOLVE = wglink_watch.KIND_PREPARE_AND_SOLVE
FIELDS = {
    "bundle_relative": "wgreturn/speaker.wgreturn",
    "manifest_sha256": "sha256:" + "a" * 64,
    "requested_at": "2026-09-21T10:00:00Z",
}


def _advertise(ipc: Path, value: object, *, live: bool = False) -> None:
    payload: dict[str, object] = {"schemaVersion": 1, "fusionRequestDelivery": 3}
    if value is not None:
        payload["solveCommandDelivery"] = value
    if live:
        payload["liveProtocol"] = 1
    (ipc / wglink_watch.CAPABILITIES_FILENAME).write_text(json.dumps(payload))


def _inbox(ipc: Path) -> list[dict]:
    folder = ipc / wglink_watch.SOLVE_REQUESTS_DIRECTORY
    if not folder.is_dir():
        return []
    return [
        json.loads(path.read_text())
        for path in sorted(folder.iterdir())
        if not path.name.startswith(".")
    ]


def _outbox(ipc: Path) -> list[dict]:
    items, _foreign = wglink_live.Outbox(ipc).scan()
    return items


# -- C2/C3: the writer and the versioning rule ---------------------------------


def test_a_send_for_a_wg_that_reads_schema_4_is_a_receive_snapshot_without_return_id(
    tmp_path: Path,
) -> None:
    _advertise(tmp_path, 4)

    path = wglink_watch.write_wg_request(
        tmp_path, kind=SNAPSHOT, command_id="op-1", return_id="wgr_ignored", **FIELDS
    )

    assert path == tmp_path / ".wg-solve-requests" / "op-1.json"
    assert json.loads(path.read_text()) == {
        "schemaVersion": 4,
        "target": "waveguide-generator",
        "kind": "receive_snapshot",
        "commandId": "op-1",
        "operationId": "op-1",
        "bundlePath": "wgreturn/speaker.wgreturn",
        "manifestSha256": "sha256:" + "a" * 64,
        "requestedAt": "2026-09-21T10:00:00Z",
    }


@pytest.mark.parametrize("return_id", ["wgr_1", ""])
def test_a_solve_for_a_wg_that_reads_schema_4_carries_its_return_id_even_empty(
    tmp_path: Path, return_id: str
) -> None:
    _advertise(tmp_path, 4)

    path = wglink_watch.write_wg_request(
        tmp_path, kind=SOLVE, command_id="op-2", return_id=return_id, **FIELDS
    )

    payload = json.loads(path.read_text())
    assert (payload["schemaVersion"], payload["kind"], payload["returnId"]) == (4, SOLVE, return_id)
    assert payload["commandId"] == payload["operationId"] == "op-2"


def test_a_solve_for_a_wg_that_reads_schema_3_is_written_exactly_as_before(tmp_path: Path) -> None:
    _advertise(tmp_path, 3)

    path = wglink_watch.write_wg_request(
        tmp_path, kind=SOLVE, command_id="op-3", return_id="wgr_1", **FIELDS
    )

    payload = json.loads(path.read_text())
    assert payload["schemaVersion"] == 3
    assert "kind" not in payload
    assert payload["returnId"] == "wgr_1"


def test_a_send_for_a_wg_that_reads_only_schema_3_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """A schema-3 reader ignores ``kind``: a Send written for it would start a solve."""

    _advertise(tmp_path, 3)

    with pytest.raises(wglink_watch.WgOutdatedError, match="Update Waveguide Generator"):
        wglink_watch.write_wg_request(
            tmp_path, kind=SNAPSHOT, command_id="op-4", return_id=None, **FIELDS
        )

    assert _inbox(tmp_path) == []


@pytest.mark.parametrize(
    ("advertised", "error"),
    [
        ("no-file", wglink_watch.WgNotCollectingError),
        (None, wglink_watch.WgNotCollectingError),
        (2, wglink_watch.WgOutdatedError),
    ],
    ids=["no-capabilities", "no-solve-delivery", "older-wg"],
)
@pytest.mark.parametrize("kind", [SNAPSHOT, SOLVE])
def test_a_wg_that_advertises_nothing_or_less_than_3_gets_nothing(
    tmp_path: Path, advertised: object, error: type, kind: str
) -> None:
    if advertised != "no-file":
        _advertise(tmp_path, advertised)

    with pytest.raises(error):
        wglink_watch.write_wg_request(
            tmp_path, kind=kind, command_id="op-5", return_id="", **FIELDS
        )

    assert _inbox(tmp_path) == []


@pytest.mark.parametrize("advertised", ["no-file", None, 1, 2, 3, 4, 5, "4", True])
def test_no_send_is_ever_written_in_a_schema_a_reader_could_run_as_a_solve(
    tmp_path: Path, advertised: object
) -> None:
    """The versioning rule as one property, over every advertisement."""

    if advertised != "no-file":
        _advertise(tmp_path, advertised)
    try:
        wglink_watch.write_wg_request(
            tmp_path, kind=SNAPSHOT, command_id="op-6", return_id=None, **FIELDS
        )
    except (wglink_watch.WgOutdatedError, wglink_watch.WgNotCollectingError):
        pass

    for payload in _inbox(tmp_path):
        assert payload["schemaVersion"] >= wglink_watch.WG_REQUEST_SCHEMA_VERSION
        assert payload["kind"] == SNAPSHOT
        assert "returnId" not in payload


def test_the_inbox_schema_is_its_own_constant(tmp_path: Path) -> None:
    """The Fusion-bound reader and the heartbeat keep version 3 (C3)."""

    assert wglink_watch.WG_REQUEST_SCHEMA_VERSION == 4
    assert wglink_watch.REQUEST_SCHEMA_VERSION == 3
    assert wglink_watch.DELIVERY_VERSION == 3


@pytest.mark.parametrize("command_id", ["", "../escape", "a" * 129, "has space"])
def test_an_id_that_is_not_a_plain_file_name_is_refused(tmp_path: Path, command_id: str) -> None:
    _advertise(tmp_path, 4)

    with pytest.raises(ValueError):
        wglink_watch.write_wg_request(
            tmp_path, kind=SNAPSHOT, command_id=command_id, return_id=None, **FIELDS
        )


# -- the commands: one path, synchronous outcome -------------------------------


def _transfer(monkeypatch, tmp_path: Path, name: str, *, advertised: object = 4, live: bool = False):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(monkeypatch, name, ui, app)
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _advertise(ipc, advertised, live=live)
    bundle = tmp_path / "workspace" / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b'{"return": "speaker"}')
    monkeypatch.setattr(
        module.wglink_send,
        "send",
        lambda _app, _options, **_kwargs: {
            "bundle_path": str(bundle),
            "return_id": "wgr_1",
            "scope": {"status": "clean"},
            "sources": [],
        },
    )
    timers: list[tuple[float, object]] = []
    monkeypatch.setattr(
        module, "_start_timer", lambda delay, function: timers.append((delay, function))
    )
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    return types.SimpleNamespace(module=module, ui=ui, app=app, ipc=ipc, bundle=bundle, timers=timers)


def _run(module, operation: str) -> None:
    module.CommandExecuteHandler(operation).notify(
        types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=_dialog_inputs()))
    )


@pytest.mark.parametrize(("operation", "kind"), [("send", SNAPSHOT), ("solve", SOLVE)])
def test_send_and_solve_write_one_request_differing_only_in_kind(
    monkeypatch, tmp_path: Path, operation: str, kind: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_one_path_{operation}")

    _run(fixture.module, operation)

    [payload] = _inbox(fixture.ipc)
    assert payload["kind"] == kind
    assert payload["schemaVersion"] == 4
    assert payload["bundlePath"] == "wgreturn/speaker.wgreturn"
    assert ("returnId" in payload) is (kind == SOLVE)
    assert _outbox(fixture.ipc) == []
    title, text = fixture.ui.messages[-1]
    assert title == "WGLink"
    assert f"Sent to Waveguide Generator (request {payload['operationId'][:8]})" in text
    assert fixture.module._request_trace["correlationId"] == payload["operationId"]


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_a_healthy_live_session_does_not_make_a_second_path(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    """C1: whatever the gate says, new work is a file and never an outbox item."""

    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_live_one_path_{operation}", live=True)
    enqueued: list[int] = []
    client = types.SimpleNamespace(
        healthy=lambda: True,
        enqueue_delivery=lambda: enqueued.append(1),
        take_log_lines=lambda: [],
    )
    monkeypatch.setattr(fixture.module, "_live_client", client)

    _run(fixture.module, operation)

    assert len(_inbox(fixture.ipc)) == 1
    assert _outbox(fixture.ipc) == []
    assert enqueued == []


def test_a_send_to_a_wg_that_reads_only_schema_3_is_refused_visibly(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_send_old", advertised=3)

    _run(fixture.module, "send")

    assert _inbox(fixture.ipc) == [] and _outbox(fixture.ipc) == []
    title, text = fixture.ui.messages[-1]
    assert title == "WGLink refused"
    assert "was not asked to take it" in text
    assert "Update Waveguide Generator" in text
    assert fixture.timers == []


def test_a_solve_to_a_wg_that_reads_only_schema_3_still_solves(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_solve_old", advertised=3)

    _run(fixture.module, "solve")

    [payload] = _inbox(fixture.ipc)
    assert payload["schemaVersion"] == 3 and "kind" not in payload
    assert "Sent to Waveguide Generator" in fixture.ui.messages[-1][1]


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_a_wg_not_collecting_requests_is_named_at_command_time(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_not_collecting_{operation}", advertised=None)

    _run(fixture.module, operation)

    assert _inbox(fixture.ipc) == []
    title, text = fixture.ui.messages[-1]
    assert title == "WGLink refused"
    assert "not collecting requests from Fusion" in text


def test_a_write_whose_outcome_is_unknown_is_retried_once_with_the_same_id(
    monkeypatch, tmp_path: Path
) -> None:
    """C5: the retry re-writes the same file; WG recovers it as the same operation."""

    fixture = _transfer(monkeypatch, tmp_path, "WGLink_retry_once")
    real = fixture.module.wglink_watch.write_wg_request
    calls: list[dict] = []

    def flaky(ipc, **fields):
        calls.append(dict(fields))
        if len(calls) == 1:
            real(ipc, **fields)
            raise OSError("the rename reported an error after it landed")
        return real(ipc, **fields)

    monkeypatch.setattr(fixture.module.wglink_watch, "write_wg_request", flaky)

    _run(fixture.module, "send")

    assert len(calls) == 2 and calls[0] == calls[1]
    [payload] = _inbox(fixture.ipc)
    assert payload["operationId"] == calls[0]["command_id"]
    assert "Sent to Waveguide Generator" in fixture.ui.messages[-1][1]


def test_a_write_that_keeps_failing_is_a_visible_refusal(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_write_fails")
    calls: list[int] = []

    def failing(_ipc, **_fields):
        calls.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(fixture.module.wglink_watch, "write_wg_request", failing)

    _run(fixture.module, "solve")

    assert len(calls) == fixture.module.WG_REQUEST_WRITE_ATTEMPTS
    title, text = fixture.ui.messages[-1]
    assert title == "WGLink refused"
    assert "could not write the request for WG" in text and "disk full" in text


def test_a_refusal_is_an_answer_and_is_never_retried(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_refusal_not_retried", advertised=2)
    calls: list[int] = []
    real = fixture.module.wglink_watch.write_wg_request

    def counting(ipc, **fields):
        calls.append(1)
        return real(ipc, **fields)

    monkeypatch.setattr(fixture.module.wglink_watch, "write_wg_request", counting)

    _run(fixture.module, "solve")

    assert calls == [1]


def test_pressing_send_again_is_a_new_request(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_send_twice")

    _run(fixture.module, "send")
    _run(fixture.module, "send")

    ids = {payload["operationId"] for payload in _inbox(fixture.ipc)}
    assert len(ids) == 2


# -- C6: the pickup check -------------------------------------------------------


def _deliver_followups(module, app) -> None:
    event = app.events.get(module.FOLLOWUP_EVENT_ID)
    handlers = list(event.handlers) if event is not None else [module.FollowupEventHandler()]
    for handler in handlers:
        handler.notify(None)


def _fire_timers(fixture) -> None:
    for _delay, function in list(fixture.timers):
        function()
    fixture.timers.clear()


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_a_request_wg_leaves_untaken_is_reported_once_under_its_command(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_pickup_{operation}")
    module = fixture.module
    reads: list[int] = []
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: reads.append(1) or {})

    _run(module, operation)
    [(delay, _function)] = fixture.timers
    assert delay == module.wglink_watch.SOLVE_PICKUP_NOTICE_SECONDS
    before = len(fixture.ui.messages)
    _fire_timers(fixture)
    _deliver_followups(module, fixture.app)

    titles = [title for title, _text in fixture.ui.messages[before:]]
    assert titles == ["WGLink request waiting"]
    text = fixture.ui.messages[-1][1]
    for cause in ("closed", "older than this WGLink add-in", "not collecting requests"):
        assert cause in text
    assert "solve" not in text.lower()
    # The request is not moved or dropped, and nothing in Fusion was read.
    assert len(_inbox(fixture.ipc)) == 1
    assert reads == []
    assert module.wglink_activity.LOG.counts()["pickup_check"] == {f"command:{operation}": 1}


def test_a_request_wg_took_is_not_reported(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_pickup_taken")
    module = fixture.module

    _run(module, "send")
    for path in (fixture.ipc / ".wg-solve-requests").iterdir():
        path.unlink()
    before = len(fixture.ui.messages)
    _fire_timers(fixture)
    _deliver_followups(module, fixture.app)

    assert fixture.ui.messages[before:] == []
    assert module._recent_outcomes[-1]["outcome"] == "taken"
    assert module.wglink_activity.LOG.counts()["pickup_check"] == {"command:send": 1}


def test_a_pickup_check_held_behind_a_command_runs_when_it_ends(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_pickup_busy")
    module = fixture.module
    fired: list[str] = []
    monkeypatch.setattr(fixture.app, "fireCustomEvent", lambda event_id, *_a: fired.append(event_id))
    monkeypatch.setattr(module, "_followup_event", object())

    _run(module, "send")
    module._command_busy = True
    _fire_timers(fixture)
    before = len(fixture.ui.messages)
    _deliver_followups(module, fixture.app)
    assert fixture.ui.messages[before:] == []
    assert len(module._followups) == 1

    # The command that held the thread finishes (the user cancels Detach).
    fixture.ui.dialog_result = "no"
    module._command_busy = False
    fired.clear()
    _run(module, "detach")
    assert module.FOLLOWUP_EVENT_ID in fired
    _deliver_followups(module, fixture.app)
    assert [title for title, _text in fixture.ui.messages] [-1] == "WGLink request waiting"


@pytest.mark.parametrize("coordination", [False, None], ids=["off", "on"])
def test_the_pickup_check_rides_its_own_event_in_both_gate_states(
    monkeypatch, tmp_path: Path, coordination: bool | None
) -> None:
    """C8: the transfer path is behind neither gate."""

    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_pickup_gate_{coordination}")
    module = fixture.module
    if coordination is not None:
        module.SETTINGS_PATH.write_text(json.dumps({"automatic_coordination": coordination}))
    module.run(None)
    try:
        assert module.FOLLOWUP_EVENT_ID in fixture.app.events
        _run(module, "send")
        _fire_timers(fixture)
        before = len(fixture.ui.messages)
        _deliver_followups(module, fixture.app)
        assert [title for title, _text in fixture.ui.messages[before:]] == ["WGLink request waiting"]
    finally:
        module.stop(None)


# -- C1/E1: converting what an earlier session queued ----------------------------


def _queued(ipc: Path, kind: str, operation_id: str, *, file_written: bool = False, answered: bool = False) -> None:
    item = wglink_live.delivery_item(
        kind,
        operation_id=operation_id,
        bundle_path="wgreturn/speaker.wgreturn",
        manifest_sha256="sha256:" + "b" * 64,
        requested_at="2026-09-20T12:00:00Z",
        created_at="2026-09-20T12:00:00.000Z",
        return_id="wgr_9" if kind == wglink_live.KIND_SOLVE else None,
        file_written=file_written,
    )
    if answered:
        item["answer"] = {"outcome": "delivered"}
    wglink_live.Outbox(ipc).add(item)


def _queue_both(ipc: Path) -> None:
    _queued(ipc, wglink_live.KIND_SNAPSHOT, "snap-1")
    _queued(ipc, wglink_live.KIND_SOLVE, "solve-1")


def test_at_schema_4_every_unanswered_item_becomes_its_request_file(tmp_path: Path) -> None:
    _advertise(tmp_path, 4)
    _queue_both(tmp_path)
    _queued(tmp_path, wglink_live.KIND_SNAPSHOT, "done-1", answered=True)

    result = wglink_live.convert_outbox(tmp_path, solve_files=wglink_watch, live_will_run=True)

    assert sorted(result["converted"]) == ["snap-1", "solve-1"]
    by_id = {payload["operationId"]: payload for payload in _inbox(tmp_path)}
    assert by_id["snap-1"]["kind"] == SNAPSHOT and by_id["snap-1"]["schemaVersion"] == 4
    assert by_id["solve-1"]["kind"] == SOLVE and by_id["solve-1"]["returnId"] == "wgr_9"
    assert by_id["snap-1"]["requestedAt"] == "2026-09-20T12:00:00Z"
    assert [item["operationId"] for item in _outbox(tmp_path)] == ["done-1"]


@pytest.mark.parametrize("live_will_run", [True, False])
def test_at_schema_3_a_solve_converts_and_a_snapshot_waits_only_for_a_live_worker(
    tmp_path: Path, live_will_run: bool
) -> None:
    _advertise(tmp_path, 3)
    _queue_both(tmp_path)

    result = wglink_live.convert_outbox(tmp_path, solve_files=wglink_watch, live_will_run=live_will_run)

    [solve] = _inbox(tmp_path)
    assert solve["operationId"] == "solve-1" and solve["schemaVersion"] == 3
    left = [item["operationId"] for item in _outbox(tmp_path)]
    if live_will_run:
        assert left == ["snap-1"] and result["left"] == ["snap-1"]
    else:
        assert left == [] and result["abandoned"] == ["snap-1"]


@pytest.mark.parametrize("advertised", ["no-file", None, 2])
def test_when_wg_advertises_nothing_nothing_moves(tmp_path: Path, advertised: object) -> None:
    if advertised != "no-file":
        _advertise(tmp_path, advertised)
    _queue_both(tmp_path)

    result = wglink_live.convert_outbox(tmp_path, solve_files=wglink_watch, live_will_run=False)

    assert _inbox(tmp_path) == []
    assert sorted(item["operationId"] for item in _outbox(tmp_path)) == ["snap-1", "solve-1"]
    assert sorted(result["left"]) == ["snap-1", "solve-1"]


def test_a_solve_whose_file_was_written_when_queued_gets_no_second_file(tmp_path: Path) -> None:
    _advertise(tmp_path, 4)
    _queued(tmp_path, wglink_live.KIND_SOLVE, "solve-2", file_written=True)

    result = wglink_live.convert_outbox(tmp_path, solve_files=wglink_watch, live_will_run=False)

    assert result["converted"] == ["solve-2"]
    assert _inbox(tmp_path) == [] and _outbox(tmp_path) == []


@pytest.mark.parametrize("coordination", [False, None], ids=["off", "on"])
def test_start_up_converts_before_any_live_thread_starts(
    monkeypatch, tmp_path: Path, coordination: bool | None
) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    app = _Application(ui)
    module = _load_instance(monkeypatch, f"WGLink_convert_{coordination}", ui, app)
    if coordination is not None:
        module.SETTINGS_PATH.write_text(json.dumps({"automatic_coordination": coordination}))
    ipc = tmp_path / "ipc"
    ipc.mkdir()
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: ipc)
    _advertise(ipc, 4)
    _queue_both(ipc)
    order: list[str] = []
    real_convert = module.wglink_live.convert_outbox
    monkeypatch.setattr(
        module.wglink_live,
        "convert_outbox",
        lambda *a, **k: (order.append("convert"), real_convert(*a, **k))[1],
    )
    real_start = module._start_live
    monkeypatch.setattr(module, "_start_live", lambda: (order.append("live"), real_start())[1])

    module.run(None)
    try:
        assert order == (["convert"] if coordination is False else ["convert", "live"])
        assert sorted(payload["operationId"] for payload in _inbox(ipc)) == ["snap-1", "solve-1"]
        assert _outbox(ipc) == []
    finally:
        module.stop(None)
