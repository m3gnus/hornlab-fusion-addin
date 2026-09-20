"""Panel ownership when one add-in is registered twice.

Fusion loads every registered path as its own module with its own globals, so
two registrations of WGLink are two instances that share only the toolbar panel
they build. The second instance used to tear the first one's panel and command
definitions down and rebuild them, which left the toolbar holding buttons whose
definitions were dead.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
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


class _Proxy:
    """A fresh wrapper over one underlying object, forwarding every access.

    Fusion's Python bindings mint one of these on every property read, which is
    why this repository identifies entities by ``entityToken`` rather than by
    Python identity: ``a is b`` and ``id(a)`` answer about the wrapper, not
    about the thing. A fake that hands back the identical object every time
    cannot see a design that depends on either.
    """

    def __init__(self, target: object) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> object:
        return getattr(object.__getattribute__(self, "_target"), name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(object.__getattribute__(self, "_target"), name, value)


class _ProxyingApplication(_Application):
    """An application whose ``activeDocument`` is a property, as Fusion's is."""

    def __init__(self, ui: "_UI") -> None:
        super().__init__(ui)
        object.__setattr__(self, "_document", None)

    @property
    def activeDocument(self) -> object | None:
        document = object.__getattribute__(self, "_document")
        return None if document is None else _Proxy(document)

    @activeDocument.setter
    def activeDocument(self, value: object | None) -> None:
        object.__setattr__(self, "_document", value)


def _proxying_root(design: object) -> object:
    """``design`` with a ``rootComponent`` that is freshly wrapped per read."""

    root = design.rootComponent
    proxied = types.SimpleNamespace(
        **{
            name: value
            for name, value in vars(design).items()
            if name != "rootComponent"
        }
    )
    return _RootProxying(proxied, root)


class _RootProxying:
    """A design whose ``rootComponent`` mints a wrapper on every access."""

    def __init__(self, design: object, root: object) -> None:
        object.__setattr__(self, "_design", design)
        object.__setattr__(self, "_root", root)

    @property
    def rootComponent(self) -> object:
        return _Proxy(object.__getattribute__(self, "_root"))

    def __getattr__(self, name: str) -> object:
        return getattr(object.__getattribute__(self, "_design"), name)


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
    # Fusion's own cast returns None for a product that is not a Design, which
    # is what lets `wglink_core._design` refuse a non-design document. Tests
    # whose active product is a bare namespace keep that refusal; only a
    # fixture that supplies a real design type reaches the live return state.
    fusion.Design = types.SimpleNamespace(
        cast=lambda product: (
            product if getattr(product, "designType", None) is not None else None
        )
    )
    fusion.DesignTypes = types.SimpleNamespace(ParametricDesignType="parametric")
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


@pytest.mark.parametrize(
    "capabilities, declared",
    [
        (None, False),
        ({"schemaVersion": 1, "solveCommandDelivery": 3, "fusionRequestDelivery": 3}, False),
        ({"schemaVersion": 1, "sourceIdentity": True}, False),
        ({"schemaVersion": 1, "sourceIdentity": 1}, True),
    ],
)
def test_every_source_path_declares_identity_only_when_wg_advertises_it(
    monkeypatch, tmp_path: Path, capabilities, declared
) -> None:
    """Send, the heartbeat token and a return WG asked for must carry one set of
    source ids, so all of them ask the same capability file."""

    module = _load_instance(
        monkeypatch,
        f"WGLink_identity_gate_{declared}_{bool(capabilities)}",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    if capabilities is not None:
        (tmp_path / "wg-capabilities.json").write_text(
            json.dumps(capabilities), encoding="utf-8"
        )
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: tmp_path)
    monkeypatch.setattr(module, "_send_selection", lambda _inputs: "root")
    seen: list[object] = []
    monkeypatch.setattr(
        module.wglink_send,
        "return_state",
        lambda _app, options: seen.append(options.get("source_identity")) or {"hash": "h"},
    )

    assert module._send_options(types.SimpleNamespace())["source_identity"] is declared
    module._measure_geometry_state(object(), {})
    assert seen == [declared]

    previewed: list[object] = []

    def preview(_app, options):
        previewed.append(options.get("source_identity"))
        raise module.wglink_core.WgLinkError("preview stops here")

    monkeypatch.setattr(module.wglink_send, "preflight_scope", preview)
    box = types.SimpleNamespace(formattedText="", text="")
    module._sync_preflight(_dialog_inputs(preflight=box))
    assert previewed == [declared]


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


class _DropDownInput:
    """A Fusion dropdown *command input* as the dialog builder uses it.

    Distinct from ``_DropDown`` above, which is a toolbar control.

    The Send dialog populates the model-domain dropdown at creation, so a stub
    with no ``listItems`` raises inside ``CommandCreatedHandler.notify`` -- and
    that handler swallows exceptions, which silently truncates the dialog
    instead of failing. A fake that accepts what the real object accepts is the
    only kind that can catch that.
    """

    def __init__(self) -> None:
        self.isVisible = True
        self.items: list[tuple[str, bool]] = []
        self.listItems = types.SimpleNamespace(
            add=lambda name, selected, *rest: self.items.append((name, selected)),
            clear=lambda: self.items.clear(),
        )

    @property
    def selectedItem(self) -> object:
        chosen = next((name for name, selected in self.items if selected), None)
        return None if chosen is None else types.SimpleNamespace(name=chosen)


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
    anchor = _DropDownInput()
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
    # Two read-only boxes: what the domain dropdown means, and what the export
    # will do before the user commits to it.
    assert [args[0] for args in text_boxes] == ["model_domain_help", "preflight"]
    assert all(args[-1] is True for args in text_boxes)


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
    anchor = _DropDownInput()
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
        "model_domain",
        "model_domain_help",
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
        attributes=_Attributes(),
    )


class _FoundAttribute:
    def __init__(self, parent: object, key: tuple[str, str]) -> None:
        self.parent, self.groupName, self.name = parent, key[0], key[1]
        self._key = key

    @property
    def value(self) -> object:
        return self.parent.attributes.values.get(self._key)

    def deleteMe(self) -> bool:
        self.parent.attributes.values.pop(self._key, None)
        return True


def _face_design(faces: list[object]) -> object:
    """A design whose attribute search sees exactly these faces' stamps."""

    def find(group: str, name: str) -> list[object]:
        return [
            _FoundAttribute(face, key)
            for face in faces
            for key in list(face.attributes.values)
            if key == (group, name)
        ]

    return types.SimpleNamespace(findAttributes=find)


def _identity_stamp(face: object) -> dict[str, object] | None:
    raw = face.attributes.values.get(("WGLink", "source_identity"))
    return None if raw is None else json.loads(raw)


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
        "LF", "MF", "HF", "PASSIVE_CARDIOID", "Clear WG source",
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
    minted: list[str] = []
    monkeypatch.setattr(
        module.wglink_core,
        "_named_appearance",
        lambda _app, _design, name: (minted.append(name), types.SimpleNamespace(name=name))[1],
    )
    blank, wrong, already = _fake_face(None), _fake_face("MF"), _fake_face("HF")
    design = _face_design([blank, wrong, already])
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)
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
    # Every selected face -- the one that already carried HF too -- is stamped
    # with one source identity, which is what a later Send resolves.
    stamps = [_identity_stamp(face) for face in (blank, wrong, already)]
    assert all(stamp is not None for stamp in stamps)
    assert {stamp["id"] for stamp in stamps} == {stamps[0]["id"]}
    assert {stamp["faces"] for stamp in stamps} == {3}
    assert {stamp["role"] for stamp in stamps} == {"HF"}


def test_setting_a_source_again_on_a_refused_source_reassigns_a_new_identity(
    monkeypatch,
) -> None:
    module = _load_instance(
        monkeypatch, "WGLink_reassign_source", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    monkeypatch.setattr(
        module.wglink_core,
        "_named_appearance",
        lambda _app, _design, name: types.SimpleNamespace(name=name),
    )
    first, second = _fake_face("HF"), _fake_face("HF")
    design = _face_design([first, second])
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)
    module._apply_source_role(_dialog_inputs(
        source_faces=_SelectionInput([first]), source_role=_chosen("HF")
    ))
    original = _identity_stamp(first)
    # A split: Fusion copied the face's stamp onto the other half.
    second.attributes.values = dict(first.attributes.values)
    with pytest.raises(module.wglink_core.WgLinkError, match="split or copied"):
        module.wglink_send._painted_source_identity("HF", [first, second])

    module._apply_source_role(_dialog_inputs(
        source_faces=_SelectionInput([first, second]), source_role=_chosen("HF")
    ))

    assert module.wglink_send._painted_source_identity("HF", [first, second]) != original["id"]


def test_clearing_a_source_returns_only_role_faces_to_their_body_appearance(
    monkeypatch,
) -> None:
    module = _load_instance(
        monkeypatch, "WGLink_clear_source", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    minted: list[str] = []
    monkeypatch.setattr(
        module.wglink_core,
        "_named_appearance",
        lambda _app, _design, name: minted.append(name),
    )
    # PORT_EXIT is the retired spelling of PASSIVE_CARDIOID; Clear still has
    # to recognise -- and strip -- a face painted under the old name.
    role, painted = _fake_face("PORT_EXIT"), _fake_face("Steel - Satin")
    kept = _fake_face("PORT_EXIT")
    design = _face_design([role, painted, kept])
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)
    stamp = {"schema": 1, "id": "wgs-00000000000000000001", "role": "PORT_EXIT", "faces": 2}
    role.attributes.values[("WGLink", "source_identity")] = json.dumps(dict(stamp, face="a"))
    kept.attributes.values[("WGLink", "source_identity")] = json.dumps(dict(stamp, face="b"))
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
    # Its identity goes with the role, and the face that stays is one short.
    assert _identity_stamp(role) is None
    assert _identity_stamp(kept)["faces"] == 1


def test_a_failed_stamp_says_the_paint_it_applied_was_kept(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch, "WGLink_stamp_failure", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    monkeypatch.setattr(
        module.wglink_core,
        "_named_appearance",
        lambda _app, _design, name: types.SimpleNamespace(name=name),
    )
    blank = _fake_face(None)

    class _Locked(_Attributes):
        def add(self, group: str, name: str, value: object) -> None:
            raise RuntimeError("the component is read-only")

    blank.attributes = _Locked()
    design = _face_design([blank])
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)

    with pytest.raises(module.wglink_core.WgLinkError) as refusal:
        module._apply_source_role(_dialog_inputs(
            source_faces=_SelectionInput([blank]), source_role=_chosen("HF")
        ))

    text = str(refusal.value)
    assert "Every source identity stamp was restored" in text
    assert "HF paint applied to 1 face(s) was kept" in text
    assert blank.appearance.name == "HF"


def test_clear_takes_the_identity_off_a_face_whose_paint_was_removed_by_hand(
    monkeypatch,
) -> None:
    """The face carries no role any more, so the appearance plan leaves it alone;
    its stale stamp still has to go, or its source stays refused for good."""

    module = _load_instance(
        monkeypatch, "WGLink_clear_stale_stamp", _UI(_Panels(), _Definitions(reserve_ids=False))
    )
    stripped, kept = _fake_face(None), _fake_face("HF")
    design = _face_design([stripped, kept])
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)
    stamp = {"schema": 1, "id": "wgs-00000000000000000002", "role": "HF", "faces": 2}
    stripped.attributes.values[("WGLink", "source_identity")] = json.dumps(dict(stamp, face="a"))
    kept.attributes.values[("WGLink", "source_identity")] = json.dumps(dict(stamp, face="b"))

    report = module._apply_source_role(_dialog_inputs(
        source_faces=_SelectionInput([stripped]),
        source_role=_chosen(module.wglink_author.CLEAR_SOURCE_LABEL),
    ))

    assert _identity_stamp(stripped) is None
    assert _identity_stamp(kept)["faces"] == 1
    assert "stale WG source identity from 1 face" in report["summary"]


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

    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [
        {"instance_id": "wgi_one", "design_name": "Tritonia"},
    ])
    assert build() == []

    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [
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
    # The owner registers both of its events -- the four-second watch tick and
    # the live worker's hand-over; the standby registers only its private
    # promotion event and never the live one.
    assert set(app.events) == {
        first.WATCH_EVENT_ID, first.LIVE_EVENT_ID, second._candidate_event_id
    }
    assert second._ipc_lease_snapshot()["owner"] == first._watch_session_id

    # The standby stops only its private candidate event and leaves the owner.
    second.stop(None)
    assert first.WATCH_EVENT_ID in app.events
    assert second._candidate_event_id not in app.events
    assert first._watch_thread.is_alive()

    first.stop(None)
    assert first.WATCH_EVENT_ID not in app.events
    assert first.LIVE_EVENT_ID not in app.events
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


class _Collection(list):
    @property
    def count(self) -> int:
        return len(self)

    def item(self, index: int) -> object:
        return self[index]

    def itemByName(self, name: str) -> object | None:
        return next((item for item in self if getattr(item, "name", None) == name), None)


class _Attribute:
    """Fusion's attribute handle: it writes through to its owner's table."""

    def __init__(self, owner: "_Attributes", key: tuple[str, str]) -> None:
        self._owner, self._key = owner, key

    @property
    def value(self) -> object:
        return self._owner.values.get(self._key)

    @value.setter
    def value(self, value: object) -> None:
        self._owner.values[self._key] = value

    def deleteMe(self) -> bool:
        self._owner.values.pop(self._key, None)
        return True


class _Attributes:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def itemByName(self, group: str, name: str) -> _Attribute | None:
        key = (group, name)
        return _Attribute(self, key) if self.values.get(key) is not None else None

    def add(self, group: str, name: str, value: object) -> None:
        self.values[(group, name)] = value


def _point(x: float, y: float, z: float) -> object:
    return types.SimpleNamespace(x=x, y=y, z=z)


def _box_mm(low: tuple[float, float, float], high: tuple[float, float, float]) -> object:
    """A bounding box written in millimetres, stored in Fusion's centimetres."""

    return types.SimpleNamespace(
        minPoint=_point(*(value / 10.0 for value in low)),
        maxPoint=_point(*(value / 10.0 for value in high)),
    )


_THROAT_DIAMETER_MM = 25.4
_THROAT_AREA_MM2 = math.pi * _THROAT_DIAMETER_MM * _THROAT_DIAMETER_MM / 4.0


def _managed_body(name: str = "Waveguide") -> object:
    """One managed solid carrying the painted throat face a return needs."""

    throat = types.SimpleNamespace(
        area=_THROAT_AREA_MM2 / 100.0,  # Fusion reports face area in cm2
        appearance=types.SimpleNamespace(name="HF"),
        edges=_Collection(),
        boundingBox=_box_mm((0.0, 0.0, 0.0), (10.0, 10.0, 0.0)),
        attributes=_Attributes(),
    )
    return types.SimpleNamespace(
        name=name,
        isSolid=True,
        isVisible=True,
        faces=_Collection([throat]),
        attributes=_Attributes(),
        boundingBox=_box_mm((-40.0, -40.0, 0.0), (40.0, 40.0, 120.0)),
        volume=1.0,
        entityToken=f"token-{name}",
        objectType="adsk::fusion::BRepBody",
    )


def _parameter(name: str, expression: str, value_mm: float) -> object:
    return types.SimpleNamespace(
        name=name, expression=expression, value=value_mm / 10.0, unit="mm"
    )


def _linked_document(
    module,
    *,
    instance_id: str = "wgi-heartbeat",
    bundle_path: str = "bundles/horn.wglink",
    export_id: str = "wge_1",
    stored_expressions: dict[str, str] | None = None,
    live_expressions: dict[str, str] | None = None,
):
    """A document holding one real, fully attributed WGLink insertion.

    Everything the heartbeat reads is present as genuine Fusion attributes, so
    ``_link_records``, ``_local_body_state``, ``_parameter_drift`` and the live
    ``return_state`` all run for real against it. Stubbing those out is what
    let the heartbeat report an intact managed body as ``missing`` for a whole
    release without a single test noticing.
    """

    core = module.wglink_core
    body = _managed_body()
    root = types.SimpleNamespace(
        name="Doc",
        objectType="adsk::fusion::Component",
        # A real root component has one, and it is how this add-in names an
        # unsaved document. ``instance_id`` keeps two fixture documents apart
        # exactly as two real documents are kept apart.
        entityToken=f"component-token-{instance_id}",
        bRepBodies=_Collection([body]),
        meshBodies=_Collection(),
        constructionPlanes=_Collection([
            types.SimpleNamespace(
                name="WG_THROAT_PLANE",
                geometry=types.SimpleNamespace(
                    origin=_point(0.0, 0.0, 0.0), normal=_point(0.0, 0.0, 1.0)
                ),
            )
        ]),
        constructionAxes=_Collection([
            types.SimpleNamespace(
                name="WG_AXIS",
                geometry=types.SimpleNamespace(
                    origin=_point(0.0, 0.0, 0.0), direction=_point(0.0, 0.0, 1.0)
                ),
            )
        ]),
        sketches=_Collection(),
        occurrences=_Collection(),
        allOccurrences=_Collection(),
        attributes=_Attributes(),
    )
    body.parentComponent = root
    core._set_attribute(body, "instance_id", instance_id)
    core._set_attribute(body, "role", "waveguide")
    core._set_attribute(body, "face_role", "HF")

    stored = dict(stored_expressions or {})
    payload = {
        "instance_id": instance_id,
        "topology": "wg",
        "design_id": "wgd-a",
        "design_name": "Horn",
        "bundle_path": bundle_path,
        "export_id": export_id,
        "export_sequence": "1",
        "build_mode": "freestanding",
        "parameter_prefix": "wg_",
        "source_role": "HF",
        "expected_throat_area_mm2": f"{_THROAT_AREA_MM2:.6f}",
        "throat_z_mm": "0",
        "wrapper": "root",
        "body_fingerprint": json.dumps(core._body_fingerprint(body)),
        "parameter_expressions": json.dumps(stored),
    }
    attributes = [
        types.SimpleNamespace(
            name=core._wrapper_attribute_name(instance_id),
            value=json.dumps(payload),
            parent=root,
        ),
        *[
            types.SimpleNamespace(name=key[1], value=str(value), parent=body)
            for key, value in body.attributes.values.items()
        ],
    ]

    live = dict(live_expressions if live_expressions is not None else stored)
    parameters = _Collection([
        _parameter("wg_throat_dia", f"{_THROAT_DIAMETER_MM} mm", _THROAT_DIAMETER_MM),
        *[
            _parameter(name, expression, float(expression.split()[0]))
            for name, expression in sorted(live.items())
        ],
    ])
    design = types.SimpleNamespace(
        objectType="adsk::fusion::Design",
        designType="parametric",
        rootComponent=root,
        findAttributes=lambda _group, _name: _Collection(attributes),
        userParameters=parameters,
        unitsManager=types.SimpleNamespace(
            convert=lambda value, source, _target: (
                value * 10.0 if source == "internalUnits" else value
            )
        ),
    )
    return design, body


def test_document_links_derive_sorted_drifted_parameter_names_from_drift(
    monkeypatch,
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_parameter_drift", ui, app)
    design, _body = _linked_document(
        module,
        stored_expressions={"wg_mouth_thickness": "4 mm", "wg_length": "25 mm"},
        live_expressions={"wg_mouth_thickness": "6 mm", "wg_length": "30 mm"},
    )
    app.activeProduct = design

    links = module._document_links()

    assert links[0]["drifted_parameters"] == ["wg_length", "wg_mouth_thickness"]
    assert int(links[0]["parameter_drift_count"]) == len(
        links[0]["drifted_parameters"]
    )


def _tick_module(monkeypatch, name: str):
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, name, ui, app)
    design, body = _linked_document(module)
    app.activeProduct = design
    # Unsaved: no dataFile, so the identity comes from the root component's
    # entity token, which is what production reads.
    app.activeDocument = types.SimpleNamespace(name="Horn")
    body.revisionId = "revision-1"
    return module, design, body


def _unmoved_live_state(monkeypatch, module, signature_hash: str) -> None:
    """Stand in for the guard's fresh measurement of an unmoved document.

    Only for tests whose fixture is a bare namespace product, which no real
    ``return_state`` can measure. Tests that exercise the guard itself run
    against ``_linked_document`` and measure for real.
    """

    monkeypatch.setattr(
        module,
        "_fresh_geometry_state",
        lambda: {
            "document_signature_hash": signature_hash,
            "document_body_count": "1",
            "source_state_hash": "",
            "instance_identities": {},
            "bodies": {},
        },
    )


def _ask_for_refresh(module, reason: str = "test") -> None:
    """Request a refresh for the document that is active right now.

    Every production caller names the document it is asking about, so a test
    that leaves it out would exercise a request shape that cannot occur.
    """

    module._request_geometry_refresh(reason, module._active_document_id())


def _prime_measured_state(module) -> None:
    """Run what the first two ticks do: see the document, then measure it once.

    The periodic heartbeat measures nothing at all, so a fixture that needs a
    published measured state has to arrive at one the way production does: a
    tick notices a linked document and asks for one look, and the next tick
    pays for it. Asking by hand instead would leave the sighting unrecorded,
    and the request it makes would still be outstanding.
    """

    module._document_links()
    module._service_geometry_refresh()
    assert module._geometry_refresh_pending is None


def _count_measurements(monkeypatch, module) -> list[int]:
    calls: list[int] = []
    real = module.wglink_send.return_state

    def counted(app, options=None):
        calls.append(1)
        return real(app, options)

    monkeypatch.setattr(module.wglink_send, "return_state", counted)
    return calls


def test_document_links_time_each_phase_of_the_tick_it_runs_on(monkeypatch) -> None:
    """Which phase costs the four-second tick, measured where it runs.

    The measured half walks the whole export scope and fingerprints every
    included face. The tick no longer pays for it -- it reads the cache, and a
    cache miss answers "cannot tell" -- but the phases are still timed, because
    until they are, "Fusion is slow" and "the heartbeat is slow" are the same
    unfalsifiable sentence.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_tick_timings")

    timings: dict[str, float] = {}
    module._document_links(timings)

    assert timings["geometry_state"] == "unavailable"
    assert {"resolve_links_ms", "geometry_state_ms", "per_link_ms"} <= set(timings)
    assert all(
        value >= 0.0 for value in timings.values() if isinstance(value, float)
    )

    snapshot = module._fusion_snapshot()
    diagnostics = snapshot["diagnostics"]
    assert diagnostics["watchIntervalSeconds"] == module.WATCH_INTERVAL_SECONDS
    assert "snapshot_ms" in diagnostics["lastTickMs"]


def test_an_unlinked_document_heartbeat_never_measures_geometry(monkeypatch) -> None:
    """Enabling WGLink must not inspect an unrelated Fusion model.

    The root-scope return-state walk evaluates every included face and body on
    Fusion's main thread.  There is no link identity or update token to report
    when the document has no WGLink records, so paying that cost can only make
    an unrelated modelling session stall.
    """

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_unlinked_tick", ui, app)
    app.activeProduct = types.SimpleNamespace(
        objectType="adsk::fusion::Design",
        findAttributes=lambda _group, _name: _Collection(),
    )
    monkeypatch.setattr(
        module,
        "_measure_geometry_state",
        lambda *_args, **_kwargs: pytest.fail(
            "an unlinked heartbeat traversed the root assembly"
        ),
    )

    timings: dict[str, float] = {}
    assert module._document_links(timings) == []
    assert timings["geometry_state"] == "not-linked"
    assert timings["geometry_state_ms"] == 0.0

    # There is no cache age at which an unrelated model becomes WGLink's
    # geometry to inspect.  A later heartbeat stays on the same cheap path.
    later_timings: dict[str, float] = {}
    assert module._document_links(later_timings) == []
    assert later_timings["geometry_state"] == "not-linked"
    assert later_timings["geometry_state_ms"] == 0.0


def test_the_heartbeat_does_not_re_measure_a_document_that_has_not_moved(
    monkeypatch,
) -> None:
    """An idle document must cost the tick nothing it can feel.

    ``return_state`` evaluates ``face.area`` and ``body.volume``, which are
    computed on a dense NURBS body rather than looked up. On a four-second
    timer with no change detection that is a permanent load on Fusion's main
    thread for a document nobody is editing.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_tick_cached")
    _prime_measured_state(module)
    calls = _count_measurements(monkeypatch, module)

    first = module._document_links()
    second_timings: dict[str, float] = {}
    second = module._document_links(second_timings)

    assert calls == []
    assert second_timings["geometry_state"] == "cached"
    assert second[0]["document_signature_hash"] == first[0]["document_signature_hash"]
    assert second[0]["local_body_state"] == first[0]["local_body_state"]


def test_the_heartbeat_reports_a_moved_body_without_re_measuring(monkeypatch) -> None:
    """The cache may not outlive the geometry it describes -- and may not renew itself.

    ``revisionId`` is a property read, so asking it every tick is free; the
    measurement it guards is not. The tick therefore says the published
    observation is no longer current and leaves it at that. Only an explicit
    refresh pays for a new one.
    """

    module, _design, body = _tick_module(monkeypatch, "WGLink_tick_revision")
    _prime_measured_state(module)
    calls = _count_measurements(monkeypatch, module)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)

    module._document_links()
    body.revisionId = "revision-2"
    timings: dict[str, float] = {}
    module._document_links(timings)

    assert calls == []
    assert timings["geometry_state"] == "stale"

    _ask_for_refresh(module, "explicit")
    module._service_geometry_refresh()
    refreshed: dict[str, float] = {}
    module._document_links(refreshed)

    assert len(calls) == 1
    assert refreshed["geometry_state"] == "cached"


