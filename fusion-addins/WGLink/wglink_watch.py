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
docs/architecture/CAD-OPERATIONS.md in that repository ("Delivery version",
"Solve-command delivery" and "WG-produced Fusion requests"). Both sides speak
delivery version 3 and nothing older: every request, in either direction, is
its own file, and there is no single-slot marker and no twin. This add-in
reports its version in the heartbeat, and WG refuses an add-in that reports
less. It refuses a WG that advertises less in turn, and asks for WG to be
updated instead of guessing at an older format.
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


FUSION_STATUS_FILENAME = ".fusion-status.json"

# The delivery version this add-in speaks, published in its heartbeat. WG
# refuses an add-in that reports anything lower, and this add-in refuses a WG
# that advertises anything lower.
DELIVERY_VERSION = 3
# WG's advertisement of the delivery versions it reads.
CAPABILITIES_FILENAME = "wg-capabilities.json"
CAPABILITIES_SCHEMA_VERSION = 1
SOLVE_COMMAND_DELIVERY = "solveCommandDelivery"
FUSION_REQUEST_DELIVERY = "fusionRequestDelivery"
# WG reads returns that require ``source-identity-v1`` when it advertises this
# as an integer of at least 1. The add-in declares the feature only then: a WG
# that does not advertise it refuses the bundle as an unknown required feature.
SOURCE_IDENTITY = "sourceIdentity"
# Every request file, in both directions, carries this schema version.
REQUEST_SCHEMA_VERSION = 3
# One file per solve command, one per WG request.
SOLVE_REQUESTS_DIRECTORY = ".wg-solve-requests"
RETURN_REQUESTS_DIRECTORY = ".fusion-return-requests"
HANDOFFS_DIRECTORY = ".fusion-handoffs"
SEQUENCE_FIELD = "deliverySequence"
# The single slots a WG older than delivery version 3 writes. This add-in never
# runs one; finding one while WG advertises less than 3 is how it knows to ask
# for a WG update.
LEGACY_HANDOFF_FILENAME = ".fusion-handoff.json"
LEGACY_RETURN_REQUEST_FILENAME = ".fusion-return-request.json"
# A request this add-in has claimed. The leading "." keeps it out of every
# reader's listing, WG's included.
CLAIM_PREFIX = ".wglink-claim-"
# An id that becomes part of a file name: WG's request ids, this add-in's
# command ids.
_PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

# How long a solve command may wait untaken before the user is told. WG takes
# a file on its next poll while its window is open, so a minute is far past
# normal; past it, WG is closed or older than version 3 (after a downgrade the
# capability file can still say 3). The command is never dropped or rerouted.
SOLVE_PICKUP_NOTICE_SECONDS = 60.0
SOLVE_NOT_TAKEN_MESSAGE = (
    "Waveguide Generator has not taken the solve request for a minute. Open "
    "Waveguide Generator; if an older version is running, update it. The request "
    "waits, and is solved once WG takes it."
)

WG_OUTDATED_MESSAGE = (
    "This Waveguide Generator is older than its WGLink add-in, so the two cannot "
    "exchange requests. Update Waveguide Generator; it installs the WGLink that "
    "matches it."
)


