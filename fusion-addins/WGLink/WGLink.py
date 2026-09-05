"""Fusion command shell for the head-less WGLink adapter."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys
import threading
import time
import traceback
import types
import uuid

import adsk.core


ADDIN_DIR = Path(__file__).resolve().parent
_watch_session_id = str(uuid.uuid4())
_registration_package_name = (
    f"_hornlab_wglink_registration_{_watch_session_id.replace('-', '_')}"
)


def _load_registration_package() -> dict[str, types.ModuleType]:
    """Load every helper under this registration's private package namespace.

    Fusion caches ordinary imports process-wide even though it loads every
    registered add-in entry point as a separate module. A private package keeps
    a newer or older WGLink checkout from borrowing any executable helper from
    the registration that happened to start first.
    """

    package = types.ModuleType(_registration_package_name)
    package.__file__ = str(ADDIN_DIR / "__init__.py")
    package.__package__ = _registration_package_name
    package.__path__ = [str(ADDIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[_registration_package_name] = package
    loaded: dict[str, types.ModuleType] = {}
    for name in (
        "wglink_workspace",
        "wglink_author",
        "wglink_bundle",
        "wglink_core",
        "wglink_return",
        "wglink_send",
        "wglink_watch",
    ):
        loaded[name] = importlib.import_module(f"{_registration_package_name}.{name}")
    return loaded


_registration_modules = _load_registration_package()
wglink_workspace = _registration_modules["wglink_workspace"]
wglink_author = _registration_modules["wglink_author"]
wglink_core = _registration_modules["wglink_core"]
wglink_send = _registration_modules["wglink_send"]
wglink_watch = _registration_modules["wglink_watch"]
format_measurement_mm = _registration_modules["wglink_bundle"].format_measurement_mm


PANEL_ID = "hornlab_wglink_panel"
PANEL_NAME = "WGLink"
BROWSE_FOLDER = "Browse for a bundle folder…"
BROWSE_ZIP = "Browse for a zipped .wglink file…"
SETTINGS_PATH = Path.home() / ".hornlab" / "WGLink" / "settings.json"
COMMANDS = {
    "source": (
        "hornlab_wglink_set_source",
        "Set WG Source…",
        "Mark the selected faces as the LF, MF, HF, or PASSIVE_CARDIOID drive source.",
    ),
    "declare": (
        "hornlab_wglink_declare_body",
        "Declare Body…",
        "Classify a body as the exterior WG solves, or leave it out of the return.",
    ),
    "solve": (
        "hornlab_wglink_solve",
        "Solve in WG",
        "Export the assembly and ask Waveguide Generator to prepare and solve it.",
    ),
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
    "send": (
        "hornlab_wglink_send",
        "Send to WG",
        "Export the displayed acoustic assembly as a validated .wgreturn bundle.",
    ),
    "detach": (
        "hornlab_wglink_detach",
        "Detach",
        "Remove WGLink attributes without changing bodies or features.",
    ),
}
# The commands a user starts. Everything else is maintenance or recovery and
# lives under one dropdown; the automatic handoff already covers the ordinary
# Insert and Update.
#
# Set WG Source is promoted even though it is authoring rather than an export:
# a model with no painted source face cannot be sent at all, and the convention
# is invisible -- it used to be "rename a Fusion appearance to exactly HF".
# Filed under a dropdown named "Manage WG Link…" it would read as maintenance
# for an existing link, which is exactly what a from-scratch model does not
# have. Declare Body stays in the dropdown: it is the remedy for one refusal,
# and that refusal names it.
PROMOTED_COMMANDS = ("source", "solve", "send")

_handlers: list[object] = []
_definitions: list[object] = []
_controls: list[object] = []
_panel = None
# True only after this registration committed a complete panel + watcher
# transaction. The process-wide lease is separately checked before touching
# shared UI during shutdown, because an expired owner may already be replaced.
_owned = False

WATCH_INTERVAL_SECONDS = 4.0
OWNER_LEASE_SECONDS = WATCH_INTERVAL_SECONDS * 3
WATCH_EVENT_ID = f"hornlab_wglink_export_available_{_watch_session_id}"
_candidate_event_id = f"hornlab_wglink_owner_candidate_{_watch_session_id}"

# Executable add-in modules are registration-local. This deliberately tiny
# data-only broker is the one shared object: it serializes which registration
# may service WGLink's machine-local IPC inside this Fusion process. The lease
# is renewed by the owner's worker without making Fusion API calls.
_RUNTIME_MODULE_NAME = "_hornlab_wglink_ipc_owner_runtime_v1"
_runtime_candidate = types.ModuleType(_RUNTIME_MODULE_NAME)
_runtime_candidate.lock = threading.RLock()  # type: ignore[attr-defined]
_runtime_candidate.owner = None  # type: ignore[attr-defined]
_runtime_candidate.phase = None  # type: ignore[attr-defined]
_runtime_candidate.renewed_at = 0.0  # type: ignore[attr-defined]
_runtime_candidate.generation = 0  # type: ignore[attr-defined]
_runtime_candidate.watch_event_id = None  # type: ignore[attr-defined]
_ipc_owner_runtime = sys.modules.setdefault(_RUNTIME_MODULE_NAME, _runtime_candidate)
if not hasattr(_ipc_owner_runtime, "watch_event_id"):
    _ipc_owner_runtime.watch_event_id = None

_watcher = wglink_watch.ExportWatcher()
_watch_event = None
_watch_handler = None
_watch_stop: threading.Event | None = None
_watch_thread: threading.Thread | None = None
_candidate_event = None
_candidate_handler = None
_candidate_stop: threading.Event | None = None
_candidate_thread: threading.Thread | None = None
_replaced_watch_event_id: str | None = None
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


def _lease_now() -> float:
    return time.monotonic()


def _claim_ipc_lease() -> bool:
    """Atomically claim an absent or expired process-local IPC lease."""

    global _replaced_watch_event_id
    now = _lease_now()
    with _ipc_owner_runtime.lock:
        owner = _ipc_owner_runtime.owner
        expired = (
            owner is not None
            and now - float(_ipc_owner_runtime.renewed_at) > OWNER_LEASE_SECONDS
        )
        if owner not in (None, _watch_session_id) and not expired:
            return False
        if owner != _watch_session_id:
            _ipc_owner_runtime.generation += 1
            _replaced_watch_event_id = (
                str(_ipc_owner_runtime.watch_event_id)
                if _ipc_owner_runtime.watch_event_id
                else None
            )
        _ipc_owner_runtime.owner = _watch_session_id
        _ipc_owner_runtime.phase = "constructing"
        _ipc_owner_runtime.renewed_at = now
        _ipc_owner_runtime.watch_event_id = None
        return True


def _activate_ipc_lease() -> bool:
    """Commit the lease after panel construction and watcher registration."""

    with _ipc_owner_runtime.lock:
        if _ipc_owner_runtime.owner != _watch_session_id:
            return False
        _ipc_owner_runtime.phase = "active"
        _ipc_owner_runtime.renewed_at = _lease_now()
        _ipc_owner_runtime.watch_event_id = WATCH_EVENT_ID
        return True


def _renew_ipc_lease() -> bool:
    with _ipc_owner_runtime.lock:
        if (
            _ipc_owner_runtime.owner != _watch_session_id
            or _ipc_owner_runtime.phase != "active"
        ):
            return False
        _ipc_owner_runtime.renewed_at = _lease_now()
        return True


def _owns_ipc_lease() -> bool:
    with _ipc_owner_runtime.lock:
        return _ipc_owner_runtime.owner == _watch_session_id


def _owns_active_ipc_lease() -> bool:
    with _ipc_owner_runtime.lock:
        return (
            _ipc_owner_runtime.owner == _watch_session_id
            and _ipc_owner_runtime.phase == "active"
        )


def _release_ipc_lease() -> bool:
    with _ipc_owner_runtime.lock:
        if _ipc_owner_runtime.owner != _watch_session_id:
            return False
        _ipc_owner_runtime.owner = None
        _ipc_owner_runtime.phase = None
        _ipc_owner_runtime.renewed_at = 0.0
        _ipc_owner_runtime.watch_event_id = None
        return True


def _ipc_lease_snapshot() -> dict[str, object]:
    """A deterministic diagnostic surface for lifecycle tests and support."""

    with _ipc_owner_runtime.lock:
        return {
            "owner": _ipc_owner_runtime.owner,
            "phase": _ipc_owner_runtime.phase,
            "renewed_at": _ipc_owner_runtime.renewed_at,
            "generation": _ipc_owner_runtime.generation,
            "watch_event_id": _ipc_owner_runtime.watch_event_id,
        }


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


def _confirm_detach() -> bool:
    """Ask before irreversibly removing the selected link's WG identity."""

    ui = _ui()
    message_box = getattr(ui, "messageBox", None) if ui is not None else None
    try:
        yes_no = adsk.core.MessageBoxButtonTypes.YesNoButtonType
        question = adsk.core.MessageBoxIconTypes.QuestionIconType
        accepted = adsk.core.DialogResults.DialogYes
    except AttributeError:
        # A command shell without Fusion's modal API must fail closed. The
        # head-less core API remains available to callers that explicitly ask
        # for detach without going through this UI flow.
        return False
    if not callable(message_box):
        return False
    answer = message_box(
        "Detach permanently removes this link's WG identity. Geometry stays in "
        "the Fusion document, but it cannot be re-attached. The only way back "
        "is to insert a fresh copy from Waveguide Generator.\n\nDetach this link?",
        f"{PANEL_NAME} — confirm Detach",
        yes_no,
        question,
    )
    return answer == accepted