def test_a_costly_measurement_throttles_the_next_explicit_refresh(monkeypatch) -> None:
    """The duty cycle now bounds explicit refreshes, which is all that is left.

    Nothing periodic can ask for a measurement any more, so the cycle no longer
    defers a heartbeat's own recompute. What it still does is bound a stream of
    explicit causes -- a burst of commands, repeated document switches -- to a
    small share of Fusion's main thread on a document where one measurement is
    expensive. The request stays pending while it waits, so nothing is lost.
    """

    module, _design, body = _tick_module(monkeypatch, "WGLink_tick_duty")
    real = module.wglink_send.return_state

    def slow(app, options=None):
        time.sleep(0.02)
        return real(app, options)

    monkeypatch.setattr(module.wglink_send, "return_state", slow)
    _prime_measured_state(module)
    calls = _count_measurements(monkeypatch, module)

    body.revisionId = "revision-2"
    _ask_for_refresh(module, "command")
    module._service_geometry_refresh()

    assert calls == []
    assert module._geometry_refresh_pending is not None
    timings: dict[str, float] = {}
    module._document_links(timings)
    assert timings["geometry_state"] == "stale"
    assert timings["geometry_state_ms"] < 20.0


def _claim_in_place(monkeypatch, module) -> None:
    """Claim and retire a fake request -- a namespace with no file -- in place.

    Real requests are files and are claimed by renaming them; a test that
    stands a namespace in for one has nothing to rename.
    """

    watch = module.wglink_watch
    for name in ("claim_request", "acknowledge_handoff", "acknowledge_return_request"):
        real = getattr(watch, name)

        def in_place(pending, *args, _real=real, _name=name, **kwargs):
            if hasattr(pending, "marker_path"):
                return _real(pending, *args, **kwargs)
            return pending if _name == "claim_request" else True

        monkeypatch.setattr(watch, name, in_place)


def _recording(record: list) -> object:
    """A stand-in for Update or Insert that honours the caller's precondition.

    The dispatcher hands WG's baseline check to the core as ``precondition``,
    run immediately before the first write; a refusal there means nothing was
    recorded, exactly as nothing would have been written.
    """

    def mutate(_app, path, options):
        options = dict(options)
        precondition = options.pop("precondition", None)
        if precondition is not None:
            precondition()
        record.append((path, options))

    return mutate


def _handoff_path(bundle_root: Path, request_id: str = "req-1") -> Path:
    folder = bundle_root / ".fusion-handoffs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{request_id}.json"


def _write_version_3(marker: Path, payload: dict[str, object], sequence: int = 1) -> None:
    """Write one request file as WG does (delivery version 3)."""

    marker.write_text(json.dumps({
        **payload,
        "schemaVersion": 3,
        "requestId": marker.stem,
        "operationId": marker.stem,
        "deliverySequence": sequence,
    }))


def _guarded_document(monkeypatch, name: str):
    """A real linked document, one published heartbeat, and a deferring cache.

    The cache is told its last measurement cost a second, so the duty cycle
    postpones the next one for twelve; four seconds later the heartbeat still
    publishes the token measured before the edit. That token is exactly what WG
    holds and hands back as ``expected_return_state_hash``, which is what let a
    guarded operation compare a stale value with itself and pass.
    """

    panels = _Panels()
    ui = _UI(panels, _Definitions(reserve_ids=False))
    app = _Application(ui)
    module = _load_instance(monkeypatch, name, ui, app)
    design, body = _linked_document(module)
    app.activeProduct = design
    body.revisionId = "revision-1"
    clock = types.SimpleNamespace(value=100.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.value)
    active = {"document_id": "fusion:doc-a"}
    monkeypatch.setattr(module, "_active_document_id", lambda: active["document_id"])
    _prime_measured_state(module)
    published = module._document_links()
    assert published[0]["document_signature_hash"]
    module._geometry_state_cache["cost_ms"] = 1000.0
    clock.value += 4.0
    return types.SimpleNamespace(
        module=module,
        ui=ui,
        app=app,
        design=design,
        body=body,
        clock=clock,
        active=active,
        published=published,
    )


def _heartbeat_snapshot(fixture) -> dict[str, object]:
    """The snapshot a tick builds: live identity, heartbeat-cached measurement."""

    timings: dict[str, float] = {}
    links = fixture.module._document_links(timings)
    return {
        "document_id": fixture.module._active_document_id(),
        "links": links,
        "timings": timings,
    }


def _pending_return(fixture, monkeypatch, tmp_path, **overrides):
    module = fixture.module
    published = fixture.published[0]
    fields = {
        "request_id": "request-a",
        "design_id": published["design_id"],
        "document_id": "fusion:doc-a",
        "instance_id": published["instance_id"],
        "expected_return_state_hash": published["document_signature_hash"],
        "operation_id": "request-a",
        "delivery_sequence": 1,
    }
    fields.update(overrides)
    request = types.SimpleNamespace(**fields)
    _claim_in_place(monkeypatch, module)
    monkeypatch.setattr(module, "_pending_return_request", lambda: request)
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: tmp_path)
    monkeypatch.setattr(module.wglink_workspace, "capture_document", lambda: False)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.wglink_send, "send", lambda _app, options: sent.append(options)
    )
    monkeypatch.setattr(
        module.wglink_watch, "acknowledge_return_request", lambda _request: True
    )
    return sent


def _pending_update(fixture, monkeypatch, tmp_path, **overrides):
    module = fixture.module
    published = fixture.published[0]
    fields = {
        "export_id": "wge_2",
        "bundle_path": str(tmp_path / "horn.wglink"),
        "design_id": published["design_id"],
        "expected_document_id": "fusion:doc-a",
        "expected_instance_id": published["instance_id"],
        "expected_return_state_hash": published["document_signature_hash"],
        "request_id": "req-u",
        "operation_id": "req-u",
        "delivery_sequence": 1,
    }
    fields.update(overrides)
    handoff = types.SimpleNamespace(**fields)
    _claim_in_place(monkeypatch, module)
    monkeypatch.setattr(module, "_pending_handoff", lambda: handoff)
    recorded: list[tuple[str, dict[str, object]]] = []
    mutations: list[dict[str, object]] = []

    def update(_app, path, options):
        _recording(recorded)(_app, path, options)
        if recorded:
            mutations.append(recorded.pop()[1])

    monkeypatch.setattr(module.wglink_core, "update", update)
    monkeypatch.setattr(
        module.wglink_core,
        "insert",
        lambda *_args, **_kwargs: mutations.append({"insert": True}),
    )
    monkeypatch.setattr(
        module.wglink_watch,
        "acknowledge_handoff",
        lambda _handoff, **_kwargs: True,
    )
    return mutations


@pytest.mark.parametrize("channel", ["return", "update"])
def test_a_cached_token_does_not_authorize_an_edited_managed_body(
    monkeypatch, tmp_path: Path, channel: str
) -> None:
    """The guard is optimistic concurrency, so it has to observe the present.

    WG holds the token the heartbeat published; the user then edits the managed
    body; the next tick is still inside the duty cycle and republishes that same
    token. Comparing WG's expectation against the snapshot therefore compared a
    stale value with itself and passed, and ``wglink_core.update`` went on to
    rebuild sketches and parameters over the edit.
    """

    fixture = _guarded_document(monkeypatch, f"WGLink_guard_revision_{channel}")
    module = fixture.module
    fixture.body.revisionId = "revision-2"
    snapshot = _heartbeat_snapshot(fixture)
    # The stale republication is the precondition, not the thing under test: a
    # cache-only heartbeat publishes the observation it has, labelled stale,
    # because refusing to publish anything would leave WG unable to ask for
    # the model at all.
    assert snapshot["timings"]["geometry_state"] == "stale"
    assert (
        snapshot["links"][0]["document_signature_hash"]
        == fixture.published[0]["document_signature_hash"]
    )

    if channel == "return":
        effects = _pending_return(fixture, monkeypatch, tmp_path)
        assert module._apply_pending_return_request(snapshot) == module.HANDLED
        expected_title = "WGLink return to WG refused"
        fragment = "changed after WG displayed its status"
    else:
        effects = _pending_update(fixture, monkeypatch, tmp_path)
        assert module._apply_pending_handoff(snapshot) == module.HANDLED
        expected_title = "WGLink automatic update refused"
        fragment = "changed after WG prepared this update"

    assert effects == []
    assert [title for title, _text in fixture.ui.messages] == [expected_title]
    assert fragment in fixture.ui.messages[0][1]


@pytest.mark.parametrize("channel", ["return", "update"])
def test_a_cached_token_does_not_authorize_changed_untracked_geometry(
    monkeypatch, tmp_path: Path, channel: str
) -> None:
    """The cheap change key cannot see the whole document, and never could.

    It reads the managed bodies' revisions and the timeline count. A body this
    add-in does not manage, added or edited beside them, moves the return state
    the guard is about while leaving that key exactly where it was -- so the
    heartbeat reports ``cached``, not merely ``deferred``, for a document that
    has genuinely moved.
    """

    fixture = _guarded_document(monkeypatch, f"WGLink_guard_untracked_{channel}")
    module = fixture.module
    neighbour = _managed_body("Bracket")
    neighbour.parentComponent = fixture.design.rootComponent
    fixture.design.rootComponent.bRepBodies.append(neighbour)
    snapshot = _heartbeat_snapshot(fixture)
    assert snapshot["timings"]["geometry_state"] == "cached"
    assert (
        snapshot["links"][0]["document_signature_hash"]
        == fixture.published[0]["document_signature_hash"]
    )

    if channel == "return":
        effects = _pending_return(fixture, monkeypatch, tmp_path)
        assert module._apply_pending_return_request(snapshot) == module.HANDLED
    else:
        effects = _pending_update(fixture, monkeypatch, tmp_path)
        assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert effects == []
    assert len(fixture.ui.messages) == 1


def test_the_heartbeat_never_publishes_another_documents_measured_state(
    monkeypatch,
) -> None:
    """Measured state belongs to the document it was measured in.

    The change key carries the document id, so the *unchanged* branch already
    refused a switch. The duty-cycle branch was time-only, so a switch inside
    the wait handed back the previous document's fingerprint, which
    ``_document_links`` then published beside the new document's identity.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_guard_document_switch")
    module = fixture.module
    other_design, _other_body = _linked_document(
        module, instance_id="wgi-other", export_id="wge_9"
    )
    fixture.app.activeProduct = other_design
    fixture.active["document_id"] = "fusion:doc-b"

    snapshot = _heartbeat_snapshot(fixture)

    assert snapshot["timings"]["geometry_state"] == "unavailable"
    assert snapshot["links"][0]["instance_id"] == "wgi-other"
    assert snapshot["links"][0]["document_signature_hash"] == ""
    assert snapshot["links"][0]["local_body_state"] == "unknown"
    assert snapshot["links"][0]["body_fingerprint_hash"] == ""


def test_an_idle_linked_document_keeps_its_baseline_indefinitely(
    monkeypatch,
) -> None:
    """There is no age at which an idle document loses the state WG needs.

    Withdrawing the cached observation after a minute made sense while the wire
    could not say how old it was. It cannot survive a cache-only heartbeat:
    nothing renews a cache the tick may not measure into, so the ceiling only
    decided how long it took for the published ``documentSignatureHash`` to go
    empty and stay empty. WG refuses to publish a return request or an
    exact-target handoff without one, and nothing in its UI can cause a
    measurement, so a user who left Fusion alone for a minute could no longer
    ask for their own model.

    What the tokens buy is the honest version: the observation is still there,
    still labelled with the revision it was taken at, and still free.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_guard_idle_baseline")
    module = fixture.module
    calls = _count_measurements(monkeypatch, module)
    # Twenty seconds a measurement: expensive enough to reach every cap there
    # is, on a document nobody is touching.
    module._geometry_state_cache["cost_ms"] = 20_000.0
    measured = fixture.published[0]

    for _tick in range(50):
        fixture.clock.value += module.WATCH_INTERVAL_SECONDS
        snapshot = _heartbeat_snapshot(fixture)
        link = snapshot["links"][0]
        assert snapshot["timings"]["geometry_state"] == "cached"
        assert link["document_signature_hash"] == measured["document_signature_hash"]
        assert link["measured_revision_token"] == measured["geometry_revision_token"]

    assert (
        snapshot["timings"]["geometry_state_age_s"]
        > module.GEOMETRY_STATE_MAX_AGE_SECONDS
    )
    assert calls == []


def test_an_edited_document_keeps_its_older_baseline_past_the_old_ceiling(
    monkeypatch,
) -> None:
    """The same promise for a document that moved: labelled, not withdrawn.

    ``stale`` is the honest answer, at four seconds and at four minutes. The
    tokens differ, so nothing can read the published observation as current,
    and WG still has a baseline it can name -- which the guard measures against
    for real and refuses if the model has moved.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_guard_stale_forever")
    module = fixture.module
    calls = _count_measurements(monkeypatch, module)
    measured = fixture.published[0]
    fixture.body.revisionId = "revision-2"
    fixture.clock.value += module.GEOMETRY_STATE_MAX_AGE_SECONDS * 4

    snapshot = _heartbeat_snapshot(fixture)
    link = snapshot["links"][0]

    assert snapshot["timings"]["geometry_state"] == "stale"
    assert link["document_signature_hash"] == measured["document_signature_hash"]
    assert link["measured_revision_token"] == measured["geometry_revision_token"]
    assert link["geometry_revision_token"] != link["measured_revision_token"]
    assert calls == []


def test_a_startup_refresh_never_walks_an_unlinked_document(
    monkeypatch, tmp_path: Path
) -> None:
    """Loading the add-in must not inspect a model WGLink has never touched.

    Startup asks for one refresh because a restart has no cached state. On a
    document with no WGLink records there is nothing for that measurement to
    report -- ``_document_links`` returns ``[]`` either way -- and
    ``return_state`` still walks the root export scope to produce it. The
    request is spent without measuring instead.
    """

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_startup_unlinked", ui, app)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_k: tmp_path)
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    app.activeProduct = types.SimpleNamespace(
        objectType="adsk::fusion::Design",
        findAttributes=lambda _group, _name: _Collection(),
    )
    app.activeDocument = types.SimpleNamespace(name="Somebody else's model")
    calls = _count_measurements(monkeypatch, module)

    module.run(None)
    try:
        handler = module._watch_handler
        assert handler is not None
        for _tick in range(5):
            handler.notify(None)

        assert calls == []
        assert module._document_links() == []
        assert module._geometry_refresh_pending is None
    finally:
        module.stop(None)


def test_a_refresh_is_dropped_when_the_document_it_named_is_no_longer_active(
    monkeypatch,
) -> None:
    """A request is about one document, and only that one can answer it.

    A slot carrying only a reason is serviced against whatever document the
    tick happens to land on, so asking about a linked document and switching
    away bought a walk of the document the user moved to.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_refresh_wrong_document")
    module = fixture.module
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    calls = _count_measurements(monkeypatch, module)
    fixture.body.revisionId = "revision-2"
    _ask_for_refresh(module, "command")

    other_design, _other_body = _linked_document(
        module, instance_id="wgi-other", export_id="wge_9"
    )
    fixture.app.activeProduct = other_design
    fixture.active["document_id"] = "fusion:doc-b"
    module._service_geometry_refresh()

    assert calls == []
    assert module._geometry_refresh_pending is None


