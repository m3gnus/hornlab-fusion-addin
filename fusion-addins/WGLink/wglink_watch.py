"""Notice that WG has exported a newer bundle for a link in this document.

Deliberately free of ``adsk``: every Fusion API call has to happen on Fusion's
main thread, so the background thread that drives this may only touch the
filesystem. WGLink.py owns the thread, the custom event, and the prompt; this
module owns the question "is there anything new?" and is therefore testable
without Fusion.

Only ``wglink.json`` is read, never the whole bundle. Validating a bundle hashes
a couple of megabytes of STEP and point grid, which is the right thing to do
before mutating a document and the wrong thing to do every few seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping


HANDOFF_FILENAME = ".fusion-handoff.json"
FUSION_STATUS_FILENAME = ".fusion-status.json"
RETURN_REQUEST_FILENAME = ".fusion-return-request.json"
SOLVE_REQUEST_FILENAME = ".wg-solve-request.json"


def write_solve_request(
    ipc_folder: Path,
    *,
    command_id: str,
    return_id: str,
    bundle_path: Path,
    workspace_root: Path,
    requested_at: datetime | None = None,
) -> Path:
    """Ask WG to ingest one exact return bundle and start a solve.

    Deliberately separate from ``wgreturn.json``: that manifest is immutable
    geometry evidence which WG re-reads whenever it re-lists the workspace, so
    an intent flag inside it would be re-observed and re-solved. A marker with
    its own command id can be spent exactly once.

    The manifest hash is recorded here, after the bundle has been published, so
    WG can refuse a bundle that changed between the publish and this write.
    """

    bundle = bundle_path.expanduser().resolve()
    root = workspace_root.expanduser().resolve()
    try:
        relative = bundle.relative_to(root)
    except ValueError as exc:
        raise OSError(
            f"Return bundle {bundle} is not inside the WGLink workspace {root}."
        ) from exc
    digest = hashlib.sha256((bundle / "wgreturn.json").read_bytes()).hexdigest()
    payload = {
        "schemaVersion": 1,
        "target": "waveguide-generator",
        "commandId": str(command_id),
        "returnId": str(return_id),
        "bundlePath": relative.as_posix(),
        "manifestSha256": f"sha256:{digest}",
        "requestedAt": (requested_at or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    folder = ipc_folder.expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / SOLVE_REQUEST_FILENAME
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{SOLVE_REQUEST_FILENAME}.", dir=folder
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    return marker


@dataclass(frozen=True)
class Announcement:
    """A link whose bundle on disk has moved past what the document holds."""

    instance_id: str
    bundle_path: str
    stored_export_id: str
    available_export_id: str
    available_sequence: str

    def describe(self) -> str:
        return (
            f"{self.instance_id} — export sequence {self.available_sequence or '?'}"
        )


@dataclass(frozen=True)
class PendingHandoff:
    """A completed WG export that the user explicitly sent to Fusion."""

    marker_path: Path
    bundle_path: str
    bundle_id: str
    export_id: str
    sequence: str
    design_id: str
    expected_document_id: str
    expected_instance_id: str
    expected_return_state_hash: str


@dataclass(frozen=True)
class PendingReturnRequest:
    """A request from WG to export the active Fusion document back to WG."""

    marker_path: Path
    request_id: str
    session_id: str
    design_id: str
    document_id: str
    instance_id: str
    expected_return_state_hash: str


def read_return_request(
    marker_path: Path, *, session_id: str
) -> PendingReturnRequest | None:
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    request_id = payload.get("requestId")
    target_session = payload.get("sessionId")
    if (
        payload.get("schemaVersion") != 1
        or payload.get("target") != "fusion360"
        or not isinstance(request_id, str)
        or not request_id
        or target_session != session_id
    ):
        return None
    return PendingReturnRequest(
        marker_path=marker_path,
        request_id=request_id,
        session_id=session_id,
        design_id=str(payload.get("designId") or ""),
        document_id=str(payload.get("documentId") or ""),
        instance_id=str(payload.get("instanceId") or ""),
        expected_return_state_hash=str(payload.get("expectedReturnStateHash") or ""),
    )


def acknowledge_return_request(request: PendingReturnRequest) -> bool:
    current = read_return_request(request.marker_path, session_id=request.session_id)
    if current is None or current.request_id != request.request_id:
        return False
    try:
        request.marker_path.unlink()
    except OSError:
        return False
    return True


def read_pending_handoff(
    marker_path: Path,
    *,
    bundle_root: Path | None = None,
) -> PendingHandoff | None:
    """Read a scoped one-shot handoff without trusting an arbitrary path."""

    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schemaVersion") != 1 or payload.get("target") != "fusion360":
        return None
    bundle_id = payload.get("bundleId")
    export_id = payload.get("exportId")
    bundle_value = payload.get("bundlePath")
    if not all(isinstance(value, str) and value for value in (bundle_id, export_id, bundle_value)):
        return None
    try:
        allowed_bundle_root = (bundle_root or marker_path.parent).resolve()
        bundle_path = Path(str(bundle_value)).expanduser().resolve()
    except OSError:
        return None
    if bundle_path.parent != allowed_bundle_root:
        return None
    if bundle_path.is_symlink() or not bundle_path.is_dir():
        return None
    sequence = payload.get("sequence")
    return PendingHandoff(
        marker_path=marker_path,
        bundle_path=str(bundle_path),
        bundle_id=str(bundle_id),
        export_id=str(export_id),
        sequence="" if sequence is None else str(sequence),
        design_id=str(payload.get("designId") or ""),
        expected_document_id=str(payload.get("expectedDocumentId") or ""),
        expected_instance_id=str(payload.get("expectedInstanceId") or ""),
        expected_return_state_hash=str(payload.get("expectedReturnStateHash") or ""),
    )


def acknowledge_handoff(
    handoff: PendingHandoff,
    *,
    bundle_root: Path | None = None,
) -> bool:
    """Remove only the marker this insert consumed, never a newer send."""

    current = read_pending_handoff(handoff.marker_path, bundle_root=bundle_root)
    if current is None or current.export_id != handoff.export_id:
        return False
    try:
        handoff.marker_path.unlink()
    except OSError:
        return False
    return True


def write_fusion_status(
    bundle_root: Path,
    *,
    session_id: str,
    document_name: str | None,
    document_id: str | None = None,
    adapter_version: str | None = None,
    workspace_root: Path | None = None,
    links: Iterable[Mapping[str, Any]],
    updated_at: datetime | None = None,
) -> Path:
    """Atomically publish the active Fusion document as inert JSON."""

    root = bundle_root.expanduser().resolve()
    if not root.is_dir():
        raise OSError(f"WGLink bundle folder is unavailable: {root}")
    allowed = {
        "instance_id": "instanceId",
        "bundle_path": "bundlePath",
        "design_id": "designId",
        "lineage_id": "lineageId",
        "edit_version": "editVersion",
        "design_hash": "designHash",
        "design_name": "designName",
        "formula": "formula",
        "config_present": "configPresent",
        "parameter_count": "parameterCount",
        "parameter_drift_count": "parameterDriftCount",
        "local_body_state": "localBodyState",
        "body_fingerprint_hash": "bodyFingerprintHash",
        "document_signature_hash": "documentSignatureHash",
        "document_body_count": "documentBodyCount",
        "source_state_hash": "sourceStateHash",
        "export_id": "exportId",
        "export_sequence": "exportSequence",
    }
    copied = []
    for link in links:
        instance_id = str(link.get("instance_id") or "")
        if not instance_id:
            continue
        record = {
            wire_name: str(link.get(source_name) or "") or None
            for source_name, wire_name in allowed.items()
        }
        record["configPresent"] = record["configPresent"] == "true"
        try:
            record["parameterCount"] = int(record["parameterCount"] or 0)
        except (TypeError, ValueError):
            record["parameterCount"] = 0
        raw_drifted_parameters = link.get("drifted_parameters")
        record["driftedParameters"] = sorted(
            name
            for name in (
                raw_drifted_parameters
                if isinstance(raw_drifted_parameters, list)
                else []
            )
            if isinstance(name, str)
        )
        record["parameterDriftCount"] = len(record["driftedParameters"])
        try:
            record["documentBodyCount"] = int(record["documentBodyCount"] or 0)
        except (TypeError, ValueError):
            record["documentBodyCount"] = 0
        transform_hash = link.get("transform_hash")
        if isinstance(transform_hash, str) and transform_hash:
            record["transformHash"] = transform_hash
        for source_name, wire_name in (
            ("body_object_ids", "bodyObjectIds"),
            ("source_ids", "sourceIds"),
            ("drive_channel_ids", "driveChannelIds"),
        ):
            values = link.get(source_name)
            if (
                isinstance(values, list)
                and values
                and all(isinstance(value, str) and bool(value) for value in values)
            ):
                record[wire_name] = sorted(set(values))
        copied.append(record)
    timestamp = (updated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {
        "schemaVersion": 1,
        "cadApplication": "fusion360",
        "sessionId": str(session_id),
        "adapterVersion": str(adapter_version or "") or None,
        "workspaceRoot": (
            str(workspace_root.expanduser().resolve())
            if workspace_root is not None
            else None
        ),
        "updatedAt": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "document": (
            {"name": document_name, "id": document_id, "links": copied}
            if document_name is not None
            else None
        ),
    }
    marker = root / FUSION_STATUS_FILENAME
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{FUSION_STATUS_FILENAME}.", dir=root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    return marker


def remove_fusion_status(bundle_root: Path, *, session_id: str) -> bool:
    """Remove only this add-in session's heartbeat, never a replacement's."""

    marker = bundle_root.expanduser().resolve() / FUSION_STATUS_FILENAME
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("sessionId") != session_id:
            return False
        marker.unlink()
    except (OSError, ValueError, TypeError):
        return False
    return True


def _manifest_path(bundle_path: str) -> Path:
    return Path(bundle_path).expanduser() / "wglink.json"


def read_export_identity(bundle_path: str) -> tuple[str, str] | None:
    """Return ``(export_id, sequence)`` from a bundle manifest, or None."""

    try:
        manifest = json.loads(_manifest_path(bundle_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(manifest, Mapping):
        return None
    export = manifest.get("export")
    if not isinstance(export, Mapping):
        return None
    export_id = export.get("id")
    if not isinstance(export_id, str) or not export_id:
        return None
    sequence = export.get("sequence")
    return export_id, "" if sequence is None else str(sequence)


class ExportWatcher:
    """Tracks which bundles have moved on, and which the user has been told about."""

    def __init__(self) -> None:
        self._announced: dict[str, str] = {}
        self._stamps: dict[str, tuple[float, int]] = {}

    def forget(self, instance_id: str) -> None:
        """Drop the announcement record once a link has been updated."""

        self._announced.pop(instance_id, None)

    def reset(self) -> None:
        self._announced.clear()
        self._stamps.clear()

    def _changed_on_disk(self, bundle_path: str) -> bool:
        """Cheap gate so an idle workspace costs one stat per link per tick."""

        try:
            status = _manifest_path(bundle_path).stat()
        except OSError:
            self._stamps.pop(bundle_path, None)
            return False
        stamp = (status.st_mtime, status.st_size)
        if self._stamps.get(bundle_path) == stamp:
            return False
        self._stamps[bundle_path] = stamp
        return True

    def survey(self, links: Iterable[Mapping[str, Any]]) -> list[Announcement]:
        """Announcements for links whose bundle names an unseen newer export.

        ``links`` carries plain strings copied off the document on the main
        thread -- never live Fusion objects, which must not cross a thread.
        """

        found: list[Announcement] = []
        for link in links:
            instance_id = str(link.get("instance_id") or "")
            bundle_path = str(link.get("bundle_path") or "")
            stored_export_id = str(link.get("export_id") or "")
            if not instance_id or not bundle_path:
                continue
            # A link whose stored export is unknown has never been updated from
            # a manifest; announcing it would be guessing, so leave it alone.
            if not stored_export_id:
                continue
            if not self._changed_on_disk(bundle_path):
                continue
            identity = read_export_identity(bundle_path)
            if identity is None:
                continue
            available_export_id, sequence = identity
            if available_export_id == stored_export_id:
                continue
            if self._announced.get(instance_id) == available_export_id:
                continue
            self._announced[instance_id] = available_export_id
            found.append(
                Announcement(
                    instance_id=instance_id,
                    bundle_path=bundle_path,
                    stored_export_id=stored_export_id,
                    available_export_id=available_export_id,
                    available_sequence=sequence,
                )
            )
        return found


def prompt_text(announcements: list[Announcement]) -> str:
    if len(announcements) == 1:
        one = announcements[0]
        return (
            f"Waveguide Generator exported a newer bundle for {one.instance_id}.\n\n"
            f"Export sequence {one.available_sequence or '?'}\n{one.bundle_path}\n\n"
            "Update the link now? This rebuilds the managed geometry in place; "
            "it creates and deletes no features, and Undo reverses it."
        )
    listed = "\n".join(f"  • {item.describe()}" for item in announcements)
    return (
        "Waveguide Generator exported newer bundles for these links:\n\n"
        f"{listed}\n\nUpdate them now? This rebuilds the managed geometry in "
        "place; it creates and deletes no features, and Undo reverses it."
    )
