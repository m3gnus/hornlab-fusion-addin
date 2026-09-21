"""The activation boundary (STEP2 section 4) and the execution counters wired to it.

The acceptance criterion is about what runs *between* the user's commands, so
every "nothing ran" here is a count read from ``wglink_activity`` -- recorded
at the entry point itself, outside any advisory ``except`` -- and every such
zero has a sibling in which the same count rises. An unwired counter would
otherwise pass every idle test while measuring nothing.

"Between commands" is modelled by delivering every custom event the add-in has
registered, several times, after the thing a user might do (switch document,
edit, reopen, restart WG). That is everything Fusion can call back into; with
coordination on it includes the watch tick, and the tick's work shows up
attributed to the tick, which is today's baseline.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import types

import pytest

from test_wglink_addin_lifecycle import (
    _Application,
    _Definitions,
    _Panels,
    _SelectionInput,
    _UI,
    _chosen,
    _dialog_inputs,
    _fake_body,
    _fake_face,
    _linked_document,
    _load_instance,
    _per_request_folders,
    _per_request_handoff,
    _recording,
)


ENGINE_THREADS = {
    "WGLinkExportWatch",
    "WGLinkOwnerCandidate",
    "WGLinkLiveSend",
    "WGLinkLivePoll",
}


def _write_gate(module, value: object) -> None:
    """Set the owner's switch the way the owner does: in WGLink's settings file."""

    module.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    module.SETTINGS_PATH.write_text(json.dumps({ACTIVATION: value}), encoding="utf-8")


ACTIVATION = "automatic_coordination"


def _boundary(monkeypatch, tmp_path: Path, name: str, *, coordination: bool | None):
    """One registration in a Fusion holding a real linked document, not yet started."""

    ui = _UI(_Panels(), _Definitions(reserve_ids=False), dialog_result="yes")
    app = _Application(ui)
    module = _load_instance(monkeypatch, name, ui, app)
    if coordination is not None:
        _write_gate(module, coordination)
    ipc = tmp_path / "ipc"
    ipc.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: ipc)
    design, body = _linked_document(module)
    body.revisionId = "revision-1"
    app.activeProduct = design
    app.activeDocument = types.SimpleNamespace(name="Horn")
    return types.SimpleNamespace(
        module=module, ui=ui, app=app, ipc=ipc, design=design, body=body
    )


def _deliver_every_event(app, rounds: int = 3) -> None:
    """Fusion calling back into everything registered: what "between commands" can run."""

    for _round in range(rounds):
        for event in list(app.events.values()):
            for handler in list(event.handlers):
                handler.notify(None)


def _command(module, operation: str, **inputs: object) -> None:
    module.CommandExecuteHandler(operation).notify(
        types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=_dialog_inputs(**inputs)))
    )


def _declare_command(module) -> None:
    """A real command that changes the document: Declare Body on one body."""

    labels = module.wglink_author.declaration_choices()
    _command(
        module,
        "declare",
        declare_bodies=_SelectionInput([_fake_body("Shell", solid=False)]),
        declaration=_chosen(labels[0]),
    )


def _counts(module) -> dict[str, dict[str, int]]:
    return module.wglink_activity.LOG.counts()


def _count(module, entry: str, cause: str) -> int:
    return _counts(module).get(entry, {}).get(cause, 0)


def _started_threads(before: set[threading.Thread]) -> set[str]:
    return {
        thread.name
        for thread in threading.enumerate()
        if thread not in before and thread.is_alive() and thread.name in ENGINE_THREADS
    }


# -- the gate itself -----------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "coordinating", "setting"),
    [
        (None, True, "default"),
        ({}, True, "default"),
        ({ACTIVATION: True}, True, "settings"),
        ({ACTIVATION: False}, False, "settings"),
        ({ACTIVATION: "false"}, True, "invalid"),
        ({ACTIVATION: 0}, True, "invalid"),
    ],
    ids=["no-file", "no-key", "true", "false", "string", "zero"],
)
def test_the_gate_is_on_unless_the_owner_writes_false(
    monkeypatch, settings, coordinating: bool, setting: str
) -> None:
    """Default is today's behaviour; only a JSON ``false`` switches coordination off.

    A value that is not a boolean is reported as invalid and changes nothing: a
    typo must not turn a subsystem off without anyone noticing.
    """

    module = _load_instance(
        monkeypatch, f"WGLink_gate_{setting}_{coordinating}", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    if settings is not None:
        module.SETTINGS_PATH.write_text(json.dumps(settings), encoding="utf-8")

    state = module._read_activation()

    assert state["automaticCoordination"] is coordinating
    assert state["setting"] == setting
    assert state["settingsKey"] == ACTIVATION


def test_the_gate_is_read_once_at_start(monkeypatch, tmp_path: Path) -> None:
    fixture = _boundary(monkeypatch, tmp_path, "WGLink_gate_once", coordination=False)
    module = fixture.module
    module.run(None)
    try:
        _write_gate(module, True)
        _declare_command(module)
        _deliver_every_event(fixture.app)

        assert module._coordinating() is False
        assert module.WATCH_EVENT_ID not in fixture.app.events
    finally:
        module.stop(None)


# -- which engines start -------------------------------------------------------


def _engines(module, app) -> dict[str, object]:
    return {
        "watch_thread": module._watch_thread is not None,
        "candidate_thread": module._candidate_thread is not None,
        "live_client": module._live_client is not None,
        "watch_event": module.WATCH_EVENT_ID in app.events,
        "live_event": module.LIVE_EVENT_ID in app.events,
        "followup_event": module.FOLLOWUP_EVENT_ID in app.events,
    }


def test_with_coordination_off_no_engine_starts_or_registers(
    monkeypatch, tmp_path: Path
) -> None:
    """A, C, D not started; E not registered. The live layer is dormant, not ignored."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_engines_off", coordination=False)
    before = set(threading.enumerate())
    fixture.module.run(None)
    try:
        assert _engines(fixture.module, fixture.app) == {
            "watch_thread": False,
            "candidate_thread": False,
            "live_client": False,
            "watch_event": False,
            "live_event": False,
            "followup_event": True,
        }
        assert _started_threads(before) == set()
        assert set(fixture.app.events) == {fixture.module.FOLLOWUP_EVENT_ID}
        # Start-up raised nothing for later either.
        assert fixture.app.fired == []
        assert fixture.module._owned is True
    finally:
        fixture.module.stop(None)
    assert fixture.app.events == {}