def test_the_refresh_throttle_never_outlives_the_observation_it_protects(
    monkeypatch,
) -> None:
    """A wait longer than the state it defends is a wait that defends nothing.

    While the cap was two minutes and the observation's own ceiling one, an
    expensive document postponed the measurement past the point where the state
    it was waiting to replace had already been withdrawn.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_throttle_cap")
    module = fixture.module
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    calls = _count_measurements(monkeypatch, module)
    module._geometry_state_cache["cost_ms"] = 20_000.0
    fixture.body.revisionId = "revision-2"
    _ask_for_refresh(module, "command")

    assert module.GEOMETRY_STATE_MAX_WAIT_SECONDS <= (
        module.GEOMETRY_STATE_MAX_AGE_SECONDS
    )
    for _tick in range(20):
        fixture.clock.value += module.WATCH_INTERVAL_SECONDS
        module._on_watch_tick()

    assert len(calls) == 1
    assert module._geometry_refresh_pending is None


def test_a_switch_is_not_throttled_by_the_previous_documents_cost(
    monkeypatch,
) -> None:
    """One document's expense is not a reason to withhold another's state.

    The throttle reads the cache's cost and age. Applied to a cache entry
    belonging to the document just switched away from, a dense model would
    silently delay the new document's first measurement by up to the full cap.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_throttle_other_document")
    module = fixture.module
    calls = _count_measurements(monkeypatch, module)
    module._geometry_state_cache["cost_ms"] = 20_000.0
    other_design, _other_body = _linked_document(
        module, instance_id="wgi-other", export_id="wge_9"
    )
    fixture.app.activeProduct = other_design
    fixture.active["document_id"] = "fusion:doc-b"

    _ask_for_refresh(module, "document-changed")
    module._service_geometry_refresh()

    assert len(calls) == 1
    assert module._geometry_refresh_pending is None


@pytest.mark.parametrize("channel", ["return", "update"])
def test_a_guard_that_cannot_measure_refuses_the_operation(
    monkeypatch, tmp_path: Path, channel: str
) -> None:
    """"Cannot confirm" is a refusal, not a pass.

    The guard has two halves and only one was covered: a measurement that
    disagrees with WG's baseline refuses, and a measurement that produces no
    token at all must refuse too. A document WGLink cannot read is exactly the
    case where a permissive guard would let a rebuild run over an unknown
    model, and a cache-only heartbeat makes an empty token an ordinary state
    rather than an exotic one.
    """

    fixture = _guarded_document(monkeypatch, f"WGLink_guard_unmeasurable_{channel}")
    module = fixture.module
    snapshot = _heartbeat_snapshot(fixture)
    monkeypatch.setattr(
        module,
        "_fresh_geometry_state",
        lambda: {
            "document_signature_hash": "",
            "document_body_count": "",
            "source_state_hash": "",
            "instance_identities": {},
            "bodies": {},
        },
    )

    if channel == "return":
        effects = _pending_return(fixture, monkeypatch, tmp_path)
        assert module._apply_pending_return_request(snapshot) == module.HANDLED
        expected_title = "WGLink return to WG refused"
    else:
        effects = _pending_update(fixture, monkeypatch, tmp_path)
        assert module._apply_pending_handoff(snapshot) == module.HANDLED
        expected_title = "WGLink automatic update refused"

    assert effects == []
    assert [title for title, _text in fixture.ui.messages] == [expected_title]
    assert "could not measure this Fusion document" in fixture.ui.messages[0][1]


def test_a_forced_measurement_is_published_as_current(monkeypatch) -> None:
    """A fresh measurement is current by definition, and says so.

    Both tokens name the revision it was taken at, so a consumer handed a
    forced measurement never has to guess whether the labels apply to it.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_forced_tokens")
    app = module._app()
    records = module.wglink_core._resolved_link_records(app.activeProduct)

    state, verdict = module._geometry_state(
        app, app.activeProduct, records, "fusion:doc-a", force=True
    )

    assert verdict == "measured"
    assert state["geometry_revision_token"]
    assert state["measured_revision_token"] == state["geometry_revision_token"]


def _proxying_module(monkeypatch, name: str):
    """A registration whose application behaves the way Fusion's does.

    ``activeDocument`` is a property that mints a new wrapper on every read,
    and the design's ``rootComponent`` does the same. Any identity derived from
    Python object identity is unstable under this and stable under the
    plain-attribute fake, which is precisely why the plain fake could not see
    the defect.
    """

    panels = _Panels()
    ui = _UI(panels, _Definitions(reserve_ids=False))
    app = _ProxyingApplication(ui)
    module = _load_instance(monkeypatch, name, ui, app)
    design, body = _linked_document(module)
    app.activeProduct = _proxying_root(design)
    app.activeDocument = types.SimpleNamespace(name="Untitled")
    body.revisionId = "revision-1"
    return module, app, design, body


def test_an_unsaved_documents_identity_is_stable_under_fusions_proxies(
    monkeypatch,
) -> None:
    """The identity must come from Fusion, not from a Python object.

    Fusion's bindings construct a fresh proxy for every property read, so
    ``id(document)`` and ``document is held`` describe a temporary wrapper. An
    identity built on either changes on every read: the document id in the
    snapshot and the one the links were cached under disagree inside a single
    tick, every tick is a cache miss, and a refresh requested for the active
    document is dropped for naming the wrong one -- a linked unsaved document
    that can never report a state.
    """

    module, app, _design, _body = _proxying_module(
        monkeypatch, "WGLink_proxy_identity"
    )

    reads = {module._active_document_id() for _ in range(5)}

    assert len(reads) == 1
    only = reads.pop()
    assert only and only.startswith("local:")

    # A second document: a different root component, so a different handle.
    other_design, _other_body = _linked_document(
        module, instance_id="wgi-other", export_id="wge_9"
    )
    app.activeProduct = _proxying_root(other_design)
    app.activeDocument = types.SimpleNamespace(name="Untitled")

    assert module._active_document_id() != only


def test_a_linked_unsaved_document_reports_its_state_under_fusions_proxies(
    monkeypatch,
) -> None:
    """The end-to-end consequence, on the application shape Fusion presents.

    One measurement, then a published baseline: an unstable identity turned
    this into zero measurements and an empty hash for ever.
    """

    module, _app, _design, _body = _proxying_module(
        monkeypatch, "WGLink_proxy_heartbeat"
    )
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    calls = _count_measurements(monkeypatch, module)

    for _tick in range(5):
        module._on_watch_tick()

    timings: dict[str, float] = {}
    links = module._document_links(timings)

    assert len(calls) == 1
    assert timings["geometry_state"] == "cached"
    assert links[0]["document_signature_hash"]


def test_an_uncomputable_revision_key_is_never_reported_as_current(
    monkeypatch,
) -> None:
    """Two blanks matching is not evidence that nothing moved.

    An empty revision token means the key could not be computed. Comparing it
    with the equally empty token of the cached measurement reported ``cached``,
    so a full ``documentSignatureHash`` was published as current on the
    strength of two absences agreeing -- and with no age ceiling left to cap
    it, for as long as the key stayed uncomputable.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_blank_revision")
    monkeypatch.setattr(module, "_geometry_revision_token", lambda *_a, **_k: "")
    _prime_measured_state(module)
    calls = _count_measurements(monkeypatch, module)

    timings: dict[str, float] = {}
    links = module._document_links(timings)

    assert calls == []
    assert timings["geometry_state"] != "cached"
    assert links[0]["geometry_revision_token"] == ""


def test_a_linked_document_opened_after_the_add_in_loaded_reports_its_state(
    monkeypatch, tmp_path: Path
) -> None:
    """WGLink loads before the user opens a model. That is the ordinary order.

    Nothing was active at load, so nothing could be asked about then; the
    linked design the user opens next is the first document this registration
    has ever seen, and a first sighting used to be exempt from asking. Between
    them, no cause ever fired, and the document reported an empty hash for
    ever -- which WG cannot publish a return request or an exact-target handoff
    from.
    """

    panels = _Panels()
    ui = _UI(panels, _Definitions(reserve_ids=False))
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_open_after_load", ui, app)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_k: tmp_path)
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    calls = _count_measurements(monkeypatch, module)

    module.run(None)
    try:
        handler = module._watch_handler
        assert handler is not None
        handler.notify(None)
        assert calls == []  # nothing is open; there is nothing to look at

        design, body = _linked_document(module)
        body.revisionId = "revision-1"
        app.activeProduct = design
        app.activeDocument = types.SimpleNamespace(name="Horn")
        for _tick in range(5):
            handler.notify(None)

        assert len(calls) == 1
        timings: dict[str, float] = {}
        links = module._document_links(timings)
        assert timings["geometry_state"] == "cached"
        assert links[0]["document_signature_hash"]
    finally:
        module.stop(None)


def test_a_linked_document_opened_after_a_drawing_reports_its_state(
    monkeypatch, tmp_path: Path
) -> None:
    """The same hole reached through a product that is not a Design.

    A Drawing active at load never reached the part of the tick that records
    what is active, so the linked model opened next was still a first sighting.
    """

    panels = _Panels()
    ui = _UI(panels, _Definitions(reserve_ids=False))
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_open_after_drawing", ui, app)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_k: tmp_path)
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::drawing::Drawing")
    app.activeDocument = types.SimpleNamespace(name="Sheet 1")
    calls = _count_measurements(monkeypatch, module)

    module.run(None)
    try:
        handler = module._watch_handler
        assert handler is not None
        for _tick in range(3):
            handler.notify(None)
        assert calls == []

        design, body = _linked_document(module)
        body.revisionId = "revision-1"
        app.activeProduct = design
        app.activeDocument = types.SimpleNamespace(name="Horn")
        for _tick in range(5):
            handler.notify(None)

        assert len(calls) == 1
        assert module._document_links()[0]["document_signature_hash"]
    finally:
        module.stop(None)


def test_a_request_lost_before_it_is_answered_is_asked_again(
    monkeypatch, tmp_path: Path
) -> None:
    """Asking once is not the same as being answered.

    A request is spent without answering anything whenever the document stops
    being nameable between the tick that asked and the tick that would have
    paid. A rule that asks only when the document *changes* never learns that
    the answer never came, so the document publishes an empty hash for the rest
    of the session -- the same dead end as an expired observation, reached from
    a different direction.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_lost_request")
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    calls = _count_measurements(monkeypatch, module)
    real_id = module._active_document_id

    # One tick sees the document and asks about it.
    module._document_links()
    assert module._geometry_refresh_pending is not None

    # The next tick cannot name the active document, so the request it was
    # holding is about nothing it can measure, and is spent.
    monkeypatch.setattr(module, "_active_document_id", lambda: None)
    module._service_geometry_refresh()
    assert module._geometry_refresh_pending is None
    assert calls == []

    # The document is nameable again from here, and still has no observation.
    # Asking only when the document *changes* never asks a second time.
    monkeypatch.setattr(module, "_active_document_id", real_id)
    for _tick in range(4):
        module._on_watch_tick()

    assert len(calls) == 1
    assert module._document_links()[0]["document_signature_hash"]


def test_a_refresh_for_a_document_whose_links_went_is_spent_without_walking_it(
    monkeypatch,
) -> None:
    """Links can go between the tick that asks and the tick that would pay.

    ``_measure_geometry_state`` walks the root export scope whatever the
    document holds, so a request that arrives at a document with no WGLink
    records must not be answered -- that is the cost the unlinked fast path
    exists to avoid, reached through the refresh instead of the heartbeat. And
    it must be *spent*, not left pending, or every later tick tries the same
    walk again.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_links_vanished")
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    _ask_for_refresh(module, "document-seen")
    assert module._geometry_refresh_pending is not None
    calls = _count_measurements(monkeypatch, module)
    # Detached while the request was in flight.
    monkeypatch.setattr(
        module.wglink_core, "_resolved_link_records", lambda _design: {}
    )

    for _tick in range(5):
        module._on_watch_tick()

    assert calls == []
    assert module._geometry_refresh_pending is None


def _unmeasurable(monkeypatch, module) -> list[int]:
    """Make every measurement fail the way a real one fails: silently.

    ``_measure_geometry_state`` swallows the exception and returns empty
    hashes, so a document that is still loading or regenerating is
    indistinguishable from one that can never be measured.
    """

    calls: list[int] = []

    def refuses(_app, _options=None):
        calls.append(1)
        raise RuntimeError("this document cannot be measured")

    monkeypatch.setattr(module.wglink_send, "return_state", refuses)
    return calls


def test_a_document_that_cannot_be_measured_is_not_retried_every_tick(
    monkeypatch,
) -> None:
    """Re-asking is a rate, not a loop.

    Asking again whenever a linked document has no observation is what stops
    one lost request from silencing the document. Left ungoverned it would be a
    measurement attempt every four seconds on a document that cannot be
    measured at all -- the load this whole item removes, rebuilt out of
    recovery logic.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_retry_rate")
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    calls = _unmeasurable(monkeypatch, module)

    for _tick in range(20):
        module._on_watch_tick()

    assert len(calls) == 1
    assert module._document_links()[0]["document_signature_hash"] == ""


def test_a_document_that_becomes_measurable_recovers_by_itself(
    monkeypatch,
) -> None:
    """Giving up is D1's dead end behind a counter.

    A measurement fails for ordinary, temporary reasons -- the document is
    still loading, or regenerating -- and a failure is indistinguishable from
    permanent, because ``_measure_geometry_state`` swallows it and returns
    empty hashes. A fixed allowance is spent in a few fast ticks, and the
    document then publishes an empty ``documentSignatureHash`` for the rest of
    the session however measurable it becomes: WG refuses every return request
    and every exact-target handoff, and nothing in WG can cause a measurement.
    So the retry never stops; it only slows down.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_retry_recovery")
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    clock = types.SimpleNamespace(value=1000.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.value)
    real_state = module.wglink_send.return_state
    failing = _unmeasurable(monkeypatch, module)

    for _tick in range(20):
        clock.value += module.WATCH_INTERVAL_SECONDS
        module._on_watch_tick()
    assert len(failing) == 2  # once, then once a minute

    # Whatever it was, it passed.
    monkeypatch.setattr(module.wglink_send, "return_state", real_state)
    calls = _count_measurements(monkeypatch, module)
    clock.value += module.GEOMETRY_REFRESH_RETRY_SECONDS
    for _tick in range(3):
        clock.value += module.WATCH_INTERVAL_SECONDS
        module._on_watch_tick()

    assert len(calls) == 1
    timings: dict[str, float] = {}
    links = module._document_links(timings)
    assert timings["geometry_state"] == "cached"
    assert links[0]["document_signature_hash"]


def test_a_failed_measurement_is_never_published_as_a_measurement(
    monkeypatch,
) -> None:
    """A measurement that produced no document baseline is not a baseline.

    Publishing it as ``cached``, with a revision token beside an empty
    signature hash, said "this measurement is current" while carrying no
    measurement for anything to be current about -- and WG cannot publish a
    return request or an exact-target handoff from it either way.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_failed_measurement")
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    _unmeasurable(monkeypatch, module)

    _ask_for_refresh(module, "document-seen")
    module._service_geometry_refresh()

    timings: dict[str, float] = {}
    link = module._document_links(timings)[0]
    assert timings["geometry_state"] != "cached"
    assert link["document_signature_hash"] == ""
    assert link["measured_revision_token"] == ""


def test_a_failed_measurement_does_not_evict_the_last_real_observation(
    monkeypatch,
) -> None:
    """The observation WG is working from outlives a measurement that failed."""

    module, _design, body = _tick_module(monkeypatch, "WGLink_failed_keeps_cache")
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    _prime_measured_state(module)
    measured = module._document_links()[0]
    assert measured["document_signature_hash"]

    _unmeasurable(monkeypatch, module)
    body.revisionId = "revision-2"
    _ask_for_refresh(module, "document-seen")
    module._service_geometry_refresh()

    timings: dict[str, float] = {}
    link = module._document_links(timings)[0]
    assert timings["geometry_state"] == "stale"
    assert link["document_signature_hash"] == measured["document_signature_hash"]


def test_two_documents_this_add_in_cannot_name_never_share_one_observation(
    monkeypatch,
) -> None:
    """``None`` is "cannot name this document", and it is not a name.

    A saved document has a data file id and an unsaved one has its root
    component's entity token. A document with neither cannot be told apart from
    any other document with neither, so giving them all one identity -- any
    constant, however obviously a placeholder -- files every one of them under
    a single cache entry, and the next one reads back the last one's
    fingerprint. That is exactly the substitution A1 forbids, and it is why
    nothing is cached under ``None`` and nothing is read back from it.
    """

    module, design, _body = _tick_module(monkeypatch, "WGLink_unnameable_pair")
    app = module._app()
    design.rootComponent.entityToken = ""

    assert module._active_document_id() is None
    assert module._fresh_geometry_state() is not None
    assert module._geometry_state_cache is None  # nowhere to file it

    other_design, _other_body = _linked_document(
        module, instance_id="wgi-other", export_id="wge_9"
    )
    other_design.rootComponent.entityToken = ""
    app.activeProduct = other_design

    timings: dict[str, float] = {}
    link = module._document_links(timings)[0]

    assert link["instance_id"] == "wgi-other"
    assert timings["geometry_state"] == "unavailable"
    assert link["document_signature_hash"] == ""
    assert link["measured_revision_token"] == ""


def test_an_observation_filed_under_no_document_is_never_read_back(
    monkeypatch,
) -> None:
    """The read guard is what makes "nothing is cached under None" a property.

    Nothing writes such an entry today. The guard is what stops that being a
    fact about the current code rather than about the design: one entry keyed
    on "unknown" is read back by every other document that cannot be named.
    """

    module, design, _body = _tick_module(monkeypatch, "WGLink_none_keyed_cache")
    design.rootComponent.entityToken = ""
    assert module._active_document_id() is None
    records = module.wglink_core._resolved_link_records(design)
    module._geometry_state_cache = {
        "key": (None, module._geometry_revision_token(design, records)),
        "at": module.time.monotonic(),
        "cost_ms": 1.0,
        "state": {
            "document_signature_hash": "sha256:another-documents-model",
            "document_body_count": "1",
            "source_state_hash": "",
            "instance_identities": {},
            "bodies": {},
        },
    }

    timings: dict[str, float] = {}
    link = module._document_links(timings)[0]

    assert timings["geometry_state"] == "unavailable"
    assert link["document_signature_hash"] == ""


def test_a_measurement_that_arrives_stops_the_add_in_asking(monkeypatch) -> None:
    """The budget is about documents with no answer, not about documents.

    Once an observation exists, no number of ticks asks for another -- which is
    the property the whole item is about, restated for the recovery path.
    """

    module, _design, body = _tick_module(monkeypatch, "WGLink_budget_cleared")
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    calls = _count_measurements(monkeypatch, module)

    for index in range(30):
        # Edited on every tick: stale, which is not the same as unobserved.
        body.revisionId = f"revision-{index}"
        module._on_watch_tick()

    assert len(calls) == 1
    assert module._geometry_refresh_attempts == {}
    assert module._document_links()[0]["document_signature_hash"]


def test_a_stale_stamped_request_never_blocks_the_active_documents_own(
    monkeypatch,
) -> None:
    """One slot, and the live question owns it.

    Coalescing keeps one pending request, not the first one ever made. A
    request stamped for a document nobody is asking about any more would
    otherwise sit in the slot, drop out on the next tick for mismatch, and
    leave the document actually on screen with nothing asked about it.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_stale_stamp")
    module = fixture.module
    module._geometry_refresh_pending = None
    module._request_geometry_refresh("startup", None)

    _ask_for_refresh(module, "document-seen")

    pending = module._geometry_refresh_pending
    assert pending is not None
    assert pending["document_id"] == module._active_document_id()
    # Still one slot, not a queue.
    _ask_for_refresh(module, "command")
    assert module._geometry_refresh_pending is pending


