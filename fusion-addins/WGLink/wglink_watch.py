"""Notice that WG has exported a newer bundle for a link in this document.

Deliberately free of ``adsk``: every Fusion API call has to happen on Fusion's
main thread, so the background thread that drives this may only touch the
filesystem. WGLink.py owns the thread, the custom event, and the prompt; this
module owns the question "is there anything new?" and is therefore testable
without Fusion.

Only ``wglink.json`` is read, never the whole bundle. Validating a bundle hashes
a couple of megabytes of STEP and point grid, which is the right thing to do
before mutating a document and the wrong thing to do every few seconds.

Delivery between the add-in and Waveguide Generator follows WG's contract,
docs/architecture/CAD-OPERATIONS.md in that repository ("Capability file",
"Solve-command delivery" and "WG-produced Fusion requests"). A solve command
is written as its own file only when WG advertises that it reads them. WG's
return requests and handoffs are read from their own files; the legacy slot
then holds a twin under the same id, which only an add-in that reads the slot
alone may take, so this one never runs or deletes it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, TypeVar
import uuid


HANDOFF_FILENAME = ".fusion-handoff.json"
FUSION_STATUS_FILENAME = ".fusion-status.json"
RETURN_REQUEST_FILENAME = ".fusion-return-request.json"
SOLVE_REQUEST_FILENAME = ".wg-solve-request.json"

# WG's advertisement of the delivery versions it reads.
CAPABILITIES_FILENAME = "wg-capabilities.json"
CAPABILITIES_SCHEMA_VERSION = 1
SOLVE_COMMAND_DELIVERY = "solveCommandDelivery"
# One file per solve command, written only when WG advertises that it reads them.
SOLVE_REQUESTS_DIRECTORY = ".wg-solve-requests"
# One file per WG request. The legacy slot beside it then holds only a twin.
RETURN_REQUESTS_DIRECTORY = ".fusion-return-requests"
HANDOFFS_DIRECTORY = ".fusion-handoffs"
# WG's record of the twin now in a legacy slot. Read here, never written.
SLOT_RECORD_FILENAME = ".legacy-slot.json"
SEQUENCE_FIELD = "deliverySequence"
LEGACY_SCHEMA_VERSION = 1
PER_REQUEST_SCHEMA_VERSION = 2
# A request this add-in has claimed. The leading "." keeps it out of every
# reader's listing, WG's included.
CLAIM_PREFIX = ".wglink-claim-"
# An id that becomes part of a file name: WG's request ids, this add-in's
# command ids.
_PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> Path:
    """Stage under a name that starts with "." and ends in .tmp, then rename.

    Neither WG nor this add-in takes such a name, so a reader sees the file
    whole or not at all.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name.lstrip('.')}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def wg_delivery_version(ipc_folder: Path, name: str) -> int:
    """The delivery version WG advertises for ``name``; 1 is the legacy route.

    The capability file's reading rules: fields this add-in does not know are
    ignored, and a missing or unreadable file, a ``schemaVersion`` it does not
    know, or a value that is not an integer of at least 2 all mean the legacy
    route -- never a refusal.
    """

    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if not isinstance(payload, Mapping):
        return 1
    schema = payload.get("schemaVersion")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != CAPABILITIES_SCHEMA_VERSION
    ):
        return 1
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        return 1
    return value


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

    When WG advertises ``solveCommandDelivery`` 2 or later, the command is
    written as its own file, ``.wg-solve-requests/<commandId>.json``, so a
    second command never replaces one WG has not read yet. Otherwise it goes
    into the legacy single slot, which every WG reads.
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
    folder = ipc_folder.expanduser().resolve()
    per_command = wg_delivery_version(folder, SOLVE_COMMAND_DELIVERY) >= 2
    if per_command and not _PLAIN_ID.fullmatch(str(command_id)):
        raise ValueError(
            f"A solve command id must be a plain file name, got {command_id!r}."
        )
    payload = {
        "schemaVersion": LEGACY_SCHEMA_VERSION,
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
    folder.mkdir(parents=True, exist_ok=True)
    if not per_command:
        return _write_json_atomically(folder / SOLVE_REQUEST_FILENAME, payload)
    payload["schemaVersion"] = PER_REQUEST_SCHEMA_VERSION
    payload["operationId"] = payload["commandId"]
    directory = folder / SOLVE_REQUESTS_DIRECTORY
    directory.mkdir(exist_ok=True)
    return _write_json_atomically(directory / f"{payload['commandId']}.json", payload)


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
    """A completed WG export that the user explicitly sent to Fusion.

    ``per_request`` is true for a request read from its own file. Such a
    request carries ``request_id`` = ``operation_id`` and a
    ``delivery_sequence``, and must be claimed (``claim_request``) before it
    runs. A legacy slot from a WG that predates per-request files carries
    neither id, and is acknowledged by its export id as before.
    """

    marker_path: Path
    bundle_path: str
    bundle_id: str
    export_id: str
    sequence: str
    design_id: str
    expected_document_id: str
    expected_instance_id: str
    expected_return_state_hash: str
    request_id: str = ""
    operation_id: str = ""
    delivery_sequence: int | None = None
    per_request: bool = False


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
    operation_id: str = ""
    delivery_sequence: int | None = None
    per_request: bool = False


_Pending = TypeVar("_Pending", PendingHandoff, PendingReturnRequest)


def _sequence(payload: Any) -> int | None:
    value = payload.get(SEQUENCE_FIELD) if isinstance(payload, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _per_request_identity(payload: Mapping[str, Any]) -> tuple[str, int] | None:
    """``(request_id, sequence)`` of a request file WG wrote, or None.

    WG writes the request id as the operation id and gives every request a
    positive sequence. A file without both is not one of WG's, and is left
    where it is.
    """

    request_id = payload.get("requestId")
    sequence = _sequence(payload)
    if (
        not isinstance(request_id, str)
        or not _PLAIN_ID.fullmatch(request_id)
        or payload.get("operationId") != request_id
        or sequence is None
    ):
        return None
    return request_id, sequence


def _names_an_operation(payload: Mapping[str, Any]) -> bool:
    """A legacy slot that names an operation id is a twin: never run it."""

    return bool(payload.get("operationId"))


def read_return_request(
    marker_path: Path,
    *,
    session_id: str,
    schema_version: int = LEGACY_SCHEMA_VERSION,
) -> PendingReturnRequest | None:
    """Read one return request: the legacy slot, or with schema 2 its own file.

    A request runs only in the add-in session it names. A legacy slot that
    names an ``operationId`` is a twin of a per-request file and reads as
    nothing.
    """

    payload = _read_json(marker_path)
    if not isinstance(payload, Mapping):
        return None
    request_id = payload.get("requestId")
    target_session = payload.get("sessionId")
    if (
        payload.get("schemaVersion") != schema_version
        or payload.get("target") != "fusion360"
        or not isinstance(request_id, str)
        or not request_id
        or target_session != session_id
    ):
        return None
    per_request = schema_version == PER_REQUEST_SCHEMA_VERSION
    sequence: int | None = None
    if per_request:
        identity = _per_request_identity(payload)
        if identity is None:
            return None
        sequence = identity[1]
    elif _names_an_operation(payload):
        return None
    return PendingReturnRequest(
        marker_path=marker_path,
        request_id=request_id,
        session_id=session_id,
        design_id=str(payload.get("designId") or ""),
        document_id=str(payload.get("documentId") or ""),
        instance_id=str(payload.get("instanceId") or ""),
        expected_return_state_hash=str(payload.get("expectedReturnStateHash") or ""),
        operation_id=request_id if per_request else "",
        delivery_sequence=sequence,
        per_request=per_request,
    )


def _remove_claim(pending: PendingHandoff | PendingReturnRequest) -> bool:
    """Delete a claim this add-in made, and nothing else."""

    if not pending.marker_path.name.startswith(CLAIM_PREFIX):
        return False
    try:
        pending.marker_path.unlink()
    except OSError:
        return False
    return True


def acknowledge_return_request(request: PendingReturnRequest) -> bool:
    """Retire a request that ran.

    A per-request file: delete its claim. A legacy slot: delete it only while
    it still holds this request and still names no operation id, so a newer
    request, or WG's twin of one, is never removed.
    """

    if request.per_request:
        return _remove_claim(request)
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
    schema_version: int = LEGACY_SCHEMA_VERSION,
) -> PendingHandoff | None:
    """Read a scoped one-shot handoff without trusting an arbitrary path.

    The legacy slot by default, or with schema 2 a handoff's own file. A
    legacy slot that names an ``operationId`` is a twin and reads as nothing.
    """

    payload = _read_json(marker_path)
    if not isinstance(payload, Mapping):
        return None
    if (
        payload.get("schemaVersion") != schema_version
        or payload.get("target") != "fusion360"
    ):
        return None
    per_request = schema_version == PER_REQUEST_SCHEMA_VERSION
    identity: tuple[str, int] | None = None
    if per_request:
        identity = _per_request_identity(payload)
        if identity is None:
            return None
    elif _names_an_operation(payload):
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
        request_id=identity[0] if identity else str(payload.get("requestId") or ""),
        operation_id=identity[0] if identity else "",
        delivery_sequence=identity[1] if identity else None,
        per_request=per_request,
    )


