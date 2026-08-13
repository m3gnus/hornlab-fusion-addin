"""Fusion command shell for the head-less WGLink adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import traceback
import uuid

import adsk.core


ADDIN_DIR = Path(__file__).resolve().parent
try:
    sys.path.remove(str(ADDIN_DIR))
except ValueError:
    pass
sys.path.insert(0, str(ADDIN_DIR))


def _load_local_workspace_module():
    """Load this registration's workspace helper, not Fusion's cached copy.

    Fusion can load two WGLink registrations in one Python process.  Bare
    imports are cached by module name, so a newer registration could otherwise
    inherit an older ``wglink_workspace`` that does not implement its API.
    """

    name = "wglink_workspace"
    path = ADDIN_DIR / f"{name}.py"
    cached = sys.modules.get(name)
    try:
        cached_path = Path(str(getattr(cached, "__file__", ""))).resolve()
    except (OSError, RuntimeError, ValueError):
        cached_path = None
    if cached_path == path.resolve() and hasattr(cached, "ipc_folder"):
        return cached

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module

import wglink_core  # noqa: E402
import wglink_send  # noqa: E402
import wglink_watch  # noqa: E402
wglink_workspace = _load_local_workspace_module()
from wglink_bundle import format_measurement_mm  # noqa: E402


PANEL_ID = "hornlab_wglink_panel"
PANEL_NAME = "WGLink"
BROWSE_FOLDER = "Browse for a bundle folder…"
BROWSE_ZIP = "Browse for a zipped .wglink file…"
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
# False while another registration of this add-in owns the panel. Fusion loads
# each registered path as its own module with its own globals, so two
# registrations of one add-in are two instances that cannot see each other's
# state -- only the panel they share.
_owned = False

WATCH_EVENT_ID = "hornlab_wglink_export_available"
WATCH_INTERVAL_SECONDS = 4.0
_watcher = wglink_watch.ExportWatcher()
_watch_event = None
_watch_handler = None
_watch_stop: threading.Event | None = None
_watch_thread: threading.Thread | None = None
_watch_session_id = str(uuid.uuid4())
_presence_event_id = f"hornlab_wglink_presence_{_watch_session_id}"
_presence_event = None
_presence_handler = None
_presence_stop: threading.Event | None = None
_presence_thread: threading.Thread | None = None
# Set while a WGLink command runs. The heartbeat keeps ticking, but the handler
# refuses to raise a prompt over a command that is mid-execution.
_command_busy = False
# The marker remains on disk when Insert refuses, so remember that refusal for
# this Fusion session instead of showing the same error every four seconds. A
# later Send carries a new export id and is attempted normally.
_handoff_attempted_id: str | None = None
# A refused return request also remains on disk. Suppress repeated attempts for
# that exact request while allowing a later request id through normally.
_return_request_attempted_id: str | None = None


def _fingerprint_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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


def _discovered_bundles() -> list:
    try:
        return wglink_workspace.discover_bundles()
    except Exception:  # noqa: BLE001 - discovery is a convenience, never a gate
        return []


def _resolve_source(source_kind: str) -> tuple[str, str | None]:
    """Turn a dropdown choice into either a chosen bundle or a dialog to open.

    Returns ``(kind, path)``: a path means the workspace already answered the
    question, and ``None`` means fall through to the picker.
    """

    if source_kind in {BROWSE_FOLDER, BROWSE_ZIP}:
        return source_kind, None
    for bundle in _discovered_bundles():
        if bundle.label() == source_kind:
            return BROWSE_FOLDER, str(bundle.path)
    # A workspace that changed under an open dialog: ask rather than guess.
    return BROWSE_FOLDER, None


def _choose_bundle(title: str, source_kind: str) -> str | None:
    ui = _ui()
    if ui is None:
        return None
    settings = _load_settings()
    default_folder = wglink_workspace.bundle_folder()
    previous = Path(
        str(settings.get("last_bundle_folder", default_folder or Path.home()))
    ).expanduser()
    if not previous.is_dir() and default_folder is not None:
        previous = default_folder
    initial = previous if previous.is_dir() else previous.parent
    if source_kind == BROWSE_ZIP:
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


def _default_return_folder(settings: dict[str, object]) -> str:
    """Prefer WG's live workspace over Fusion's historical picker value."""

    workspace_return = wglink_workspace.return_folder()
    if workspace_return is not None:
        return str(workspace_return)
    return str(settings.get("last_return_folder", Path.home()))


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
            f"Sources: {len(report.get('sources', []))}\n\n"
            "Waveguide Generator will detect and open this return automatically."
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
        global _command_busy
        # Hold the watcher off: it must not open a prompt over a running
        # command, and an operation that rewrites a link's stored export id
        # would otherwise be surveyed halfway through.
        _command_busy = True
        try:
            adsk.doEvents()
            inputs = args.command.commandInputs
            options = _command_options(inputs)
            if self.operation == "insert":
                kind, chosen = _resolve_source(
                    _selected_name(inputs, "bundle_source", BROWSE_FOLDER)
                )
                path = chosen or _choose_bundle(
                    "Select a .wglink bundle to insert", kind
                )
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
                kind, chosen = _resolve_source(
                    _selected_name(inputs, "bundle_source", BROWSE_FOLDER)
                )
                path = chosen or _choose_bundle(
                    "Select the relocated .wglink bundle", kind
                )
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
        finally:
            _command_busy = False
            # This command may have moved the link on; re-survey from scratch so
            # a stale announcement cannot re-offer what was just applied.
            _watcher.reset()


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
                default_folder = _default_return_folder(settings)
                inputs.addStringValueInput(
                    "output_folder", "WG return folder", default_folder
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
                # Bundles already sitting in WG's workspace come first, so the
                # ordinary case needs no file dialog and no second copy of a
                # folder the user set once in WG.
                discovered = _discovered_bundles()
                for index, bundle in enumerate(discovered):
                    source.listItems.add(bundle.label(), index == 0)
                source.listItems.add(BROWSE_FOLDER, not discovered)
                source.listItems.add(BROWSE_ZIP, False)
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


def _document_links() -> list[dict[str, str]]:
    """Copy each managed link's identity out of the document as plain strings.

    Called on Fusion's main thread only. What it returns is deliberately inert
    data: the watcher runs on another thread and must never hold a live Fusion
    object.
    """

    app = _app()
    design = app.activeProduct if app else None
    if design is None or not isinstance(getattr(design, "objectType", ""), str):
        return []
    if "Design" not in str(design.objectType):
        return []
    try:
        records = wglink_core._link_records(design)
    except Exception:  # noqa: BLE001 - a document we cannot read has no links
        return []
    links: list[dict[str, str]] = []
    first_instance = next(iter(sorted(records)), None)
    try:
        return_state = wglink_send.return_state(
            app,
            {
                "selection": "root",
                **({"anchor_instance_id": first_instance} if first_instance else {}),
            },
        )
        document_signature_hash = str(return_state.get("hash") or "")
        document_body_count = str(return_state.get("body_count") or "")
        source_state_hash = str(return_state.get("source_hash") or "")
    except Exception:  # noqa: BLE001 - advisory heartbeat may omit the token
        document_signature_hash = ""
        document_body_count = ""
        source_state_hash = ""
    for instance_id, record in records.items():
        payload = record.get("payload") or {}
        try:
            stored_config = json.loads(str(payload.get("config_json") or ""))
            config_present = isinstance(stored_config, dict) and bool(stored_config)
        except (TypeError, ValueError):
            config_present = False
        try:
            parameter_expressions = json.loads(
                str(payload.get("parameter_expressions") or "{}")
            )
            parameter_count = (
                len(parameter_expressions)
                if isinstance(parameter_expressions, dict)
                else 0
            )
        except (TypeError, ValueError):
            parameter_count = 0
        try:
            parameter_drift_count = len(wglink_core._parameter_drift(design, record))
            local_body_state = wglink_core._local_body_state(record)
            body = record.get("body")
            body_fingerprint = (
                wglink_core._body_fingerprint(body) if body is not None else None
            )
        except Exception:  # noqa: BLE001 - advisory status may degrade to unknown
            parameter_drift_count = 0
            local_body_state = "unknown"
            body_fingerprint = None
        links.append({
            "instance_id": str(instance_id),
            "bundle_path": str(payload.get("bundle_path") or ""),
            "design_id": str(payload.get("design_id") or ""),
            "lineage_id": str(payload.get("lineage_id") or ""),
            "edit_version": str(payload.get("edit_version") or ""),
            "design_hash": str(payload.get("design_hash") or ""),
            "design_name": str(payload.get("design_name") or ""),
            "formula": str(payload.get("formula") or ""),
            "config_present": "true" if config_present else "false",
            "parameter_count": str(parameter_count),
            "parameter_drift_count": str(parameter_drift_count),
            "local_body_state": str(local_body_state),
            "body_fingerprint_hash": (
                _fingerprint_hash(body_fingerprint) if body_fingerprint else ""
            ),
            "document_signature_hash": document_signature_hash,
            "document_body_count": document_body_count,
            "source_state_hash": source_state_hash,
            "export_id": str(payload.get("export_id") or ""),
            "export_sequence": str(payload.get("export_sequence") or ""),
        })
    return links


def _active_document_id() -> str | None:
    app = _app()
    document = getattr(app, "activeDocument", None) if app else None
    if document is None:
        return None
    try:
        native_id = str(document.dataFile.id or "").strip()
        if native_id:
            return f"fusion:{native_id}"
    except Exception:  # noqa: BLE001 - unsaved local document
        pass
    return f"local:{_watch_session_id}:{id(document)}"


def _publish_fusion_status() -> None:
    """Best-effort presence for WG; every Fusion read stays on this thread."""

    folder = wglink_workspace.ipc_folder(create=True)
    if folder is None:
        return
    app = _app()
    product = app.activeProduct if app else None
    active_document = getattr(app, "activeDocument", None) if app else None
    if active_document is None and product is None:
        document_name = None
        links: list[dict[str, str]] = []
    else:
        document_name = str(getattr(active_document, "name", "") or "Untitled")
        links = _document_links()
    try:
        wglink_watch.write_fusion_status(
            folder,
            session_id=_watch_session_id,
            document_name=document_name,
            document_id=_active_document_id(),
            links=links,
        )
    except Exception:  # noqa: BLE001 - presence must never block CAD commands
        pass


def _apply_announced_updates(announcements: list) -> None:
    applied, failed = [], []
    for announcement in announcements:
        try:
            wglink_core.update(_app(), announcement.bundle_path, {
                "instance_id": announcement.instance_id,
            })
        except Exception as exc:  # noqa: BLE001 - one bad link must not stop the rest
            failed.append(f"{announcement.instance_id}: {exc}")
            # Let the next tick offer it again rather than swallowing it.
            _watcher.forget(announcement.instance_id)
        else:
            applied.append(announcement.describe())
    lines = []
    if applied:
        lines.append("Updated:\n" + "\n".join(f"  • {item}" for item in applied))
    if failed:
        lines.append("Not updated:\n" + "\n".join(f"  • {item}" for item in failed))
    if lines:
        _message("\n\n".join(lines), f"{PANEL_NAME} update")


def _pending_handoff() -> wglink_watch.PendingHandoff | None:
    ipc = wglink_workspace.ipc_folder(create=True)
    bundles = wglink_workspace.bundle_folder()
    if ipc is None or bundles is None:
        return None
    return wglink_watch.read_pending_handoff(
        ipc / wglink_watch.HANDOFF_FILENAME,
        bundle_root=bundles,
    )


def _pending_return_request() -> wglink_watch.PendingReturnRequest | None:
    ipc = wglink_workspace.ipc_folder(create=True)
    if ipc is None:
        return None
    return wglink_watch.read_return_request(
        ipc / wglink_watch.RETURN_REQUEST_FILENAME,
        session_id=_watch_session_id,
    )


def _apply_pending_return_request() -> bool:
    """Export the current Fusion body/tags after an explicit WG request."""

    global _command_busy, _return_request_attempted_id
    request = _pending_return_request()
    if request is None:
        return False
    if _return_request_attempted_id == request.request_id:
        return True
    _return_request_attempted_id = request.request_id
    _command_busy = True
    try:
        if not request.design_id or not request.document_id or not request.instance_id:
            raise wglink_core.WgLinkError(
                "WG return request is missing an exact document and link target. Refresh CAD Link and try again."
            )
        if request.document_id != _active_document_id():
            raise wglink_core.WgLinkError(
                "The active Fusion document changed after WG requested the model. Reopen CAD Link and try again."
            )
        matching = [
            link for link in _document_links()
            if link.get("design_id") == request.design_id
            and link.get("instance_id") == request.instance_id
        ]
        if len(matching) != 1:
            raise wglink_core.WgLinkError(
                "The active Fusion document no longer contains the exact WG link requested by WG."
            )
        current_state_hash = str(matching[0].get("document_signature_hash") or "")
        if (
            request.expected_return_state_hash
            and current_state_hash != request.expected_return_state_hash
        ):
            raise wglink_core.WgLinkError(
                "The Fusion model changed after WG displayed its status. Refresh CAD Link and try again."
            )
        output = wglink_workspace.return_folder()
        if output is None:
            raise wglink_core.WgLinkError(
                "Waveguide Generator has no selected return workspace."
            )
        options: dict[str, object] = {
            "selection": "root",
            "output_folder": str(output),
            "overwrite": True,
            "request_id": request.request_id,
            "anchor_instance_id": request.instance_id,
        }
        wglink_send.send(_app(), options)
        wglink_watch.acknowledge_return_request(request)
    except wglink_core.WgLinkError as exc:
        _message(str(exc), "WGLink return to WG refused")
    except Exception:  # noqa: BLE001 - main-thread add-in boundary
        _message(traceback.format_exc(), "WGLink return to WG error")
    finally:
        _command_busy = False
    return True


def _design_ready() -> bool:
    app = _app()
    design = app.activeProduct if app else None
    return bool(design and "Design" in str(getattr(design, "objectType", "")))


def _ensure_design_ready() -> bool:
    """Create a Fusion Design document for an explicit WG handoff if needed."""

    if _design_ready():
        return True
    app = _app()
    if app is None:
        return False
    try:
        app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    except Exception as exc:  # noqa: BLE001 - surfaced as an actionable refusal
        raise wglink_core.WgLinkError(
            f"Could not create a Fusion Design document for this waveguide: {exc}"
        ) from exc
    return _design_ready()


def _apply_pending_handoff() -> bool:
    """Insert or update the bundle named by WG's explicit CAD action."""

    global _command_busy, _handoff_attempted_id
    handoff = _pending_handoff()
    if handoff is None:
        return False

    if _handoff_attempted_id == handoff.export_id:
        return True
    try:
        ready = _ensure_design_ready()
    except wglink_core.WgLinkError as exc:
        _handoff_attempted_id = handoff.export_id
        _message(str(exc), "WGLink automatic open refused")
        return True
    if not ready:
        return False

    links = _document_links()
    try:
        pending_path = Path(handoff.bundle_path).resolve()
        linked = [
            link
            for link in links
            if (
                handoff.design_id
                and link.get("design_id") == handoff.design_id
            ) or (
                not handoff.design_id
                and link.get("bundle_path")
                and Path(link.get("bundle_path") or "").expanduser().resolve()
                == pending_path
            )
        ]
    except OSError:
        linked = []

    _handoff_attempted_id = handoff.export_id
    _command_busy = True
    try:
        if handoff.expected_document_id:
            if handoff.expected_document_id != _active_document_id():
                raise wglink_core.WgLinkError(
                    "The active Fusion document changed after WG prepared this update. Refresh CAD Link and try again."
                )
            if len(linked) != 1:
                raise wglink_core.WgLinkError(
                    "The active Fusion document no longer contains exactly one matching WG link. Refresh CAD Link and try again."
                )
        if linked:
            # One design identity can appear more than once in an assembly.
            # An explicit WG update targets the first active-document instance;
            # never silently rewrite every copy.
            link = linked[0]
            current_state_hash = str(link.get("document_signature_hash") or "")
            if (
                handoff.expected_return_state_hash
                and current_state_hash != handoff.expected_return_state_hash
            ):
                raise wglink_core.WgLinkError(
                    "The Fusion model changed after WG prepared this update. Refresh CAD Link and choose a sync direction again."
                )
            if link.get("export_id") != handoff.export_id:
                wglink_core.update(
                    _app(),
                    handoff.bundle_path,
                    {"instance_id": link["instance_id"]},
                )
        else:
            wglink_core.insert(
                _app(), handoff.bundle_path, {"allow_root_fallback": True}
            )
        wglink_watch.acknowledge_handoff(
            handoff,
            bundle_root=wglink_workspace.bundle_folder(),
        )
        _watcher.reset()
        # An automatic send already has two visible success signals: the model
        # appears and the browser reports the completed bundle. A modal report
        # here blocked Fusion on every send and made the ordinary Part Design
        # root-fallback warning look like an error. Keep detailed reports for
        # the manual Insert command; automatic success stays silent. Genuine
        # refusals and unexpected failures below still demand attention.
    except wglink_core.WgLinkError as exc:
        operation = "update" if linked else "insert"
        _message(str(exc), f"WGLink automatic {operation} refused")
    except Exception:  # noqa: BLE001 - main-thread add-in boundary
        operation = "update" if linked else "insert"
        _message(traceback.format_exc(), f"WGLink automatic {operation} error")
    finally:
        _command_busy = False
    return True