@pytest.mark.parametrize("channel", ["return", "update"])
def test_a_document_switch_does_not_authorize_the_other_documents_token(
    monkeypatch, tmp_path: Path, channel: str
) -> None:
    """WG's expectation can only ever be honoured against the live document.

    This is the switch seen from the consumer side: WG was handed the previous
    document's signature labelled as this one's, and asks for an operation on
    this one. The identity checks all pass -- the request names the active
    document and a link that really is in it -- so the state guard is the only
    thing between a foreign token and a rebuild.
    """

    fixture = _guarded_document(monkeypatch, f"WGLink_guard_switch_{channel}")
    module = fixture.module
    other_design, _other_body = _linked_document(
        module, instance_id="wgi-other", export_id="wge_9"
    )
    fixture.app.activeProduct = other_design
    fixture.active["document_id"] = "fusion:doc-b"
    snapshot = _heartbeat_snapshot(fixture)

    if channel == "return":
        effects = _pending_return(
            fixture,
            monkeypatch,
            tmp_path,
            document_id="fusion:doc-b",
            instance_id="wgi-other",
        )
        assert module._apply_pending_return_request(snapshot) == module.HANDLED
    else:
        effects = _pending_update(
            fixture,
            monkeypatch,
            tmp_path,
            expected_document_id="fusion:doc-b",
            expected_instance_id="wgi-other",
        )
        assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert effects == []
    assert len(fixture.ui.messages) == 1


@pytest.mark.parametrize("channel", ["return", "update"])
def test_an_unchanged_exact_target_still_completes_its_guarded_operation(
    monkeypatch, tmp_path: Path, channel: str
) -> None:
    """The guard refuses a moved model, not a slow one.

    A document that has not moved measures to the token WG is holding, however
    old the cached copy of it is, so the operation the user asked for runs.
    """

    fixture = _guarded_document(monkeypatch, f"WGLink_guard_unchanged_{channel}")
    module = fixture.module
    snapshot = _heartbeat_snapshot(fixture)
    assert snapshot["timings"]["geometry_state"] == "cached"

    if channel == "return":
        effects = _pending_return(fixture, monkeypatch, tmp_path)
        assert module._apply_pending_return_request(snapshot) == module.HANDLED
        assert [options["anchor_instance_id"] for options in effects] == [
            fixture.published[0]["instance_id"]
        ]
    else:
        effects = _pending_update(fixture, monkeypatch, tmp_path)
        assert module._apply_pending_handoff(snapshot) == module.HANDLED
        assert effects == [
            {"instance_id": fixture.published[0]["instance_id"], "operation_id": "req-u"}
        ]

    assert fixture.ui.messages == []


def test_an_idle_tick_still_costs_the_document_no_measurement(monkeypatch) -> None:
    """The fresh guard measurement belongs to an explicit operation only.

    The duty cycle exists because ``return_state`` evaluates faces and volumes
    on Fusion's main thread. Nothing here may reintroduce that cost on the
    four-second timer: with no handoff and no return request pending, a tick
    measures nothing at all.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_guard_idle")
    module = fixture.module
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    calls = _count_measurements(monkeypatch, module)

    for _tick in range(3):
        module._on_watch_tick()
        fixture.clock.value += 4.0

    assert calls == []
    assert module._geometry_state_cache is not None


# --- the periodic heartbeat is cache-only ---------------------------------
#
# The four-second tick may read what a previous measurement left behind. It may
# never take one. Every measurement below is caused by something a user or WG
# did, and the tick is only the main thread it is allowed to run on.


def test_a_linked_heartbeat_never_measures_geometry_on_a_cache_miss(
    monkeypatch,
) -> None:
    """A first tick on a linked document is still a tick.

    The unlinked fast path saved documents WGLink had never touched. A linked
    one still paid a full root-scope evaluation on the first tick after load,
    after a document switch and after a restart -- on Fusion's main thread, in
    the middle of whatever the user was doing. There is nothing to publish that
    a cache miss can honestly answer, so it answers "cannot tell".
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_cache_miss")
    calls = _count_measurements(monkeypatch, module)

    timings: dict[str, float] = {}
    links = module._document_links(timings)

    assert calls == []
    assert timings["geometry_state"] == "unavailable"
    assert links[0]["document_signature_hash"] == ""
    assert links[0]["local_body_state"] == "unknown"
    assert links[0]["body_fingerprint_hash"] == ""
    # Identity is stored attributes and stays free, so it is still published.
    assert links[0]["instance_id"]
    assert links[0]["design_id"]


def test_a_stale_cache_is_published_as_stale_without_measuring(monkeypatch) -> None:
    """A moved document does not buy the heartbeat a new measurement.

    The cheap revision token says the measurement is no longer current, which
    is a property read. Acting on that by measuring is exactly the four-second
    load this item removes, so the heartbeat publishes the measurement it has
    and labels it. Nothing here claims a newly measured state.
    """

    module, _design, body = _tick_module(monkeypatch, "WGLink_stale_cache")
    # Rule the duty cycle out: with it in play the base heartbeat would defer
    # rather than measure, and this test would pass without saying anything.
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    _prime_measured_state(module)
    first = module._document_links()
    assert first[0]["document_signature_hash"]

    calls = _count_measurements(monkeypatch, module)
    body.revisionId = "revision-2"
    timings: dict[str, float] = {}
    second = module._document_links(timings)

    assert calls == []
    assert timings["geometry_state"] == "stale"
    assert (
        second[0]["document_signature_hash"] == first[0]["document_signature_hash"]
    )
    # The token the measurement was taken at, beside the token the document is
    # at now: unequal, so no consumer can mistake the one for the other.
    assert second[0]["measured_revision_token"] == first[0]["geometry_revision_token"]
    assert second[0]["geometry_revision_token"] != second[0]["measured_revision_token"]


def test_only_an_explicit_refresh_measures_and_it_measures_once(monkeypatch) -> None:
    """Ticks are the carrier, never the cause.

    With nothing asked for, any number of ticks on a document that keeps moving
    measure nothing at all. One explicit request measures exactly once, however
    many ticks follow it.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_explicit_refresh")
    module = fixture.module
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    fixture.body.revisionId = "revision-2"
    calls = _count_measurements(monkeypatch, module)

    for _tick in range(4):
        module._on_watch_tick()
        fixture.clock.value += module.WATCH_INTERVAL_SECONDS
    assert calls == []

    _ask_for_refresh(module, "explicit")
    for _tick in range(4):
        module._on_watch_tick()
        fixture.clock.value += module.WATCH_INTERVAL_SECONDS

    assert len(calls) == 1
    assert module._geometry_refresh_pending is None
    timings: dict[str, float] = {}
    module._document_links(timings)
    assert timings["geometry_state"] == "cached"


def test_a_geometry_refresh_is_coalesced_to_one_pending_request(monkeypatch) -> None:
    """One add-in instance, at most one pending refresh.

    Every explicit cause -- startup, a document switch, a finished command --
    may ask. A queue of them would reinstate the load this removes on the first
    busy minute, so the second request onwards is dropped, not stacked.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_refresh_coalescing")
    module = fixture.module
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    fixture.body.revisionId = "revision-2"
    calls = _count_measurements(monkeypatch, module)

    _ask_for_refresh(module, "startup")
    pending = module._geometry_refresh_pending
    for reason in ("document-switch", "command", "command", "command"):
        _ask_for_refresh(module, reason)
    assert module._geometry_refresh_pending is pending

    module._on_watch_tick()
    fixture.clock.value += module.WATCH_INTERVAL_SECONDS
    module._on_watch_tick()

    assert len(calls) == 1
    assert module._geometry_refresh_pending is None


def test_a_refresh_on_an_unmeasurable_document_is_not_retried_forever(
    monkeypatch,
) -> None:
    """A request that cannot be answered is spent, not left on the timer.

    The document has links, so the request is serviced; the measurement then
    comes back with nothing -- an inventory that stopped being readable between
    the two reads. If that left the request standing, every later tick would
    try again, which is a periodic inspection attempt by another name.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_unmeasurable_refresh")
    _prime_measured_state(module)
    # Rule the refresh throttle out: a request it is merely waiting on is
    # still outstanding, which is a different thing from one it has spent.
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    calls = _count_measurements(monkeypatch, module)
    monkeypatch.setattr(module, "_fresh_geometry_state", lambda: None)

    _ask_for_refresh(module, "document-changed")
    module._service_geometry_refresh()

    assert calls == []
    assert module._geometry_refresh_pending is None


def test_a_guarded_operation_settles_a_pending_refresh(
    monkeypatch, tmp_path: Path
) -> None:
    """One explicit measurement answers every request outstanding for it.

    A guarded return measures the live document for real. A refresh asked for
    a moment earlier wants exactly that measurement, so it is answered by it;
    leaving the request pending would buy a second walk of the same document
    on the next tick.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_guard_settles_refresh")
    module = fixture.module
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    snapshot = _heartbeat_snapshot(fixture)
    sent = _pending_return(fixture, monkeypatch, tmp_path)
    calls = _count_measurements(monkeypatch, module)
    _ask_for_refresh(module, "document-changed")

    assert module._apply_pending_return_request(snapshot) == module.HANDLED

    assert sent and fixture.ui.messages == []
    assert len(calls) == 1
    assert module._geometry_refresh_pending is None

    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    fixture.clock.value += module.WATCH_INTERVAL_SECONDS
    module._on_watch_tick()
    assert len(calls) == 1


def test_a_document_switch_schedules_one_refresh_and_publishes_no_foreign_state(
    monkeypatch,
) -> None:
    """Switching documents is a change notification, not a scan.

    The switch invalidates the cached measurement -- the previous document's
    fingerprint is never this document's evidence -- and schedules one bounded
    refresh. It does not walk the new document on the tick that noticed it, and
    a second tick on the same document does not ask again.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_switch_refresh")
    module = fixture.module
    calls = _count_measurements(monkeypatch, module)
    other_design, _other_body = _linked_document(
        module, instance_id="wgi-other", export_id="wge_9"
    )
    fixture.app.activeProduct = other_design
    fixture.active["document_id"] = "fusion:doc-b"

    snapshot = _heartbeat_snapshot(fixture)

    assert calls == []
    assert snapshot["timings"]["geometry_state"] == "unavailable"
    assert snapshot["links"][0]["instance_id"] == "wgi-other"
    assert snapshot["links"][0]["document_signature_hash"] == ""
    assert snapshot["links"][0]["measured_revision_token"] == ""
    pending = module._geometry_refresh_pending
    assert pending is not None

    _heartbeat_snapshot(fixture)
    assert module._geometry_refresh_pending is pending
    assert calls == []


def test_neither_a_linked_nor_an_unlinked_design_is_inspected_by_a_tick(
    monkeypatch,
) -> None:
    """The instrumented count for periodic activity alone is zero, both ways.

    The unlinked path returns before it resolves anything; the linked path
    reads stored attributes and the cache. Neither reaches ``return_state``.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_linked_and_unlinked")
    module = fixture.module
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "GEOMETRY_STATE_DUTY_CYCLE", 0.0)
    calls = _count_measurements(monkeypatch, module)
    linked = fixture.app.activeProduct
    unlinked = types.SimpleNamespace(
        objectType="adsk::fusion::Design",
        findAttributes=lambda _group, _name: _Collection(),
    )

    for index in range(6):
        fixture.app.activeProduct = linked if index % 2 == 0 else unlinked
        fixture.body.revisionId = f"revision-{index}"
        module._on_watch_tick()
        fixture.clock.value += module.WATCH_INTERVAL_SECONDS

    assert calls == []


def test_a_second_add_in_instance_measures_nothing_while_it_is_not_the_owner(
    monkeypatch,
) -> None:
    """Two Fusion processes, one data directory, one measuring instance.

    A standby registration keeps its own cache and its own pending refresh, and
    its watch handler refuses the tick because it does not hold the active IPC
    lease. Nothing it does reaches the document.
    """

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    owner = _load_instance(monkeypatch, "WGLink_multi_owner", ui, app)
    standby = _load_instance(monkeypatch, "WGLink_multi_standby", ui, app)
    design, body = _linked_document(owner)
    body.revisionId = "revision-1"
    app.activeProduct = design
    assert owner._claim_ipc_lease() and owner._activate_ipc_lease()
    try:
        assert standby._owns_active_ipc_lease() is False
        calls = _count_measurements(monkeypatch, standby)
        _ask_for_refresh(standby, "document-switch")

        handler = standby.WatchEventHandler()
        for _tick in range(3):
            handler.notify(None)

        assert calls == []
        # Its own slot, untouched: the owner never services another instance's
        # refresh, and the standby never services its own.
        assert standby._geometry_refresh_pending is not None
        assert owner._geometry_refresh_pending is None
    finally:
        owner._release_ipc_lease()


def test_a_restart_recovers_its_measured_state_from_one_startup_refresh(
    monkeypatch, tmp_path: Path
) -> None:
    """A restart is an explicit cause, and it converges in one measurement.

    Otherwise a reloaded add-in would publish "cannot tell" for a linked
    document until the user happened to run a command, and WG cannot ask for a
    model whose state was never reported.
    """

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_restart_refresh", ui, app)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_k: tmp_path)
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    design, body = _linked_document(module)
    body.revisionId = "revision-1"
    app.activeProduct = design
    app.activeDocument = types.SimpleNamespace(name="Horn")
    calls = _count_measurements(monkeypatch, module)

    module.run(None)
    try:
        assert module._owned is True
        # Startup publishes what it has, which after a restart is nothing.
        assert calls == []
        assert module._geometry_refresh_pending is not None

        handler = module._watch_handler
        assert handler is not None
        for _tick in range(3):
            handler.notify(None)

        assert len(calls) == 1
        timings: dict[str, float] = {}
        links = module._document_links(timings)
        assert timings["geometry_state"] == "cached"
        assert links[0]["document_signature_hash"]
        assert (
            links[0]["measured_revision_token"] == links[0]["geometry_revision_token"]
        )
    finally:
        module.stop(None)


def test_a_finished_command_is_an_explicit_cause_of_one_refresh(monkeypatch) -> None:
    """A command the user ran may have moved the model, so it asks for a look.

    This is the ordinary way a linked document's published state becomes
    current again without a timer ever measuring anything.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_command_refresh")
    module = fixture.module
    module._geometry_refresh_pending = None
    monkeypatch.setattr(module, "_command_options", lambda _inputs: {})
    monkeypatch.setattr(module, "_summary", lambda _operation, _report: "done")
    monkeypatch.setattr(module.wglink_core, "update", lambda *_a, **_k: {"ok": True})

    handler = module.CommandExecuteHandler("update")
    handler.notify(
        types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=None))
    )

    pending = module._geometry_refresh_pending
    assert pending is not None
    assert pending["document_id"] == module._active_document_id()
    assert module._command_busy is False


def test_a_cancelled_command_asks_for_no_measurement(monkeypatch) -> None:
    """A command the user backed out of changed nothing to look at.

    Detach asks for confirmation and Insert asks for a bundle; answering "no"
    to either returns before anything is written. Asking for a measurement
    there buys a root-scope walk for a decision not to act.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_command_cancelled")
    module = fixture.module
    module._geometry_refresh_pending = None
    detached: list[int] = []
    monkeypatch.setattr(module, "_command_options", lambda _inputs: {})
    monkeypatch.setattr(module, "_confirm_detach", lambda: False)
    monkeypatch.setattr(
        module.wglink_core, "detach", lambda *_a, **_k: detached.append(1)
    )

    handler = module.CommandExecuteHandler("detach")
    handler.notify(
        types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=None))
    )

    assert detached == []
    assert module._geometry_refresh_pending is None
    assert module._command_busy is False


def test_a_blocked_delivery_does_not_starve_heartbeat_scheduling(
    monkeypatch, tmp_path: Path
) -> None:
    """A long poll belongs to the worker; the tick carrier keeps its cadence.

    The scheduling thread renews the lease and raises the owner's event without
    calling the Fusion API or the live client, so a delivery stuck in a long
    poll cannot delay a tick -- and nothing it does measures geometry.
    """

    module, _design, _body = _tick_module(monkeypatch, "WGLink_fair_scheduling")
    calls = _count_measurements(monkeypatch, module)
    app = module._app()
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_k: tmp_path)
    monkeypatch.setattr(module, "WATCH_INTERVAL_SECONDS", 0.01)
    assert module._claim_ipc_lease() and module._activate_ipc_lease()
    entered, release = threading.Event(), threading.Event()

    class _StuckDelivery(_IdleDispatch):
        """A live client whose worker is inside a long poll."""

        def offer_heartbeat(self, _payload: object) -> None:
            entered.set()
            release.wait(5.0)

    module._live_client = _StuckDelivery()
    stop = threading.Event()
    publisher = threading.Thread(target=module._publish_fusion_status, daemon=True)
    carrier = threading.Thread(
        target=module._watch_loop, args=(app, stop), daemon=True
    )
    try:
        publisher.start()
        assert entered.wait(5.0)
        carrier.start()
        deadline = time.monotonic() + 5.0
        while len(app.fired) < 5 and time.monotonic() < deadline:
            time.sleep(0.005)
        # Still blocked in delivery, and the carrier has ticked regardless.
        assert publisher.is_alive()
        assert len(app.fired) >= 5
        assert calls == []
    finally:
        stop.set()
        release.set()
        carrier.join(timeout=5)
        publisher.join(timeout=5)
        module._live_client = None
        module._release_ipc_lease()


def test_document_links_report_an_intact_body_as_audit_does(monkeypatch) -> None:
    """The heartbeat and Audit read one inventory, so they cannot disagree.

    Measured on an ordinary inserted model: the heartbeat obtained raw
    ``_link_records``, which resolve no geometry, so every intact managed body
    was published to WG as ``missing`` with no fingerprint beside it while
    Audit -- going through ``_resolve_link`` -- called the same link
    unmodified.
    """

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_body_state", ui, app)
    design, body = _linked_document(module)
    app.activeProduct = design
    app.activeDocument = types.SimpleNamespace(name="Horn")

    _prime_measured_state(module)
    link = module._document_links()[0]

    audited = module.wglink_core._resolve_link(design, {"instance_id": "wgi-heartbeat"})
    assert link["local_body_state"] == "unmodified"
    assert link["local_body_state"] == module.wglink_core._local_body_state(audited)
    assert link["body_fingerprint_hash"] == module._fingerprint_hash(
        module.wglink_core._body_fingerprint(body)
    )


def test_document_links_report_a_deleted_body_as_missing(monkeypatch) -> None:
    """The state stays honest: a link whose body is gone still reads missing."""

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_body_deleted", ui, app)
    design, body = _linked_document(module)
    # Fusion drops the deleted body's attributes with it, which is what leaves
    # the wrapper payload as the only surviving evidence of the link.
    body.attributes.values.clear()
    design.rootComponent.bRepBodies = _Collection()
    app.activeProduct = design
    app.activeDocument = types.SimpleNamespace(name="Horn")

    _prime_measured_state(module)
    link = module._document_links()[0]

    assert link["local_body_state"] == "missing"
    assert link["body_fingerprint_hash"] == ""