def acknowledge_handoff(
    handoff: PendingHandoff,
    *,
    bundle_root: Path | None = None,
) -> bool:
    """Remove only the marker this insert consumed, never a newer send.

    A per-request handoff: delete its claim. A legacy slot: delete it only
    while it still holds this export and names no operation id.
    """

    if handoff.per_request:
        return _remove_claim(handoff)
    current = read_pending_handoff(handoff.marker_path, bundle_root=bundle_root)
    if current is None or current.export_id != handoff.export_id:
        return False
    try:
        handoff.marker_path.unlink()
    except OSError:
        return False
    return True


def _request_files(directory: Path) -> list[Path]:
    """A request folder's requests: ``*.json`` names not starting with "."."""

    try:
        return sorted(
            path for path in directory.iterdir()
            if path.suffix == ".json" and not path.name.startswith(".") and path.is_file()
        )
    except OSError:
        return []


def _requests_in_order(directory: Path) -> list[Path]:
    """Request files in ``deliverySequence`` order, then by name.

    A file without a valid sequence is not WG's, and is left out.
    """

    ordered: list[tuple[int, str, Path]] = []
    for path in _request_files(directory):
        sequence = _sequence(_read_json(path))
        if sequence is not None:
            ordered.append((sequence, path.name, path))
    return [path for _sequence_value, _name, path in sorted(ordered)]