def _on_watch_tick() -> None:
    """Main-thread half of the watcher: survey, ask, and apply if asked."""

    global _command_busy
    if _command_busy:
        return
    _publish_fusion_status()
    try:
        if _apply_pending_handoff():
            return
        if _apply_pending_return_request():
            return
        announcements = _watcher.survey(_document_links())
        if not announcements:
            return
        ui = _ui()
        if ui is None:
            return
        _command_busy = True
        try:
            answer = ui.messageBox(
                wglink_watch.prompt_text(announcements),
                f"{PANEL_NAME} — newer export available",
                adsk.core.MessageBoxButtonTypes.YesNoButtonType,
                adsk.core.MessageBoxIconTypes.QuestionIconType,
            )
            if answer == adsk.core.DialogResults.DialogYes:
                _apply_announced_updates(announcements)
        except Exception:  # noqa: BLE001
            _message(traceback.format_exc(), "WGLink watch error")
        finally:
            _command_busy = False
    finally:
        _publish_fusion_status()


class WatchEventHandler(adsk.core.CustomEventHandler):
    def notify(self, _args: object) -> None:
        try:
            _on_watch_tick()
        except Exception:  # noqa: BLE001
            _message(traceback.format_exc(), "WGLink watch error")


class PresenceEventHandler(adsk.core.CustomEventHandler):
    """Publish status for a registration that adopted another one's panel."""

    def notify(self, _args: object) -> None:
        try:
            _publish_fusion_status()
        except Exception:  # noqa: BLE001 - presence is advisory
            pass