class WgOutdatedError(RuntimeError):
    """WG does not read this add-in's delivery version; nothing was written."""


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
    """The delivery version WG advertises for ``name``; 1 when it names none.

    The capability file's reading rules: fields this add-in does not know are
    ignored, and a missing or unreadable file, a ``schemaVersion`` it does not
    know, or a value that is not an integer of at least 2 all read as 1.
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


def wg_source_identity(ipc_folder: Path | None) -> bool:
    """Whether WG advertises that it reads ``source-identity-v1`` returns.

    The same reading rules as the delivery versions: a missing or unreadable
    file, a ``schemaVersion`` this add-in does not know, or a value that is not
    an integer of at least 1 (a boolean is not one) all mean "do not declare it".
    """

    if ipc_folder is None:
        return False
    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if not isinstance(payload, Mapping):
        return False
    schema = payload.get("schemaVersion")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != CAPABILITIES_SCHEMA_VERSION
    ):
        return False
    value = payload.get(SOURCE_IDENTITY)
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def wg_speaks_delivery_version(ipc_folder: Path) -> bool:
    """Whether WG advertises this add-in's delivery version on both channels."""

    folder = Path(ipc_folder)
    return all(
        wg_delivery_version(folder, name) >= DELIVERY_VERSION
        for name in (SOLVE_COMMAND_DELIVERY, FUSION_REQUEST_DELIVERY)
    )


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
    an intent flag inside it would be re-observed and re-solved. A command with
    its own id can be spent exactly once.

    The manifest hash is recorded here, after the bundle has been published, so
    WG can refuse a bundle that changed between the publish and this write.

    The command is written as its own file,
    ``.wg-solve-requests/<commandId>.json``, so a second command never replaces
    one WG has not read yet. A WG that does not advertise this add-in's
    delivery version gets nothing: ``WgOutdatedError`` asks for it to be
    updated instead.
    """

    folder = ipc_folder.expanduser().resolve()
    if wg_delivery_version(folder, SOLVE_COMMAND_DELIVERY) < DELIVERY_VERSION:
        raise WgOutdatedError(WG_OUTDATED_MESSAGE)
    if not _PLAIN_ID.fullmatch(str(command_id)):
        raise ValueError(
            f"A solve command id must be a plain file name, got {command_id!r}."
        )
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
        "schemaVersion": REQUEST_SCHEMA_VERSION,
        "target": "waveguide-generator",
        "commandId": str(command_id),
        "operationId": str(command_id),
        "returnId": str(return_id),
        "bundlePath": relative.as_posix(),
        "manifestSha256": f"sha256:{digest}",
        "requestedAt": (requested_at or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    directory = folder / SOLVE_REQUESTS_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    return _write_json_atomically(directory / f"{payload['commandId']}.json", payload)


@dataclass(frozen=True)
class PendingHandoff:
    """A completed WG export that the user explicitly sent to Fusion.

    Read from its own file, ``.fusion-handoffs/<requestId>.json``. The request
    id is its operation id, and the request must be claimed (``claim_request``)
    before it runs. An update names its exact target -- document and instance
    -- and the baseline it expects; an insert names neither instance nor
    baseline.
    """

    marker_path: Path
    request_id: str
    delivery_sequence: int
    bundle_path: str
    bundle_id: str
    export_id: str
    sequence: str
    design_id: str
    expected_document_id: str
    expected_instance_id: str
    expected_return_state_hash: str
    requested_at: str
    destination: dict[str, str] | None

    @property
    def operation_id(self) -> str:
        return self.request_id

    @property
    def exact_target(self) -> tuple[str, str] | None:
        """``(document_id, instance_id)`` for an update, None for an insert."""

        if self.expected_document_id and self.expected_instance_id:
            return self.expected_document_id, self.expected_instance_id
        return None


@dataclass(frozen=True)
class PendingReturnRequest:
    """A request from WG to export the active Fusion document back to WG."""

    marker_path: Path
    request_id: str
    delivery_sequence: int
    session_id: str
    design_id: str
    document_id: str
    instance_id: str
    expected_return_state_hash: str

    @property
    def operation_id(self) -> str:
        return self.request_id


_Pending = TypeVar("_Pending", PendingHandoff, PendingReturnRequest)


def _sequence(payload: Any) -> int | None:
    value = payload.get(SEQUENCE_FIELD) if isinstance(payload, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _request_identity(payload: Any) -> tuple[str, int] | None:
    """``(request_id, sequence)`` of a request file WG wrote, or None.

    WG writes schema version 3, the request id as the operation id, and a
    positive sequence. A file without all of them is not one of this delivery
    version's requests, and is left where it is.
    """

    if (
        not isinstance(payload, Mapping)
        or payload.get("schemaVersion") != REQUEST_SCHEMA_VERSION
        or payload.get("target") != "fusion360"
    ):
        return None
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


def read_return_request(
    marker_path: Path, *, session_id: str
) -> PendingReturnRequest | None:
    """Read one return request. It runs only in the add-in session it names."""

    payload = _read_json(marker_path)
    identity = _request_identity(payload)
    if identity is None or payload.get("sessionId") != session_id:
        return None
    return PendingReturnRequest(
        marker_path=marker_path,
        request_id=identity[0],
        delivery_sequence=identity[1],
        session_id=session_id,
        design_id=str(payload.get("designId") or ""),
        document_id=str(payload.get("documentId") or ""),
        instance_id=str(payload.get("instanceId") or ""),
        expected_return_state_hash=str(payload.get("expectedReturnStateHash") or ""),
    )


def read_pending_handoff(
    marker_path: Path, *, bundle_root: Path | None = None
) -> PendingHandoff | None:
    """Read one handoff without trusting an arbitrary bundle path."""

    payload = _read_json(marker_path)
    identity = _request_identity(payload)
    if identity is None:
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
    raw_destination = payload.get("destination")
    destination = None
    if isinstance(raw_destination, Mapping):
        kind = raw_destination.get("kind")
        value = raw_destination.get("value")
        if isinstance(kind, str) and isinstance(value, str) and kind and value:
            destination = {"kind": kind, "value": value}
    return PendingHandoff(
        marker_path=marker_path,
        request_id=identity[0],
        delivery_sequence=identity[1],
        bundle_path=str(bundle_path),
        bundle_id=str(bundle_id),
        export_id=str(export_id),
        sequence="" if sequence is None else str(sequence),
        design_id=str(payload.get("designId") or ""),
        expected_document_id=str(payload.get("expectedDocumentId") or ""),
        expected_instance_id=str(payload.get("expectedInstanceId") or ""),
        expected_return_state_hash=str(payload.get("expectedReturnStateHash") or ""),
        requested_at=str(payload.get("requestedAt") or ""),
        destination=destination,
    )


def _remove_claim(path: Path) -> bool:
    """Delete a claim this add-in made, and nothing else."""

    if not path.name.startswith(CLAIM_PREFIX):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def acknowledge_return_request(request: PendingReturnRequest) -> bool:
    """Retire a return request that ran: delete its claim."""

    return _remove_claim(request.marker_path)


def acknowledge_handoff(handoff: PendingHandoff) -> bool:
    """Retire a handoff that ran: delete its claim."""

    return _remove_claim(handoff.marker_path)


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


def next_return_request(
    ipc_folder: Path, *, session_id: str
) -> PendingReturnRequest | None:
    """The next return request for this session, in sequence order, or None.

    The result must be claimed before it runs.
    """

    for path in _requests_in_order(Path(ipc_folder) / RETURN_REQUESTS_DIRECTORY):
        request = read_return_request(path, session_id=session_id)
        if request is not None:
            return request
    return None


def next_pending_handoff(
    ipc_folder: Path, *, bundle_root: Path | None
) -> PendingHandoff | None:
    """The next handoff, in sequence order, or None; claim it before it runs."""

    for path in _requests_in_order(Path(ipc_folder) / HANDOFFS_DIRECTORY):
        handoff = read_pending_handoff(path, bundle_root=bundle_root)
        if handoff is not None:
            return handoff
    return None


def discard_superseded_handoffs(
    ipc_folder: Path, *, bundle_root: Path | None
) -> list[str]:
    """Drop every unstarted update a newer update for the same target replaces.

    WG's supersession policy (CAD-OPERATIONS.md, "Ordering"): an update that has
    not started may be superseded only by a newer one for the same exact
    target, the same document and instance. WG withdraws the older file when it
    publishes the newer one; this covers the file it could not remove, on
    Windows while this add-in held it open. An insert is never superseded.
    Each superseded file is claimed first, so a request is either run or
    dropped, never both. Returns the request ids dropped.
    """

    handoffs = [
        handoff
        for path in _requests_in_order(Path(ipc_folder) / HANDOFFS_DIRECTORY)
        if (handoff := read_pending_handoff(path, bundle_root=bundle_root)) is not None
    ]
    newest: dict[tuple[str, str], PendingHandoff] = {}
    for handoff in handoffs:
        target = handoff.exact_target
        if target is not None:
            newest[target] = handoff
    dropped: list[str] = []
    for handoff in handoffs:
        target = handoff.exact_target
        if target is None or newest[target] is handoff:
            continue
        claimed = claim_request(handoff)
        if claimed is not None and _remove_claim(claimed.marker_path):
            dropped.append(handoff.request_id)
    return dropped


@dataclass(frozen=True)
class LeftoverClaim:
    """A claim an earlier session made and never finished.

    Claims are hidden from every listing, so nothing else will ever look at
    one again. ``channel`` is ``"handoff"`` or ``"returnRequest"``; the other
    fields are what reconciliation reads, empty when the claim is unreadable.
    """

    path: Path
    channel: str
    request_id: str
    export_id: str
    design_id: str
    expected_document_id: str
    expected_instance_id: str


def leftover_claims(ipc_folder: Path) -> list[LeftoverClaim]:
    """Every claim left in WG's request folders by an interrupted session."""

    found: list[LeftoverClaim] = []
    for directory, channel in (
        (HANDOFFS_DIRECTORY, "handoff"),
        (RETURN_REQUESTS_DIRECTORY, "returnRequest"),
    ):
        try:
            paths = sorted(
                path for path in (Path(ipc_folder) / directory).iterdir()
                if path.name.startswith(CLAIM_PREFIX) and path.suffix == ".json"
            )
        except OSError:
            continue
        for path in paths:
            payload = _read_json(path)
            fields = payload if isinstance(payload, Mapping) else {}
            found.append(
                LeftoverClaim(
                    path=path,
                    channel=channel,
                    request_id=str(fields.get("requestId") or ""),
                    export_id=str(fields.get("exportId") or ""),
                    design_id=str(fields.get("designId") or ""),
                    expected_document_id=str(
                        fields.get("expectedDocumentId") or fields.get("documentId") or ""
                    ),
                    expected_instance_id=str(
                        fields.get("expectedInstanceId") or fields.get("instanceId") or ""
                    ),
                )
            )
    return found


