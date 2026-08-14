"""Panel ownership when one add-in is registered twice.

Fusion loads every registered path as its own module with its own globals, so
two registrations of WGLink are two instances that share only the toolbar panel
they build. The second instance used to tear the first one's panel and command
definitions down and rebuild them, which left the toolbar holding buttons whose
definitions were dead.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest


ADDIN = Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink" / "WGLink.py"

_HANDLER_TYPES = (
    "CommandCreatedEventHandler",
    "CommandEventHandler",
    "CustomEventHandler",
    "InputChangedEventHandler",
    "ValidateInputsEventHandler",
    "SelectionEventHandler",
    "CommandEventArgs",
    "InputChangedEventArgs",
)


class _CustomEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def add(self, handler: object) -> None:
        self.handlers.append(handler)

    def remove(self, handler: object) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)


class _Application:
    """Enough Application surface for the watcher's registration dance."""

    def __init__(self, ui: "_UI") -> None:
        self.userInterface = ui
        self.events: dict[str, _CustomEvent] = {}
        self.fired: list[str] = []
        self.activeProduct = None
        self.activeDocument = None
        self.documents = types.SimpleNamespace(add=self._add_document)

    def _add_document(self, _document_type: object) -> object:
        self.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
        self.activeDocument = types.SimpleNamespace(name="Untitled")
        return self.activeDocument

    def registerCustomEvent(self, event_id: str) -> _CustomEvent:
        event = _CustomEvent()
        self.events[event_id] = event
        return event

    def unregisterCustomEvent(self, event_id: str) -> bool:
        return self.events.pop(event_id, None) is not None

    def fireCustomEvent(self, event_id: str, _payload: str = "") -> bool:
        self.fired.append(event_id)
        return True


class _Control:
    def __init__(self, definition: object) -> None:
        self.definition, self.isValid = definition, True

    def deleteMe(self) -> None:
        self.isValid = False


class _Controls:
    """Stands in for Fusion's control collection, whose count is a property."""

    def __init__(self) -> None:
        self._items: list[_Control] = []

    @property
    def count(self) -> int:
        return len(self._items)

    def addCommand(self, definition: object) -> _Control:
        if definition is None:
            raise RuntimeError("addCommand received None")
        control = _Control(definition)
        self._items.append(control)
        return control

    def clear(self) -> None:
        self._items.clear()


class _Panel:
    def __init__(self, panel_id: str) -> None:
        self.id, self.isValid, self.controls = panel_id, True, _Controls()

    def deleteMe(self) -> None:
        self.isValid = False
        self.controls.clear()


class _Panels:
    def __init__(self) -> None:
        self.items: dict[str, _Panel] = {}

    def itemById(self, panel_id: str) -> _Panel | None:
        panel = self.items.get(panel_id)
        return panel if panel is not None and panel.isValid else None

    def add(self, panel_id: str, _name: str, _after: str, _before: bool) -> _Panel:
        panel = _Panel(panel_id)
        self.items[panel_id] = panel
        return panel


class _Definition:
    def __init__(self, definition_id: str) -> None:
        self.id, self.isValid = definition_id, True
        self.commandCreated = types.SimpleNamespace(add=lambda handler: None)

    def deleteMe(self) -> None:
        self.isValid = False


class _Definitions:
    def __init__(self, *, reserve_ids: bool) -> None:
        self.items: dict[str, _Definition] = {}
        self.resource_folders: list[str] = []
        self._reserve_ids = reserve_ids

    def itemById(self, definition_id: str) -> _Definition | None:
        definition = self.items.get(definition_id)
        return definition if definition is not None and definition.isValid else None

    def addButtonDefinition(
        self, definition_id: str, _name: str, _description: str, resource_folder: str = ""
    ) -> _Definition | None:
        self.resource_folders.append(resource_folder)
        stale = self.items.get(definition_id)
        if self._reserve_ids and stale is not None and not stale.isValid:
            return None
        definition = _Definition(definition_id)
        self.items[definition_id] = definition
        return definition