def test_with_coordination_on_every_engine_starts_as_before(
    monkeypatch, tmp_path: Path
) -> None:
    """The positive control for the test above: the same measurement, rising."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_engines_on", coordination=None)
    before = set(threading.enumerate())
    fixture.module.run(None)
    try:
        assert _engines(fixture.module, fixture.app) == {
            "watch_thread": True,
            "candidate_thread": False,
            "live_client": True,
            "watch_event": True,
            "live_event": True,
            "followup_event": False,
        }
        assert {"WGLinkExportWatch", "WGLinkLiveSend", "WGLinkLivePoll"} <= _started_threads(before)
    finally:
        fixture.module.stop(None)


@pytest.mark.parametrize("coordination", [False, None], ids=["off", "on"])
def test_a_standby_registration_promotes_itself_only_with_coordination_on(
    monkeypatch, tmp_path: Path, coordination: bool | None
) -> None:
    """Engine B: the candidate thread and its event exist only while coordinating."""

    owner = _boundary(monkeypatch, tmp_path, f"WGLink_owner_{coordination}", coordination=coordination)
    standby = _load_instance(monkeypatch, f"WGLink_standby_{coordination}", owner.ui, owner.app)
    if coordination is not None:
        _write_gate(standby, coordination)
    owner.module.run(None)
    before = set(threading.enumerate())
    standby.run(None)
    try:
        assert standby._owned is False
        started = standby._candidate_thread is not None
        assert started is (coordination is None)
        assert (standby._candidate_event_id in owner.app.events) is (coordination is None)
        assert ("WGLinkOwnerCandidate" in _started_threads(before)) is (coordination is None)
    finally:
        standby.stop(None)
        owner.module.stop(None)


def test_with_coordination_off_an_idle_owner_keeps_its_lease(monkeypatch, tmp_path: Path) -> None:
    """No watch thread renews it, so it must not age out and be taken over."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_lease_off", coordination=False)
    fixture.module.run(None)
    standby = _load_instance(monkeypatch, "WGLink_lease_off_standby", fixture.ui, fixture.app)
    _write_gate(standby, False)
    try:
        clock = fixture.module._lease_now() + 3600.0
        monkeypatch.setattr(standby, "_lease_now", lambda: clock)
        assert standby._claim_ipc_lease() is False
        assert fixture.module._owns_active_ipc_lease() is True
    finally:
        fixture.module.stop(None)
    assert standby._claim_ipc_lease() is True
    standby._release_ipc_lease()