def _watch_loop(app: object, stop: threading.Event) -> None:
    """Heartbeat only. Every Fusion call belongs to the event handler."""

    while not stop.wait(WATCH_INTERVAL_SECONDS):
        try:
            app.fireCustomEvent(WATCH_EVENT_ID)
        except Exception:  # noqa: BLE001 - a torn-down event ends the thread
            return


def _presence_loop(app: object, stop: threading.Event) -> None:
    """Raise only this registration's private presence event."""

    while not stop.wait(WATCH_INTERVAL_SECONDS):
        try:
            app.fireCustomEvent(_presence_event_id)
        except Exception:  # noqa: BLE001 - a torn-down event ends the thread
            return


def _start_presence(app: object) -> None:
    """Keep WG online when an older duplicate owns the command panel."""

    global _presence_event, _presence_handler, _presence_stop, _presence_thread
    _presence_event = app.registerCustomEvent(_presence_event_id)
    if _presence_event is None:
        return
    _presence_handler = PresenceEventHandler()
    _presence_event.add(_presence_handler)
    _presence_stop = threading.Event()
    _presence_thread = threading.Thread(
        target=_presence_loop,
        args=(app, _presence_stop),
        name="WGLinkPresence",
        daemon=True,
    )
    _presence_thread.start()
    _publish_fusion_status()