def discard_what_a_legacy_reader_took(
    ipc_folder: Path, slot_name: str, directory_name: str
) -> list[str]:
    """Step 1 of the contract: drop the files of requests a slot reader took.

    Applies only when the record names a request and its sequence, the
    legacy slot is absent, and that request's own file is still present --
    read in that order. WG writes the twin before the record, so a record
    read first is never newer than the slot seen after it; checking the slot
    first could discard a request WG publishes in between. That file goes,
    with every file whose sequence is not above the record's; none of them
    runs. Files with a higher sequence stay: WG may be writing their twins.
    """

    folder = Path(ipc_folder)
    directory = folder / directory_name
    record = _read_json(directory / SLOT_RECORD_FILENAME)
    twin_id = record.get("operationId") if isinstance(record, Mapping) else None
    last = _sequence(record)
    if not isinstance(twin_id, str) or not _PLAIN_ID.fullmatch(twin_id) or last is None:
        return []
    if os.path.lexists(folder / slot_name):
        return []
    taken = directory / f"{twin_id}.json"
    if not taken.is_file():
        return []
    discarded: list[str] = []
    for path in _request_files(directory):
        sequence = _sequence(_read_json(path))
        if path != taken and (sequence is None or sequence > last):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        discarded.append(path.stem)
    return discarded


def next_return_request(
    ipc_folder: Path, *, session_id: str
) -> PendingReturnRequest | None:
    """The next return request for this session, or None.

    Per-request files first, in sequence order; then a legacy slot, which
    runs only when it names no operation id (a WG that predates per-request
    files). A per-request result must be claimed before it runs.
    """

    folder = Path(ipc_folder)
    discard_what_a_legacy_reader_took(
        folder, RETURN_REQUEST_FILENAME, RETURN_REQUESTS_DIRECTORY
    )
    for path in _requests_in_order(folder / RETURN_REQUESTS_DIRECTORY):
        request = read_return_request(
            path, session_id=session_id, schema_version=PER_REQUEST_SCHEMA_VERSION
        )
        if request is not None:
            return request
    return read_return_request(folder / RETURN_REQUEST_FILENAME, session_id=session_id)