# -- what engines still on can reach -------------------------------------------


_TIMER_ONLY = (
    "_on_watch_tick",
    "_on_live_dispatch",
    "_apply_pending_handoff",
    "_apply_pending_return_request",
    "_apply_announced_updates",
    "_notice_untaken_solves",
    "_notice_deliveries",
    "_notice_outdated_wg",
)


def _spy_timer_only(monkeypatch, module) -> list[str]:
    reached: list[str] = []
    for name in _TIMER_ONLY:
        real = getattr(module, name)

        def spy(*args, _name=name, _real=real, **kwargs):
            reached.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, name, spy)
    real_survey = module._watcher.survey
    monkeypatch.setattr(
        module._watcher,
        "survey",
        lambda links: (reached.append("survey"), real_survey(links))[1],
    )
    return reached


def _live_scenario(fixture) -> None:
    """Commands, then everything a user does between them, then every callback."""

    module, app = fixture.module, fixture.app
    _declare_command(module)
    _deliver_every_event(app)
    fixture.body.revisionId = "revision-2"
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    _deliver_every_event(app)
    app.activeProduct = fixture.design
    _deliver_every_event(app)


def test_with_coordination_off_the_consumers_and_prompts_are_unreachable(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _boundary(monkeypatch, tmp_path, "WGLink_reach_off", coordination=False)
    reached = _spy_timer_only(monkeypatch, fixture.module)
    fixture.module.run(None)
    try:
        _live_scenario(fixture)
        assert reached == []
        # The command itself did run, and its follow-up did publish.
        assert _count(fixture.module, "mutation_body_declaration", "command:declare") == 1
        assert _count(fixture.module, "status_publication", "command:declare") == 1
    finally:
        fixture.module.stop(None)


def test_with_coordination_on_the_same_scenario_reaches_them(
    monkeypatch, tmp_path: Path
) -> None:
    """Positive control: the spies are wired, and the tick reaches every consumer."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_reach_on", coordination=None)
    reached = _spy_timer_only(monkeypatch, fixture.module)
    fixture.module.run(None)
    try:
        _live_scenario(fixture)
        assert {
            "_on_watch_tick",
            "_on_live_dispatch",
            "_apply_pending_handoff",
            "_apply_pending_return_request",
            "_notice_untaken_solves",
            "_notice_deliveries",
            "_notice_outdated_wg",
            "survey",
        } <= set(reached)
    finally:
        fixture.module.stop(None)


# -- counters: a positive control at every entry point -------------------------


def _source_command(fixture, monkeypatch) -> None:
    module = fixture.module
    monkeypatch.setattr(
        module.wglink_core,
        "_named_appearance",
        lambda _app, _design, name: types.SimpleNamespace(name=name),
    )
    _command(
        module,
        "source",
        source_faces=_SelectionInput([_fake_face(None)]),
        source_role=_chosen("HF"),
    )


def _insert_command(fixture, monkeypatch) -> None:
    missing = fixture.ipc.parent / "missing.wglink"
    monkeypatch.setattr(fixture.module, "_choose_bundle", lambda *_a: str(missing))
    _command(fixture.module, "insert")


def _return_folder(fixture, monkeypatch) -> None:
    monkeypatch.setattr(
        fixture.module.wglink_workspace, "return_folder", lambda: fixture.ipc.parent / "wgreturn"
    )


def _send_command(fixture, monkeypatch) -> None:
    _return_folder(fixture, monkeypatch)
    _command(fixture.module, "send")


def _solve_command(fixture, monkeypatch) -> None:
    _return_folder(fixture, monkeypatch)
    _command(fixture.module, "solve")


_REACHES = [
    # Inspection entry points (Gate AR): reached by a command's follow-up,
    # which pays for the refresh the command requested and publishes status.
    ("link_resolution", "declare", lambda f, m: _declare_command(f.module)),
    ("geometry_measurement", "declare", lambda f, m: _declare_command(f.module)),
    ("source_inventory", "declare", lambda f, m: _declare_command(f.module)),
    ("document_signature", "declare", lambda f, m: _declare_command(f.module)),
    ("status_publication", "declare", lambda f, m: _declare_command(f.module)),
    # Mutation and export paths, each reached by the command that owns it.
    ("mutation_body_declaration", "declare", lambda f, m: _declare_command(f.module)),
    ("mutation_source_identity", "source", _source_command),
    ("mutation_update", "update", lambda f, m: _command(f.module, "update")),
    ("mutation_detach", "detach", lambda f, m: _command(f.module, "detach")),
    ("mutation_insert", "insert", _insert_command),
    ("export_return", "send", _send_command),
    ("export_return", "solve", _solve_command),
    # The dialog a command opens inspects too, under that command.
    ("link_resolution", "update", lambda f, m: _command(f.module, "update")),
]


@pytest.mark.parametrize(
    ("entry", "operation", "act"),
    _REACHES,
    ids=[f"{entry}-{operation}" for entry, operation, _act in _REACHES],
)
def test_a_command_reaches_each_entry_point_and_its_count_rises(
    monkeypatch, tmp_path: Path, entry: str, operation: str, act
) -> None:
    """Every zero elsewhere in this file is only worth something because of this."""

    fixture = _boundary(monkeypatch, tmp_path, f"WGLink_reach_{entry}_{operation}", coordination=False)
    fixture.module.run(None)
    try:
        before = _count(fixture.module, entry, f"command:{operation}")
        act(fixture, monkeypatch)
        _deliver_every_event(fixture.app)

        assert _count(fixture.module, entry, f"command:{operation}") > before
        assert fixture.module.wglink_activity.LOG.between_commands() == {}
    finally:
        fixture.module.stop(None)


def test_an_entry_point_that_raises_is_still_counted(monkeypatch, tmp_path: Path) -> None:
    """Counted before the body: a refusal inside an advisory ``except`` still ran."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_counted_raise", coordination=False)
    module = fixture.module
    monkeypatch.setattr(
        module.wglink_core,
        "_all_link_attributes",
        lambda _design: (_ for _ in ()).throw(RuntimeError("unreadable")),
    )
    with module.wglink_activity.because("command:probe"):
        with pytest.raises(RuntimeError):
            module.wglink_core._link_records(fixture.design)
        assert _count(module, "link_resolution", "command:probe") == 1
        # The heartbeat's measurement swallows every failure; it still ran.
        module._measure_geometry_state(fixture.app, {})

    assert _count(module, "geometry_measurement", "command:probe") == 1