def test_document_links_attach_exact_live_return_identities(monkeypatch) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_live_identities", ui, app)
    design, body = _linked_document(module)
    app.activeProduct = design
    app.activeDocument = types.SimpleNamespace(name="Horn")

    _prime_measured_state(module)
    link = module._document_links()[0]

    state = module.wglink_send.return_state(
        app, {"selection": "root", "anchor_instance_id": "wgi-heartbeat"}
    )
    identity = state["instance_identities"]["wgi-heartbeat"]
    assert link["body_object_ids"] == [body.entityToken]
    assert link["transform_hash"] == identity["transform_hash"]
    assert link["source_ids"] == identity["source_ids"]
    assert link["drive_channel_ids"] == identity["drive_channel_ids"]
    assert link["document_signature_hash"] == state["hash"]
    assert link["source_state_hash"] == state["source_hash"]


def test_a_failed_announced_update_is_offered_again_on_the_next_tick(
    monkeypatch, tmp_path: Path
) -> None:
    """The retry the failure branch promises, on the disk it actually has.

    Nothing touches the bundle between the two surveys, because in production
    nothing does: a refused Update leaves the export exactly where it was.
    """

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    module = _load_instance(monkeypatch, "WGLink_failed_update_retry", ui, app)
    bundle = tmp_path / "horn.wglink"
    bundle.mkdir()
    (bundle / "wglink.json").write_text(
        json.dumps({"export": {"id": "wge_2", "sequence": 2}}), encoding="utf-8"
    )
    links = [{
        "instance_id": "instance-a",
        "bundle_path": str(bundle),
        "export_id": "wge_1",
    }]

    def _refuse(_app, _path, _options):
        raise module.wglink_core.WgLinkError("the managed body is in an edit")

    monkeypatch.setattr(module.wglink_core, "update", _refuse)
    announced = module._watcher.survey(links)
    assert [item.instance_id for item in announced] == ["instance-a"]

    module._apply_announced_updates(announced)

    assert any("Not updated" in text for _title, text in ui.messages)
    assert [item.instance_id for item in module._watcher.survey(links)] == [
        "instance-a"
    ]


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
        lambda *_a, **_k: snapshots.append(links) or links,
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
    marker = _handoff_path(bundle_root)
    _write_version_3(marker, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_2",
        "exportId": "wge_2",
        "sequence": 2,
    })
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root)
    monkeypatch.setattr(module.wglink_core, "_link_records", lambda _design: {})
    inserted: list[tuple[object, str, dict[str, object]]] = []
    def insert(active_app, path, options):
        options = dict(options)
        options.pop("precondition")()
        inserted.append((active_app, path, options))

    monkeypatch.setattr(
        module.wglink_core,
        "insert",
        lambda active_app, path, options: (
            insert(active_app, path, options)
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

    assert inserted == [
        (app, str(bundle), {"allow_root_fallback": True, "operation_id": "req-1"})
    ]
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
    _marker = _handoff_path(bundle_root)
    _write_version_3(_marker, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_2",
        "exportId": "wge_2",
        "sequence": 2,
    })
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


def test_a_handoff_without_an_exact_instance_never_updates_a_linked_design(
    monkeypatch, tmp_path: Path
) -> None:
    """Design-only resolution is gone (CAD-OPERATIONS.md, "Operation kinds").

    A handoff that names no instance is an insert. When the active document
    already links this design, updating "the one matching link" would change a
    model WG never measured, with no baseline: it is refused, and the user is
    told how to send an update WG can target.
    """

    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeDocument = types.SimpleNamespace(name="Tritonia V")
    module = _load_instance(monkeypatch, "WGLink_pending_update", ui, app)
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "horn.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    marker = _handoff_path(bundle_root)
    _write_version_3(marker, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_3",
        "exportId": "wge_3",
        "sequence": 3,
        "designId": "wgd-a",
    })
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root)
    design, _body = _linked_document(
        module,
        instance_id="instance-a",
        bundle_path=str(bundle),
        export_id="wge_2",
    )
    app.activeProduct = design
    mutations: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(mutations))
    monkeypatch.setattr(module.wglink_core, "insert", _recording(mutations))

    module._on_watch_tick()
    module._on_watch_tick()

    assert mutations == []
    assert not marker.exists()
    assert [title for title, _text in ui.messages] == ["WGLink automatic insert refused"]
    assert "already linked in the active Fusion document" in ui.messages[0][1]


def test_a_pending_update_targets_design_identity_after_bundle_move(
    monkeypatch, tmp_path: Path
) -> None:
    panels = _Panels()
    definitions = _Definitions(reserve_ids=False)
    ui = _UI(panels, definitions)
    app = _Application(ui)
    app.activeDocument = types.SimpleNamespace(name="Tritonia V")
    module = _load_instance(monkeypatch, "WGLink_pending_moved_bundle", ui, app)
    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "renamed.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    marker = _handoff_path(bundle_root)
    _write_version_3(marker, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_4",
        "exportId": "wge_4",
        "sequence": 4,
        "designId": "wgd-a",
        "expectedDocumentId": "fusion:doc-a",
        "expectedInstanceId": "instance-a",
        "expectedReturnStateHash": "sha256:state-a",
    })
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root)
    design, _body = _linked_document(
        module,
        instance_id="instance-a",
        bundle_path=str(tmp_path / "old" / "horn.wglink"),
        export_id="wge_3",
    )
    app.activeProduct = design
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    _unmoved_live_state(monkeypatch, module, "sha256:state-a")
    updated: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))

    module._on_watch_tick()

    assert updated == [
        (str(bundle), {"instance_id": "instance-a", "operation_id": "req-1"})
    ]
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
    marker = _handoff_path(bundle_root)
    _write_version_3(marker, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_5",
        "exportId": "wge_5",
        "sequence": 5,
        "designId": "wgd-shared",
        "expectedDocumentId": "fusion:doc-a",
        "expectedInstanceId": "instance-b",
        "expectedReturnStateHash": "sha256:state-b",
    })
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(
        module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root
    )
    _unmoved_live_state(monkeypatch, module, "sha256:state-b")
    updated: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
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

    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert updated == [
        (str(bundle), {"instance_id": "instance-b", "operation_id": "req-1"})
    ]
    assert not marker.exists()
    assert ui.messages == []


@pytest.mark.parametrize(
    ("expected_instance_id", "instance_ids", "message_fragment"),
    [
        (
            None,
            ["instance-a", "instance-b"],
            "already linked in the active Fusion document",
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
    marker = _handoff_path(bundle_root)
    payload = {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_5",
        "exportId": "wge_5",
        "sequence": 5,
        "designId": "wgd-shared",
        "expectedDocumentId": "fusion:doc-a",
        "expectedReturnStateHash": "sha256:state-b",
    }
    if expected_instance_id is not None:
        payload["expectedInstanceId"] = expected_instance_id
    _write_version_3(marker, payload)
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

    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert mutations == []
    # The request is spent; nothing in Fusion retries it. Saying only "refresh
    # and try again" sends the user to a control that cannot unblock this.
    assert not marker.exists()
    assert len(ui.messages) == 1
    assert message_fragment in ui.messages[0][1]
    assert module._HANDOFF_RETRY_HINT in ui.messages[0][1]


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
    _claim_in_place(monkeypatch, module)
    request = types.SimpleNamespace(
        design_id="wgd-a",
        document_id="fusion:expected",
        instance_id="instance-a",
        expected_return_state_hash="sha256:state-a",
        request_id="request-a",
        operation_id="request-a",
        delivery_sequence=1,
    )
    monkeypatch.setattr(module, "_pending_return_request", lambda: request)
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:other")
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(module.wglink_send, "send", lambda _app, options: sent.append(options))

    assert module._apply_pending_return_request() == module.HANDLED

    assert sent == []
    assert ui.messages == [(
        "WGLink return to WG refused",
        "The active Fusion document changed after WG requested the model. "
        "Reopen CAD Link and try again."
        f"\n\n{module._RETURN_RETRY_HINT}",
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
    _claim_in_place(monkeypatch, module)
    request_a = types.SimpleNamespace(
        design_id="wgd-a",
        document_id="fusion:expected",
        instance_id="instance-a",
        expected_return_state_hash="sha256:state-a",
        request_id="request-a",
        operation_id="request-a",
        delivery_sequence=1,
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

    assert module._apply_pending_return_request() == module.HANDLED
    # The second call must not re-run the request, and must not re-prompt --
    # but it did no work, so it reports SUPPRESSED and leaves the tick to the
    # survey channel behind it rather than claiming it.
    assert module._apply_pending_return_request() == module.SUPPRESSED
    pending["request"] = request_b
    assert module._apply_pending_return_request() == module.HANDLED

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
    _claim_in_place(monkeypatch, module)
    request = types.SimpleNamespace(
        design_id="wgd-a",
        document_id="fusion:doc-a",
        instance_id="instance-a",
        expected_return_state_hash="sha256:state-a",
        request_id="request-a",
        operation_id="request-a",
        delivery_sequence=1,
    )
    monkeypatch.setattr(module, "_pending_return_request", lambda: request)
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [{
        "design_id": "wgd-a", "instance_id": "instance-a",
        "document_signature_hash": "sha256:state-a",
    }])
    # This test's subject is exact-link targeting, not the state guard: the
    # model has not moved, so the guard's fresh measurement agrees with the
    # token WG holds.
    _unmoved_live_state(monkeypatch, module, "sha256:state-a")
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: tmp_path)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(module.wglink_send, "send", lambda _app, options: sent.append(options))
    acknowledged: list[object] = []
    monkeypatch.setattr(module.wglink_watch, "acknowledge_return_request", acknowledged.append)

    assert module._apply_pending_return_request() == module.HANDLED

    assert sent == [{
        "selection": "root",
        "output_folder": str(tmp_path),
        "overwrite": True,
        "request_id": "request-a",
        "anchor_instance_id": "instance-a",
        "capture_document": True,
        # No capability file advertises sourceIdentity here.
        "source_identity": False,
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
    marker = _handoff_path(bundle_root)
    _write_version_3(marker, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_2",
        "exportId": "wge_2",
        "sequence": 2,
    })
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
    assert not marker.exists()
    assert ui.messages == [(
        "WGLink automatic insert refused",
        f"bad bundle\n\n{module._HANDOFF_RETRY_HINT}",
    )]


def _refusing_handoff(monkeypatch, module, tmp_path: Path) -> Path:
    """A handoff whose insert always refuses.

    This is the state that used to wedge the dispatcher: a refusal kept its id
    in ``_handoff_attempted_id`` for the rest of the Fusion session, and every
    later tick met it again.
    """

    bundle_root = tmp_path / "wglink"
    bundle = bundle_root / "horn.wglink"
    bundle.mkdir(parents=True)
    (bundle / "wglink.json").write_text("{}")
    marker = _handoff_path(bundle_root)
    _write_version_3(marker, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_2",
        "exportId": "wge_2",
        "sequence": 2,
    })
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundle_root)
    monkeypatch.setattr(
        module.wglink_workspace, "ipc_folder", lambda **_kwargs: bundle_root
    )

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise module.wglink_core.WgLinkError("bad bundle")

    monkeypatch.setattr(module.wglink_core, "insert", refuse)
    monkeypatch.setattr(module.wglink_core, "update", refuse)
    return marker


def test_a_refused_handoff_does_not_starve_a_pending_return_request(
    monkeypatch, tmp_path: Path
) -> None:
    """The handoff channel runs first; a refusal must not own every later tick.

    ``_apply_pending_handoff`` reported the same "yes, handled" for a real
    attempt and for the suppression that follows one, and ``_on_watch_tick``
    returned on both. WG's next Ask for model then reached a dispatcher that
    had already left, silently, for the rest of the session -- and the refusal
    the user saw named none of that.
    """

    panels = _Panels()
    ui = _UI(panels, _Definitions(reserve_ids=False))
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    app.activeDocument = types.SimpleNamespace(name="Tritonia V")
    module = _load_instance(monkeypatch, "WGLink_refusal_starves_return", ui, app)
    _claim_in_place(monkeypatch, module)
    marker = _refusing_handoff(monkeypatch, module, tmp_path)

    request = types.SimpleNamespace(
        design_id="wgd-a",
        document_id="fusion:doc-a",
        instance_id="instance-a",
        expected_return_state_hash="sha256:state-a",
        request_id="request-a",
        operation_id="request-a",
        delivery_sequence=1,
    )
    monkeypatch.setattr(module, "_pending_return_request", lambda: request)
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [{
        "instance_id": "instance-a",
        "design_id": "wgd-a",
        "bundle_path": str(tmp_path / "elsewhere" / "other.wglink"),
        "export_id": "wge_1",
        "document_signature_hash": "sha256:state-a",
    }])
    # This test's subject is tick dispatch, not the state guard: the model has
    # not moved, so the guard's fresh measurement agrees with the token WG
    # holds. The guard itself has its own tests against a real document.
    _unmoved_live_state(monkeypatch, module, "sha256:state-a")
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: tmp_path)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.wglink_send, "send", lambda _app, options: sent.append(options)
    )
    acknowledged: list[object] = []
    monkeypatch.setattr(
        module.wglink_watch, "acknowledge_return_request", acknowledged.append
    )

    module._on_watch_tick()
    # The refusal took the first tick, so the request is still waiting.
    assert sent == []

    module._on_watch_tick()

    assert [options["request_id"] for options in sent] == ["request-a"]
    assert acknowledged == [request]
    # The refused handoff is still refused and still on disk, and the user was
    # told about it exactly once -- that suppression is the correct half.
    assert not marker.exists()
    assert [title for title, _text in ui.messages] == [
        "WGLink automatic insert refused",
    ]


def test_a_refused_handoff_does_not_starve_a_newer_export_offer(
    monkeypatch, tmp_path: Path
) -> None:
    """The survey sits behind both IPC channels, so it starved the same way."""

    panels = _Panels()
    ui = _UI(panels, _Definitions(reserve_ids=False), dialog_result="no")
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    app.activeDocument = types.SimpleNamespace(name="Tritonia V")
    module = _load_instance(monkeypatch, "WGLink_refusal_starves_survey", ui, app)
    _refusing_handoff(monkeypatch, module, tmp_path)

    linked_bundle = tmp_path / "elsewhere" / "other.wglink"
    linked_bundle.mkdir(parents=True)
    (linked_bundle / "wglink.json").write_text(
        json.dumps({"export": {"id": "wge_9", "sequence": 9}})
    )
    monkeypatch.setattr(module, "_pending_return_request", lambda: None)
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [{
        "instance_id": "instance-a",
        "design_id": "wgd-a",
        "bundle_path": str(linked_bundle),
        "export_id": "wge_8",
    }])

    module._on_watch_tick()
    module._on_watch_tick()

    assert [title for title, _text in ui.messages] == [
        "WGLink automatic insert refused",
        f"{module.PANEL_NAME} — newer export available",
    ]


def test_a_refused_return_request_does_not_starve_a_newer_export_offer(
    monkeypatch, tmp_path: Path
) -> None:
    """The return channel carries the same attempted-id early return."""

    panels = _Panels()
    ui = _UI(panels, _Definitions(reserve_ids=False), dialog_result="no")
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    app.activeDocument = types.SimpleNamespace(name="Tritonia V")
    module = _load_instance(monkeypatch, "WGLink_return_refusal_starves", ui, app)
    _claim_in_place(monkeypatch, module)

    linked_bundle = tmp_path / "elsewhere" / "other.wglink"
    linked_bundle.mkdir(parents=True)
    (linked_bundle / "wglink.json").write_text(
        json.dumps({"export": {"id": "wge_9", "sequence": 9}})
    )
    request = types.SimpleNamespace(
        design_id="wgd-a",
        document_id="fusion:expected",
        instance_id="instance-a",
        expected_return_state_hash="sha256:state-a",
        request_id="request-a",
        operation_id="request-a",
        delivery_sequence=1,
    )
    monkeypatch.setattr(module, "_pending_handoff", lambda: None)
    monkeypatch.setattr(module, "_pending_return_request", lambda: request)
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:other")
    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [{
        "instance_id": "instance-a",
        "design_id": "wgd-a",
        "bundle_path": str(linked_bundle),
        "export_id": "wge_8",
    }])
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.wglink_send, "send", lambda _app, options: sent.append(options)
    )

    module._on_watch_tick()
    module._on_watch_tick()

    assert sent == []
    assert [title for title, _text in ui.messages] == [
        "WGLink return to WG refused",
        f"{module.PANEL_NAME} — newer export available",
    ]
    assert module._RETURN_RETRY_HINT in ui.messages[0][1]


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


def test_the_link_chooser_shows_the_users_label_before_wgs_design_name(
    monkeypatch,
) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_link_label_preference",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [
        {
            "instance_id": "wgi_one",
            "design_name": "260308Tritonia-M",
            "link_name": "Left waveguide",
        },
        {"instance_id": "wgi_two", "design_name": "260308Tritonia-M"},
    ])

    # The label is how the user recognises their own link; WG's design name is
    # the fallback for every link made before the label existed.
    assert module._document_link_choices() == [
        ("Left waveguide · wgi_one", "wgi_one"),
        ("260308Tritonia-M · wgi_two", "wgi_two"),
    ]


def test_the_insert_dialog_offers_an_optional_link_name(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_insert_link_name_input",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    module.adsk.core.DropDownStyles = types.SimpleNamespace(
        TextListDropDownStyle="text-list"
    )
    monkeypatch.setattr(module, "_discovered_bundles", lambda: [])
    strings: list[str] = []
    texts: list[tuple[str, str]] = []
    inputs = types.SimpleNamespace(
        addDropDownCommandInput=lambda *_args: types.SimpleNamespace(
            listItems=types.SimpleNamespace(add=lambda *_a: None),
        ),
        addStringValueInput=lambda name, *_args: strings.append(name),
        addTextBoxCommandInput=lambda name, _label, body, *_args: texts.append(
            (name, body)
        ),
    )
    module.CommandCreatedHandler("insert").notify(types.SimpleNamespace(
        command=types.SimpleNamespace(
            commandInputs=inputs,
            execute=types.SimpleNamespace(add=lambda _handler: None),
        ),
    ))

    assert strings == ["link_name"]
    help_text = dict(texts)["link_name_help"]
    # The field must say what it does NOT change, or a user reasonably reads it
    # as renaming the wg_<name>_* parameters their features already reference.
    assert "Leave empty" in help_text
    assert "parameters keep the bundle" in help_text


def test_a_typed_link_name_reaches_the_headless_insert_options(monkeypatch) -> None:
    module = _load_instance(
        monkeypatch,
        "WGLink_insert_link_name_options",
        _UI(_Panels(), _Definitions(reserve_ids=False)),
    )
    values = {"link_name": "  Left waveguide  "}
    inputs = types.SimpleNamespace(
        itemById=lambda name: types.SimpleNamespace(value=values[name])
        if name in values
        else None,
    )

    assert module._command_options(inputs) == {"link_name": "Left waveguide"}

    values["link_name"] = "   "
    assert module._command_options(inputs) == {}


# --- Delivery version 3: per-request files, reconciliation, correlation
#
# WG's contract is docs/architecture/CAD-OPERATIONS.md in that repository. WG
# writes each request as its own file, and nothing else. These tests drive the
# real dispatcher against such a folder; every path is under tmp_path.


def _per_request_folders(monkeypatch, module, tmp_path: Path) -> tuple[Path, Path]:
    ipc = tmp_path / "ipc"
    bundles = tmp_path / "workspace" / "wglink"
    ipc.mkdir(parents=True)
    bundles.mkdir(parents=True)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: ipc)
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: bundles)
    monkeypatch.setattr(
        module.wglink_workspace, "workspace_root", lambda: tmp_path / "workspace"
    )
    # WG advertises version 3, and the model has not moved since WG measured
    # it: these tests are about delivery, not the state guard.
    (ipc / "wg-capabilities.json").write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 3, "fusionRequestDelivery": 3,
    }))
    _unmoved_live_state(monkeypatch, module, "sha256:state-b")
    return ipc, bundles