def remove_leftover_claim(claim: LeftoverClaim) -> bool:
    return _remove_claim(claim.path)


def outdated_wg_requests(ipc_folder: Path) -> list[str]:
    """Requests an older WG wrote: a single-slot marker, or another schema.

    None of them is ever run. The capability file is not consulted: a WG that
    speaks version 3 removes such files at its start, before it advertises, so
    one beside a "3" was written after that -- by an older WG running on the
    same data folder since a downgrade, which never rewrites the file.
    """

    folder = Path(ipc_folder)
    found = [
        name
        for name in (LEGACY_HANDOFF_FILENAME, LEGACY_RETURN_REQUEST_FILENAME)
        if (folder / name).is_file()
    ]
    for directory in (HANDOFFS_DIRECTORY, RETURN_REQUESTS_DIRECTORY):
        for path in _request_files(folder / directory):
            payload = _read_json(path)
            if isinstance(payload, Mapping) and payload.get("schemaVersion") != REQUEST_SCHEMA_VERSION:
                found.append(f"{directory}/{path.name}")
    return found


def claim_request(pending: _Pending) -> _Pending | None:
    """Take a request file by renaming it to a hidden claim.

    Returns the request with ``marker_path`` naming the claim, or None when
    the rename failed -- the file is gone, or on Windows WG still has it
    open -- in which case the next pass tries again. What the rename took is
    the request: a claim that no longer holds the same request is put back.
    """

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