# -- the cause survives the hop ------------------------------------------------


def test_a_commands_follow_up_is_counted_under_that_command_not_its_carrier(
    monkeypatch, tmp_path: Path
) -> None:
    """The follow-up runs later, from a custom event with no cause of its own."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_hop", coordination=False)
    module = fixture.module
    module.run(None)
    try:
        published = _count(module, "status_publication", "command:declare")
        _declare_command(module)
        # The command has returned; its follow-up has not run yet.
        assert _count(module, "status_publication", "command:declare") == published
        assert module.FOLLOWUP_EVENT_ID in fixture.app.fired
        assert module.wglink_activity.current_cause() == module.wglink_activity.CAUSE_UNATTRIBUTED

        _deliver_every_event(fixture.app)

        assert _count(module, "status_publication", "command:declare") == published + 1
        assert _count(module, "link_resolution", "command:declare") >= 1
        counts = _counts(module)
        assert all(
            "unattributed" not in causes for causes in counts.values()
        ), counts
    finally:
        module.stop(None)


# -- the busy retry --------------------------------------------------------------


def test_a_follow_up_held_behind_a_running_command_runs_when_that_command_ends(
    monkeypatch, tmp_path: Path
) -> None:
    """No tick retries it any more: the command's own completion must."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_busy_retry", coordination=False)
    module, app = fixture.module, fixture.app
    module.run(None)
    try:
        _declare_command(module)
        published = _count(module, "status_publication", "command:declare")
        # Fusion delivers the follow-up while another WGLink command holds the
        # main thread (a doEvents inside its progress update).
        module._command_busy = True
        _deliver_every_event(app)
        assert _count(module, "status_publication", "command:declare") == published
        assert len(module._followups) == 1

        # That other command ends without scheduling anything of its own: the
        # user cancelled Detach at its confirmation.
        fixture.ui.dialog_result = "no"
        fired = app.fired.count(module.FOLLOWUP_EVENT_ID)
        module._command_busy = False
        _command(module, "detach")
        assert app.fired.count(module.FOLLOWUP_EVENT_ID) == fired + 1

        _deliver_every_event(app)
        assert _count(module, "status_publication", "command:declare") == published + 1
        assert module._followups == []
    finally:
        module.stop(None)


