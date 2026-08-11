"""Fusion command shell for the head-less WGLink adapter."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback

import adsk.core


ADDIN_DIR = Path(__file__).resolve().parent
if str(ADDIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADDIN_DIR))

import wglink_core  # noqa: E402
import wglink_send  # noqa: E402
from wglink_bundle import format_measurement_mm  # noqa: E402


PANEL_ID = "hornlab_wglink_panel"
PANEL_NAME = "WGLink"
SETTINGS_PATH = Path.home() / ".hornlab" / "WGLink" / "settings.json"
COMMANDS = {
    "insert": (
        "hornlab_wglink_insert",
        "Insert",
        "Insert the full WG viewport model from a .wglink bundle.",
    ),
    "update": (
        "hornlab_wglink_update",
        "Update",
        "Rebuild a managed link in place from its current bundle.",
    ),
    "audit": (
        "hornlab_wglink_audit",
        "Audit",
        "Inspect link identity, parameter drift, tag state, and feature health.",
    ),
    "send": (
        "hornlab_wglink_send",
        "Send to WG",
        "Export the displayed acoustic assembly as a validated .wgreturn bundle.",
    ),
    "relink": (
        "hornlab_wglink_relink",
        "Relink",
        "Point a managed link at a moved or renamed .wglink bundle.",
    ),
    "detach": (
        "hornlab_wglink_detach",
        "Detach",
        "Remove WGLink attributes without changing bodies or features.",
    ),
}

_handlers: list[object] = []
_definitions: list[object] = []
_controls: list[object] = []
_panel = None


def _app() -> object:
    return adsk.core.Application.get()


def _ui() -> object | None:
    app = _app()
    return app.userInterface if app else None


def _message(text: str, title: str = PANEL_NAME) -> None:
    ui = _ui()
    if ui:
        ui.messageBox(text, title)


def _load_settings() -> dict[str, object]:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - settings are optional
        return {}
    return value if isinstance(value, dict) else {}


def _save_settings(settings: dict[str, object]) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - a picker preference must not fail a command
        pass


def _choose_bundle(title: str, source_kind: str) -> str | None:
    ui = _ui()
    if ui is None:
        return None
    settings = _load_settings()
    previous = Path(str(settings.get("last_bundle_folder", Path.home()))).expanduser()
    initial = previous if previous.is_dir() else previous.parent
    if source_kind == "Zipped .wglink file":
        dialog = ui.createFileDialog()
        dialog.title = title
        dialog.isMultiSelectEnabled = False
        dialog.filter = "WGLink bundles (*.wglink);;All files (*.*)"
        if initial.exists():
            dialog.initialDirectory = str(initial)
        if dialog.showOpen() != adsk.core.DialogResults.DialogOK:
            return None
        selected = Path(dialog.filename)
    else:
        dialog = ui.createFolderDialog()
        dialog.title = title
        if initial.exists():
            dialog.initialDirectory = str(initial)
        if dialog.showDialog() != adsk.core.DialogResults.DialogOK:
            return None
        selected = Path(dialog.folder)
    settings["last_bundle_folder"] = str(selected.parent)
    _save_settings(settings)
    return str(selected)


def _choose_return_folder(command_inputs: object) -> None:
    ui = _ui()
    if ui is None:
        return
    current = Path(str(_input_value(command_inputs, "output_folder") or Path.home())).expanduser()
    dialog = ui.createFolderDialog()
    dialog.title = "Select the WG return output folder"
    initial = current if current.is_dir() else current.parent
    if initial.exists():
        dialog.initialDirectory = str(initial)
    if dialog.showDialog() != adsk.core.DialogResults.DialogOK:
        return
    target = _input(command_inputs, "output_folder")
    if target is not None:
        target.value = str(dialog.folder)


def _input(command_inputs: object, name: str) -> object | None:
    try:
        return command_inputs.itemById(name)
    except Exception:  # noqa: BLE001
        return None


def _input_value(command_inputs: object, name: str) -> object | None:
    item = _input(command_inputs, name)
    return item.value if item else None


def _selected_name(command_inputs: object, name: str, default: str) -> str:
    item = _input(command_inputs, name)
    try:
        return str(item.selectedItem.name)
    except Exception:  # noqa: BLE001
        return default


def _command_options(command_inputs: object) -> dict[str, object]:
    result: dict[str, object] = {}
    instance = str(_input_value(command_inputs, "instance_id") or "").strip()
    if instance:
        result["instance_id"] = instance
    force = _input_value(command_inputs, "force")
    if force is not None:
        result["force"] = bool(force)
    fallback = _input_value(command_inputs, "allow_root_fallback")
    if fallback is not None:
        result["allow_root_fallback"] = bool(fallback)
    return result


def _send_selection(command_inputs: object) -> object:
    item = _input(command_inputs, "send_selection")
    try:
        if item.selectionCount:
            return item.selection(0).entity
    except Exception:  # noqa: BLE001
        pass
    return "root"


def _send_options(command_inputs: object) -> dict[str, object]:
    options: dict[str, object] = {
        "selection": _send_selection(command_inputs),
        "output_folder": str(_input_value(command_inputs, "output_folder") or "").strip(),
        "overwrite": bool(_input_value(command_inputs, "overwrite")),
    }
    anchor_input = _input(command_inputs, "anchor_instance_id")
    try:
        if anchor_input.isVisible:
            anchor = str(anchor_input.selectedItem.name).strip()
            if anchor:
                options["anchor_instance_id"] = anchor
    except Exception:  # noqa: BLE001
        pass
    return options


def _sync_anchor_choices(command_inputs: object) -> None:
    anchor = _input(command_inputs, "anchor_instance_id")
    if anchor is None:
        return
    try:
        report = wglink_send.inspect_scope(
            _app(), {"selection": _send_selection(command_inputs)}
        )
        instance_ids = list(report.get("instance_ids", []))
    except Exception:  # noqa: BLE001 - execute will present the actionable refusal
        instance_ids = []
    anchor.isVisible = len(instance_ids) > 1
    try:
        previous = str(anchor.selectedItem.name) if anchor.selectedItem else None
    except Exception:  # noqa: BLE001
        previous = None
    try:
        anchor.listItems.clear()
        for index, instance_id in enumerate(instance_ids):
            anchor.listItems.add(str(instance_id), str(instance_id) == previous or (previous is None and index == 0))
    except Exception:  # noqa: BLE001
        pass


def _tag_summary(tag: object) -> str:
    if not isinstance(tag, dict):
        return "Tag: not reported"
    return (
        f"Tag: {tag.get('role', '?')} on {tag.get('tagged_faces', 0)} face(s), "
        f"{float(tag.get('area_mm2', 0.0)):.3f} mm²"
    )


def _warnings(report: dict[str, object]) -> str:
    warnings = report.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        return ""
    return "\n\nWarnings:\n- " + "\n- ".join(str(item) for item in warnings)


def _summary(operation: str, report: dict[str, object]) -> str:
    if operation == "insert":
        deviation = report.get("deviation", {})
        return (
            f"Inserted WGLink instance {report.get('instance_id', '?')}.\n"
            f"Wrapper: {report.get('wrapper', '?')}\n"
            f"{_tag_summary(report.get('tag'))}\n"
            f"Deviation: mean {format_measurement_mm(deviation.get('mean_mm'))}, "
            f"max {format_measurement_mm(deviation.get('max_mm'))}"
            f"{_warnings(report)}"
        )
    if operation == "update":
        deviation = report.get("deviation", {})
        return (
            "WGLink update finished.\n"
            f"Fit points moved: {report.get('fit_points_moved', 0)}\n"
            f"Sections done: {report.get('sections_done', 0)}\n"
            f"Health regressions: {len(report.get('regressed', []))}\n"
            f"{_tag_summary(report.get('tag'))}\n"
            f"Deviation max: {format_measurement_mm(deviation.get('max_mm'))}"
            f"{_warnings(report)}"
        )
    if operation == "audit":
        return (
            "WGLink audit finished.\n"
            f"Link state: {report.get('link_state', '?')}\n"
            f"Local body evidence: {report.get('local_body_state', '?')}\n"
            f"Parameter drift: {len(report.get('parameter_drift', []))}\n"
            f"Unhealthy features: {len(report.get('regressed', []))}\n"
            f"{_tag_summary(report.get('tag'))}\n\n"
            f"{report.get('direct_reference_limit', '')}"
            f"{_warnings(report)}"
        )
    if operation == "send":
        scope = report.get("scope", {})
        status = scope.get("status", "?") if isinstance(scope, dict) else "?"
        message = (
            f"Return bundle written: {report.get('bundle_path', '?')}\n"
            f"Return ID: {report.get('return_id', '?')}\n"
            f"Scope: {status}\n"
            f"Sources: {len(report.get('sources', []))}"
        )
        if status == "degraded" and isinstance(scope, dict):
            skipped = scope.get("skipped", [])
            names = []
            if isinstance(skipped, list):
                for record in skipped:
                    if not isinstance(record, dict) or record.get("kind") == "construction":
                        continue
                    names.append(
                        str(record.get("path") or record.get("name") or record.get("object_id") or "unnamed body")
                    )
            message += "\n\nDEGRADED EXPORT — skipped bodies:\n- " + "\n- ".join(names or ["none reported"])
        return message
    if operation == "relink":
        return (
            f"Relinked WGLink instance {report.get('instance_id', '?')}.\n"
            f"Bundle: {report.get('bundle_path', '?')}"
            f"{_warnings(report)}"
        )
    return (
        f"Detached WGLink instance {report.get('instance_id', '?')}.\n"
        f"Attributes removed: {report.get('attributes_removed', 0)}\n"
        "Bodies and features changed: 0"
        f"{_warnings(report)}"
    )


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, operation: str):
        super().__init__()
        self.operation = operation

    def notify(self, args: object) -> None:
        try:
            adsk.doEvents()
            inputs = args.command.commandInputs
            options = _command_options(inputs)
            if self.operation == "insert":
                source_kind = _selected_name(inputs, "bundle_source", "Bundle folder")
                path = _choose_bundle("Select a .wglink bundle to insert", source_kind)
                if path is None:
                    return
                report = wglink_core.insert(_app(), path, options)
            elif self.operation == "update":
                report = wglink_core.update(_app(), None, options)
            elif self.operation == "audit":
                report = wglink_core.audit(_app(), options)
            elif self.operation == "send":
                send_options = _send_options(inputs)
                output_folder = str(send_options.get("output_folder", ""))
                if output_folder:
                    settings = _load_settings()
                    settings["last_return_folder"] = output_folder
                    _save_settings(settings)
                report = wglink_send.send(_app(), send_options)
            elif self.operation == "relink":
                source_kind = _selected_name(inputs, "bundle_source", "Bundle folder")
                path = _choose_bundle("Select the relocated .wglink bundle", source_kind)
                if path is None:
                    return
                report = wglink_core.relink(_app(), path, options)
            else:
                report = wglink_core.detach(_app(), options)
            _message(_summary(self.operation, report))
        except wglink_core.WgLinkError as exc:
            _message(str(exc), "WGLink refused")
        except Exception:  # noqa: BLE001 - UI boundary; core remains head-less
            _message(traceback.format_exc(), "WGLink error")


class CommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args: object) -> None:
        try:
            changed = getattr(args, "input", None)
            input_id = str(getattr(changed, "id", ""))
            inputs = args.inputs
            if input_id == "browse_output_folder":
                _choose_return_folder(inputs)
                changed.value = False
            elif input_id == "send_selection":
                _sync_anchor_choices(inputs)
        except Exception:  # noqa: BLE001 - execute remains the validation boundary
            pass


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, operation: str):
        super().__init__()
        self.operation = operation

    def notify(self, args: object) -> None:
        try:
            inputs = args.command.commandInputs
            if self.operation == "send":
                selection = inputs.addSelectionInput(
                    "send_selection",
                    "Assembly scope",
                    "Leave empty for the root, or select one occurrence subtree.",
                )
                selection.addSelectionFilter("Occurrences")
                selection.setSelectionLimits(0, 1)
                settings = _load_settings()
                default_folder = str(settings.get("last_return_folder", Path.home()))
                inputs.addStringValueInput(
                    "output_folder", "Output folder", default_folder
                )
                inputs.addBoolValueInput(
                    "browse_output_folder", "Browse output folder", False, "", False
                )
                anchor = inputs.addDropDownCommandInput(
                    "anchor_instance_id",
                    "Solver anchor instance",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                anchor.isVisible = False
                inputs.addBoolValueInput(
                    "overwrite", "Replace an existing return bundle", True, "", False
                )
                _sync_anchor_choices(inputs)
                changed = CommandInputChangedHandler()
                args.command.inputChanged.add(changed)
                _handlers.append(changed)
            if self.operation in {"insert", "relink"}:
                source = inputs.addDropDownCommandInput(
                    "bundle_source",
                    "Bundle source",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                source.listItems.add("Bundle folder", True)
                source.listItems.add("Zipped .wglink file", False)
            if self.operation not in {"insert", "send"}:
                inputs.addStringValueInput(
                    "instance_id",
                    "Instance ID (optional)",
                    "",
                )
            if self.operation in {"update", "relink"}:
                inputs.addBoolValueInput(
                    "force",
                    "Force identity/sequence refusal",
                    True,
                    "",
                    False,
                )
            if self.operation == "insert":
                inputs.addBoolValueInput(
                    "allow_root_fallback",
                    "Allow Part Design root fallback",
                    True,
                    "",
                    True,
                )
            handler = CommandExecuteHandler(self.operation)
            args.command.execute.add(handler)
            _handlers.append(handler)
        except Exception:  # noqa: BLE001
            _message(traceback.format_exc(), "WGLink command error")


def _delete_quietly(entity: object) -> None:
    try:
        if entity and entity.isValid:
            entity.deleteMe()
    except Exception:  # noqa: BLE001 - tolerate duplicate/stale add-in registrations
        pass


def _workspace(ui: object) -> object:
    workspace = ui.workspaces.itemById("FusionSolidEnvironment")
    if workspace is None:
        raise RuntimeError("Could not find Fusion's Design workspace.")
    return workspace


def run(_context: object) -> None:
    global _panel
    try:
        ui = _ui()
        if ui is None:
            return
        workspace = _workspace(ui)
        stale_panel = workspace.toolbarPanels.itemById(PANEL_ID)
        if stale_panel:
            _delete_quietly(stale_panel)
        _panel = workspace.toolbarPanels.add(
            PANEL_ID,
            PANEL_NAME,
            "SolidScriptsAddinsPanel",
            False,
        )
        for operation, (command_id, name, description) in COMMANDS.items():
            stale = ui.commandDefinitions.itemById(command_id)
            if stale:
                _delete_quietly(stale)
            definition = ui.commandDefinitions.addButtonDefinition(
                command_id,
                name,
                description,
            )
            created = CommandCreatedHandler(operation)
            definition.commandCreated.add(created)
            _handlers.append(created)
            _definitions.append(definition)
            control = _panel.controls.addCommand(definition)
            _controls.append(control)
    except Exception:  # noqa: BLE001
        _message(traceback.format_exc(), "WGLink start error")


def stop(_context: object) -> None:
    global _panel
    try:
        for control in reversed(_controls):
            _delete_quietly(control)
        _controls.clear()
        for definition in reversed(_definitions):
            _delete_quietly(definition)
        _definitions.clear()
        ui = _ui()
        if ui:
            for command_id, _name, _description in COMMANDS.values():
                _delete_quietly(ui.commandDefinitions.itemById(command_id))
        _delete_quietly(_panel)
        _panel = None
        _handlers.clear()
    except Exception:  # noqa: BLE001
        _message(traceback.format_exc(), "WGLink stop error")