def next_pending_handoff(
    ipc_folder: Path, *, bundle_root: Path | None
) -> PendingHandoff | None:
    """The next handoff, or None; the same order as ``next_return_request``."""

    folder = Path(ipc_folder)
    discard_what_a_legacy_reader_took(folder, HANDOFF_FILENAME, HANDOFFS_DIRECTORY)
    for path in _requests_in_order(folder / HANDOFFS_DIRECTORY):
        handoff = read_pending_handoff(
            path, bundle_root=bundle_root, schema_version=PER_REQUEST_SCHEMA_VERSION
        )
        if handoff is not None:
            return handoff
    return read_pending_handoff(folder / HANDOFF_FILENAME, bundle_root=bundle_root)


def claim_request(pending: _Pending) -> _Pending | None:
    """Take a per-request file by renaming it to a hidden claim.

    Returns the request with ``marker_path`` naming the claim, or None when
    the rename failed -- the file is gone, or on Windows WG still has it
    open -- in which case the next pass tries again. What the rename took is
    the request: a claim that no longer holds the same request is put back.
    A legacy slot is not claimed; it is returned unchanged.
    """

    if not pending.per_request:
        return pending
    source = pending.marker_path
    claim = source.with_name(
        f"{CLAIM_PREFIX}{pending.request_id}-{uuid.uuid4().hex[:12]}.json"
    )
    try:
        os.rename(source, claim)
    except OSError:
        return None
    taken = _read_json(claim)
    if (
        not isinstance(taken, Mapping)
        or taken.get("requestId") != pending.request_id
        or _sequence(taken) != pending.delivery_sequence
    ):
        try:
            os.rename(claim, source)
        except OSError:
            pass
        return None
    return replace(pending, marker_path=claim)


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
    diagnostics: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically publish the active Fusion document as inert JSON.

    ``diagnostics`` is advisory and additive under heartbeat schema 1: it
    carries what the last tick cost and which source the add-in is running, so
    a change can be shown to be live -- and a slow heartbeat measured -- from
    outside Fusion, without a debugger and without shipping a build. An older
    WG client ignores the key, exactly as it ignores ``linkName``.
    """

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
        # The user's own label for this link, or null when they never set one.
        # Additive under heartbeat schema 1: an older WG client ignores it and
        # keeps showing designName, exactly as it does today.
        "link_name": "linkName",
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
        # The WG operation that last updated this link, stamped beside its
        # export id: the reconciliation evidence. Omitted, never invented,
        # for a link no WG operation has updated. Additive under schema 1.
        operation_id = link.get("operation_id")
        if isinstance(operation_id, str) and operation_id:
            record["operationId"] = operation_id
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
    if diagnostics:
        payload["diagnostics"] = json.loads(json.dumps(diagnostics, default=str))
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
        self._identities: dict[str, tuple[str, str] | None] = {}

    def forget(self, instance_id: str) -> None:
        """Drop the announcement record once a link has been updated."""

        self._announced.pop(instance_id, None)

    def reset(self) -> None:
        self._announced.clear()
        self._stamps.clear()
        self._identities.clear()

    def _current_identity(self, bundle_path: str) -> tuple[str, str] | None:
        """The bundle's export identity, re-parsed only when the file moved.

        The stat stamp is a read cache and nothing else: it decides whether the
        manifest is worth parsing again, never whether a link is announced. It
        used to do both, so the first link to consult a bundle consumed the
        change on behalf of every other link sharing that bundle -- a second
        insertion of the same design was never announced, and a failed update
        could not be retried without the file being touched again.
        """

        try:
            status = _manifest_path(bundle_path).stat()
        except OSError:
            self._stamps.pop(bundle_path, None)
            self._identities.pop(bundle_path, None)
            return None
        stamp = (status.st_mtime, status.st_size)
        if self._stamps.get(bundle_path) != stamp:
            self._stamps[bundle_path] = stamp
            self._identities[bundle_path] = read_export_identity(bundle_path)
        return self._identities.get(bundle_path)

    def survey(self, links: Iterable[Mapping[str, Any]]) -> list[Announcement]:
        """Announcements for links whose bundle names an unseen newer export.

        ``links`` carries plain strings copied off the document on the main
        thread -- never live Fusion objects, which must not cross a thread.

        Identity is read once per bundle path per tick and every link is then
        compared against it, so several instances of one design each get their
        own answer. Whether the user has already been told is tracked per
        instance in ``_announced``, and nowhere else.
        """

        found: list[Announcement] = []
        identities: dict[str, tuple[str, str] | None] = {}
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
            if bundle_path not in identities:
                identities[bundle_path] = self._current_identity(bundle_path)
            identity = identities[bundle_path]
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