# -- the trap: not inspected is not "no links" ---------------------------------


def _leftover_applied_claim(monkeypatch, module, tmp_path: Path):
    """A handoff an interrupted session claimed, whose evidence is on the link."""

    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(ipc, bundles, export_id="wge_5")
    left = module.wglink_watch.claim_request(
        module.wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)
    )
    assert left is not None and left.marker_path.exists()
    applied = [{
        "instance_id": "instance-b", "design_id": "wgd-shared",
        "export_id": "wge_5", "operation_id": "req-1",
    }]
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    return ipc, left, applied


@pytest.mark.parametrize("readable", [False, True], ids=["unreadable", "control"])
def test_start_up_never_settles_a_claim_against_links_it_could_not_read(
    monkeypatch, tmp_path: Path, readable: bool
) -> None:
    """With the tick gone, start-up is where claims are settled -- and a document
    whose links cannot be read gives no evidence either way. Its claim is kept,
    unsettled, never reported as never-started."""

    fixture = _boundary(monkeypatch, tmp_path, f"WGLink_trap_{readable}", coordination=False)
    module = fixture.module
    ipc, left, applied = _leftover_applied_claim(monkeypatch, module, tmp_path / "x")

    def links(_timings=None):
        if not readable:
            raise module.LinksNotInspected(module._NOT_INSPECTED_TEXT)
        return applied

    monkeypatch.setattr(module, "_document_links", links)
    mutations: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(mutations))
    monkeypatch.setattr(module.wglink_core, "insert", _recording(mutations))
    module.run(None)
    try:
        assert mutations == []
        if readable:
            assert not left.marker_path.exists()
            assert {"channel": "handoff", "requestId": "req-1", "outcome": "reconciled"} in (
                module._recent_outcomes
            )
        else:
            assert left.marker_path.exists()
            assert module._claims_swept is False
            assert module._recent_outcomes == []
            # Nor was the empty list published as "this document has no links".
            assert not (ipc / module.wglink_watch.FUSION_STATUS_FILENAME).exists()
    finally:
        module.stop(None)


@pytest.mark.parametrize(
    ("inspected", "outcome"),
    [(False, None), (True, "discarded")],
    ids=["not-inspected", "inspected-empty"],
)
def test_not_inspected_and_no_links_are_different_answers(
    monkeypatch, tmp_path: Path, inspected: bool, outcome: str | None
) -> None:
    fixture = _boundary(monkeypatch, tmp_path, f"WGLink_distinct_{inspected}", coordination=False)
    module = fixture.module
    _ipc, left, _applied = _leftover_applied_claim(monkeypatch, module, tmp_path / "x")
    snapshot = {
        "document_name": "Horn",
        "document_id": "fusion:doc-a",
        "links": [],
        "links_inspected": inspected,
        "applying_operation": None,
    }

    module._sweep_leftover_claims(snapshot)

    if outcome is None:
        assert left.marker_path.exists()
        assert module._link_evidence(snapshot) is None
    else:
        assert not left.marker_path.exists()
        assert module._link_evidence(snapshot) == []
        assert module._recent_outcomes[-1]["outcome"] == outcome