def release_claim(pending: _Pending) -> bool:
    """Put back a claim whose prerequisites are not ready, without consuming it."""

    claim = pending.marker_path
    if not claim.name.startswith(CLAIM_PREFIX):
        return False
    source = claim.with_name(f"{pending.request_id}.json")
    try:
        os.rename(claim, source)
    except OSError:
        return False
    return True


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
    applying_operation: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically publish the active Fusion document as inert JSON.

    The payload is :func:`fusion_status_payload`; the writer is
    :func:`write_fusion_status_payload`.
    """

    root = bundle_root.expanduser().resolve()
    if not root.is_dir():
        raise OSError(f"WGLink bundle folder is unavailable: {root}")
    payload = fusion_status_payload(
        session_id=session_id,
        document_name=document_name,
        document_id=document_id,
        adapter_version=adapter_version,
        workspace_root=workspace_root,
        links=links,
        updated_at=updated_at,
        diagnostics=diagnostics,
        applying_operation=applying_operation,
    )
    return write_fusion_status_payload(root, payload)


def fusion_status_payload(
    *,
    session_id: str,
    document_name: str | None,
    document_id: str | None = None,
    adapter_version: str | None = None,
    workspace_root: Path | None = None,
    links: Iterable[Mapping[str, Any]],
    updated_at: datetime | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    applying_operation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The heartbeat object: the active Fusion document as inert JSON.

    The same object is written to ``.fusion-status.json`` and, while a live
    session is healthy, posted to WG over HTTP (``wglink_live``).

    ``deliveryVersion`` tells WG which delivery version this add-in speaks; WG
    refuses an add-in that reports less than its own. ``applying_operation``
    is the WG operation the document is marked as applying, from
    ``wglink_core.applying_operation``: one whose evidence never followed is an
    interrupted mutation, and WG reports it as needing recovery.

    ``diagnostics`` is advisory and additive under heartbeat schema 1: it
    carries what the last tick cost and which source the add-in is running, so
    a change can be shown to be live -- and a slow heartbeat measured -- from
    outside Fusion, without a debugger and without shipping a build. An older
    WG client ignores the key, exactly as it ignores ``linkName``.
    """

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
        "deliveryVersion": DELIVERY_VERSION,
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
    operation_id = (
        str(applying_operation.get("operation_id") or "")
        if isinstance(applying_operation, Mapping)
        else ""
    )
    if payload["document"] is not None and operation_id:
        payload["document"]["applyingOperation"] = {
            "operationId": operation_id,
            **{
                wire: str(applying_operation.get(name) or "") or None
                for name, wire in (
                    ("kind", "kind"),
                    ("instance_id", "instanceId"),
                    ("export_id", "exportId"),
                )
            },
        }
        for name in ("phase", "startedAt"):
            value = applying_operation.get(name)
            if isinstance(value, str) and value:
                payload["document"]["applyingOperation"][name] = value
    if diagnostics:
        payload["diagnostics"] = json.loads(json.dumps(diagnostics, default=str))
    return payload


def write_fusion_status_payload(bundle_root: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically write a :func:`fusion_status_payload` object as the heartbeat file."""

    root = bundle_root.expanduser().resolve()
    if not root.is_dir():
        raise OSError(f"WGLink bundle folder is unavailable: {root}")
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