def _log(text: str) -> None:
    """Put diagnostics where diagnostics belong: Fusion's Text Commands palette."""

    try:
        palette = _ui().palettes.itemById("TextCommands")
    except Exception:  # noqa: BLE001 - an unavailable palette must not raise
        palette = None
    if palette is not None:
        try:
            palette.writeText(text)
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        print(text)
    except Exception:  # noqa: BLE001 - logging is never worth a second failure
        pass


def _report_error(action: str, title: str, exc: BaseException | None = None) -> None:
    """Log the traceback, show one human line.

    A raw ``traceback.format_exc()`` in a modal states the add-in's internals
    and nothing the user can act on, and the one sentence that identifies the
    failure is buried in the middle of it.
    """

    _log(f"[{PANEL_NAME}] {action}\n{traceback.format_exc()}")
    _message(wglink_author.failure_message(action, exc), title)


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


def _document_link_choices() -> list[tuple[str, str]]:
    """``(label, instance_id)`` for every managed link in the active document."""

    choices = []
    for link in _document_links():
        instance_id = str(link.get("instance_id") or "")
        if not instance_id:
            continue
        name = str(link.get("design_name") or "").strip()
        choices.append((f"{name} · {instance_id}" if name else instance_id, instance_id))
    return choices


def _command_options(command_inputs: object) -> dict[str, object]:
    result: dict[str, object] = {}
    chosen = _selected_name(command_inputs, "instance_choice", "")
    if chosen:
        # The label carries the design name for recognition; the id is the part
        # the head-less API takes.
        result["instance_id"] = chosen.rsplit(" · ", 1)[-1].strip()
    return result