@pytest.mark.parametrize(
    ("inspected", "kept"), [(False, True), (True, False)], ids=["not-inspected", "inspected-empty"]
)
def test_a_held_claim_is_kept_when_the_later_reading_is_not_one(
    monkeypatch, tmp_path: Path, inspected: bool, kept: bool
) -> None:
    """The deferred-claim path (a later command's follow-up, or the tick) too."""

    fixture = _boundary(monkeypatch, tmp_path, f"WGLink_held_{inspected}", coordination=False)
    module = fixture.module
    ipc, left, _applied = _leftover_applied_claim(monkeypatch, module, tmp_path / "x")

    module._settle_file_claims(module.wglink_watch.leftover_claims(ipc), {
        "document_name": "Horn", "document_id": "fusion:doc-a", "links": [],
        "links_inspected": inspected, "applying_operation": None,
    })

    assert left.marker_path.exists() is kept
    assert bool(module._deferred_file_claims) is kept


def test_an_unread_document_refuses_a_handoff_rather_than_inserting(
    monkeypatch, tmp_path: Path
) -> None:
    """Without evidence the already-linked and already-applied checks cannot run."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_trap_handoff", coordination=None)
    module = fixture.module
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path / "x")
    _per_request_handoff(ipc, bundles, instance_id=None)
    inserted: list[object] = []
    monkeypatch.setattr(module.wglink_core, "insert", _recording(inserted))
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")

    module._apply_pending_handoff({
        "document_name": "Horn", "document_id": "fusion:doc-a", "links": [],
        "links_inspected": False, "applying_operation": None,
    })

    assert inserted == []
    assert "could not read this Fusion document's WG links" in fixture.ui.messages[-1][1]


# -- scenarios: nothing between commands with the gate off; the tick with it on -


def _between_commands_scenarios(fixture) -> None:
    module, app = fixture.module, fixture.app
    unlinked = types.SimpleNamespace(
        objectType="adsk::fusion::Design",
        findAttributes=lambda _group, _name: [],
    )
    # Idle.
    _deliver_every_event(app)
    # Document switching.
    app.activeProduct = unlinked
    _deliver_every_event(app)
    app.activeProduct = fixture.design
    _deliver_every_event(app)
    # Ordinary modelling.
    fixture.body.revisionId = "revision-3"
    fixture.body.isVisible = False
    _deliver_every_event(app)
    fixture.body.isVisible = True
    # Reopening a linked document.
    app.activeProduct, app.activeDocument = None, None
    _deliver_every_event(app)
    app.activeProduct, app.activeDocument = fixture.design, types.SimpleNamespace(name="Horn")
    _deliver_every_event(app)
    # WG restarts: it rewrites its capabilities and its view of Fusion is gone.
    (fixture.ipc / module.wglink_watch.CAPABILITIES_FILENAME).write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 3, "fusionRequestDelivery": 3,
    }))
    (fixture.ipc / module.wglink_watch.FUSION_STATUS_FILENAME).unlink(missing_ok=True)
    _deliver_every_event(app)


def test_with_coordination_off_nothing_executes_between_commands(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _boundary(monkeypatch, tmp_path, "WGLink_scenarios_off", coordination=False)
    module = fixture.module
    module.run(None)
    try:
        # Positive control first: a command runs, and the counters see it.
        _declare_command(module)
        _deliver_every_event(fixture.app)
        assert _count(module, "link_resolution", "command:declare") >= 1
        assert _count(module, "status_publication", "command:declare") == 1
        settled = _counts(module)
        fired = list(fixture.app.fired)

        _between_commands_scenarios(fixture)

        assert _counts(module) == settled
        assert module.wglink_activity.LOG.between_commands() == {}
        assert fixture.app.fired == fired
        # Nothing re-published after WG restarted; the next command will.
        assert not (fixture.ipc / module.wglink_watch.FUSION_STATUS_FILENAME).exists()
        _declare_command(module)
        _deliver_every_event(fixture.app)
        status = json.loads(
            (fixture.ipc / module.wglink_watch.FUSION_STATUS_FILENAME).read_text()
        )
        assert status["diagnostics"]["activation"]["automaticCoordination"] is False
        assert status["diagnostics"]["activity"]["betweenCommands"] == {}
    finally:
        module.stop(None)


def test_with_coordination_on_the_tick_is_attributed_to_the_tick(
    monkeypatch, tmp_path: Path
) -> None:
    """Today's baseline, and the evidence the scenario above could have failed."""

    fixture = _boundary(monkeypatch, tmp_path, "WGLink_scenarios_on", coordination=None)
    module = fixture.module
    module.run(None)
    try:
        _declare_command(module)
        _between_commands_scenarios(fixture)

        between = module.wglink_activity.LOG.between_commands()
        assert between.get("link_resolution", 0) > 0
        assert between.get("status_publication", 0) > 0
        assert _count(module, "link_resolution", "tick") > 0
        assert _count(module, "status_publication", "tick") > 0
        assert "unattributed" not in {
            cause for causes in _counts(module).values() for cause in causes
        }
    finally:
        module.stop(None)


