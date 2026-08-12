"""Panel ownership when one add-in is registered twice.

Fusion loads every registered path as its own module with its own globals, so
two registrations of WGLink are two instances that share only the toolbar panel
they build. The second instance used to tear the first one's panel and command
definitions down and rebuild them, which left the toolbar holding buttons whose
definitions were dead.
"""

from __future__ import annotations

import importlib.util
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
    return module


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


def test_only_the_owning_instance_runs_an_export_watcher(monkeypatch) -> None:
    """Two watchers would prompt twice for one export."""

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)

    first = _load_instance(monkeypatch, "WGLink_watch_first", ui, app)
    first.run(None)
    assert first.WATCH_EVENT_ID in app.events
    assert first._watch_thread is not None and first._watch_thread.is_alive()

    second = _load_instance(monkeypatch, "WGLink_watch_second", ui, app)
    second.run(None)
    assert second._watch_thread is None
    assert list(app.events) == [first.WATCH_EVENT_ID]

    # The adopted instance stopping must leave the owner's watcher registered.
    second.stop(None)
    assert first.WATCH_EVENT_ID in app.events
    assert first._watch_thread.is_alive()

    first.stop(None)
    assert first.WATCH_EVENT_ID not in app.events
    assert first._watch_thread is None


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