class _UI:
    def __init__(self, panels: _Panels, definitions: _Definitions) -> None:
        self.commandDefinitions = definitions
        self.messages: list[tuple[str, str]] = []
        self.workspaces = types.SimpleNamespace(
            itemById=lambda _id: types.SimpleNamespace(toolbarPanels=panels)
        )

    def messageBox(self, text: str, title: str = "") -> None:
        self.messages.append((title, text))


def _load_instance(monkeypatch, name: str, ui: _UI, app: _Application | None = None):
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    for handler in _HANDLER_TYPES:
        setattr(core, handler, type(handler, (object,), {}))
    resolved = app if app is not None else _Application(ui)
    core.Application = types.SimpleNamespace(get=lambda: resolved)
    core.DocumentTypes = types.SimpleNamespace(FusionDesignDocumentType="fusion-design")
    adsk.core, adsk.fusion = core, fusion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)
    monkeypatch.syspath_prepend(str(ADDIN.parent))

    spec = importlib.util.spec_from_file_location(name, ADDIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    # Lifecycle tests should never publish presence into the user's real WG
    # workspace. Individual handoff tests replace this with their temp folder.
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: None)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: None)
    return module


def test_send_writes_to_wgs_workspace_and_never_overwrites(monkeypatch, tmp_path: Path) -> None:
    """WG only ingests from its own workspace, so the destination is not a choice.

    Collision-safe naming is not optional either: a return WG has not ingested
    yet is not in content-addressed storage, so replacing one loses evidence.
    """

    module = _load_instance(
        monkeypatch,
        "WGLink_send_destination",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    expected = tmp_path / "selected-workspace" / "wgreturn"
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: expected)
    monkeypatch.setattr(module, "_send_selection", lambda _inputs: "root")

    options = module._send_options(types.SimpleNamespace())

    assert options["output_folder"] == str(expected)
    assert options["overwrite"] is False