@pytest.mark.parametrize(
    ("coordination", "reported"), [(False, False), (None, True)], ids=["off", "on"]
)
def test_the_heartbeat_says_how_the_add_in_is_configured(
    monkeypatch, tmp_path: Path, coordination: bool | None, reported: bool
) -> None:
    fixture = _boundary(monkeypatch, tmp_path, f"WGLink_selfreport_{reported}", coordination=coordination)
    fixture.module.run(None)
    try:
        status = json.loads(
            (fixture.ipc / fixture.module.wglink_watch.FUSION_STATUS_FILENAME).read_text()
        )
        activation = status["diagnostics"]["activation"]
        assert activation["automaticCoordination"] is reported
        assert activation["setting"] == ("settings" if coordination is False else "default")
        assert activation["settingsKey"] == ACTIVATION
        assert ("watchIntervalSeconds" in status["diagnostics"]) is reported
        # Start-up is bounded and counted as such, never as between-commands work.
        assert status["diagnostics"]["activity"]["counts"]["status_publication"] == {"startup": 1}
    finally:
        fixture.module.stop(None)


def test_start_up_and_shutdown_are_counted_separately(monkeypatch, tmp_path: Path) -> None:
    fixture = _boundary(monkeypatch, tmp_path, "WGLink_bounded", coordination=False)
    module = fixture.module
    module.run(None)
    module.stop(None)
    causes = {cause for per in _counts(module).values() for cause in per}
    assert causes <= {"startup", "claim-settlement", "shutdown"}
    assert _count(module, "status_publication", "startup") == 1
    assert module.wglink_activity.LOG.between_commands() == {}


# -- F11: a persisted folder that is gone ---------------------------------------


def test_a_cad_link_folder_that_no_longer_exists_is_named_at_command_time(
    monkeypatch, tmp_path: Path
) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    module = _load_instance(monkeypatch, "WGLink_f11", ui)
    data = tmp_path / "data"
    data.mkdir()
    gone = tmp_path / "Dropbox" / "fusion2 project"
    (data / "cadlink_settings.json").write_text(json.dumps({"schemaVersion": 1, "cadLinkPath": str(gone)}))
    monkeypatch.setenv("WG2_DATA_DIR", str(data))

    _command(module, "send")

    title, text = ui.messages[-1]
    assert title == "WGLink refused"
    assert "no longer exists" in text
    assert str(gone) in text
    assert "Settings → CAD Link" in text


@pytest.mark.parametrize("state", ["unset", "present"])
def test_a_folder_never_chosen_or_still_there_is_not_called_missing(
    monkeypatch, tmp_path: Path, state: str
) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    module = _load_instance(monkeypatch, f"WGLink_f11_{state}", ui)
    data = tmp_path / "data"
    data.mkdir()
    settings = {"schemaVersion": 1}
    if state == "present":
        settings["cadLinkPath"] = str(tmp_path)
    (data / "cadlink_settings.json").write_text(json.dumps(settings))
    monkeypatch.setenv("WG2_DATA_DIR", str(data))

    assert module.wglink_workspace.missing_workspace() is None
    assert "no longer exists" not in module._no_workspace_text()