def _stop_presence(app: object) -> None:
    global _presence_event, _presence_handler, _presence_stop, _presence_thread
    if _presence_stop is not None:
        _presence_stop.set()
    if _presence_thread is not None and _presence_thread.is_alive():
        _presence_thread.join(timeout=WATCH_INTERVAL_SECONDS + 1)
    if _presence_event is not None and _presence_handler is not None:
        try:
            _presence_event.remove(_presence_handler)
        except Exception:  # noqa: BLE001
            pass
    try:
        if app:
            app.unregisterCustomEvent(_presence_event_id)
    except Exception:  # noqa: BLE001
        pass
    folder = wglink_workspace.ipc_folder()
    if folder is not None:
        try:
            wglink_watch.remove_fusion_status(folder, session_id=_watch_session_id)
        except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
            pass
    _presence_event = _presence_handler = _presence_stop = _presence_thread = None


def _start_watch(app: object) -> None:
    global _watch_event, _watch_handler, _watch_stop, _watch_thread
    global _handoff_attempted_id, _return_request_attempted_id
    _watcher.reset()
    _handoff_attempted_id = None
    _return_request_attempted_id = None
    try:
        app.unregisterCustomEvent(WATCH_EVENT_ID)
    except Exception:  # noqa: BLE001 - no stale registration to clear
        pass
    _watch_event = app.registerCustomEvent(WATCH_EVENT_ID)
    if _watch_event is None:
        return
    _watch_handler = WatchEventHandler()
    _watch_event.add(_watch_handler)
    _watch_stop = threading.Event()
    _watch_thread = threading.Thread(
        target=_watch_loop,
        args=(app, _watch_stop),
        name="WGLinkExportWatch",
        daemon=True,
    )
    _watch_thread.start()
    _publish_fusion_status()