def _publish_like_wg(
    ipc: Path,
    slot_name: str,
    folder_name: str,
    request_id: str,
    sequence: int,
    body: dict[str, object],
) -> None:
    folder = ipc / folder_name
    folder.mkdir(exist_ok=True)
    request = {
        **body,
        "requestId": request_id,
        "operationId": request_id,
        "deliverySequence": sequence,
    }
    (folder / f"{request_id}.json").write_text(json.dumps({**request, "schemaVersion": 3}))


def _per_request_handoff(
    ipc: Path,
    bundles: Path,
    *,
    request_id: str = "req-1",
    sequence: int = 1,
    export_id: str = "wge_5",
    instance_id: str | None = "instance-b",
    requested_at: str | None = None,
    destination: dict[str, str] | None = None,
) -> Path:
    bundle = bundles / "horn.wglink"
    bundle.mkdir(exist_ok=True)
    (bundle / "wglink.json").write_text("{}")
    body = {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_5",
        "exportId": export_id,
        "sequence": 5,
        "designId": "wgd-shared",
    }
    if instance_id is not None:
        body.update({
            "expectedDocumentId": "fusion:doc-a",
            "expectedInstanceId": instance_id,
            "expectedReturnStateHash": "sha256:state-b",
        })
    if requested_at is not None:
        body["requestedAt"] = requested_at
    if destination is not None:
        body["destination"] = destination
    _publish_like_wg(
        ipc, ".fusion-handoff.json", ".fusion-handoffs", request_id, sequence, body
    )
    return bundle


def _design_module(monkeypatch, name: str):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    return _load_instance(monkeypatch, name, ui, app), ui


def _shared_snapshot() -> dict[str, object]:
    return {
        "document_name": "Tritonia V",
        "document_id": "fusion:doc-a",
        "links": [
            {"instance_id": "instance-b", "design_id": "wgd-shared", "export_id": "wge_4"},
        ],
        "diagnostics": {"watchIntervalSeconds": 4.0},
    }


def test_a_per_request_handoff_updates_once_with_its_operation_id(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_per_request_update")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    bundle = _per_request_handoff(ipc, bundles)
    updated: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    snapshot = _shared_snapshot()

    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert updated == [(str(bundle), {"instance_id": "instance-b", "operation_id": "req-1"})]
    # The request's file is consumed and its claim deleted; nothing is left.
    assert list((ipc / ".fusion-handoffs").iterdir()) == []
    assert not (ipc / ".fusion-handoff.json").exists()
    assert module._apply_pending_handoff(snapshot) == module.IDLE
    assert len(updated) == 1
    assert ui.messages == []


def test_a_refused_per_request_handoff_is_consumed_and_says_how_to_retry(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_per_request_refused")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(ipc, bundles)
    attempts: list[str] = []

    def refuse(*_args: object, **_kwargs: object) -> None:
        attempts.append("update")
        raise module.wglink_core.WgLinkError("bad bundle")

    monkeypatch.setattr(module.wglink_core, "update", refuse)
    snapshot = _shared_snapshot()

    assert module._apply_pending_handoff(snapshot) == module.HANDLED
    assert module._apply_pending_handoff(snapshot) == module.IDLE

    assert attempts == ["update"]
    assert list((ipc / ".fusion-handoffs").iterdir()) == []
    assert len(ui.messages) == 1
    # The request is already spent: no file would help.
    assert ".json" not in ui.messages[0][1]
    assert "Send the model from WG again" in ui.messages[0][1]


def test_an_expired_insert_is_consumed_without_touching_the_document(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_expired_insert")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(
        ipc,
        bundles,
        instance_id=None,
        requested_at="2000-01-01T00:00:00Z",
        destination={"kind": "document", "value": "fusion:doc-a"},
    )
    inserted: list[object] = []
    monkeypatch.setattr(module.wglink_core, "insert", _recording(inserted))

    snapshot = {**_shared_snapshot(), "links": []}
    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert inserted == []
    assert list((ipc / ".fusion-handoffs").iterdir()) == []
    assert module._request_trace["outcome"] == "expired"
    assert "expired" in ui.messages[0][1].lower()


def test_an_insert_for_another_document_is_refused_without_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_wrong_insert_destination")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(
        ipc,
        bundles,
        instance_id=None,
        requested_at="2099-01-01T00:00:00Z",
        destination={"kind": "document", "value": "fusion:doc-other"},
    )
    inserted: list[object] = []
    monkeypatch.setattr(module.wglink_core, "insert", _recording(inserted))

    assert module._apply_pending_handoff(_shared_snapshot()) == module.HANDLED

    assert inserted == []
    assert module._request_trace["outcome"] == "refused"
    assert "destination" in ui.messages[0][1].lower()


def test_insert_destination_is_rechecked_immediately_before_the_first_write(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_insert_destination_race")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(
        ipc,
        bundles,
        instance_id=None,
        requested_at="2099-01-01T00:00:00Z",
        destination={"kind": "document", "value": "fusion:doc-a"},
    )
    active = {"id": "fusion:doc-a"}
    monkeypatch.setattr(module, "_active_document_id", lambda: active["id"])
    monkeypatch.setattr(module, "_document_links", lambda: [])
    writes: list[str] = []

    def insert(_app, _path, options):
        active["id"] = "fusion:doc-b"
        options["precondition"]()
        writes.append("model")

    monkeypatch.setattr(module.wglink_core, "insert", insert)

    snapshot = {**_shared_snapshot(), "links": []}
    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert writes == []
    assert module._request_trace["outcome"] == "refused"
    assert "destination" in ui.messages[0][1].lower()


def test_an_insert_for_the_active_destination_keeps_working(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_matching_insert_destination")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(
        ipc,
        bundles,
        instance_id=None,
        requested_at="2099-01-01T00:00:00Z",
        destination={"kind": "document", "value": "fusion:doc-a"},
    )
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda: [])
    writes: list[str] = []

    def insert(_app, _path, options):
        options["precondition"]()
        writes.append("model")

    monkeypatch.setattr(module.wglink_core, "insert", insert)
    snapshot = {**_shared_snapshot(), "links": []}

    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert writes == ["model"]
    assert module._request_trace["outcome"] == "applied"
    assert ui.messages == []


def test_a_failed_claim_never_creates_the_new_destination_document(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_new_document_claim_race")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(
        ipc,
        bundles,
        instance_id=None,
        requested_at="2099-01-01T00:00:00Z",
        destination={"kind": "new_document", "value": "req-1"},
    )
    monkeypatch.setattr(module, "_active_document_id", lambda: None)
    monkeypatch.setattr(module.wglink_watch, "claim_request", lambda _handoff: None)
    created: list[str] = []
    monkeypatch.setattr(
        module, "_ensure_design_ready", lambda: created.append("document") or True
    )

    assert module._apply_pending_handoff({"document_id": None, "links": []}) == module.IDLE

    assert created == []
    assert ui.messages == []


def test_a_claimed_new_document_destination_inserts_into_the_document_it_created(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_new_document_destination")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(
        ipc,
        bundles,
        instance_id=None,
        requested_at="2099-01-01T00:00:00Z",
        destination={"kind": "new_document", "value": "req-1"},
    )
    active = {"id": None}
    monkeypatch.setattr(module, "_active_document_id", lambda: active["id"])
    monkeypatch.setattr(module, "_document_links", lambda: [])

    def create_document():
        active["id"] = "fusion:new-doc"
        return True

    monkeypatch.setattr(module, "_ensure_design_ready", create_document)
    writes: list[str] = []

    def insert(_app, _path, options):
        options["precondition"]()
        writes.append("model")

    monkeypatch.setattr(module.wglink_core, "insert", insert)

    assert module._apply_pending_handoff({"document_id": None, "links": []}) == module.HANDLED

    assert writes == ["model"]
    assert module._request_trace["outcome"] == "applied"
    assert ui.messages == []


def test_an_insert_crossing_its_ttl_is_refused_at_the_first_write(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_insert_expiry_race")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(
        ipc,
        bundles,
        instance_id=None,
        requested_at="2026-09-15T09:00:00Z",
        destination={"kind": "document", "value": "fusion:doc-a"},
    )
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda: [])
    clock = iter([
        datetime(2026, 9, 15, 9, 29, 59, tzinfo=timezone.utc),
        datetime(2026, 9, 15, 9, 30, 1, tzinfo=timezone.utc),
    ])
    monkeypatch.setattr(module, "_utc_now", lambda: next(clock))
    writes: list[str] = []

    def insert(_app, _path, options):
        options["precondition"]()
        writes.append("model")

    monkeypatch.setattr(module.wglink_core, "insert", insert)
    snapshot = {**_shared_snapshot(), "links": []}

    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert writes == []
    assert module._request_trace["outcome"] == "expired"
    assert "expired" in ui.messages[0][1].lower()


def test_a_per_request_return_request_runs_in_its_session_and_is_consumed(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui = _design_module(monkeypatch, "WGLink_per_request_return")
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _publish_like_wg(
        ipc,
        ".fusion-return-request.json",
        ".fusion-return-requests",
        "req-r1",
        1,
        {
            "target": "fusion360",
            "sessionId": module._watch_session_id,
            "designId": "wgd-a",
            "documentId": "fusion:doc-a",
            "instanceId": "instance-a",
            "expectedReturnStateHash": "sha256:state-a",
        },
    )
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [{
        "design_id": "wgd-a", "instance_id": "instance-a",
        "document_signature_hash": "sha256:state-a",
    }])
    _unmoved_live_state(monkeypatch, module, "sha256:state-a")
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: tmp_path / "wgreturn")
    monkeypatch.setattr(module.wglink_workspace, "capture_document", lambda: False)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(module.wglink_send, "send", lambda _app, options: sent.append(options))

    assert module._apply_pending_return_request() == module.HANDLED
    assert module._apply_pending_return_request() == module.IDLE

    assert [options["request_id"] for options in sent] == ["req-r1"]
    # This WG does not advertise sourceIdentity, so the return declares nothing.
    assert sent[0]["source_identity"] is False
    assert list((ipc / ".fusion-return-requests").iterdir()) == []
    assert ui.messages == []


def test_a_return_wg_asked_for_declares_source_identity_when_wg_reads_it(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui = _design_module(monkeypatch, "WGLink_identity_return")
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    (ipc / "wg-capabilities.json").write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 3, "fusionRequestDelivery": 3,
        "sourceIdentity": 1,
    }))
    _publish_like_wg(
        ipc,
        ".fusion-return-request.json",
        ".fusion-return-requests",
        "req-identity",
        1,
        {
            "target": "fusion360",
            "sessionId": module._watch_session_id,
            "designId": "wgd-a",
            "documentId": "fusion:doc-a",
            "instanceId": "instance-a",
            "expectedReturnStateHash": "sha256:state-a",
        },
    )
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [{
        "design_id": "wgd-a", "instance_id": "instance-a",
        "document_signature_hash": "sha256:state-a",
    }])
    _unmoved_live_state(monkeypatch, module, "sha256:state-a")
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: tmp_path / "wgreturn")
    monkeypatch.setattr(module.wglink_workspace, "capture_document", lambda: False)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(module.wglink_send, "send", lambda _app, options: sent.append(options))

    assert module._apply_pending_return_request() == module.HANDLED

    assert [options["source_identity"] for options in sent] == [True]


def test_a_redelivered_update_already_applied_is_reconciled_before_the_baseline_check(
    monkeypatch, tmp_path: Path
) -> None:
    """A lost acknowledgement must not become a false conflict.

    WG's contract (CAD-OPERATIONS.md, "Fusion-bound mutations"): reconciliation
    comes before the baseline check, and it is a read. The update itself is
    what moved the document-wide token, so checking the baseline first refuses
    the redelivery of work that already completed. The evidence -- the export
    identity already on the exact link -- is read first, and nothing mutates.
    """

    fixture = _guarded_document(monkeypatch, "WGLink_reconcile_before_guard")
    module = fixture.module
    fixture.body.revisionId = "revision-2"
    snapshot = _heartbeat_snapshot(fixture)
    applied = fixture.published[0]["export_id"]
    effects = _pending_update(fixture, monkeypatch, tmp_path, export_id=applied)
    acknowledged: list[str] = []
    monkeypatch.setattr(
        module.wglink_watch,
        "acknowledge_handoff",
        lambda handoff, **_kwargs: acknowledged.append(handoff.export_id) or True,
    )

    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert effects == []
    assert fixture.ui.messages == []
    assert acknowledged == [applied]


def test_the_heartbeat_names_the_request_and_the_attempt_it_last_ran(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui = _design_module(monkeypatch, "WGLink_request_correlation")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    monkeypatch.setattr(module.wglink_core, "update", lambda *_a, **_k: None)
    snapshot = _shared_snapshot()

    def last_request() -> dict[str, object]:
        module._publish_fusion_status(snapshot)
        status = json.loads((ipc / ".fusion-status.json").read_text())
        assert status["diagnostics"]["watchIntervalSeconds"] == 4.0
        return status["diagnostics"]["lastRequest"]

    _per_request_handoff(ipc, bundles)
    module._apply_pending_handoff(snapshot)
    first = last_request()
    _per_request_handoff(ipc, bundles, request_id="req-2", sequence=2, export_id="wge_6")
    module._apply_pending_handoff(snapshot)
    second = last_request()

    assert {key: first[key] for key in ("channel", "correlationId", "delivery", "outcome")} == {
        "channel": "handoff",
        "correlationId": "req-1",
        "delivery": "perRequest",
        "outcome": "applied",
    }
    assert second["correlationId"] == "req-2"
    assert first["attemptId"] and second["attemptId"]
    assert first["attemptId"] != second["attemptId"]
    # The tick's own diagnostics are published, not replaced.
    assert "lastRequest" not in snapshot["diagnostics"]


def test_a_solve_command_is_written_per_command_and_correlated_by_its_id(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui = _design_module(monkeypatch, "WGLink_solve_correlation")
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    (ipc / "wg-capabilities.json").write_text(json.dumps(
        {"schemaVersion": 1, "solveCommandDelivery": 3, "fusionRequestDelivery": 3}
    ))
    bundle = tmp_path / "workspace" / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b"{}")
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)

    assert module._request_wg_solve({"return_id": "wgr_1", "bundle_path": str(bundle)}) is True

    written = [
        path for path in (ipc / ".wg-solve-requests").iterdir()
        if not path.name.startswith(".")
    ]
    assert len(written) == 1
    command_id = json.loads(written[0].read_text())["commandId"]
    assert not (ipc / ".wg-solve-request.json").exists()
    module._publish_fusion_status({
        "document_name": None, "document_id": None, "links": [], "diagnostics": {},
    })
    last = json.loads((ipc / ".fusion-status.json").read_text())["diagnostics"]["lastRequest"]
    assert (last["channel"], last["correlationId"], last["delivery"]) == (
        "solveCommand", command_id, module.DELIVERY,
    )
    assert last["attemptId"]


def test_a_claim_that_fails_while_the_design_cannot_be_made_ready_waits_for_the_next_pass(
    monkeypatch, tmp_path: Path
) -> None:
    """A failed claim is retried on the next pass, whatever else is wrong.

    On Windows WG can hold a request file open while it publishes. Refusing
    then would tell the user to send again, suppress the request for the
    session, and leave it on disk to run in the next session anyway.
    """

    module, ui = _design_module(monkeypatch, "WGLink_per_request_refusal_claim")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(ipc, bundles)
    request = ipc / ".fusion-handoffs" / "req-1.json"

    def cannot_open_a_design() -> bool:
        raise module.wglink_core.WgLinkError("no design")

    monkeypatch.setattr(module, "_ensure_design_ready", cannot_open_a_design)
    real_rename = os.rename
    held = {"left": 1}

    def held_open(source, destination, *args, **kwargs):
        if Path(source).name == request.name and held["left"]:
            held["left"] -= 1
            raise PermissionError("WG has the file open")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", held_open)
    snapshot = _shared_snapshot()

    assert module._apply_pending_handoff(snapshot) == module.IDLE
    assert ui.messages == []
    assert request.exists()

    assert module._apply_pending_handoff(snapshot) == module.HANDLED
    assert len(ui.messages) == 1
    assert not request.exists()
    assert module._apply_pending_handoff(snapshot) == module.IDLE


@pytest.mark.parametrize(
    ("stamped", "outcome"),
    [("req-1", "reconciled"), ("", "alreadyCurrent"), ("req-0", "alreadyCurrent")],
    ids=["this-operation", "manual-update", "another-operation"],
)
def test_a_handoff_whose_export_the_link_already_carries_changes_nothing(
    monkeypatch, tmp_path: Path, stamped: str, outcome: str
) -> None:
    """Only this operation's own id on the link is evidence that it applied.

    The export already on the exact link means nothing is left to do, however
    it got there, so nothing mutates either way. But the heartbeat says
    "reconciled" only when the stamped operation id is this request's.
    """

    module, ui = _design_module(monkeypatch, f"WGLink_already_current_{outcome}_{stamped}")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(ipc, bundles, export_id="wge_4")
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", lambda *a, **_k: updated.append(a))
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    snapshot = _shared_snapshot()
    snapshot["links"][0]["operation_id"] = stamped

    assert module._apply_pending_handoff(snapshot) == module.HANDLED
    module._publish_fusion_status(snapshot)

    assert updated == []
    assert ui.messages == []
    assert list((ipc / ".fusion-handoffs").iterdir()) == []
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["lastRequest"]["outcome"] == outcome


# --- Phase 0B: the exact target, the baseline, interruption and old peers ------


def test_an_update_without_a_baseline_never_changes_the_model(
    monkeypatch, tmp_path: Path
) -> None:
    """The baseline is mandatory for an update (CAD-OPERATIONS.md, R4).

    WG measured the model when it prepared the update; without that token
    nothing can show the document still matches, so nothing is changed.
    """

    module, ui = _design_module(monkeypatch, "WGLink_update_needs_baseline")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    bundle = bundles / "horn.wglink"
    bundle.mkdir()
    _publish_like_wg(ipc, "", ".fusion-handoffs", "req-1", 1, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_5",
        "exportId": "wge_5",
        "sequence": 5,
        "designId": "wgd-shared",
        "expectedDocumentId": "fusion:doc-a",
        "expectedInstanceId": "instance-b",
    })
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))

    assert module._apply_pending_handoff(_shared_snapshot()) == module.HANDLED

    assert updated == []
    assert [title for title, _text in ui.messages] == ["WGLink automatic update refused"]
    assert "model state it expects" in ui.messages[0][1]


def test_an_interrupted_operation_is_recovery_required_and_never_repeated(
    monkeypatch, tmp_path: Path
) -> None:
    """Marked as applying, with no evidence: it began and did not finish.

    Running it again would be the blind second mutation WG's contract forbids
    (M3). The outcome is recovery_required, in the heartbeat and in words.
    """

    module, ui = _design_module(monkeypatch, "WGLink_interrupted_update")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(ipc, bundles)
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    snapshot = _shared_snapshot()
    snapshot["applying_operation"] = {
        "operation_id": "req-1", "kind": "update",
        "instance_id": "instance-b", "export_id": "wge_5",
    }

    assert module._apply_pending_handoff(snapshot) == module.HANDLED
    module._publish_fusion_status(snapshot)

    assert updated == []
    assert "recovery required" in ui.messages[0][1]
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["lastRequest"]["outcome"] == "recoveryRequired"
    assert status["deliveryVersion"] == module.wglink_watch.DELIVERY_VERSION
    assert status["document"]["applyingOperation"]["operationId"] == "req-1"