def test_send_refuses_when_wg_has_no_selected_workspace(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_send_no_workspace",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: None)
    monkeypatch.setattr(module, "_send_selection", lambda _inputs: "root")

    with pytest.raises(module.wglink_core.WgLinkError) as refusal:
        module._send_options(types.SimpleNamespace())
    assert "Settings" in str(refusal.value)


def test_the_send_dialog_asks_only_for_scope(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_send_inputs",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    string_inputs: list[tuple[object, ...]] = []
    bool_inputs: list[tuple[object, ...]] = []
    selection = types.SimpleNamespace(
        addSelectionFilter=lambda _value: None,
        setSelectionLimits=lambda _minimum, _maximum: None,
    )
    anchor = types.SimpleNamespace(isVisible=True)
    inputs = types.SimpleNamespace(
        addSelectionInput=lambda *_args: selection,
        addStringValueInput=lambda *args: string_inputs.append(args),
        addBoolValueInput=lambda *args: bool_inputs.append(args),
        addDropDownCommandInput=lambda *_args: anchor,
    )
    command = types.SimpleNamespace(
        commandInputs=inputs,
        inputChanged=types.SimpleNamespace(add=lambda _handler: None),
        execute=types.SimpleNamespace(add=lambda _handler: None),
    )
    module.adsk.core.DropDownStyles = types.SimpleNamespace(
        TextListDropDownStyle="text-list"
    )
    monkeypatch.setattr(module, "_sync_anchor_choices", lambda _inputs: None)

    module.CommandCreatedHandler("send").notify(types.SimpleNamespace(command=command))

    # No output folder, no browse button, no overwrite checkbox.
    assert string_inputs == []
    assert bool_inputs == []


def test_solve_in_wg_shares_the_send_dialog(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_solve_inputs",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    added: list[str] = []
    selection = types.SimpleNamespace(
        addSelectionFilter=lambda _value: None,
        setSelectionLimits=lambda _minimum, _maximum: None,
    )
    anchor = types.SimpleNamespace(isVisible=True)
    inputs = types.SimpleNamespace(
        addSelectionInput=lambda *args: (added.append(args[0]), selection)[1],
        addStringValueInput=lambda *args: added.append(args[0]),
        addBoolValueInput=lambda *args: added.append(args[0]),
        addDropDownCommandInput=lambda *args: (added.append(args[0]), anchor)[1],
    )
    command = types.SimpleNamespace(
        commandInputs=inputs,
        inputChanged=types.SimpleNamespace(add=lambda _handler: None),
        execute=types.SimpleNamespace(add=lambda _handler: None),
    )
    module.adsk.core.DropDownStyles = types.SimpleNamespace(
        TextListDropDownStyle="text-list"
    )
    monkeypatch.setattr(module, "_sync_anchor_choices", lambda _inputs: None)

    module.CommandCreatedHandler("solve").notify(types.SimpleNamespace(command=command))

    assert added == ["send_selection", "anchor_instance_id"]


def test_the_link_chooser_appears_only_with_several_links(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_link_chooser",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    module.adsk.core.DropDownStyles = types.SimpleNamespace(
        TextListDropDownStyle="text-list"
    )

    def build() -> list[str]:
        added: list[str] = []
        chooser = types.SimpleNamespace(
            listItems=types.SimpleNamespace(add=lambda *args: added.append(args[0])),
        )
        inputs = types.SimpleNamespace(
            addStringValueInput=lambda *args: added.append(args[0]),
            addDropDownCommandInput=lambda *_args: chooser,
        )
        module.CommandCreatedHandler("update").notify(types.SimpleNamespace(
            command=types.SimpleNamespace(
                commandInputs=inputs,
                execute=types.SimpleNamespace(add=lambda _handler: None),
            ),
        ))
        return added

    monkeypatch.setattr(module, "_document_links", lambda: [
        {"instance_id": "wgi_one", "design_name": "Tritonia"},
    ])
    assert build() == []

    monkeypatch.setattr(module, "_document_links", lambda: [
        {"instance_id": "wgi_one", "design_name": "Tritonia"},
        {"instance_id": "wgi_two", "design_name": "asro68"},
    ])
    # The label carries the design name; _command_options recovers the id.
    assert build() == ["Tritonia · wgi_one", "asro68 · wgi_two"]


def test_the_chosen_link_label_resolves_back_to_its_instance_id(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_link_choice_options",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    inputs = types.SimpleNamespace(
        itemById=lambda name: types.SimpleNamespace(
            selectedItem=types.SimpleNamespace(name="asro68 · wgi_two"),
        ) if name == "instance_choice" else None,
    )

    assert module._command_options(inputs) == {"instance_id": "wgi_two"}


def test_load_ignores_an_incompatible_workspace_module_cached_by_fusion(
    monkeypatch,
) -> None:
    stale = types.ModuleType("wglink_workspace")
    stale.__file__ = "/old/WGLink/wglink_workspace.py"
    monkeypatch.setitem(sys.modules, "wglink_workspace", stale)

    module = _load_instance(
        monkeypatch,
        "WGLink_with_stale_workspace",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )

    assert module.wglink_workspace is not stale
    assert Path(module.wglink_workspace.__file__).resolve() == (
        ADDIN.parent / "wglink_workspace.py"
    ).resolve()


@pytest.mark.parametrize("reserve_ids", [True, False], ids=["id-reserved", "id-freed"])
def test_a_second_registration_adopts_the_panel_instead_of_rebuilding_it(
    monkeypatch, reserve_ids: bool,
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=reserve_ids)
    ui = _UI(panels, definitions)

    first = _load_instance(monkeypatch, "WGLink_first", ui)
    first.run(None)
    panel = panels.itemById(first.PANEL_ID)
    assert panel is not None
    assert panel.controls.count == len(first.COMMANDS)
    assert first._owned is True
    # Every command ships an icon folder holding the 16 px art Fusion draws in
    # the panel dropdown; a missing folder would silently give a blank button.
    assert len(definitions.resource_folders) == len(first.COMMANDS)
    for folder in definitions.resource_folders:
        assert folder and (Path(folder) / "16x16.png").is_file()

    second = _load_instance(monkeypatch, "WGLink_second", ui)
    second.run(None)

    # The buttons the first instance installed are untouched, and starting the
    # second instance reports no error -- whether or not Fusion keeps a deleted
    # command id reserved.
    assert ui.messages == []
    assert panels.itemById(first.PANEL_ID) is panel
    assert panel.controls.count == len(first.COMMANDS)
    assert second._owned is False
    assert second._definitions == []

    # Stopping the adopted instance must not strip the owner's definitions.
    second.stop(None)
    assert ui.messages == []
    assert panel.isValid
    assert panel.controls.count == len(first.COMMANDS)
    assert [d.id for d in definitions.items.values() if d.isValid] == [
        command_id for command_id, _name, _description in first.COMMANDS.values()
    ]

    # The owner still tears its own panel down.
    first.stop(None)
    assert panels.itemById(first.PANEL_ID) is None
    assert [d for d in definitions.items.values() if d.isValid] == []


def test_adopted_instance_runs_presence_without_a_second_export_watcher(
    monkeypatch,
) -> None:
    """A stale owner cannot suppress presence, but prompts remain single-owner."""

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)

    first = _load_instance(monkeypatch, "WGLink_watch_first", ui, app)
    first.run(None)
    assert first.WATCH_EVENT_ID in app.events
    assert first._watch_thread is not None and first._watch_thread.is_alive()

    second = _load_instance(monkeypatch, "WGLink_watch_second", ui, app)
    published: list[bool] = []
    monkeypatch.setattr(second, "_publish_fusion_status", lambda: published.append(True))
    second.run(None)
    assert second._watch_thread is None
    assert second._presence_thread is not None and second._presence_thread.is_alive()
    assert published == [True]
    assert set(app.events) == {first.WATCH_EVENT_ID, second._presence_event_id}

    second._presence_handler.notify(None)
    assert published == [True, True]

    # The adopted instance stops only its private heartbeat and leaves the
    # owner's export watcher registered.
    second.stop(None)
    assert first.WATCH_EVENT_ID in app.events
    assert second._presence_event_id not in app.events
    assert first._watch_thread.is_alive()

    first.stop(None)
    assert first.WATCH_EVENT_ID not in app.events
    assert first._watch_thread is None


def test_document_links_derive_sorted_drifted_parameter_names_from_drift(
    monkeypatch,
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(monkeypatch, "WGLink_parameter_drift", ui, app)
    record = {"payload": {}, "body": None}
    monkeypatch.setattr(
        module.wglink_core,
        "_link_records",
        lambda _design: {"instance-a": record},
    )
    monkeypatch.setattr(
        module.wglink_core,
        "_parameter_drift",
        lambda _design, _record: [
            {"name": "wg_mouth_thickness"},
            {"name": "wg_length"},
        ],
    )
    monkeypatch.setattr(
        module.wglink_core,
        "_local_body_state",
        lambda _record: "unchanged",
    )
    monkeypatch.setattr(module.wglink_send, "return_state", lambda *_args: {})

    links = module._document_links()

    assert links[0]["drifted_parameters"] == ["wg_length", "wg_mouth_thickness"]
    assert int(links[0]["parameter_drift_count"]) == len(
        links[0]["drifted_parameters"]
    )


def test_heartbeat_loops_use_the_main_thread_application_reference(monkeypatch) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_captured_app", ui, app)

    class OneTick:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _interval: float) -> bool:
            self.calls += 1
            return self.calls > 1

    monkeypatch.setattr(
        module,
        "_app",
        lambda: (_ for _ in ()).throw(AssertionError("worker called Application.get()")),
    )

    module._watch_loop(app, OneTick())
    module._presence_loop(app, OneTick())

    assert app.fired == [module.WATCH_EVENT_ID, module._presence_event_id]


def test_the_watcher_prompt_is_held_off_while_a_command_runs(monkeypatch) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_busy", ui, app)

    surveyed: list[object] = []
    monkeypatch.setattr(
        module._watcher, "survey", lambda links: surveyed.append(links) or []
    )
    module._command_busy = True
    module._on_watch_tick()
    assert surveyed == []

    module._command_busy = False
    module._on_watch_tick()
    assert len(surveyed) == 1


def test_a_pending_new_bundle_is_inserted_once_and_acknowledged(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(monkeypatch, "WGLink_pending_insert", ui, app)
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "horn.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text(
        json.dumps({"export": {"id": "wge_2", "sequence": 2}})
    )
    marker = bundle_root / module.wglink_watch.HANDOFF_FILENAME
    marker.write_text(json.dumps({
        "schemaVersion": 1,
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_2",
        "exportId": "wge_2",
        "sequence": 2,
    }))
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root)
    monkeypatch.setattr(module.wglink_core, "_link_records", lambda _design: {})
    inserted: list[tuple[object, str, dict[str, object]]] = []
    monkeypatch.setattr(
        module.wglink_core,
        "insert",
        lambda active_app, path, options: (
            inserted.append((active_app, path, options))
            or {
                "instance_id": "horn-1",
                "wrapper": "WGLink horn",
                "tag": {},
                "deviation": {},
                "warnings": [],
            }
        ),
    )

    module._on_watch_tick()
    module._on_watch_tick()

    assert inserted == [(app, str(bundle), {"allow_root_fallback": True})]
    assert not marker.exists()
    assert ui.messages == []


def test_a_pending_bundle_creates_a_design_document_when_none_is_open(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_pending_new_document", ui, app)
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "horn.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    _marker = bundle_root / module.wglink_watch.HANDOFF_FILENAME
    _marker.write_text(json.dumps({
        "schemaVersion": 1,
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_2",
        "exportId": "wge_2",
        "sequence": 2,
    }))
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root)
    monkeypatch.setattr(module.wglink_core, "_link_records", lambda _design: {})
    inserted: list[str] = []
    monkeypatch.setattr(
        module.wglink_core,
        "insert",
        lambda _app, path, _options: inserted.append(path),
    )

    module._on_watch_tick()

    assert app.activeDocument.name == "Untitled"
    assert inserted == [str(bundle)]
    assert not _marker.exists()


def test_a_pending_new_export_updates_the_existing_link_without_a_prompt(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    app.activeDocument = types.SimpleNamespace(name="Tritonia V")
    module = _load_instance(monkeypatch, "WGLink_pending_update", ui, app)
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "horn.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    marker = bundle_root / module.wglink_watch.HANDOFF_FILENAME
    marker.write_text(json.dumps({
        "schemaVersion": 1,
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_3",
        "exportId": "wge_3",
        "sequence": 3,
        "designId": "wgd-a",
    }))
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root)
    monkeypatch.setattr(module.wglink_core, "_link_records", lambda _design: {
        "instance-a": {"payload": {
            "bundle_path": str(bundle),
            "design_id": "wgd-a",
            "export_id": "wge_2",
        }},
    })
    updated: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        module.wglink_core,
        "update",
        lambda _app, path, options: updated.append((path, options)),
    )

    module._on_watch_tick()

    assert updated == [(str(bundle), {"instance_id": "instance-a"})]
    assert not marker.exists()
    assert ui.messages == []


