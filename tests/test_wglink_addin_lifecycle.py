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

    def addDropDown(self, text: str, resource_folder: str, control_id: str) -> "_DropDown":
        dropdown = _DropDown(text, resource_folder, control_id)
        self._items.append(dropdown)
        return dropdown

    def itemById(self, control_id: str) -> object | None:
        return next((item for item in self._items if getattr(item, "id", None) == control_id), None)

    def clear(self) -> None:
        self._items.clear()


class _DropDown:
    """A panel dropdown, which owns its own control collection."""

    def __init__(self, text: str, resource_folder: str, control_id: str) -> None:
        self.text, self.resourceFolder, self.id = text, resource_folder, control_id
        self.isValid, self.controls = True, _Controls()

    def deleteMe(self) -> None:
        self.isValid = False


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


class _Palette:
    """Fusion's Text Commands palette, which is where tracebacks belong."""

    def __init__(self) -> None:
        self.written: list[str] = []

    def writeText(self, text: str) -> None:
        self.written.append(text)


class _UI:
    def __init__(
        self,
        panels: _Panels,
        definitions: _Definitions,
        *,
        dialog_result: object | None = None,
    ) -> None:
        self.commandDefinitions = definitions
        self.messages: list[tuple[str, str]] = []
        self.dialog_result = dialog_result
        self.text_palette = _Palette()
        self.palettes = types.SimpleNamespace(
            itemById=lambda palette_id: (
                self.text_palette if palette_id == "TextCommands" else None
            )
        )
        self.workspaces = types.SimpleNamespace(
            itemById=lambda _id: types.SimpleNamespace(toolbarPanels=panels)
        )
        self.progress_dialogs: list[object] = []

    def messageBox(
        self, text: str, title: str = "", *_args: object
    ) -> object | None:
        self.messages.append((title, text))
        return self.dialog_result

    def createProgressDialog(self) -> object:
        events: list[tuple[object, ...]] = []
        dialog = types.SimpleNamespace(
            isCancelButtonShown=True,
            isBackgroundTranslucent=True,
            message="",
            progressValue=0,
            events=events,
        )

        def show(*args: object) -> None:
            events.append(("show", *args))

        def hide() -> None:
            events.append(("hide", dialog.progressValue, dialog.message))

        dialog.show = show
        dialog.hide = hide
        self.progress_dialogs.append(dialog)
        return dialog


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
    core.MessageBoxButtonTypes = types.SimpleNamespace(YesNoButtonType="yes-no")
    core.MessageBoxIconTypes = types.SimpleNamespace(QuestionIconType="question")
    core.DialogResults = types.SimpleNamespace(
        DialogOK="ok", DialogYes="yes", DialogNo="no"
    )
    adsk.doEvents = lambda: None  # type: ignore[attr-defined]
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
    text_boxes: list[tuple[object, ...]] = []
    inputs = types.SimpleNamespace(
        addSelectionInput=lambda *_args: selection,
        addStringValueInput=lambda *args: string_inputs.append(args),
        addBoolValueInput=lambda *args: bool_inputs.append(args),
        addDropDownCommandInput=lambda *_args: anchor,
        addTextBoxCommandInput=lambda *args: (
            text_boxes.append(args), types.SimpleNamespace(formattedText="")
        )[1],
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

    # No output folder, no browse button, no overwrite checkbox. The one button
    # re-surveys Fusion state without changing it.
    assert string_inputs == []
    assert bool_inputs == [
        ("refresh_preflight", "Refresh body inventory", False, "", False)
    ]
    # One read-only box that states the export before the user commits to it.
    assert [args[0] for args in text_boxes] == ["preflight"]
    assert text_boxes[0][-1] is True


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
        addTextBoxCommandInput=lambda *args: (
            added.append(args[0]), types.SimpleNamespace(formattedText="")
        )[1],
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

    assert added == [
        "send_selection",
        "anchor_instance_id",
        "preflight",
        "refresh_preflight",
    ]


class _SelectionInput:
    """A Fusion selection input, read back the way the handlers read it."""

    def __init__(self, entities: list[object]) -> None:
        self._entities = list(entities)
        self.filters: list[str] = []
        self.limits: tuple[int, int] | None = None

    @property
    def selectionCount(self) -> int:
        return len(self._entities)

    def selection(self, index: int) -> object:
        return types.SimpleNamespace(entity=self._entities[index])

    def addSelectionFilter(self, value: str) -> None:
        self.filters.append(value)

    def setSelectionLimits(self, minimum: int, maximum: int) -> None:
        self.limits = (minimum, maximum)


class _Attributes:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def itemByName(self, group: str, name: str) -> object | None:
        key = (group, name)
        if self.values.get(key) is None:
            return None
        owner = self

        class _Attribute:
            @property
            def value(self) -> str:
                return owner.values[key]

            @value.setter
            def value(self, text: str) -> None:
                owner.values[key] = text

            def deleteMe(self) -> bool:
                owner.values.pop(key, None)
                return True

        return _Attribute()

    def add(self, group: str, name: str, value: str) -> None:
        self.values[(group, name)] = value


def _fake_face(role: str | None, area: float = 5.0) -> object:
    return types.SimpleNamespace(
        area=area,
        appearance=None if role is None else types.SimpleNamespace(name=role),
    )


def _fake_body(name: str, *, solid: bool = True) -> object:
    return types.SimpleNamespace(
        name=name, isSolid=solid, attributes=_Attributes(), objectType="adsk::fusion::BRepBody"
    )


def _dialog_inputs(**items: object) -> object:
    return types.SimpleNamespace(itemById=lambda name: items.get(name))


def _chosen(name: str) -> object:
    return types.SimpleNamespace(selectedItem=types.SimpleNamespace(name=name))


def _build_dialog(module, operation: str) -> tuple[list[str], dict[str, list[object]]]:
    """Run one CommandCreatedHandler and record what it put on the dialog."""

    added: list[str] = []
    listed: dict[str, list[object]] = {}

    def drop_down(input_id: str, *_args: object) -> object:
        listed[input_id] = []
        added.append(input_id)
        return types.SimpleNamespace(
            listItems=types.SimpleNamespace(
                add=lambda label, selected: listed[input_id].append((label, selected))
            ),
        )

    def text_box(input_id: str, _name: str, text: str, *_args: object) -> object:
        listed[input_id] = [text]
        added.append(input_id)
        return types.SimpleNamespace(formattedText=text)

    selections: dict[str, _SelectionInput] = {}

    def selection_input(input_id: str, *_args: object) -> object:
        added.append(input_id)
        selections[input_id] = _SelectionInput([])
        return selections[input_id]

    inputs = types.SimpleNamespace(
        addSelectionInput=selection_input,
        addDropDownCommandInput=drop_down,
        addTextBoxCommandInput=text_box,
    )
    module.adsk.core.DropDownStyles = types.SimpleNamespace(
        TextListDropDownStyle="text-list"
    )
    module.CommandCreatedHandler(operation).notify(types.SimpleNamespace(
        command=types.SimpleNamespace(
            commandInputs=inputs,
            inputChanged=types.SimpleNamespace(add=lambda _handler: None),
            execute=types.SimpleNamespace(add=lambda _handler: None),
        ),
    ))
    listed["_selections"] = list(selections.values())
    return added, listed


def test_set_wg_source_offers_the_four_roles_and_a_clear(monkeypatch) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    module = _load_instance(monkeypatch, "WGLink_source_dialog", ui)

    added, listed = _build_dialog(module, "source")

    assert added == ["source_faces", "source_role", "source_help"]
    assert [label for label, _selected in listed["source_role"]] == [
        "LF", "MF", "HF", "PORT_EXIT", "Clear WG source",
    ]
    assert [label for label, selected in listed["source_role"] if selected] == ["HF"]
    # The dialog teaches the convention rather than assuming it is known.
    assert "HF" in listed["source_help"][0] and "WG solves" in listed["source_help"][0]
    faces = listed["_selections"][0]
    assert faces.filters == ["Faces"] and faces.limits == (1, 0)
    # No managed-link chooser: authoring a source has nothing to do with links.
    assert "instance_choice" not in added
    assert ui.messages == []


def test_declare_body_offers_shell_exclude_and_clear(monkeypatch) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    module = _load_instance(monkeypatch, "WGLink_declare_dialog", ui)

    added, listed = _build_dialog(module, "declare")

    assert added == ["declare_bodies", "declaration", "declaration_help"]
    assert [selected for _label, selected in listed["declaration"]] == [True, False, False]
    labels = [label for label, _selected in listed["declaration"]]
    assert module.wglink_author.resolve_declaration_choice(labels[0]) == "exterior-shell"
    assert module.wglink_author.resolve_declaration_choice(labels[1]) == "exclude"
    assert module.wglink_author.resolve_declaration_choice(labels[2]) is None
    bodies = listed["_selections"][0]
    assert bodies.filters == ["SolidBodies", "SurfaceBodies", "MeshBodies"]
    assert bodies.limits == (1, 0)
    assert ui.messages == []


@pytest.mark.parametrize(
    ("answer", "detaches"),
    [("yes", True), ("no", False)],
    ids=["accepted", "declined"],
)
def test_detach_requires_confirmation_before_calling_the_core_api(
    monkeypatch, answer: str, detaches: bool
) -> None:
    ui = _UI(
        _Panels(),
        _Definitions(reserve_ids=False),
        dialog_result=answer,
    )
    module = _load_instance(monkeypatch, f"WGLink_detach_{answer}", ui)
    calls: list[dict[str, object]] = []

    def detach(_app: object, options: dict[str, object]) -> dict[str, object]:
        calls.append(options)
        return {
            "instance_id": "wgi_one",
            "attributes_removed": 5,
            "warnings": [],
        }

    monkeypatch.setattr(module.wglink_core, "detach", detach)
    module.CommandExecuteHandler("detach").notify(
        types.SimpleNamespace(
            command=types.SimpleNamespace(commandInputs=_dialog_inputs()),
        )
    )

    assert bool(calls) is detaches
    title, text = ui.messages[0]
    assert title == "WGLink — confirm Detach"
    assert "permanently removes" in text
    assert "Geometry stays" in text
    assert "cannot be re-attached" in text
    assert "fresh copy from Waveguide Generator" in text
    assert len(ui.messages) == (2 if detaches else 1)


def test_send_shows_and_closes_progress_around_the_slow_export(
    monkeypatch,
) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    module = _load_instance(monkeypatch, "WGLink_send_progress", ui)
    monkeypatch.setattr(module, "_send_options", lambda _inputs: {"selection": "root"})
    observed: list[tuple[int, str]] = []

    def send(_app: object, _options: dict[str, object]) -> dict[str, object]:
        dialog = ui.progress_dialogs[0]
        observed.append((dialog.progressValue, dialog.message))
        return {
            "bundle_path": "/workspace/wgreturn/test.wgreturn",
            "return_id": "wgr_test",
            "scope": {"status": "clean"},
            "sources": [],
        }

    monkeypatch.setattr(module.wglink_send, "send", send)

    module.CommandExecuteHandler("send").notify(
        types.SimpleNamespace(
            command=types.SimpleNamespace(commandInputs=_dialog_inputs()),
        )
    )

    assert observed == [
        (1, "Exporting STEP and validating the return bundle…"),
    ]
    dialog = ui.progress_dialogs[0]
    assert dialog.isCancelButtonShown is False
    assert dialog.isBackgroundTranslucent is False
    assert dialog.events[0] == (
        "show",
        "Send to WG",
        "Surveying the assembly…",
        0,
        3,
        0,
    )
    assert dialog.events[-1] == (
        "hide",
        3,
        "Return ready in Waveguide Generator.",
    )


def test_setting_a_source_paints_the_role_appearance_and_leaves_matches_alone(
    monkeypatch,
) -> None:
    module = _load_instance(
        monkeypatch, "WGLink_apply_source", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: "design")
    minted: list[str] = []
    monkeypatch.setattr(
        module.wglink_core,
        "_named_appearance",
        lambda _app, _design, name: (minted.append(name), types.SimpleNamespace(name=name))[1],
    )
    blank, wrong, already = _fake_face(None), _fake_face("MF"), _fake_face("HF")
    inputs = _dialog_inputs(
        source_faces=_SelectionInput([blank, wrong, already]),
        source_role=_chosen("HF"),
    )

    report = module._apply_source_role(inputs)

    assert minted == ["HF"]
    assert blank.appearance.name == "HF" and wrong.appearance.name == "HF"
    assert already.appearance.name == "HF"
    assert "3 faces now drive the HF source" in report["summary"]
    # Fusion reports square centimetres; the summary states square millimetres.
    assert "1500.0 mm²" in report["summary"]


def test_clearing_a_source_returns_only_role_faces_to_their_body_appearance(
    monkeypatch,
) -> None:
    module = _load_instance(
        monkeypatch, "WGLink_clear_source", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: "design")
    minted: list[str] = []
    monkeypatch.setattr(
        module.wglink_core,
        "_named_appearance",
        lambda _app, _design, name: minted.append(name),
    )
    role, painted = _fake_face("PORT_EXIT"), _fake_face("Steel - Satin")
    inputs = _dialog_inputs(
        source_faces=_SelectionInput([role, painted]),
        source_role=_chosen(module.wglink_author.CLEAR_SOURCE_LABEL),
    )

    report = module._apply_source_role(inputs)

    # No appearance is minted for a clear, and the user's own material survives.
    assert minted == []
    assert role.appearance is None
    assert painted.appearance.name == "Steel - Satin"
    assert "Cleared the WG source role from 1 face" in report["summary"]


def test_declaring_bodies_writes_the_attribute_the_export_scope_reads(
    monkeypatch,
) -> None:
    module = _load_instance(
        monkeypatch, "WGLink_apply_declare", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    shell, scaffold = _fake_body("Shell", solid=False), _fake_body("Jig", solid=False)
    labels = module.wglink_author.declaration_choices()

    module._apply_body_declaration(_dialog_inputs(
        declare_bodies=_SelectionInput([shell, scaffold]), declaration=_chosen(labels[0])
    ))

    assert module.wglink_send.read_declaration(shell) == "exterior-shell"
    assert module.wglink_send.read_declaration(scaffold) == "exterior-shell"

    module._apply_body_declaration(_dialog_inputs(
        declare_bodies=_SelectionInput([scaffold]), declaration=_chosen(labels[1])
    ))
    assert module.wglink_send.read_declaration(scaffold) == "exclude"

    report = module._apply_body_declaration(_dialog_inputs(
        declare_bodies=_SelectionInput([shell, scaffold]), declaration=_chosen(labels[2])
    ))
    assert shell.attributes.values == {} and scaffold.attributes.values == {}
    assert "Cleared the WG declaration on 2 bodies" in report["summary"]


def test_a_declaration_is_written_on_the_native_body_not_the_proxy(
    monkeypatch,
) -> None:
    """An occurrence proxy exposes no attributes, and the export reads the
    native object, so a proxy write would be silently lost."""

    module = _load_instance(
        monkeypatch, "WGLink_declare_proxy", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    native = _fake_body("Shell", solid=False)
    proxy = types.SimpleNamespace(
        name="Shell", isSolid=False, attributes=_Attributes(), nativeObject=native
    )

    module._apply_body_declaration(_dialog_inputs(
        declare_bodies=_SelectionInput([proxy]),
        declaration=_chosen(module.wglink_author.declaration_choices()[1]),
    ))

    assert module.wglink_send.read_declaration(native) == "exclude"
    assert proxy.attributes.values == {}


def test_the_send_dialog_states_the_export_before_ok(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch, "WGLink_preflight_sync", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    box = types.SimpleNamespace(formattedText="")
    monkeypatch.setattr(module, "_send_selection", lambda _inputs: "root")
    monkeypatch.setattr(module.wglink_send, "preflight_scope", lambda _app, _options: {
        "selection": "root",
        "instance_ids": [],
        "included": [{"name": "horn", "body_kind": "solid"}],
        "sources": [],
        "scope_error": None,
        "source_error": None,
        "bounds_mm": {"min": [-100.0, -100.0, -180.0], "max": [100.0, 100.0, 0.0]},
        "source_bounds_mm": None,
    })

    module._sync_preflight(_dialog_inputs(preflight=box))

    assert "1 solid" in box.formattedText
    assert "unlinked (Fusion-first) return" in box.formattedText
    # Both the missing source and the wrong-way frame are stated before OK.
    assert box.formattedText.count("<b>⚠") == 2


def test_refresh_body_inventory_resurveys_visibility_while_dialog_is_open(
    monkeypatch,
) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_preflight_visibility_refresh",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    box = types.SimpleNamespace(formattedText="")
    inputs = _dialog_inputs(preflight=box)
    monkeypatch.setattr(module, "_send_selection", lambda _inputs: "root")
    anchor_refreshes: list[object] = []
    monkeypatch.setattr(
        module, "_sync_anchor_choices", lambda value: anchor_refreshes.append(value)
    )
    visible = {"jig": True}
    surveys: list[bool] = []

    def survey(_app: object, _options: object) -> dict[str, object]:
        surveys.append(visible["jig"])
        included = [{"name": "cabinet", "body_kind": "solid"}]
        if visible["jig"]:
            included.append({"name": "measurement jig", "body_kind": "solid"})
        return {
            "selection": "root",
            "instance_ids": [],
            "included": included,
            "sources": [],
            "scope_error": None,
            "source_error": None,
            "bounds_mm": None,
            "source_bounds_mm": None,
        }

    monkeypatch.setattr(module.wglink_send, "preflight_scope", survey)
    module._sync_preflight(inputs)
    assert "Bodies included: 2 solids" in box.formattedText

    # The user hides a body in Fusion's browser while the command stays open.
    # Clicking Refresh must replace, not preserve, the stale inventory summary.
    visible["jig"] = False
    refresh = types.SimpleNamespace(id="refresh_preflight", value=True)
    module.CommandInputChangedHandler().notify(types.SimpleNamespace(
        input=refresh,
        inputs=inputs,
    ))

    assert surveys == [True, False]
    assert anchor_refreshes == [inputs]
    assert refresh.value is False
    assert "Bodies included: 1 solid" in box.formattedText
    assert "2 solids" not in box.formattedText


def test_a_model_that_cannot_be_surveyed_leaves_the_dialog_usable(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch, "WGLink_preflight_failure", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    box = types.SimpleNamespace(formattedText="")
    monkeypatch.setattr(module, "_send_selection", lambda _inputs: "root")

    def refuse(_app: object, _options: object) -> None:
        raise RuntimeError("No active Fusion design")

    monkeypatch.setattr(module.wglink_send, "preflight_scope", refuse)

    module._sync_preflight(_dialog_inputs(preflight=box))

    assert box.formattedText == "Pre-flight unavailable: No active Fusion design"


def test_an_unexpected_failure_shows_one_line_and_logs_the_traceback(
    monkeypatch,
) -> None:
    """A raw traceback in a modal states the add-in's internals and nothing the
    user can act on, and buries the sentence that identifies the failure."""

    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    module = _load_instance(monkeypatch, "WGLink_error_copy", ui)

    try:
        raise RuntimeError("Fusion refused the appearance")
    except RuntimeError as exc:
        module._report_error("Set WG Source…", "WGLink error", exc)

    (title, text), = ui.messages
    assert title == "WGLink error"
    assert text.startswith("Set WG Source… hit an unexpected error: ")
    assert "Fusion refused the appearance" in text
    assert "Traceback" not in text
    assert "Text Commands" in text
    logged, = ui.text_palette.written
    assert "Traceback" in logged and "RuntimeError" in logged


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


def test_every_helper_loads_under_a_registration_unique_package(
    monkeypatch,
) -> None:
    stale = types.ModuleType("wglink_workspace")
    stale.__file__ = "/old/WGLink/wglink_workspace.py"
    monkeypatch.setitem(sys.modules, "wglink_workspace", stale)

    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    first = _load_instance(monkeypatch, "WGLink_local_package_one", ui)
    second = _load_instance(monkeypatch, "WGLink_local_package_two", ui)

    assert first.wglink_workspace is not stale
    assert Path(first.wglink_workspace.__file__).resolve() == (
        ADDIN.parent / "wglink_workspace.py"
    ).resolve()
    assert first._registration_package_name != second._registration_package_name
    for name in (
        "wglink_workspace",
        "wglink_author",
        "wglink_bundle",
        "wglink_core",
        "wglink_return",
        "wglink_send",
        "wglink_watch",
    ):
        first_helper = first._registration_modules[name]
        second_helper = second._registration_modules[name]
        assert first_helper is not second_helper
        assert first_helper.__name__.startswith(first._registration_package_name + ".")
        assert second_helper.__name__.startswith(second._registration_package_name + ".")
    assert first.wglink_core.wglink_workspace is first.wglink_workspace
    assert second.wglink_core.wglink_workspace is second.wglink_workspace
    assert first.wglink_send.wglink_core is first.wglink_core
    assert second.wglink_send.wglink_core is second.wglink_core


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
    assert list(first.COMMANDS) == [
        "source",
        "declare",
        "solve",
        "insert",
        "update",
        "send",
        "detach",
    ]
    assert first.PROMOTED_COMMANDS == ("source", "solve", "send")
    assert {"audit", "relink"} <= set(first.wglink_core.__all__)
    # Three promoted commands plus Manage, whose four entries are the complete
    # maintenance UI. Audit and Relink remain head-less APIs only.
    assert panel.controls.count == 4
    manage = panel.controls.itemById(first.MANAGE_DROPDOWN_ID)
    assert manage is not None
    assert [control.definition.id for control in manage.controls._items] == [
        "hornlab_wglink_declare_body",
        "hornlab_wglink_insert",
        "hornlab_wglink_update",
        "hornlab_wglink_detach",
    ]
    assert [
        control.definition.id
        for control in panel.controls._items
        if isinstance(control, _Control)
    ] == [
        "hornlab_wglink_set_source",
        "hornlab_wglink_solve",
        "hornlab_wglink_send",
    ]
    assert first._owned is True
    # Every command ships an icon folder holding the 16 px art Fusion draws in
    # the panel dropdown; a missing folder would silently give a blank button.
    assert len(definitions.resource_folders) == 7
    for folder in definitions.resource_folders:
        assert folder and (Path(folder) / "16x16.png").is_file()

    second = _load_instance(monkeypatch, "WGLink_second", ui)
    second.run(None)

    # The buttons the first instance installed are untouched, and starting the
    # second instance reports no error -- whether or not Fusion keeps a deleted
    # command id reserved.
    assert ui.messages == []
    assert panels.itemById(first.PANEL_ID) is panel
    assert panel.controls.count == 4
    assert second._owned is False
    assert second._definitions == []

    # Stopping the adopted instance must not strip the owner's definitions.
    second.stop(None)
    assert ui.messages == []
    assert panel.isValid
    assert panel.controls.count == 4
    assert [d.id for d in definitions.items.values() if d.isValid] == [
        command_id for command_id, _name, _description in first.COMMANDS.values()
    ]

    # The owner still tears its own panel down.
    first.stop(None)
    assert panels.itemById(first.PANEL_ID) is None
    assert [d for d in definitions.items.values() if d.isValid] == []


def test_adopted_instance_waits_without_publishing_an_unserviceable_session(
    monkeypatch,
) -> None:
    """Only the registration that consumes requests may advertise its id."""

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
    assert second._candidate_thread is not None and second._candidate_thread.is_alive()
    assert published == []
    assert set(app.events) == {first.WATCH_EVENT_ID, second._candidate_event_id}
    assert second._ipc_lease_snapshot()["owner"] == first._watch_session_id

    # The standby stops only its private candidate event and leaves the owner.
    second.stop(None)
    assert first.WATCH_EVENT_ID in app.events
    assert second._candidate_event_id not in app.events
    assert first._watch_thread.is_alive()

    first.stop(None)
    assert first.WATCH_EVENT_ID not in app.events
    assert first._watch_thread is None


def test_owner_first_stop_promotes_the_surviving_registration(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    first = _load_instance(monkeypatch, "WGLink_owner_first", ui, app)
    second = _load_instance(monkeypatch, "WGLink_owner_survivor", ui, app)
    monkeypatch.setattr(first.wglink_workspace, "ipc_folder", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(second.wglink_workspace, "ipc_folder", lambda **_kwargs: tmp_path)

    first.run(None)
    second.run(None)
    candidate = second._candidate_handler
    old_panel = panels.itemById(first.PANEL_ID)
    assert candidate is not None and old_panel is not None
    status = tmp_path / first.wglink_watch.FUSION_STATUS_FILENAME
    assert json.loads(status.read_text())["sessionId"] == first._watch_session_id

    first.stop(None)
    assert panels.itemById(first.PANEL_ID) is None
    assert not status.exists()
    candidate.notify(None)

    replacement = panels.itemById(second.PANEL_ID)
    assert replacement is not None and replacement is not old_panel
    assert second._owned is True
    assert second._watch_thread is not None and second._watch_thread.is_alive()
    assert second._candidate_thread is None
    assert second._ipc_lease_snapshot()["owner"] == second._watch_session_id
    assert json.loads(status.read_text())["sessionId"] == second._watch_session_id
    second.stop(None)


def test_three_registrations_promote_one_at_a_time(monkeypatch) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    registrations = [
        _load_instance(monkeypatch, f"WGLink_three_{index}", ui, app)
        for index in range(3)
    ]
    for registration in registrations:
        monkeypatch.setattr(
            registration.wglink_workspace, "ipc_folder", lambda **_kwargs: None
        )
        registration.run(None)

    first, second, third = registrations
    second_candidate = second._candidate_handler
    third_candidate = third._candidate_handler
    assert second_candidate is not None and third_candidate is not None
    assert [registration._owned for registration in registrations] == [True, False, False]

    first.stop(None)
    second_candidate.notify(None)
    third_candidate.notify(None)
    assert [registration._owned for registration in registrations] == [False, True, False]
    assert third._candidate_thread is not None and third._candidate_thread.is_alive()
    assert second._ipc_lease_snapshot()["owner"] == second._watch_session_id

    second.stop(None)
    third_candidate.notify(None)
    assert third._owned is True
    assert third._ipc_lease_snapshot()["owner"] == third._watch_session_id
    third.stop(None)


def test_expired_owner_is_replaced_and_cannot_tear_successor_down(monkeypatch) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    first = _load_instance(monkeypatch, "WGLink_expired_owner", ui, app)
    second = _load_instance(monkeypatch, "WGLink_expired_successor", ui, app)
    monkeypatch.setattr(first.wglink_workspace, "ipc_folder", lambda **_kwargs: None)
    monkeypatch.setattr(second.wglink_workspace, "ipc_folder", lambda **_kwargs: None)
    first.run(None)
    second.run(None)
    candidate = second._candidate_handler
    assert candidate is not None

    # Model an owner whose worker vanished without Fusion delivering stop().
    assert first._watch_stop is not None and first._watch_thread is not None
    first._watch_stop.set()
    first._watch_thread.join(timeout=1)
    with first._ipc_owner_runtime.lock:
        first._ipc_owner_runtime.renewed_at = (
            first._lease_now() - first.OWNER_LEASE_SECONDS - 1
        )
    candidate.notify(None)
    replacement = panels.itemById(second.PANEL_ID)
    assert replacement is not None and second._owned is True
    assert first._owns_ipc_lease() is False
    assert first.WATCH_EVENT_ID not in app.events
    assert second.WATCH_EVENT_ID in app.events

    # A delayed Fusion stop callback from the expired registration sees that
    # its authority is gone and leaves the successor's controls/event intact.
    first.stop(None)
    assert panels.itemById(second.PANEL_ID) is replacement
    assert second.WATCH_EVENT_ID in app.events
    second.stop(None)


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
    monkeypatch.setattr(module, "_renew_ipc_lease", lambda: True)

    module._watch_loop(app, OneTick())
    module._candidate_loop(app, OneTick())

    assert app.fired == [module.WATCH_EVENT_ID, module._candidate_event_id]


def test_the_watcher_prompt_is_held_off_while_a_command_runs(monkeypatch) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(monkeypatch, "WGLink_busy", ui, app)

    surveyed: list[object] = []
    snapshots: list[list[dict[str, str]]] = []
    links = [{"instance_id": "instance-a"}]
    monkeypatch.setattr(
        module,
        "_document_links",
        lambda: snapshots.append(links) or links,
    )
    monkeypatch.setattr(
        module._watcher, "survey", lambda links: surveyed.append(links) or []
    )
    module._command_busy = True
    module._on_watch_tick()
    assert surveyed == []

    module._command_busy = False
    module._on_watch_tick()
    assert surveyed == [links]
    assert snapshots == [links]


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


def test_a_pending_update_targets_the_exact_selected_duplicate_instance(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(monkeypatch, "WGLink_pending_exact_instance", ui, app)
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "horn.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    marker = bundle_root / module.wglink_watch.HANDOFF_FILENAME
    marker.write_text(json.dumps({
        "schemaVersion": 1,
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_5",
        "exportId": "wge_5",
        "sequence": 5,
        "designId": "wgd-shared",
        "expectedDocumentId": "fusion:doc-a",
        "expectedInstanceId": "instance-b",
    }))
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(
        module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root
    )
    updated: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        module.wglink_core,
        "update",
        lambda _app, path, options: updated.append((path, options)),
    )
    snapshot = {
        "document_id": "fusion:doc-a",
        "links": [
            {
                "instance_id": "instance-a",
                "design_id": "wgd-shared",
                "export_id": "wge_4",
            },
            {
                "instance_id": "instance-b",
                "design_id": "wgd-shared",
                "export_id": "wge_4",
            },
        ],
    }

    assert module._apply_pending_handoff(snapshot) is True

    assert updated == [(str(bundle), {"instance_id": "instance-b"})]
    assert not marker.exists()
    assert ui.messages == []


@pytest.mark.parametrize(
    ("expected_instance_id", "instance_ids", "message_fragment"),
    [
        (
            None,
            ["instance-a", "instance-b"],
            "WG did not select an exact instance",
        ),
        (
            "instance-stale",
            ["instance-a", "instance-b"],
            "no longer contains exactly one WG link with the instance selected in WG",
        ),
        (
            "instance-b",
            ["instance-b", "instance-b"],
            "no longer contains exactly one WG link with the instance selected in WG",
        ),
    ],
)
def test_a_duplicate_pending_update_refuses_missing_stale_or_ambiguous_identity(
    monkeypatch,
    tmp_path: Path,
    expected_instance_id: str | None,
    instance_ids: list[str],
    message_fragment: str,
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(
        monkeypatch,
        f"WGLink_pending_refuse_{expected_instance_id or 'missing'}",
        ui,
        app,
    )
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "horn.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    marker = bundle_root / module.wglink_watch.HANDOFF_FILENAME
    payload = {
        "schemaVersion": 1,
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_5",
        "exportId": "wge_5",
        "sequence": 5,
        "designId": "wgd-shared",
        "expectedDocumentId": "fusion:doc-a",
    }
    if expected_instance_id is not None:
        payload["expectedInstanceId"] = expected_instance_id
    marker.write_text(json.dumps(payload))
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(
        module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root
    )
    mutations: list[str] = []
    monkeypatch.setattr(
        module.wglink_core,
        "update",
        lambda *_args, **_kwargs: mutations.append("update"),
    )
    monkeypatch.setattr(
        module.wglink_core,
        "insert",
        lambda *_args, **_kwargs: mutations.append("insert"),
    )
    snapshot = {
        "document_id": "fusion:doc-a",
        "links": [
            {
                "instance_id": instance_id,
                "design_id": "wgd-shared",
                "export_id": "wge_4",
            }
            for instance_id in instance_ids
        ],
    }

    assert module._apply_pending_handoff(snapshot) is True

    assert mutations == []
    assert marker.exists()
    assert len(ui.messages) == 1
    assert message_fragment in ui.messages[0][1]


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
        "capture_document": True,
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


def test_a_start_that_fails_partway_rolls_back_panel_and_lease(
    monkeypatch,
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    module = _load_instance(monkeypatch, "WGLink_partial", ui)

    calls = {"n": 0}
    real_add = definitions.addButtonDefinition

    def failing_add(
        definition_id: str,
        name: str,
        description: str,
        resource_folder: str = "",
    ):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Fusion refused the definition")
        return real_add(definition_id, name, description, resource_folder)

    monkeypatch.setattr(definitions, "addButtonDefinition", failing_add)
    module.run(None)

    assert calls["n"] == 2
    assert module._owned is False
    assert ui.messages and ui.messages[0][0] == "WGLink start error"
    assert panels.itemById(module.PANEL_ID) is None
    assert not [definition for definition in definitions.items.values() if definition.isValid]
    assert module._ipc_lease_snapshot()["owner"] is None

    # The failed registration did not strand either toolbar or lease, so a
    # clean registration can start immediately without a Fusion restart.
    recovery = _load_instance(monkeypatch, "WGLink_partial_recovery", ui)
    recovery.run(None)
    assert recovery._owned is True
    assert panels.itemById(recovery.PANEL_ID) is not None
    recovery.stop(None)