@pytest.mark.parametrize(
    ("evidence", "applying", "outcome", "messages"),
    [
        ("req-1", None, "reconciled", 0),
        ("", "req-1", "recoveryRequired", 1),
        ("", None, "discarded", 0),
    ],
    ids=["applied", "interrupted", "never-started"],
)
def test_a_claim_an_interrupted_session_left_is_settled_once_and_never_run(
    monkeypatch, tmp_path: Path, evidence: str, applying: str | None,
    outcome: str, messages: int,
) -> None:
    """A claim is hidden from every listing, so it used to stay forever.

    The first tick of a session reads each one against the document -- a read
    only -- and removes it. Nothing is run again, whatever it finds.
    """

    module, ui = _design_module(monkeypatch, f"WGLink_leftover_{outcome}")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(ipc, bundles, export_id="wge_5")
    left = module.wglink_watch.claim_request(
        module.wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)
    )
    assert left is not None and left.marker_path.exists()
    snapshot = _shared_snapshot()
    snapshot["links"][0].update({"export_id": "wge_5" if evidence else "wge_4",
                                 "operation_id": evidence})
    snapshot["applying_operation"] = (
        {"operation_id": applying, "kind": "update", "instance_id": "instance-b",
         "export_id": "wge_5"}
        if applying else None
    )
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: snapshot)
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    mutations: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(mutations))
    monkeypatch.setattr(module.wglink_core, "insert", _recording(mutations))

    module._on_watch_tick()
    module._on_watch_tick()

    assert mutations == []
    assert not left.marker_path.exists()
    assert list((ipc / ".fusion-handoffs").iterdir()) == []
    assert len(ui.messages) == messages
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert {"channel": "handoff", "requestId": "req-1", "outcome": outcome} in (
        status["diagnostics"]["recentOutcomes"]
    )


def test_a_request_from_an_older_wg_is_never_run_and_asks_once_for_an_update(
    monkeypatch, tmp_path: Path
) -> None:
    """Decision 4: no negotiation with an older peer, and no silent drop."""

    module, ui = _design_module(monkeypatch, "WGLink_old_wg_refused")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    (ipc / "wg-capabilities.json").write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 2, "fusionRequestDelivery": 2,
    }))
    bundle = bundles / "horn.wglink"
    bundle.mkdir()
    (ipc / ".fusion-handoff.json").write_text(json.dumps({
        "schemaVersion": 1,
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_2",
        "exportId": "wge_2",
        "sequence": 2,
    }))
    monkeypatch.setattr(module, "_fusion_snapshot", _shared_snapshot)
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    mutations: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(mutations))
    monkeypatch.setattr(module.wglink_core, "insert", _recording(mutations))

    module._on_watch_tick()
    module._on_watch_tick()

    assert mutations == []
    assert [title for title, _text in ui.messages] == ["WGLink cannot read this request"]
    assert "Update Waveguide Generator" in ui.messages[0][1]
    # The older WG's file is its own; this add-in neither runs nor deletes it.
    assert (ipc / ".fusion-handoff.json").exists()


def test_solve_in_wg_refuses_a_wg_that_does_not_read_version_3(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui = _design_module(monkeypatch, "WGLink_solve_old_wg")
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    (ipc / "wg-capabilities.json").write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 2, "fusionRequestDelivery": 2,
    }))
    bundle = tmp_path / "workspace" / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b"{}")

    with pytest.raises(module.wglink_core.WgLinkError, match="Update Waveguide Generator"):
        module._request_wg_solve({"return_id": "wgr_1", "bundle_path": str(bundle)})

    assert not (ipc / ".wg-solve-request.json").exists()
    assert not (ipc / ".wg-solve-requests").exists()


def test_a_newer_update_for_the_same_target_supersedes_an_unstarted_one(
    monkeypatch, tmp_path: Path
) -> None:
    """Decision 3: only the newest unstarted update for one target runs."""

    module, _ui = _design_module(monkeypatch, "WGLink_superseded_update")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(ipc, bundles, request_id="req-1", sequence=1, export_id="wge_5")
    _per_request_handoff(ipc, bundles, request_id="req-2", sequence=2, export_id="wge_6")
    updated: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    snapshot = _shared_snapshot()

    assert module._apply_pending_handoff(snapshot) == module.HANDLED
    assert module._apply_pending_handoff(snapshot) == module.IDLE

    assert [options["operation_id"] for _path, options in updated] == ["req-2"]
    assert list((ipc / ".fusion-handoffs").iterdir()) == []
    # Recorded visibly: the trace names req-2, and the heartbeat still names
    # the request dropped for it.
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    module._publish_fusion_status(snapshot)
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["lastRequest"]["correlationId"] == "req-2"
    assert {"channel": "handoff", "requestId": "req-1", "outcome": "superseded"} in (
        status["diagnostics"]["recentOutcomes"]
    )


def test_a_solve_request_wg_leaves_untaken_is_reported_once_and_never_dropped(
    monkeypatch, tmp_path: Path
) -> None:
    """The bound on a stale advertisement (CAD-OPERATIONS.md, "Capability file").

    After a downgrade the file can still say 3 while an older WG, which never
    reads per-command files, runs. The user is told after a minute; the
    command is not moved or deleted.
    """

    module, ui = _design_module(monkeypatch, "WGLink_untaken_solve")
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    bundle = tmp_path / "workspace" / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b"{}")
    clock = types.SimpleNamespace(value=1000.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.value)
    monkeypatch.setattr(module, "_fusion_snapshot", _shared_snapshot)
    monkeypatch.setattr(module, "_apply_pending_handoff", lambda _snapshot: module.IDLE)
    monkeypatch.setattr(module, "_apply_pending_return_request", lambda _snapshot: module.IDLE)
    monkeypatch.setattr(module._watcher, "survey", lambda _links: [])
    busy_while_shown: list[bool] = []
    real_message = module._message

    def message(text, title):
        busy_while_shown.append(module._command_busy)
        real_message(text, title)

    monkeypatch.setattr(module, "_message", message)

    assert module._request_wg_solve({"return_id": "wgr_1", "bundle_path": str(bundle)}) is True
    written = [path for path in (ipc / ".wg-solve-requests").iterdir() if not path.name.startswith(".")]
    module._on_watch_tick()
    assert ui.messages == []

    clock.value += 61.0
    module._on_watch_tick()
    module._on_watch_tick()

    assert [title for title, _text in ui.messages] == ["WGLink solve request waiting"]
    assert "has not taken the solve request" in ui.messages[0][1]
    # The modal was shown with the dispatcher held, so no tick ran inside it.
    assert busy_while_shown == [True]
    assert written[0].exists()

    # Taken: nothing more is said about it.
    written[0].unlink()
    clock.value += 120.0
    module._on_watch_tick()
    assert len(ui.messages) == 1


def test_an_insert_rechecks_the_live_document_immediately_before_it_writes(
    monkeypatch, tmp_path: Path
) -> None:
    """Item 5 for inserts: the target is read again, live, before the first write."""

    module, ui = _design_module(monkeypatch, "WGLink_insert_precondition")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    bundle = bundles / "horn.wglink"
    bundle.mkdir()
    _publish_like_wg(ipc, "", ".fusion-handoffs", "req-1", 1, {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": "wgb_5",
        "exportId": "wge_5",
        "sequence": 5,
        "designId": "wgd-shared",
    })
    snapshot = {**_shared_snapshot(), "links": []}
    # By the time the insert is about to write, the design is linked.
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_document_links", lambda *_a, **_k: [
        {"instance_id": "instance-z", "design_id": "wgd-shared", "export_id": "wge_4"},
    ])
    inserted: list[object] = []
    monkeypatch.setattr(module.wglink_core, "insert", _recording(inserted))

    assert module._apply_pending_handoff(snapshot) == module.HANDLED

    assert inserted == []
    assert "already linked in the active Fusion document" in ui.messages[0][1]


def test_leftover_claims_wait_for_an_active_document(monkeypatch, tmp_path: Path) -> None:
    module, _ui = _design_module(monkeypatch, "WGLink_leftover_waits")
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _per_request_handoff(ipc, bundles, export_id="wge_5")
    left = module.wglink_watch.claim_request(
        module.wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)
    )
    opening = {**_shared_snapshot(), "document_id": None, "links": [], "applying_operation": None}

    module._sweep_leftover_claims(opening)
    assert left.marker_path.exists() and module._claims_swept is False

    module._sweep_leftover_claims({**_shared_snapshot(), "applying_operation": None})
    assert not left.marker_path.exists() and module._claims_swept is True


# -- the live CAD Link session (wglink_live) -----------------------------------