def _stop_watch(app: object) -> None:
    global _watch_event, _watch_handler, _watch_stop, _watch_thread
    global _handoff_attempted_id, _return_request_attempted_id
    if _watch_stop is not None:
        _watch_stop.set()
    if _watch_thread is not None and _watch_thread.is_alive():
        _watch_thread.join(timeout=WATCH_INTERVAL_SECONDS + 1)
    if _watch_event is not None and _watch_handler is not None:
        try:
            _watch_event.remove(_watch_handler)
        except Exception:  # noqa: BLE001
            pass
    try:
        if app:
            app.unregisterCustomEvent(WATCH_EVENT_ID)
    except Exception:  # noqa: BLE001
        pass
    folder = wglink_workspace.ipc_folder()
    if folder is not None:
        try:
            wglink_watch.remove_fusion_status(
                folder, session_id=_watch_session_id
            )
        except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
            pass
    _watch_event = _watch_handler = _watch_stop = _watch_thread = None
    _handoff_attempted_id = None
    _return_request_attempted_id = None
    _watcher.reset()


def _icon_folder(operation: str) -> str:
    """Fusion's per-command resource folder, or '' to fall back to no icon.

    Passing a path that holds no PNGs makes Fusion draw an empty button, so an
    incomplete checkout degrades to the unadorned text button it had before.
    """

    folder = ADDIN_DIR / "resources" / operation
    return str(folder) if (folder / "16x16.png").is_file() else ""