def _send_selection(command_inputs: object) -> object:
    item = _input(command_inputs, "send_selection")
    try:
        if item.selectionCount:
            return item.selection(0).entity
    except Exception:  # noqa: BLE001
        pass
    return "root"


def _send_domain(command_inputs: object) -> tuple[str, ...]:
    """The declared model domain, defaulting to the full model.

    An unreadable dropdown falls back to the full model on purpose: that is the
    only reading that cannot turn a full model into a half behind the user's
    back.
    """

    item = _input(command_inputs, "model_domain")
    try:
        return wglink_author.resolve_domain_choice(str(item.selectedItem.name))
    except Exception:  # noqa: BLE001
        return ()


def _send_options(command_inputs: object) -> dict[str, object]:
    # WG only ingests from its own workspace, so there is one correct
    # destination and the UI no longer asks. Collision-safe naming is kept:
    # a return WG has not ingested yet is not in content-addressed storage,
    # so overwriting one loses evidence.
    output = wglink_workspace.return_folder()
    if output is None:
        raise wglink_core.WgLinkError(
            "Waveguide Generator has no selected CAD Link workspace. Choose one in "
            "WG under Settings → CAD Link, then send again."
        )
    options: dict[str, object] = {
        "selection": _send_selection(command_inputs),
        "output_folder": str(output),
        "overwrite": False,
        "capture_document": wglink_workspace.capture_document(),
        "domain": list(_send_domain(command_inputs)),
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


def _sync_preflight(command_inputs: object) -> None:
    """Refresh the read-only summary the Send and Solve dialogs show.

    Everything Fusion-specific happens here, on the main thread; the wording is
    composed by wglink_author from plain numbers.
    """

    box = _input(command_inputs, "preflight")
    if box is None:
        return
    try:
        options: dict[str, object] = {
            "selection": _send_selection(command_inputs),
            "domain": list(_send_domain(command_inputs)),
        }
        anchor = _input(command_inputs, "anchor_instance_id")
        try:
            if anchor is not None and anchor.isVisible and anchor.selectedItem:
                options["anchor_instance_id"] = str(anchor.selectedItem.name)
        except Exception:  # noqa: BLE001 - an untouched dropdown has no choice yet
            pass
        text = wglink_author.preflight_summary(
            wglink_send.preflight_scope(_app(), options)
        ).html()
    except Exception as exc:  # noqa: BLE001 - a preview never blocks the dialog
        text = wglink_author.preflight_unavailable(exc)
    for attribute in ("formattedText", "text"):
        try:
            setattr(box, attribute, text)
            return
        except Exception:  # noqa: BLE001 - older text boxes expose only one
            continue


def _sync_help(command_inputs: object, box_id: str, choice_id: str, text_of) -> None:
    box = _input(command_inputs, box_id)
    if box is None:
        return
    try:
        text = text_of(_selected_name(command_inputs, choice_id, ""))
    except wglink_author.AuthorError as exc:
        text = str(exc)
    for attribute in ("formattedText", "text"):
        try:
            setattr(box, attribute, text)
            return
        except Exception:  # noqa: BLE001
            continue


def _add_selection_filters(selection: object, *names: str) -> None:
    """Apply what this Fusion knows, rather than failing the whole dialog.

    A filter name an older Fusion does not recognise would otherwise raise
    before the execute handler is attached, leaving a button that does nothing.
    """

    for name in names:
        try:
            selection.addSelectionFilter(name)
        except Exception:  # noqa: BLE001
            pass


def _selected_entities(command_inputs: object, name: str) -> list[object]:
    item = _input(command_inputs, name)
    entities: list[object] = []
    try:
        for index in range(item.selectionCount):
            entities.append(item.selection(index).entity)
    except Exception:  # noqa: BLE001 - an empty selection is refused by the plan
        pass
    return entities


def _face_descriptors(faces: list[object]) -> list[dict[str, object]]:
    """Copy what the plan needs off live faces. Main thread only."""

    descriptors = []
    for index, face in enumerate(faces):
        try:
            area = float(face.area) * 100.0
        except Exception:  # noqa: BLE001 - an unread area only drops the total
            area = None
        descriptors.append({
            "index": index,
            "current_role": wglink_send._face_role(face),
            "area_mm2": area,
        })
    return descriptors


def _native(entity: object) -> object:
    # An occurrence proxy exposes an empty attribute collection, so a
    # declaration has to be written on the native body, which is also where the
    # export reads it back from.
    return getattr(entity, "nativeObject", None) or entity


def _body_descriptors(bodies: list[object]) -> list[dict[str, object]]:
    """Copy what the plan needs off live bodies. Main thread only."""

    descriptors = []
    for index, body in enumerate(bodies):
        descriptors.append({
            "index": index,
            "name": str(getattr(body, "name", "") or f"body {index + 1}"),
            "current": wglink_send.read_declaration(_native(body)),
            "body_kind": "solid" if bool(getattr(body, "isSolid", False)) else "surface",
        })
    return descriptors


def _apply_source_role(command_inputs: object) -> dict[str, object]:
    """Paint or strip the appearance that WG reads a source role from."""

    app = _app()
    design = wglink_core._design(app)
    faces = _selected_entities(command_inputs, "source_faces")
    plan = wglink_author.plan_source_assignment(
        _face_descriptors(faces),
        _selected_name(command_inputs, "source_role", wglink_author.DEFAULT_SOURCE_ROLE),
    )
    appearance = (
        wglink_core._named_appearance(app, design, plan.appearance_name)
        if plan.paint
        else None
    )
    for index in plan.paint:
        try:
            faces[index].appearance = appearance
        except Exception as exc:  # noqa: BLE001
            raise wglink_core.WgLinkError(
                f"Could not paint the {plan.role} appearance onto a selected face: {exc}."
            ) from exc
    for index in plan.clear:
        try:
            # None is how wglink_core strips a stray tag too: the face falls
            # back to the appearance its body carries.
            faces[index].appearance = None
        except Exception as exc:  # noqa: BLE001
            raise wglink_core.WgLinkError(
                f"Could not clear the WG source appearance from a selected face: {exc}."
            ) from exc
    return {"summary": plan.summary}


def _apply_body_declaration(command_inputs: object) -> dict[str, object]:
    """Write or remove the return classification the export scope reads."""

    bodies = _selected_entities(command_inputs, "declare_bodies")
    plan = wglink_author.plan_body_declaration(
        _body_descriptors(bodies),
        _selected_name(command_inputs, "declaration", ""),
    )
    for index in plan.write:
        wglink_send.declare_body(_native(bodies[index]), plan.declaration)
    for index in plan.clear:
        wglink_send.clear_declaration(_native(bodies[index]))
    return {"summary": plan.summary}


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
    if operation in {"source", "declare"}:
        # The plan already states what changed, in the plan's own words.
        return str(report.get("summary", ""))
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
    if operation in {"send", "solve"}:
        scope = report.get("scope", {})
        status = scope.get("status", "?") if isinstance(scope, dict) else "?"
        closing = (
            "Waveguide Generator will prepare this model and start solving it. "
            "It stops and asks if the ingestion reports something blocking."
            if report.get("solve_requested")
            else "Waveguide Generator will detect and open this return automatically."
        )
        message = (
            f"Return bundle written: {report.get('bundle_path', '?')}\n"
            f"Return ID: {report.get('return_id', '?')}\n"
            f"Scope: {status}\n"
            f"Sources: {len(report.get('sources', []))}\n\n"
            f"{closing}"
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
    return (
        f"Detached WGLink instance {report.get('instance_id', '?')}.\n"
        f"Attributes removed: {report.get('attributes_removed', 0)}\n"
        "Bodies and features changed: 0"
        f"{_warnings(report)}"
    )


def _request_wg_solve(report: dict[str, object]) -> bool:
    """Ask WG to prepare and solve the bundle this send just published.

    Written after the bundle, and only ever by this command: a plain Send must
    never start an expensive solve as a side effect, which is also why there is
    no remembered "solve next time" preference.
    """

    ipc = wglink_workspace.ipc_folder(create=True)
    workspace = wglink_workspace.workspace_root()
    if ipc is None or workspace is None:
        raise wglink_core.WgLinkError(
            "Waveguide Generator has no selected CAD Link workspace, so it cannot be asked to solve."
        )
    wglink_watch.write_solve_request(
        ipc,
        command_id=str(uuid.uuid4()),
        return_id=str(report.get("return_id") or ""),
        bundle_path=Path(str(report.get("bundle_path") or "")),
        workspace_root=workspace,
    )
    return True


def _show_export_progress(operation: str) -> object | None:
    """Open Fusion's modeless progress surface for the slow return path."""

    ui = _ui()
    factory = getattr(ui, "createProgressDialog", None) if ui is not None else None
    if not callable(factory):
        return None
    try:
        dialog = factory()
        dialog.isCancelButtonShown = False
        dialog.isBackgroundTranslucent = False
        dialog.show(
            "Solve in WG" if operation == "solve" else "Send to WG",
            "Surveying the assembly…",
            0,
            3,
            0,
        )
        return dialog
    except Exception:  # noqa: BLE001 - progress is advisory, export is not
        return None


def _update_export_progress(dialog: object | None, value: int, message: str) -> None:
    if dialog is None:
        return
    try:
        dialog.message = message
        dialog.progressValue = value
        adsk.doEvents()
    except Exception:  # noqa: BLE001 - an old Fusion progress API must not abort export
        pass


def _hide_export_progress(dialog: object | None) -> None:
    if dialog is None:
        return
    try:
        dialog.hide()
    except Exception:  # noqa: BLE001
        pass


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, operation: str):
        super().__init__()
        self.operation = operation

    def notify(self, args: object) -> None:
        global _command_busy
        progress = None
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
            elif self.operation in {"send", "solve"}:
                progress = _show_export_progress(self.operation)
                _update_export_progress(
                    progress, 1, "Exporting STEP and validating the return bundle…"
                )
                report = wglink_send.send(_app(), _send_options(inputs))
                if self.operation == "solve":
                    _update_export_progress(
                        progress, 2, "Return written. Asking Waveguide Generator to solve…"
                    )
                    report = dict(report)
                    report["solve_requested"] = _request_wg_solve(report)
                _update_export_progress(progress, 3, "Return ready in Waveguide Generator.")
            elif self.operation == "source":
                report = _apply_source_role(inputs)
            elif self.operation == "declare":
                report = _apply_body_declaration(inputs)
            elif self.operation == "detach":
                if not _confirm_detach():
                    return
                report = wglink_core.detach(_app(), options)
            else:
                raise RuntimeError(f"Unknown WGLink operation: {self.operation}")
            _message(_summary(self.operation, report))
        except (wglink_core.WgLinkError, wglink_author.AuthorError) as exc:
            _message(str(exc), "WGLink refused")
        except Exception as exc:  # noqa: BLE001 - UI boundary; core remains head-less
            _report_error(COMMANDS[self.operation][1], "WGLink error", exc)
        finally:
            _hide_export_progress(progress)
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
            if input_id == "send_selection":
                _sync_anchor_choices(inputs)
                _sync_preflight(inputs)
            elif input_id == "anchor_instance_id":
                _sync_preflight(inputs)
            elif input_id == "model_domain":
                _sync_help(
                    inputs,
                    "model_domain_help",
                    "model_domain",
                    wglink_author.domain_help_text,
                )
                _sync_preflight(inputs)
            elif input_id == "refresh_preflight":
                # Fusion body/occurrence visibility can change while this modal
                # is open without producing a command-input event. Re-survey on
                # an explicit read-only action so the displayed body inventory
                # and anchor choices cannot remain stale.
                _sync_anchor_choices(inputs)
                _sync_preflight(inputs)
                try:
                    changed.value = False
                except Exception:  # noqa: BLE001 - old Fusion buttons may be write-only
                    pass
            elif input_id == "source_role":
                _sync_help(
                    inputs, "source_help", "source_role", wglink_author.source_help_text
                )
            elif input_id == "declaration":
                _sync_help(
                    inputs,
                    "declaration_help",
                    "declaration",
                    wglink_author.declaration_help_text,
                )
        except Exception:  # noqa: BLE001 - execute remains the validation boundary
            pass


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, operation: str):
        super().__init__()
        self.operation = operation

    def notify(self, args: object) -> None:
        try:
            inputs = args.command.commandInputs
            if self.operation in {"send", "solve"}:
                selection = inputs.addSelectionInput(
                    "send_selection",
                    "Assembly scope",
                    "Leave empty for the root, or select one occurrence subtree.",
                )
                selection.addSelectionFilter("Occurrences")
                selection.setSelectionLimits(0, 1)
                # The return folder is WG's setting, like the bundle folder:
                # WG only ingests from its own workspace, so a return written
                # anywhere else is invisible to it. Head-less callers may still
                # pass output_folder and overwrite.
                anchor = inputs.addDropDownCommandInput(
                    "anchor_instance_id",
                    "Solver anchor instance",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                anchor.isVisible = False
                _sync_anchor_choices(inputs)
                # A model the author already cut has to say so: WG cannot tell a
                # deliberate half from an open shell, and solving one as the
                # other is a wrong answer rather than an error.
                domain = inputs.addDropDownCommandInput(
                    "model_domain",
                    "Model domain",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                for index, choice in enumerate(wglink_author.domain_choices()):
                    domain.listItems.add(choice, index == 0)
                inputs.addTextBoxCommandInput(
                    "model_domain_help",
                    "",
                    wglink_author.domain_help_text(wglink_author.domain_choices()[0]),
                    3,
                    True,
                )
                # State the export before it happens: what goes in, whether it
                # is linked, which sources drive it, and -- for an unlinked
                # model, whose assembly frame WG solves as-is -- how far the
                # model sits from that frame. A missing source used to be a
                # dead-end modal raised after the user pressed OK.
                inputs.addTextBoxCommandInput("preflight", "Pre-flight", "", 8, True)
                _sync_preflight(inputs)
                inputs.addBoolValueInput(
                    "refresh_preflight",
                    "Refresh body inventory",
                    False,
                    "",
                    False,
                )
                changed = CommandInputChangedHandler()
                args.command.inputChanged.add(changed)
                _handlers.append(changed)
            if self.operation == "source":
                faces = inputs.addSelectionInput(
                    "source_faces",
                    "Faces",
                    "Select the face or faces that drive this source.",
                )
                _add_selection_filters(faces, "Faces")
                faces.setSelectionLimits(1, 0)
                role = inputs.addDropDownCommandInput(
                    "source_role",
                    "Source role",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                for choice in wglink_author.source_choices():
                    role.listItems.add(
                        choice, choice == wglink_author.DEFAULT_SOURCE_ROLE
                    )
                inputs.addTextBoxCommandInput(
                    "source_help",
                    "",
                    wglink_author.source_help_text(wglink_author.DEFAULT_SOURCE_ROLE),
                    2,
                    True,
                )
                changed = CommandInputChangedHandler()
                args.command.inputChanged.add(changed)
                _handlers.append(changed)
            if self.operation == "declare":
                bodies = inputs.addSelectionInput(
                    "declare_bodies",
                    "Bodies",
                    "Select the bodies to classify for the WG return.",
                )
                _add_selection_filters(
                    bodies, "SolidBodies", "SurfaceBodies", "MeshBodies"
                )
                bodies.setSelectionLimits(1, 0)
                declaration = inputs.addDropDownCommandInput(
                    "declaration",
                    "Declaration",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                for index, choice in enumerate(wglink_author.declaration_choices()):
                    declaration.listItems.add(choice, index == 0)
                inputs.addTextBoxCommandInput(
                    "declaration_help",
                    "",
                    wglink_author.declaration_help_text(
                        wglink_author.declaration_choices()[0]
                    ),
                    3,
                    True,
                )
                changed = CommandInputChangedHandler()
                args.command.inputChanged.add(changed)
                _handlers.append(changed)
            if self.operation == "insert":
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
            if self.operation not in {"insert", "send", "solve", "source", "declare"}:
                # A document with one link needs no choice, and the old free
                # text field required a separate diagnostic call just to learn
                # the id.
                links = _document_link_choices()
                if len(links) > 1:
                    chooser = inputs.addDropDownCommandInput(
                        "instance_choice",
                        "Managed link",
                        adsk.core.DropDownStyles.TextListDropDownStyle,
                    )
                    for index, (label, _instance_id) in enumerate(links):
                        chooser.listItems.add(label, index == 0)
            # force and allow_root_fallback stay head-less-only: every refusal
            # names the recovery path, and the root fallback already warns in
            # its report.
            handler = CommandExecuteHandler(self.operation)
            args.command.execute.add(handler)
            _handlers.append(handler)
        except Exception as exc:  # noqa: BLE001
            _report_error("Building a WGLink dialog", "WGLink command error", exc)


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


def _document_links() -> list[dict[str, object]]:
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
    links: list[dict[str, object]] = []
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
        raw_instance_identities = return_state.get("instance_identities")
        instance_identities = (
            raw_instance_identities
            if isinstance(raw_instance_identities, dict)
            else {}
        )
    except Exception:  # noqa: BLE001 - advisory heartbeat may omit the token
        document_signature_hash = ""
        document_body_count = ""
        source_state_hash = ""
        instance_identities = {}
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
            parameter_drift = wglink_core._parameter_drift(design, record)
            drifted_parameters = sorted(
                str(item["name"]) for item in parameter_drift
            )
            local_body_state = wglink_core._local_body_state(record)
            body = record.get("body")
            body_fingerprint = (
                wglink_core._body_fingerprint(body) if body is not None else None
            )
        except Exception:  # noqa: BLE001 - advisory status may degrade to unknown
            drifted_parameters = []
            local_body_state = "unknown"
            body_fingerprint = None
        link = {
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
            "parameter_drift_count": str(len(drifted_parameters)),
            "drifted_parameters": drifted_parameters,
            "local_body_state": str(local_body_state),
            "body_fingerprint_hash": (
                _fingerprint_hash(body_fingerprint) if body_fingerprint else ""
            ),
            "document_signature_hash": document_signature_hash,
            "document_body_count": document_body_count,
            "source_state_hash": source_state_hash,
            "export_id": str(payload.get("export_id") or ""),
            "export_sequence": str(payload.get("export_sequence") or ""),
        }
        identity = instance_identities.get(str(instance_id))
        if isinstance(identity, dict):
            for name in ("body_object_ids", "source_ids", "drive_channel_ids"):
                value = identity.get(name)
                if isinstance(value, list):
                    link[name] = value
            transform_hash = identity.get("transform_hash")
            if isinstance(transform_hash, str) and transform_hash:
                link["transform_hash"] = transform_hash
        links.append(link)
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


def _fusion_snapshot() -> dict[str, object]:
    """Read the active document and managed-link state once on the main thread."""

    app = _app()
    product = app.activeProduct if app else None
    active_document = getattr(app, "activeDocument", None) if app else None
    if active_document is None and product is None:
        document_name = None
        links: list[dict[str, object]] = []
    else:
        document_name = str(getattr(active_document, "name", "") or "Untitled")
        links = _document_links()
    return {
        "document_name": document_name,
        "document_id": _active_document_id(),
        "links": links,
    }


def _publish_fusion_status(snapshot: dict[str, object] | None = None) -> None:
    """Best-effort owner presence for WG; every Fusion read stays on this thread."""

    if not _owns_active_ipc_lease():
        return

    folder = wglink_workspace.ipc_folder(create=True)
    if folder is None:
        return
    current = snapshot if snapshot is not None else _fusion_snapshot()
    try:
        wglink_watch.write_fusion_status(
            folder,
            session_id=_watch_session_id,
            document_name=current["document_name"],
            document_id=current["document_id"],
            adapter_version=wglink_send.ADAPTER_VERSION,
            workspace_root=wglink_workspace.workspace_root(),
            links=current["links"],
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


def _apply_pending_return_request(
    snapshot: dict[str, object] | None = None,
) -> bool:
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
        document_id = (
            snapshot["document_id"] if snapshot is not None else _active_document_id()
        )
        if request.document_id != document_id:
            raise wglink_core.WgLinkError(
                "The active Fusion document changed after WG requested the model. Reopen CAD Link and try again."
            )
        links = snapshot["links"] if snapshot is not None else _document_links()
        matching = [
            link for link in links
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
            "capture_document": wglink_workspace.capture_document(),
        }
        wglink_send.send(_app(), options)
        wglink_watch.acknowledge_return_request(request)
    except wglink_core.WgLinkError as exc:
        _message(str(exc), "WGLink return to WG refused")
    except Exception as exc:  # noqa: BLE001 - main-thread add-in boundary
        _report_error("Returning this model to WG", "WGLink return to WG error", exc)
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


def _apply_pending_handoff(snapshot: dict[str, object] | None = None) -> bool:
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

    links = snapshot["links"] if snapshot is not None else _document_links()
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
        if handoff.expected_instance_id and not handoff.expected_document_id:
            raise wglink_core.WgLinkError(
                "WG prepared an instance-specific update without an exact Fusion "
                "document target. Refresh CAD Link and try again."
            )
        if handoff.expected_document_id:
            document_id = (
                snapshot["document_id"]
                if snapshot is not None
                else _active_document_id()
            )
            if handoff.expected_document_id != document_id:
                raise wglink_core.WgLinkError(
                    "The active Fusion document changed after WG prepared this update. Refresh CAD Link and try again."
                )
        if handoff.expected_instance_id:
            selected = [
                link
                for link in linked
                if link.get("instance_id") == handoff.expected_instance_id
            ]
            if len(selected) != 1:
                raise wglink_core.WgLinkError(
                    "The active Fusion document no longer contains exactly one WG "
                    "link with the instance selected in WG. Refresh CAD Link and "
                    "try again."
                )
            linked = selected
        elif len(linked) > 1:
            raise wglink_core.WgLinkError(
                "The active Fusion document contains more than one matching WG "
                "link, but WG did not select an exact instance. Refresh CAD Link, "
                "choose the managed instance, and try again."
            )
        elif handoff.expected_document_id and len(linked) != 1:
            raise wglink_core.WgLinkError(
                "The active Fusion document no longer contains exactly one matching "
                "WG link. Refresh CAD Link and try again."
            )
        if linked:
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
    except Exception as exc:  # noqa: BLE001 - main-thread add-in boundary
        operation = "update" if linked else "insert"
        _report_error(
            f"The automatic {operation}", f"WGLink automatic {operation} error", exc
        )
    finally:
        _command_busy = False
    return True


def _on_watch_tick() -> None:
    """Main-thread half of the watcher: survey, ask, and apply if asked."""

    global _command_busy
    if _command_busy:
        return
    snapshot = _fusion_snapshot()
    try:
        if _apply_pending_handoff(snapshot):
            return
        if _apply_pending_return_request(snapshot):
            return
        announcements = _watcher.survey(snapshot["links"])
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
        except Exception as exc:  # noqa: BLE001
            _report_error("Offering a newer export", "WGLink watch error", exc)
        finally:
            _command_busy = False
    finally:
        _publish_fusion_status(snapshot)


class WatchEventHandler(adsk.core.CustomEventHandler):
    def notify(self, _args: object) -> None:
        try:
            # A queued event from an expired owner must not service a request
            # after a candidate has promoted and replaced its panel.
            if not _owns_active_ipc_lease():
                return
            _on_watch_tick()
        except Exception as exc:  # noqa: BLE001
            _report_error("Watching for WG exports", "WGLink watch error", exc)


class CandidateEventHandler(adsk.core.CustomEventHandler):
    """Try to promote an adopted registration after lease loss."""

    def notify(self, _args: object) -> None:
        try:
            _attempt_promotion()
        except Exception as exc:  # noqa: BLE001
            _report_error("Promoting a standby WGLink", "WGLink recovery error", exc)


def _watch_loop(app: object, stop: threading.Event) -> None:
    """Renew the lease and raise the owner's event without reading Fusion."""

    while not stop.wait(WATCH_INTERVAL_SECONDS):
        if not _renew_ipc_lease():
            return
        try:
            app.fireCustomEvent(WATCH_EVENT_ID)
        except Exception:  # noqa: BLE001 - a torn-down event ends the thread
            return


def _candidate_loop(app: object, stop: threading.Event) -> None:
    """Raise only this registration's private promotion event."""

    while not stop.wait(WATCH_INTERVAL_SECONDS):
        try:
            app.fireCustomEvent(_candidate_event_id)
        except Exception:  # noqa: BLE001 - a torn-down event ends the thread
            return


def _start_candidate(app: object) -> None:
    """Wait without publishing an unserviceable Fusion session id."""

    global _candidate_event, _candidate_handler, _candidate_stop, _candidate_thread
    if _candidate_event is not None:
        return
    _candidate_event = app.registerCustomEvent(_candidate_event_id)
    if _candidate_event is None:
        return
    _candidate_handler = CandidateEventHandler()
    _candidate_event.add(_candidate_handler)
    _candidate_stop = threading.Event()
    _candidate_thread = threading.Thread(
        target=_candidate_loop,
        args=(app, _candidate_stop),
        name="WGLinkOwnerCandidate",
        daemon=True,
    )
    _candidate_thread.start()


def _stop_candidate(app: object) -> None:
    global _candidate_event, _candidate_handler, _candidate_stop, _candidate_thread
    if _candidate_stop is not None:
        _candidate_stop.set()
    if _candidate_thread is not None and _candidate_thread.is_alive():
        _candidate_thread.join(timeout=WATCH_INTERVAL_SECONDS + 1)
    if _candidate_event is not None and _candidate_handler is not None:
        try:
            _candidate_event.remove(_candidate_handler)
        except Exception:  # noqa: BLE001
            pass
    try:
        if app:
            app.unregisterCustomEvent(_candidate_event_id)
    except Exception:  # noqa: BLE001
        pass
    _candidate_event = _candidate_handler = _candidate_stop = _candidate_thread = None


def _start_watch(app: object) -> bool:
    global _watch_event, _watch_handler, _watch_stop, _watch_thread
    global _handoff_attempted_id, _return_request_attempted_id
    _watcher.reset()
    _handoff_attempted_id = None
    _return_request_attempted_id = None
    _watch_event = app.registerCustomEvent(WATCH_EVENT_ID)
    if _watch_event is None:
        return False
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
    return True


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


MANAGE_DROPDOWN_ID = "hornlab_wglink_manage"
MANAGE_DROPDOWN_NAME = "Manage WG Link…"


def _manage_dropdown(panel: object) -> object | None:
    """The container for the maintenance and recovery commands.

    An older Fusion without ``addDropDown`` degrades to the flat panel every
    command used to live on, which is worse looking but never broken.
    """

    try:
        return panel.controls.addDropDown(
            MANAGE_DROPDOWN_NAME, _icon_folder("manage"), MANAGE_DROPDOWN_ID
        )
    except Exception:  # noqa: BLE001 - fall back to the flat panel
        return None


def _clear_registration_state() -> None:
    """Forget local handles without mutating UI another lease owner may own."""

    global _panel, _owned
    _controls.clear()
    _definitions.clear()
    _handlers.clear()
    _panel = None
    _owned = False


def _delete_owned_ui(ui: object | None) -> None:
    """Delete the panel transaction while this registration holds the lease."""

    global _panel, _owned
    for control in reversed(_controls):
        _delete_quietly(control)
    _controls.clear()
    for definition in reversed(_definitions):
        _delete_quietly(definition)
    _definitions.clear()
    if ui:
        for command_id, _name, _description in COMMANDS.values():
            _delete_quietly(ui.commandDefinitions.itemById(command_id))
    _delete_quietly(_panel)
    _panel = None
    _owned = False
    _handlers.clear()


def _build_owner(app: object, ui: object) -> None:
    """Build panel + watcher, then commit the already-claimed owner lease."""

    global _panel, _owned, _replaced_watch_event_id
    if _replaced_watch_event_id:
        try:
            app.unregisterCustomEvent(_replaced_watch_event_id)
        except Exception:  # noqa: BLE001 - the expired event may already be gone
            pass
        _replaced_watch_event_id = None
    workspace = _workspace(ui)
    stale_panel = workspace.toolbarPanels.itemById(PANEL_ID)
    if stale_panel:
        # This is either an orderly owner's incomplete panel or the dead panel
        # left by an expired lease. The claimant is the only registration
        # allowed to replace it.
        _delete_quietly(stale_panel)
    _panel = workspace.toolbarPanels.add(
        PANEL_ID,
        PANEL_NAME,
        "SolidScriptsAddinsPanel",
        False,
    )
    try:
        # Insert and Update are ordinarily automatic: WG's Send to CAD writes a
        # handoff this add-in applies on its own. They, and the recovery
        # commands, are demoted under one dropdown so the panel reads as the two
        # actions a user actually starts.
        manage = None
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
            if definition is None:
                raise RuntimeError(f"Fusion refused command definition {command_id!r}")
            created = CommandCreatedHandler(operation)
            definition.commandCreated.add(created)
            _handlers.append(created)
            _definitions.append(definition)
            if operation in PROMOTED_COMMANDS:
                _controls.append(_panel.controls.addCommand(definition))
                continue
            if manage is None:
                manage = _manage_dropdown(_panel)
                if manage is not None:
                    _controls.append(manage)
            target = manage.controls if manage is not None else _panel.controls
            _controls.append(target.addCommand(definition))
        if not _start_watch(app):
            raise RuntimeError("Fusion refused the WGLink owner event")
        if not _activate_ipc_lease():
            raise RuntimeError("WGLink's IPC owner lease was lost during startup")
        _owned = True
        _publish_fusion_status()
    except Exception:
        _stop_watch(app)
        _delete_owned_ui(ui)
        raise


def _attempt_promotion() -> None:
    """Promote this standby after an orderly release or expired heartbeat."""

    if _owned or not _claim_ipc_lease():
        return
    app = _app()
    ui = app.userInterface if app else None
    if ui is None:
        _release_ipc_lease()
        return
    try:
        _build_owner(app, ui)
    except Exception:
        _release_ipc_lease()
        raise
    _stop_candidate(app)


def run(_context: object) -> None:
    global _panel, _owned
    try:
        app = _app()
        ui = app.userInterface if app else None
        if ui is None:
            return
        if _owned and _owns_active_ipc_lease():
            return
        if not _claim_ipc_lease():
            _panel = _workspace(ui).toolbarPanels.itemById(PANEL_ID)
            _owned = False
            _start_candidate(app)
            return
        try:
            _build_owner(app, ui)
        except Exception:
            _release_ipc_lease()
            raise
    except Exception as exc:  # noqa: BLE001
        _report_error("Starting WGLink", "WGLink start error", exc)


def stop(_context: object) -> None:
    try:
        app = _app()
        _stop_candidate(app)
        lease_owner = _owns_ipc_lease()
        try:
            _stop_watch(app)
            if _owned and lease_owner:
                _delete_owned_ui(app.userInterface if app else None)
            else:
                # An expired former owner may retain handles to objects already
                # replaced by its promoted successor. Never delete shared UI unless
                # the lease still proves authority.
                _clear_registration_state()
        finally:
            if lease_owner:
                _release_ipc_lease()
    except Exception as exc:  # noqa: BLE001
        _report_error("Stopping WGLink", "WGLink stop error", exc)