class _IdleDispatch:
    """The live client's Fusion-bound request half, with nothing to dispatch.

    A stand-in that left these out would make the main thread's dispatch code
    look absent rather than idle, so every fake carries the whole surface.
    """

    epoch = 1

    def offers(self) -> list:
        return []

    def take_dispatch(self):
        return None

    def take_adoption(self):
        return None

    def cancel_pending(self, _operation_id: str) -> bool:
        return False

    def claim(self, _offer: object) -> None:
        raise AssertionError("nothing was offered")

    def report_progress(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("nothing was dispatched")

    def report_outcome(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("nothing was dispatched")


class _LiveRecorder(_IdleDispatch):
    """Stands in for ``wglink_live.LiveClient`` where only the hand-over matters."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.offered: list[dict] = []
        self.lines = list(lines or [])

    def offer_heartbeat(self, payload: dict) -> None:
        self.offered.append(json.loads(json.dumps(payload)))

    def take_log_lines(self) -> list[str]:
        lines, self.lines = self.lines, []
        return lines

    def take_delivery_notices(self) -> list[dict]:
        return []


def test_the_heartbeat_file_and_the_live_offer_are_the_same_object(monkeypatch, tmp_path: Path) -> None:
    module = _load_instance(monkeypatch, "WGLink_live_offer", _UI(_Panels(), _Definitions(reserve_ids=False)))
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    recorder = _LiveRecorder()
    monkeypatch.setattr(module, "_live_client", recorder)

    module._publish_fusion_status({
        "document_name": "Speaker", "document_id": "fusion:doc", "links": [],
        "diagnostics": {"watchIntervalSeconds": 4.0},
    })

    written = json.loads((tmp_path / module.wglink_watch.FUSION_STATUS_FILENAME).read_text())
    assert recorder.offered == [written]
    assert written["sessionId"] == module._watch_session_id

    # Without a live client the file is written exactly as before.
    monkeypatch.setattr(module, "_live_client", None)
    (tmp_path / module.wglink_watch.FUSION_STATUS_FILENAME).unlink()
    module._publish_fusion_status({"document_name": None, "document_id": None, "links": [], "diagnostics": {}})
    assert (tmp_path / module.wglink_watch.FUSION_STATUS_FILENAME).exists()


def test_the_live_client_log_is_printed_on_the_main_thread_tick(monkeypatch) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    module = _load_instance(monkeypatch, "WGLink_live_log", ui)
    monkeypatch.setattr(module, "_live_client", _LiveRecorder(["WGLink is live with WG."]))
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: {
        "document_name": None, "document_id": None, "links": [], "diagnostics": {},
    })
    monkeypatch.setattr(module, "_claims_swept", True)

    module._on_watch_tick()

    assert "WGLink is live with WG." in ui.text_palette.written


def test_only_the_lease_owner_runs_a_live_client_and_stop_joins_it(monkeypatch) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    app = _Application(ui)
    owner = _load_instance(monkeypatch, "WGLink_live_owner", ui, app)
    standby = _load_instance(monkeypatch, "WGLink_live_standby", ui, app)

    owner.run(None)
    standby.run(None)
    client = owner._live_client
    assert client is not None and client.is_running()
    assert standby._live_client is None

    standby.stop(None)
    assert owner._live_client is client and client.is_running()
    owner.stop(None)
    assert owner._live_client is None
    assert not client.is_running()


def test_the_main_thread_never_does_network_io_while_live(monkeypatch, tmp_path: Path) -> None:
    """Start, tick, publish and stop on this (the "main") thread against a loopback WG stand-in."""

    import socket
    import threading

    from test_wglink_live import LIVE, StubServer, _capabilities, _endpoint, _mac, _write_private

    ipc = tmp_path / "ipc" / "wglink"
    ipc.mkdir(parents=True)
    os.chmod(ipc, 0o755)
    secret, instance = "lifecycle-secret", "e" * 32
    heartbeats: list[dict] = []
    arrived = threading.Event()

    def handle(_stub, method, path, headers, body):
        if (method, path) == ("GET", LIVE + "/endpoint"):
            return 200, {}, json.dumps({"schemaVersion": 1, "producer": "waveguide-generator",
                                        "instanceId": instance, "liveProtocol": 1, "deliveryVersion": 3}).encode()
        if (method, path) == ("POST", LIVE + "/sessions"):
            sent = json.loads(body)
            proof = _mac(secret, "wglink-server", sent["clientNonce"], instance, headers["x-wglink-installation"])
            return 201, {}, json.dumps({"liveSessionId": "s", "sessionToken": "lifecycle-token",
                                        "serverProof": proof, "instanceId": instance, "liveProtocol": 1}).encode()
        if (method, path) == ("POST", LIVE + "/heartbeat"):
            heartbeats.append(json.loads(body))
            if (heartbeats[-1].get("document") or {}).get("name") == "Speaker":
                arrived.set()
        return 204, {}, b""

    stub = StubServer(handle)
    try:
        _write_private(ipc / "wg-capabilities.json", _capabilities())
        _write_private(ipc / "wg-endpoint.json", _endpoint(instance, secret, stub.port))
        main = threading.current_thread()
        connecting: list[threading.Thread] = []
        original = socket.socket.connect

        def recording_connect(self, address):
            connecting.append(threading.current_thread())
            return original(self, address)

        monkeypatch.setattr(socket.socket, "connect", recording_connect)
        ui = _UI(_Panels(), _Definitions(reserve_ids=False))
        module = _load_instance(monkeypatch, "WGLink_live_main_thread", ui)
        monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: ipc)

        module.run(None)
        client = module._live_client
        deadline = time.monotonic() + 10
        while not client.healthy() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.healthy(), client.take_log_lines()
        module._publish_fusion_status({"document_name": "Speaker", "document_id": None, "links": [],
                                       "diagnostics": {"watchIntervalSeconds": 4.0}})
        assert arrived.wait(10)
        module.stop(None)
    finally:
        stub.close()

    file_heartbeat = ipc / module.wglink_watch.FUSION_STATUS_FILENAME
    assert not file_heartbeat.exists()  # removed at stop, as before
    speaker = [h for h in heartbeats if (h.get("document") or {}).get("name") == "Speaker"]
    assert len(speaker) == 1
    assert speaker[0] == json.loads(json.dumps(speaker[0]))
    assert all(h["sessionId"] == module._watch_session_id for h in heartbeats)
    assert len(connecting) >= 4  # hello, register, heartbeat, end
    assert main not in connecting
    assert ("DELETE", LIVE + "/sessions/current") in [(m, p) for m, p, _h, _b in stub.requests]
    assert not client.is_running()


# -- the outbox, main-thread half (wglink_live producers and notices) ----------------------


class _OutboxClient(_IdleDispatch):
    """Stands in for the live client where only the main thread's half matters."""

    def __init__(self, *, healthy: bool, notices: list[dict] | None = None) -> None:
        self._healthy = healthy
        self.enqueued = 0
        self.notices = list(notices or [])
        self.acknowledged: list[str] = []

    def healthy(self) -> bool:
        return self._healthy

    def enqueue_delivery(self) -> None:
        self.enqueued += 1

    def take_delivery_notices(self) -> list[dict]:
        notices, self.notices = self.notices, []
        return notices

    def acknowledge_delivery_notice(self, operation_id: str) -> None:
        self.acknowledged.append(operation_id)

    def offer_heartbeat(self, _payload: dict) -> None:
        pass

    def take_log_lines(self) -> list[str]:
        return []


_LIVE_CAPABILITIES = {
    "schemaVersion": 1, "solveCommandDelivery": 3, "fusionRequestDelivery": 3, "liveProtocol": 1,
}


def _outbox_setup(monkeypatch, tmp_path: Path, name: str, *, live: bool = True):
    module, ui = _design_module(monkeypatch, name)
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    if live:
        (ipc / "wg-capabilities.json").write_text(json.dumps(_LIVE_CAPABILITIES))
    bundle = tmp_path / "workspace" / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b'{"return": "speaker"}')
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    return module, ui, ipc, bundle


def _outbox_items(ipc: Path) -> dict[str, dict]:
    folder = ipc / ".wglink-outbox"
    if not folder.is_dir():
        return {}
    return {p.stem: json.loads(p.read_text()) for p in folder.iterdir() if not p.name.startswith(".")}


def _v3_solve_files(ipc: Path) -> list[Path]:
    folder = ipc / ".wg-solve-requests"
    return sorted(p for p in folder.iterdir() if not p.name.startswith(".")) if folder.is_dir() else []


def test_solve_in_wg_with_a_healthy_live_session_queues_the_item_and_writes_no_file(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, ipc, bundle = _outbox_setup(monkeypatch, tmp_path, "WGLink_outbox_live_solve")
    client = _OutboxClient(healthy=True)
    monkeypatch.setattr(module, "_live_client", client)

    assert module._request_wg_solve({"return_id": "wgr_1", "bundle_path": str(bundle)}) is True

    [(operation_id, item)] = _outbox_items(ipc).items()
    assert (item["kind"], item["returnId"], item["bundlePath"], item["fileWritten"]) == (
        "prepare_and_solve", "wgr_1", "wgreturn/speaker.wgreturn", False,
    )
    assert _v3_solve_files(ipc) == []
    assert client.enqueued == 1
    assert module._request_trace["correlationId"] == operation_id


def test_solve_in_wg_without_a_healthy_session_writes_the_file_and_the_item_under_one_id(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, ipc, bundle = _outbox_setup(monkeypatch, tmp_path, "WGLink_outbox_offline_solve")
    client = _OutboxClient(healthy=False)
    monkeypatch.setattr(module, "_live_client", client)

    module._request_wg_solve({"return_id": "wgr_1", "bundle_path": str(bundle)})

    [path] = _v3_solve_files(ipc)
    payload = json.loads(path.read_text())
    [(operation_id, item)] = _outbox_items(ipc).items()
    assert path.stem == payload["commandId"] == operation_id
    assert item["fileWritten"] is True
    for key in ("returnId", "bundlePath", "manifestSha256", "requestedAt"):
        assert payload[key] == item[key]


def test_solve_in_wg_for_a_wg_without_the_live_protocol_writes_only_the_v3_file(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, ipc, bundle = _outbox_setup(monkeypatch, tmp_path, "WGLink_outbox_no_live", live=False)
    monkeypatch.setattr(module, "_live_client", _OutboxClient(healthy=True))

    module._request_wg_solve({"return_id": "wgr_1", "bundle_path": str(bundle)})

    assert len(_v3_solve_files(ipc)) == 1
    assert not (ipc / ".wglink-outbox").exists()


def test_a_plain_send_queues_a_snapshot_only_for_a_wg_that_speaks_live(monkeypatch, tmp_path: Path) -> None:
    module, _ui, ipc, bundle = _outbox_setup(monkeypatch, tmp_path, "WGLink_outbox_send", live=False)
    client = _OutboxClient(healthy=False)
    monkeypatch.setattr(module, "_live_client", client)
    report = {"return_id": "wgr_1", "bundle_path": str(bundle)}

    module._queue_snapshot(report)
    assert _outbox_items(ipc) == {} and client.enqueued == 0

    (ipc / "wg-capabilities.json").write_text(json.dumps(_LIVE_CAPABILITIES))
    module._queue_snapshot(report)
    [item] = _outbox_items(ipc).values()
    assert (item["kind"], item["bundlePath"]) == ("receive_snapshot", "wgreturn/speaker.wgreturn")
    assert "returnId" not in item and _v3_solve_files(ipc) == []
    assert client.enqueued == 1
    assert "delivery_note" not in report


def test_final_delivery_answers_are_recorded_and_the_ones_that_need_the_user_shown_then_acknowledged(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui, ipc, _bundle = _outbox_setup(monkeypatch, tmp_path, "WGLink_outbox_notices")
    notices = [
        {"operationId": "op-ok", "kind": "prepare_and_solve", "createdAt": "2026-09-17T10:00:00.000Z",
         "outcome": "delivered", "state": "received", "reason": None, "message": None, "durable": False},
        {"operationId": "op-gone", "kind": "receive_snapshot", "createdAt": "2026-09-16T10:00:00.000Z",
         "outcome": "rejected", "state": "rejected", "reason": "snapshot_unavailable",
         "message": "WG could not read this snapshot in the WGLink folder for 24 hours. Send it again from Fusion.",
         "durable": True},
    ]
    client = _OutboxClient(healthy=True, notices=notices)
    monkeypatch.setattr(module, "_live_client", client)
    monkeypatch.setattr(module, "_fusion_snapshot", _shared_snapshot)
    monkeypatch.setattr(module, "_apply_pending_handoff", lambda _snapshot: module.IDLE)
    monkeypatch.setattr(module, "_apply_pending_return_request", lambda _snapshot: module.IDLE)
    monkeypatch.setattr(module._watcher, "survey", lambda _links: [])
    monkeypatch.setattr(module, "_claims_swept", True)

    module._on_watch_tick()

    [(title, text)] = ui.messages
    assert title == "WGLink delivery to WG"
    assert "for 24 hours" in text and "snapshot_unavailable" in text and "op-gone" in text
    assert "Send to WG" in text
    assert client.acknowledged == ["op-gone"]
    status = json.loads((ipc / ".fusion-status.json").read_text())
    outcomes = status["diagnostics"]["recentOutcomes"]
    assert {"channel": "solveCommand", "requestId": "op-ok", "outcome": "delivered"} in outcomes
    assert {"channel": "snapshot", "requestId": "op-gone", "outcome": "rejected"} in outcomes


def test_a_solve_waiting_only_for_its_live_delivery_counts_as_untaken(monkeypatch, tmp_path: Path) -> None:
    module, ui, ipc, bundle = _outbox_setup(monkeypatch, tmp_path, "WGLink_outbox_untaken")
    monkeypatch.setattr(module, "_live_client", _OutboxClient(healthy=True))
    clock = types.SimpleNamespace(value=1000.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.value)
    monkeypatch.setattr(module, "_fusion_snapshot", _shared_snapshot)
    monkeypatch.setattr(module, "_apply_pending_handoff", lambda _snapshot: module.IDLE)
    monkeypatch.setattr(module, "_apply_pending_return_request", lambda _snapshot: module.IDLE)
    monkeypatch.setattr(module._watcher, "survey", lambda _links: [])
    monkeypatch.setattr(module, "_claims_swept", True)

    module._request_wg_solve({"return_id": "wgr_1", "bundle_path": str(bundle)})
    assert _v3_solve_files(ipc) == []
    clock.value += 61.0
    module._on_watch_tick()
    assert [title for title, _text in ui.messages] == ["WGLink solve request waiting"]

    # Delivered: the item is gone and nothing more is said.
    [operation_id] = _outbox_items(ipc)
    (ipc / ".wglink-outbox" / f"{operation_id}.json").unlink()
    clock.value += 120.0
    module._on_watch_tick()
    assert len(ui.messages) == 1


# -- the live transport's main-thread half (protocol section 7) ----------------


class _Dispatcher(_IdleDispatch):
    """The live client's request half, scripted, with every report recorded.

    Only the main thread's behaviour is under test here: which Fusion calls
    happen, when, and what outcome is reported. ``tests/test_wglink_live_dispatch.py``
    drives the same exchanges against WG's routes.
    """

    def __init__(self, *, healthy: bool = True, epoch: int = 1) -> None:
        self._healthy = healthy
        self.epoch = epoch
        self.offered: list[object] = []
        self.claimed: list[str] = []
        self.dispatches: list[object] = []
        self.adoptions: list[dict] = []
        self.cancelled: set[str] = set()
        self.progress: list[tuple[str, int, str]] = []
        self.outcomes: list[dict] = []
        self.lines: list[str] = []

    # -- what WGLink.py calls ---------------------------------------------------

    def healthy(self) -> bool:
        return self._healthy

    def offers(self) -> list:
        return list(self.offered)

    def claim(self, offer: object) -> None:
        self.claimed.append(offer.operation_id)
        self.offered = [item for item in self.offered if item is not offer]

    def take_dispatch(self):
        return self.dispatches.pop(0) if self.dispatches else None

    def take_adoption(self):
        return self.adoptions.pop(0) if self.adoptions else None

    def cancel_pending(self, operation_id: str) -> bool:
        return operation_id in self.cancelled

    def report_progress(self, operation_id: str, generation: int, stage: str, **_kw) -> None:
        self.progress.append((operation_id, generation, stage))

    def report_outcome(self, operation_id: str, generation: int, outcome: str, **kwargs) -> None:
        self.outcomes.append({
            "operationId": operation_id, "attemptGeneration": generation,
            "outcome": outcome, "evidence": kwargs.get("evidence"),
            "message": kwargs.get("message"),
        })

    def offer_heartbeat(self, _payload: dict) -> None:
        pass

    def take_log_lines(self) -> list[str]:
        lines, self.lines = self.lines, []
        return lines

    def take_delivery_notices(self) -> list[dict]:
        return []


def _live_request(module, bundle: Path, **overrides: object) -> dict:
    body = {
        "schemaVersion": 3,
        "target": "fusion360",
        "requestId": "op-live-1",
        "operationId": "op-live-1",
        "deliverySequence": 1,
        "bundlePath": str(bundle),
        "bundleId": "wgb_5",
        "exportId": "wge_5",
        "sequence": 5,
        "designId": "wgd-shared",
        "expectedDocumentId": "fusion:doc-a",
        "expectedInstanceId": "instance-b",
        "expectedReturnStateHash": "sha256:state-b",
    }
    body.update(overrides)
    return body


def _live_dispatch(module, request: dict, *, kind: str | None = None, generation: int = 1, epoch: int = 1):
    return module.wglink_live.Dispatch(
        str(request["operationId"]),
        kind or module.wglink_live.KIND_UPDATE_LINK,
        generation,
        dict(request),
        epoch,
    )


def _live_module(monkeypatch, tmp_path: Path, name: str):
    module, ui = _design_module(monkeypatch, name)
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path)
    bundle = bundles / "horn.wglink"
    bundle.mkdir(exist_ok=True)
    (bundle / "wglink.json").write_text("{}")
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    monkeypatch.setattr(module, "_claims_swept", True)
    return module, ui, ipc, bundle


def test_a_live_update_runs_on_the_main_thread_and_reports_what_happened(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui, ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_update")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    client.dispatches.append(_live_dispatch(module, _live_request(module, bundle)))

    module._on_live_dispatch(_shared_snapshot())

    assert [call[0] for call in updated] == [str(bundle)]
    # ``executing`` is reported immediately before the first Fusion write, so a
    # refusal before it never makes WG believe the model was being changed.
    assert client.progress == [("op-live-1", 1, module.wglink_live.STAGE_EXECUTING)]
    assert client.outcomes == [{
        "operationId": "op-live-1", "attemptGeneration": 1, "outcome": "applied",
        "evidence": {"operationId": "op-live-1", "exportId": "wge_5"}, "message": None,
    }]
    assert ui.messages == []


def test_a_live_request_never_interrupts_a_running_fusion_command(
    monkeypatch, tmp_path: Path
) -> None:
    """The request waits and the heartbeat says why; nothing is claimed."""

    module, _ui, ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_busy")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    request = _live_request(module, bundle)
    client.offered.append(module.wglink_live.Offer("op-live-1", "update_link", 0, request, 1))
    client.dispatches.append(_live_dispatch(module, request))
    reads: list[int] = []
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: (reads.append(1), _shared_snapshot())[1])
    monkeypatch.setattr(module, "_command_busy", True)

    assert module._on_live_dispatch() is False

    assert updated == [] and client.claimed == [] and client.outcomes == []
    # Nor is the document read: a command is running, and this thread is the
    # one running it.
    assert reads == []
    module._publish_fusion_status(_shared_snapshot())
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["liveDispatch"]["reason"] == "commandBusy"

    # The command finishes; the same request runs on the next pass, once.
    monkeypatch.setattr(module, "_command_busy", False)
    module._on_live_dispatch(_shared_snapshot())
    assert len(updated) == 1


def test_file_claims_stand_still_while_the_live_session_is_healthy(
    monkeypatch, tmp_path: Path
) -> None:
    """One request can never be claimed by both transports."""

    module, _ui, ipc, _bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_exclusive")
    bundles = tmp_path / "workspace" / "wglink"
    _per_request_handoff(ipc, bundles, export_id="wge_5")
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: _shared_snapshot())
    monkeypatch.setattr(module, "_live_client", _Dispatcher(healthy=True))

    module._on_watch_tick()

    assert updated == []
    assert (ipc / ".fusion-handoffs" / "req-1.json").exists(), "the file was claimed while live"

    # The session drops: the file transport takes the same request, unchanged.
    monkeypatch.setattr(module, "_live_client", _Dispatcher(healthy=False))
    module._on_watch_tick()
    assert len(updated) == 1
    assert list((ipc / ".fusion-handoffs").iterdir()) == []


def test_a_cancellation_before_the_first_write_leaves_the_model_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui, _ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_cancel_clean")
    client = _Dispatcher()
    client.cancelled.add("op-live-1")
    monkeypatch.setattr(module, "_live_client", client)
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    client.dispatches.append(_live_dispatch(module, _live_request(module, bundle)))

    module._on_live_dispatch(_shared_snapshot())

    assert updated == []
    assert client.progress == []
    [outcome] = client.outcomes
    assert outcome["outcome"] == "discarded"
    assert "stopped before it changed" in outcome["message"]


def test_a_cancellation_that_arrives_inside_a_fusion_call_is_not_a_clean_cancellation(
    monkeypatch, tmp_path: Path
) -> None:
    """A Fusion call is never interrupted: the outcome reported is the truth."""

    module, _ui, _ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_cancel_late")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    updated: list[object] = []
    recorder = _recording(updated)

    def update_then_cancel(app, path, options):
        result = recorder(app, path, options)
        # WG dismissed it while Update was writing.
        client.cancelled.add("op-live-1")
        return result

    monkeypatch.setattr(module.wglink_core, "update", update_then_cancel)
    client.dispatches.append(_live_dispatch(module, _live_request(module, bundle)))

    module._on_live_dispatch(_shared_snapshot())

    assert len(updated) == 1, "the model was changed"
    [outcome] = client.outcomes
    assert outcome["outcome"] == "applied", "a completed change was reported as cancelled"


def test_a_cancellation_at_the_precondition_stops_before_the_first_write(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, _ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_cancel_pre")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    written: list[object] = []

    def update(_app, _path, options):
        client.cancelled.add("op-live-1")
        options["precondition"]()
        written.append("wrote")

    monkeypatch.setattr(module.wglink_core, "update", update)
    client.dispatches.append(_live_dispatch(module, _live_request(module, bundle)))

    module._on_live_dispatch(_shared_snapshot())

    assert written == []
    assert client.outcomes[0]["outcome"] == "discarded"


def test_an_interrupted_live_update_settles_recovery_required_and_never_reapplies(
    monkeypatch, tmp_path: Path
) -> None:
    module, ui, _ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_recovery")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    client.adoptions.append({
        "schemaVersion": 1, "operationId": "op-live-1", "claimId": "c1",
        "kind": "update_link", "attemptGeneration": 1, "state": "claimed",
        "request": _live_request(module, bundle), "claimedAt": "2026-09-20T10:00:00Z",
    })
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    snapshot = {
        **_shared_snapshot(),
        "applying_operation": {
            "operation_id": "op-live-1", "kind": "update", "instance_id": "instance-b",
            "export_id": "wge_5",
        },
    }

    module._on_live_dispatch(snapshot)

    assert updated == [], "an interrupted update was reapplied"
    [outcome] = client.outcomes
    assert outcome["outcome"] == "recoveryRequired"
    assert "recovery required" in ui.messages[0][1]


def test_a_live_claim_for_another_document_is_retained_not_discarded(
    monkeypatch, tmp_path: Path
) -> None:
    """Gate A3: document A's update is not discarded when B is active."""

    module, _ui, ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_other_doc")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    client.adoptions.append({
        "schemaVersion": 1, "operationId": "op-live-1", "claimId": "c1",
        "kind": "update_link", "attemptGeneration": 1, "state": "claimed",
        "request": _live_request(module, bundle), "claimedAt": "2026-09-20T10:00:00Z",
    })
    elsewhere = {**_shared_snapshot(), "document_id": "fusion:doc-b", "links": [],
                 "applying_operation": None}

    module._on_live_dispatch(elsewhere)

    assert client.outcomes == [], "an operation of an unopened document was settled"
    module._publish_fusion_status(elsewhere)
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["liveDispatch"]["targetDocumentUnavailable"] == ["op-live-1"]

    # Its own document opens: it is settled then, still read-only.
    module._on_live_dispatch({**_shared_snapshot(), "applying_operation": None})
    assert [item["outcome"] for item in client.outcomes] == ["discarded"]


def test_a_file_claim_for_another_document_is_retained_not_discarded(
    monkeypatch, tmp_path: Path
) -> None:
    """The same rule on the file transport, where the defect was."""

    module, _ui, ipc, _bundle = _live_module(monkeypatch, tmp_path, "WGLink_file_other_doc")
    bundles = tmp_path / "workspace" / "wglink"
    _per_request_handoff(ipc, bundles, export_id="wge_5")
    left = module.wglink_watch.claim_request(
        module.wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)
    )
    assert left is not None
    monkeypatch.setattr(module, "_claims_swept", False)
    elsewhere = {**_shared_snapshot(), "document_id": "fusion:doc-b", "links": [],
                 "applying_operation": None}

    module._sweep_leftover_claims(elsewhere)

    assert left.marker_path.exists(), "a claim of an unopened document was destroyed"
    assert module._recent_outcomes == [], "it was reported as an outcome it cannot know"

    # Its document becomes active later in the same session.
    module._settle_file_claims(list(module._deferred_file_claims), _shared_snapshot())
    assert not left.marker_path.exists()
    assert module._recent_outcomes[-1]["outcome"] == "discarded"


def test_a_dispatch_from_an_owner_that_lost_the_connection_never_runs(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, _ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_stale_epoch")
    client = _Dispatcher(epoch=4)
    monkeypatch.setattr(module, "_live_client", client)
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    client.dispatches.append(_live_dispatch(module, _live_request(module, bundle), epoch=3))

    module._on_live_dispatch(_shared_snapshot())

    assert updated == [] and client.outcomes == []


def test_a_superseded_live_update_is_completed_without_running(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, _ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_superseded")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    older = _live_request(module, bundle, requestId="op-old", operationId="op-old", deliverySequence=1)
    newer = _live_request(module, bundle, requestId="op-new", operationId="op-new", deliverySequence=2)
    client.offered = [
        module.wglink_live.Offer("op-old", "update_link", 0, older, 1),
        module.wglink_live.Offer("op-new", "update_link", 0, newer, 1),
    ]

    module._on_live_dispatch(_shared_snapshot())
    assert client.claimed == ["op-old", "op-new"]

    client.dispatches.append(_live_dispatch(module, older, generation=1))
    module._on_live_dispatch(_shared_snapshot())
    assert updated == []
    assert client.outcomes[0]["outcome"] == "superseded"


def test_the_live_event_handler_ignores_a_queued_event_after_lease_loss(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, _ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_lease_lost")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: _shared_snapshot())
    client.dispatches.append(_live_dispatch(module, _live_request(module, bundle)))
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: False)

    module.LiveEventHandler().notify(None)

    assert updated == [] and client.outcomes == []


def test_a_live_return_request_for_another_session_is_never_claimed(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, _ipc, _bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_other_session")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    request = {
        "schemaVersion": 3, "target": "fusion360", "requestId": "op-ret",
        "operationId": "op-ret", "deliverySequence": 1, "sessionId": "another-session",
        "designId": "wgd-shared", "documentId": "fusion:doc-a",
        "instanceId": "instance-b", "expectedReturnStateHash": "sha256:state-b",
    }
    client.offered.append(
        module.wglink_live.Offer("op-ret", "request_return", 0, request, 1)
    )

    module._on_live_dispatch(_shared_snapshot())

    assert client.claimed == []


def test_a_live_return_request_exports_once_and_reports_applied(
    monkeypatch, tmp_path: Path
) -> None:
    module, _ui, _ipc, _bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_return")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    sent: list[dict] = []
    monkeypatch.setattr(module.wglink_send, "send", lambda _app, options: sent.append(dict(options)))
    monkeypatch.setattr(module.wglink_workspace, "return_folder", lambda: tmp_path / "workspace")
    request = {
        "schemaVersion": 3, "target": "fusion360", "requestId": "op-ret",
        "operationId": "op-ret", "deliverySequence": 1,
        "sessionId": module._watch_session_id, "designId": "wgd-shared",
        "documentId": "fusion:doc-a", "instanceId": "instance-b",
        "expectedReturnStateHash": "sha256:state-b",
    }
    client.dispatches.append(
        _live_dispatch(module, request, kind="request_return", generation=1)
    )

    module._on_live_dispatch(_shared_snapshot())

    assert [item["request_id"] for item in sent] == ["op-ret"]
    assert client.progress == [("op-ret", 1, module.wglink_live.STAGE_EXECUTING)]
    # A return request never changes the model, so it carries no evidence.
    assert client.outcomes == [{
        "operationId": "op-ret", "attemptGeneration": 1, "outcome": "applied",
        "evidence": None, "message": None,
    }]


def test_a_live_insert_past_its_ttl_is_refused_without_touching_the_model(
    monkeypatch, tmp_path: Path
) -> None:
    """The live path applies the file path's pre-mutation checks unchanged."""

    module, ui, _ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_ttl")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    inserted: list[object] = []
    monkeypatch.setattr(module.wglink_core, "insert", _recording(inserted))
    request = _live_request(
        module, bundle,
        expectedInstanceId="", expectedDocumentId="", expectedReturnStateHash="",
        requestedAt="2000-01-01T00:00:00Z",
        destination={"kind": "document", "value": "fusion:doc-a"},
    )
    client.dispatches.append(
        _live_dispatch(module, request, kind=module.wglink_live.KIND_INSERT_LINK)
    )

    module._on_live_dispatch(_shared_snapshot())

    assert inserted == []
    assert client.outcomes[0]["outcome"] == "refused"
    assert "expired" in ui.messages[0][1]


def test_an_offer_this_session_declines_stops_waking_the_main_thread(
    monkeypatch, tmp_path: Path
) -> None:
    """WG keeps offering it, so a declined offer must not cost a document read."""

    module, _ui, _ipc, _bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_declined")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    reads: list[int] = []
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: (reads.append(1), _shared_snapshot())[1])
    offer = module.wglink_live.Offer("op-ret", "request_return", 0, {
        "schemaVersion": 3, "target": "fusion360", "requestId": "op-ret",
        "operationId": "op-ret", "deliverySequence": 1, "sessionId": "another-session",
    }, 1)
    client.offered.append(offer)

    assert module._on_live_dispatch() is False
    assert len(reads) == 1

    # WG offers it again on the next poll: nothing is read a second time.
    client.offered.append(offer)
    assert module._on_live_dispatch() is False
    assert len(reads) == 1


def test_a_live_handoff_waits_when_no_wg_workspace_is_selected(
    monkeypatch, tmp_path: Path
) -> None:
    """Not a reason to refuse the request for good; a reason to wait and say so."""

    module, _ui, ipc, bundle = _live_module(monkeypatch, tmp_path, "WGLink_live_no_workspace")
    client = _Dispatcher()
    monkeypatch.setattr(module, "_live_client", client)
    monkeypatch.setattr(module.wglink_workspace, "bundle_folder", lambda: None)
    updated: list[object] = []
    monkeypatch.setattr(module.wglink_core, "update", _recording(updated))
    client.dispatches.append(_live_dispatch(module, _live_request(module, bundle)))

    module._on_live_dispatch(_shared_snapshot())

    assert updated == [] and client.outcomes == []
    module._publish_fusion_status(_shared_snapshot())
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["liveDispatch"]["reason"] == "workspaceNotSelected"