def _installed_control_count(panel: object) -> int:
    try:
        controls = panel.controls
    except Exception:  # noqa: BLE001 - an unreadable panel counts as not installed
        return 0
    # Fusion exposes count as an int property; a list-like collection exposes it
    # as a method, so only trust the attribute when it is already a number.
    count = getattr(controls, "count", None)
    if isinstance(count, int):
        return count
    try:
        return len(controls)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return 0


def run(_context: object) -> None:
    global _panel, _owned
    try:
        app = _app()
        ui = app.userInterface if app else None
        if ui is None:
            return
        workspace = _workspace(ui)
        stale_panel = workspace.toolbarPanels.itemById(PANEL_ID)
        if stale_panel and _installed_control_count(stale_panel):
            # Another registration of this same add-in already built the panel.
            # Rebuilding it here would delete definitions that instance owns and
            # leave the toolbar holding dead buttons, so adopt it and stay out of
            # the way. Nothing is torn down on stop either -- see _owned.
            _panel = stale_panel
            _owned = False
            _start_presence(app)
            return
        if stale_panel:
            _delete_quietly(stale_panel)
        _panel = workspace.toolbarPanels.add(
            PANEL_ID,
            PANEL_NAME,
            "SolidScriptsAddinsPanel",
            False,
        )
        # Claimed before the buttons go on, so a start that fails partway still
        # tears its own half-built panel down on stop.
        _owned = True
        for operation, (command_id, name, description) in COMMANDS.items():
            stale = ui.commandDefinitions.itemById(command_id)
            if stale:
                _delete_quietly(stale)
            definition = ui.commandDefinitions.addButtonDefinition(
                command_id,
                name,
                description,
                _icon_folder(operation),
            )
            created = CommandCreatedHandler(operation)
            definition.commandCreated.add(created)
            _handlers.append(created)
            _definitions.append(definition)
            control = _panel.controls.addCommand(definition)
            _controls.append(control)
        _start_watch(app)
    except Exception:  # noqa: BLE001
        _message(traceback.format_exc(), "WGLink start error")


def stop(_context: object) -> None:
    global _panel, _owned
    try:
        if not _owned:
            # A panel this instance adopted belongs to another registration.
            # Deleting the command definitions by id would strip that instance's
            # live buttons, which is how a duplicate registration used to leave
            # WGLink showing a panel whose commands did nothing.
            _stop_presence(_app())
            _panel = None
            return
        _stop_watch(_app())
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
        _owned = False
        _handlers.clear()
    except Exception:  # noqa: BLE001
        _message(traceback.format_exc(), "WGLink stop error")