def test_a_pending_update_targets_design_identity_after_bundle_move(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    app.activeDocument = types.SimpleNamespace(name="Tritonia V")
    module = _load_instance(monkeypatch, "WGLink_pending_moved_bundle", ui, app)
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "renamed.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    marker = bundle_root / module.wglink_watch.HANDOFF_FILENAME
    marker.write_text(json.dumps({
        "schemaVersion": 1,
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_4",
        "exportId": "wge_4",
        "sequence": 4,
        "designId": "wgd-a",
    }))
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root)
    monkeypatch.setattr(module.wglink_core, "_link_records", lambda _design: {
        "instance-a": {"payload": {
            "bundle_path": str(tmp_path / "old" / "horn.wglink"),
            "design_id": "wgd-a",
            "export_id": "wge_3",
        }},
    })
    updated: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        module.wglink_core,
        "update",
        lambda _app, path, options: updated.append((path, options)),
    )

    module._on_watch_tick()

    assert updated == [(str(bundle), {"instance_id": "instance-a"})]
    assert not marker.exists()


def test_a_targeted_return_refuses_if_the_active_document_changed(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    app.activeDocument = types.SimpleNamespace(name="Other document")
    module = _load_instance(monkeypatch, "WGLink_return_wrong_document", ui, app)
    request = types.SimpleNamespace(
        design_id="wgd-a",
        document_id="fusion:expected",
        instance_id="instance-a",
        expected_return_state_hash="sha256:state-a",
        request_id="request-a",
    )
    monkeypatch.setattr(module, "_pending_return_request", lambda: request)
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:other")
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(module.wglink_send, "send", lambda _app, options: sent.append(options))

    assert module._apply_pending_return_request() is True

    assert sent == []
    assert ui.messages == [(
        "WGLink return to WG refused",
        "The active Fusion document changed after WG requested the model. Reopen CAD Link and try again.",
    )]


def test_a_refused_return_request_is_attempted_once_until_its_id_changes(
    monkeypatch,
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(monkeypatch, "WGLink_return_refusal_suppression", ui, app)
    request_a = types.SimpleNamespace(
        design_id="wgd-a",
        document_id="fusion:expected",
        instance_id="instance-a",
        expected_return_state_hash="sha256:state-a",
        request_id="request-a",
    )
    request_b = types.SimpleNamespace(**{
        **vars(request_a),
        "request_id": "request-b",
    })
    pending = {"request": request_a}
    attempts: list[str] = []
    monkeypatch.setattr(module, "_pending_return_request", lambda: pending["request"])

    def wrong_document() -> str:
        attempts.append(pending["request"].request_id)
        return "fusion:other"

    monkeypatch.setattr(module, "_active_document_id", wrong_document)

    assert module._apply_pending_return_request() is True
    assert module._apply_pending_return_request() is True
    pending["request"] = request_b
    assert module._apply_pending_return_request() is True

    assert attempts == ["request-a", "request-b"]
    assert [title for title, _text in ui.messages] == [
        "WGLink return to WG refused",
        "WGLink return to WG refused",
    ]


def test_a_targeted_return_exports_only_the_exact_live_link(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    app.activeDocument = types.SimpleNamespace(name="Tritonia V")
    module = _load_instance(monkeypatch, "WGLink_return_exact_document", ui, app)
    request = types.SimpleNamespace(
        design_id="wgd-a",
        document_id="fusion:doc-a",
        instance_id="instance-a",
        expected_return_state_hash="sha256:state-a",
        request_id="request-a",
    )
    monkeypatch.setattr(module, "_pending_return_request", lambda: request)
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda: [{
        "design_id": "wgd-a", "instance_id": "instance-a",
        "document_signature_hash": "sha256:state-a",
    }])
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: tmp_path)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(module.wglink_send, "send", lambda _app, options: sent.append(options))
    acknowledged: list[object] = []
    monkeypatch.setattr(module.wglink_watch, "acknowledge_return_request", acknowledged.append)

    assert module._apply_pending_return_request() is True

    assert sent == [{
        "selection": "root",
        "output_folder": str(tmp_path),
        "overwrite": True,
        "request_id": "request-a",
        "anchor_instance_id": "instance-a",
    }]
    assert acknowledged == [request]
    assert ui.messages == []


def test_a_refused_automatic_insert_is_not_retried_every_tick(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(monkeypatch, "WGLink_pending_refusal", ui, app)
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "horn.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    marker = bundle_root / module.wglink_watch.HANDOFF_FILENAME
    marker.write_text(json.dumps({
        "schemaVersion": 1,
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_2",
        "exportId": "wge_2",
        "sequence": 2,
    }))
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root)
    monkeypatch.setattr(module.wglink_core, "_link_records", lambda _design: {})
    attempts: list[str] = []

    def refuse(_app: object, path: str, _options: object) -> None:
        attempts.append(path)
        raise module.wglink_core.WgLinkError("bad bundle")

    monkeypatch.setattr(module.wglink_core, "insert", refuse)

    module._on_watch_tick()
    module._on_watch_tick()

    assert attempts == [str(bundle)]
    assert marker.exists()
    assert ui.messages == [("WGLink automatic insert refused", "bad bundle")]


def test_a_start_that_fails_partway_still_owns_its_half_built_panel(
    monkeypatch,
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    module = _load_instance(monkeypatch, "WGLink_partial", ui)

    calls = {"n": 0}
    real_add = definitions.addButtonDefinition

    def failing_add(definition_id: str, name: str, description: str):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Fusion refused the definition")
        return real_add(definition_id, name, description)

    monkeypatch.setattr(definitions, "addButtonDefinition", failing_add)
    module.run(None)

    assert module._owned is True
    assert ui.messages and ui.messages[0][0] == "WGLink start error"
    module.stop(None)
    assert panels.itemById(module.PANEL_ID) is None
