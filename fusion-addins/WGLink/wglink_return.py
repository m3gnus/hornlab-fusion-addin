"""Build and validate CAD-authored ``wgreturn.json`` evidence.

This module deliberately has no Fusion dependency.  The API layer supplies
plain descriptors; this policy layer applies the export-scope rules and
serializes evidence without authoring any WG-owned ingestion verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
import json
from typing import Any

if __package__:
    from . import wglink_protocol
else:
    import wglink_protocol

MANAGE_DROPDOWN_NAME = "Manage WG Link…"
SCOPE_REASON_UNCLASSIFIED_VISIBLE_SURFACE = "unclassified_visible_surface"

SUPPORTED_RETURN_FEATURES = wglink_protocol.SUPPORTED_RETURN_FEATURES
# With this feature every ``sources[].id`` is a CAD-authored identity that WG
# bounds: trimmed, at most 25 UTF-8 bytes, and the whole gmsh physical name WG
# writes for the source (worst-case tag 9999) at most 128 bytes.
SOURCE_IDENTITY_FEATURE = wglink_protocol.SOURCE_IDENTITY_FEATURE
DOCUMENT_UP_FEATURE = wglink_protocol.DOCUMENT_UP_FEATURE
DOCUMENT_UP_AXES = wglink_protocol.DOCUMENT_UP_AXES
DOMAIN_AUTOMATIC_FEATURE = wglink_protocol.DOMAIN_AUTOMATIC_FEATURE
DOMAIN_AUTOMATIC = wglink_protocol.DOMAIN_AUTOMATIC
SOURCE_IDENTITY_MAX_BYTES = wglink_protocol.SOURCE_IDENTITY_MAX_BYTES
GMSH_PHYSICAL_NAME_MAX_BYTES = wglink_protocol.GMSH_PHYSICAL_NAME_MAX_BYTES
BASE_RETURN_FEATURES = wglink_protocol.BASE_RETURN_FEATURES

# A domain declaration says the exported bodies ARE the reduced domain: the
# author already cut the model in CAD and the missing half is to be supplied by
# the solver's mirror, not by WG's cutter. Only the planes the solver can mirror
# are declarable, and the retained side is the positive one -- both to match
# ``hornlab_mesher.step_prepare``, whose auto-cut keeps x >= 0 / y >= 0. A
# reader that does not know this vocabulary must refuse the bundle rather than
# solve a half as an open full-domain shell, which is what ``reduced-domain-v1``
# in ``required_features`` is for.
# Which component's own frame ``assembly.step`` is written in. Fusion exports a
# Component in its own coordinates, so the export scope decides the file's
# frame; every coordinate in this manifest is in that frame.
EXPORT_FRAMES = wglink_protocol.EXPORT_FRAMES
DOMAIN_PLANES = wglink_protocol.DOMAIN_PLANES
DOMAIN_KIND_FOR_PLANES = wglink_protocol.DOMAIN_KIND_FOR_PLANES
DOMAIN_KINDS = wglink_protocol.DOMAIN_KINDS
REDUCED_DOMAIN_FEATURE = wglink_protocol.REDUCED_DOMAIN_FEATURE
CUT_FEATURE_KINDS = wglink_protocol.CUT_FEATURE_KINDS
CUT_TOOL_KINDS = wglink_protocol.CUT_TOOL_KINDS
CUT_ORIGIN_PLANES = wglink_protocol.CUT_ORIGIN_PLANES
CUT_KEPT_SIDES = wglink_protocol.CUT_KEPT_SIDES


def classify_cut_provenance(
    descriptors: Sequence[Mapping[str, Any]],
    included_object_ids: set[str],
    export_frame: str,
) -> list[dict[str, Any]]:
    """Turn plain timeline observations into contract-shaped cut evidence.

    Fusion inspection lives in ``wglink_send``. This function deliberately
    knows only JSON-like descriptors, which keeps every classification rule
    executable without Fusion.
    """

    if export_frame not in EXPORT_FRAMES:
        raise WgReturnError(
            "cut provenance export_frame must be one of " + ", ".join(EXPORT_FRAMES)
        )
    result: list[dict[str, Any]] = []
    for raw in descriptors:
        descriptor = dict(raw)
        index = descriptor.get("timeline_index")
        marker = descriptor.get("marker_position")
        if descriptor.get("suppressed") is True:
            continue
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and isinstance(marker, int)
            and not isinstance(marker, bool)
            and index >= marker
        ):
            continue
        feature_kind = str(descriptor.get("feature_kind") or "")
        if feature_kind not in CUT_FEATURE_KINDS:
            continue
        tool_kind = str(descriptor.get("tool_kind") or "")
        origin_plane = str(descriptor.get("origin_plane") or "")
        if tool_kind not in CUT_TOOL_KINDS or origin_plane not in CUT_ORIGIN_PLANES:
            continue
        if tool_kind == "construction-plane" and descriptor.get("coincident") is not True:
            continue
        name = str(descriptor.get("feature_name") or "").strip()
        if not name or len(name) > 200:
            continue
        sides = descriptor.get("kept_sides")
        if not isinstance(sides, Mapping):
            continue
        for object_id in descriptor.get("body_object_ids") or ():
            body_id = str(object_id)
            side = sides.get(body_id)
            if body_id not in included_object_ids or side not in CUT_KEPT_SIDES:
                continue
            result.append({
                "body_object_id": body_id,
                "feature": {"kind": feature_kind, "name": name},
                "tool": {"kind": tool_kind, "origin_plane": origin_plane},
                "plane": CUT_ORIGIN_PLANES[origin_plane],
                "kept_side": side,
                "export_frame": export_frame,
            })
    return result


def canonical_domain_planes(planes: Sequence[Any]) -> tuple[str, ...]:
    try:
        return wglink_protocol.canonical_domain_planes(planes)
    except wglink_protocol.ProtocolValidationError as exc:
        raise WgReturnError(str(exc)) from exc

class WgReturnError(ValueError):
    """A policy refusal suitable for display before a return is written."""

    def __init__(
        self,
        message: str,
        *,
        reasons: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(message)
        self.reasons = tuple(dict(reason) for reason in reasons)


@dataclass(frozen=True)
class ScopePlan:
    """The recorded result of applying §3 to a sequence of descriptors."""

    selection: str
    included: tuple[dict[str, Any], ...]
    skipped: tuple[dict[str, Any], ...]
    fem_air_volumes: tuple[dict[str, Any], ...]
    refusals: tuple[dict[str, Any], ...]
    status: str

    def manifest_scope(self) -> dict[str, Any]:
        """Return the schema object, refusing if any terminal rule failed."""

        if self.refusals:
            details = "; ".join(reason["reason"] for reason in self.refusals)
            raise WgReturnError(
                f"return export refused: {details}", reasons=self.refusals
            )
        return {
            "selection": self.selection,
            "included": [deepcopy(record) for record in self.included],
            "skipped": [deepcopy(record) for record in self.skipped],
            "fem_air_volumes": [
                deepcopy(record) for record in self.fem_air_volumes
            ],
            "status": self.status,
        }


def _plain_descriptor(value: object, *, label: str) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, Mapping):
        raise WgReturnError(f"{label} must be a mapping or dataclass")
    return dict(value)


def _display_name(candidate: Mapping[str, Any], index: int) -> str:
    for key in ("path", "name", "object_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            return value
    return f"candidate {index}"


def _selection_name(selection: object) -> str:
    if selection == "root":
        return "root"
    descriptor = _plain_descriptor(selection, label="selection")
    kind = descriptor.get("kind")
    if kind == "root":
        return "root"
    if kind == "occurrence":
        path = descriptor.get("path")
        if isinstance(path, str) and path:
            return path
        raise WgReturnError("occurrence selection must name its path")
    if kind in {"body", "face"}:
        raise WgReturnError(
            f"cannot export a selected {kind}; select the root or exactly one "
            "occurrence subtree"
        )
    if kind in {"occurrences", "multiple-occurrences"}:
        raise WgReturnError(
            "cannot export several selected occurrences; select the root or "
            "exactly one occurrence subtree"
        )
    raise WgReturnError(
        "selection must be the root or exactly one occurrence subtree"
    )


def _scope_identity(candidate: Mapping[str, Any], index: int) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key in ("object_id", "name", "component", "path"):
        value = candidate.get(key)
        if value is not None:
            record[key] = value
    if "object_id" not in record:
        record["object_id"] = f"candidate-{index + 1:04d}"
    return record


def _dependency_reason(candidate: Mapping[str, Any]) -> str | None:
    dependencies = (
        ("contains_solver_anchor", "it contains the solver anchor"),
        (
            "contains_required_source",
            "it contains selector candidates for a required source",
        ),
        (
            "only_enclosing_exterior",
            "it is the only enclosing exterior body",
        ),
        ("requested_fem_air_volume", "it is a requested FEM air volume"),
    )
    matches = [reason for key, reason in dependencies if candidate.get(key) is True]
    return ", and ".join(matches) if matches else None


def _included_record(
    candidate: Mapping[str, Any],
    index: int,
    *,
    external_reference: str,
    reason: str,
    severity: str = "info",
) -> dict[str, Any]:
    record = _scope_identity(candidate, index)
    body_kind = candidate.get("body_kind")
    if body_kind is None and candidate.get("kind") in {"solid", "surface"}:
        body_kind = candidate.get("kind")
    record.update(
        {
            "body_kind": body_kind,
            "visible": True,
            "external_reference": external_reference,
            "reason": reason,
            "severity": severity,
        }
    )
    instance_id = candidate.get("wglink_instance_id")
    if instance_id is not None:
        record["wglink_instance_id"] = instance_id
    return record


def _skipped_record(
    candidate: Mapping[str, Any],
    index: int,
    *,
    kind: str,
    reason: str,
    severity: str,
) -> dict[str, Any]:
    record = _scope_identity(candidate, index)
    record.update({"kind": kind, "reason": reason, "severity": severity})
    return record


# Appended to whatever reason the ordinary classification rules produced, so a
# linked body says where its geometry came from without that provenance
# changing the verdict.
_EXTERNAL_NOTES = {
    "resolved-stale": (
        "; the body comes from a resolved external link that is stale, so this "
        "is the current local snapshot of it"
    ),
    "resolved-current": (
        "; the body comes from a resolved, current external link"
    ),
}


def _refusal_record(
    candidate: Mapping[str, Any],
    index: int,
    *,
    reason: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    record = _scope_identity(candidate, index)
    record.update({"decision": "refuse", "reason": reason})
    if reason_code is not None:
        record["reason_code"] = reason_code
    return record


def plan_export_scope(
    selection: object,
    candidates: Sequence[object],
) -> ScopePlan:
    """Apply §3's first-terminal-rule policy to Fusion-free descriptors.

    Body descriptors use ``body_kind`` (``solid``, ``surface``, or ``mesh``),
    ``visible``, and optional occurrence evidence such as ``suppressed`` and
    ``external_reference``.  Declarations use ``declaration`` with
    ``exterior-shell``, ``exclude``, or ``fem-air-volume``.  The four dependency booleans
    have the names used by :func:`_dependency_reason`.
    """

    selection_name = _selection_name(selection)
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise WgReturnError("scope candidates must be a sequence")

    included: list[dict[str, Any]] = []
    skipped: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    fem_air_volumes: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    degraded = False
    construction: dict[str, Any] | None = None
    body_role_declared = any(
        _plain_descriptor(raw, label=f"scope candidate {index}").get("declaration")
        is not None
        for index, raw in enumerate(candidates)
    )

    for index, raw_candidate in enumerate(candidates):
        candidate = _plain_descriptor(
            raw_candidate, label=f"scope candidate {index}"
        )
        name = _display_name(candidate, index)
        kind = candidate.get("kind", "body")
        body_kind = candidate.get("body_kind")
        external = candidate.get("external_reference", "none")

        # The order is the contract: once a rule applies, later rules do not.
        if candidate.get("suppressed") is True:
            record = _skipped_record(
                candidate,
                index,
                kind="suppressed",
                reason="suppressed objects have no evaluated geometry",
                severity="degraded",
            )
            skipped.append((record, candidate))
            degraded = True
            continue

        if (
            candidate.get("declaration") == "exclude"
            and body_kind in {"solid", "surface"}
        ):
            if candidate.get("visible") is not False:
                refusals.append(
                    _refusal_record(
                        candidate,
                        index,
                        reason=(
                            f"body {name!r} is declared 'exclude' but is still visible; "
                            "hide the body or its occurrence in Fusion before return "
                            "export, or clear the 'exclude' declaration to include it"
                        ),
                    )
                )
                continue
            painted = tuple(candidate.get("source_face_roles") or ())
            if painted:
                # An explicit exclusion is intent, but not for the painted
                # source on the body: that would vanish from the solve.
                refusals.append(
                    _refusal_record(
                        candidate,
                        index,
                        reason=(
                            f"body {name!r} is declared 'exclude' but carries painted "
                            f"source face(s) {', '.join(painted)}; clear the "
                            "declaration to solve with them, or clear the paint "
                            "with Set WG Source…"
                        ),
                    )
                )
                continue
            record = _skipped_record(
                candidate,
                index,
                kind="excluded_body",
                reason="body is explicitly excluded from the acoustic exterior",
                severity="info",
            )
            skipped.append((record, candidate))
            continue

        if external == "unresolved":
            refusals.append(
                _refusal_record(
                    candidate,
                    index,
                    reason=(
                        f"external linked occurrence {name!r} is unresolved; "
                        "resolve or remove the link before export"
                    ),
                )
            )
            continue

        # A RESOLVED external reference is evidence about a body, not a verdict
        # on it. It used to short-circuit straight into ``included`` here, which
        # gave linked geometry its own eligibility policy: a hidden external
        # solid was inventoried where a local one is skipped, an external mesh
        # was inventoried where a local one is not, and an undeclared external
        # surface was inventoried where a local one is refused. The inventory
        # feeds the STEP body-count gate, and Fusion exports visible bodies
        # only, so an included hidden body inflates the expected count against
        # a file that cannot contain it. Every rule below therefore judges
        # local and linked geometry alike; currency is attached to whatever
        # record the ordinary rules produce (see ``external_note`` on the
        # include paths, and the skipped-record pass after the loop).
        external_note = _EXTERNAL_NOTES.get(external, "")
        stale_external = external == "resolved-stale"

        if body_kind == "mesh" or kind == "mesh_body":
            record = _skipped_record(
                candidate,
                index,
                kind="mesh_body",
                reason="mesh bodies are excluded because tessellation is not authoritative B-rep",
                severity="degraded",
            )
            skipped.append((record, candidate))
            degraded = True
            continue

        if kind == "construction":
            count = candidate.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                refusals.append(
                    _refusal_record(
                        candidate,
                        index,
                        reason=f"construction count for {name!r} must be at least 1",
                    )
                )
                continue
            if construction is None:
                construction = _skipped_record(
                    candidate,
                    index,
                    kind="construction",
                    reason="construction entities have no STEP representation",
                    severity="info",
                )
                construction["count"] = 0
            construction["count"] += count
            dependency = _dependency_reason(candidate)
            if dependency:
                refusals.append(
                    _refusal_record(
                        candidate,
                        index,
                        reason=f"cannot skip {name!r} because {dependency}",
                    )
                )
            continue

        declared_fem = (
            kind == "fem_air_volume"
            or candidate.get("declaration") == "fem-air-volume"
        )
        if declared_fem:
            solid_count = candidate.get("solid_count")
            if solid_count != 1 or isinstance(solid_count, bool):
                refusals.append(
                    _refusal_record(
                        candidate,
                        index,
                        reason=(
                            f"FEM air volume {name!r} must contain exactly one "
                            "solid; make the component contain one solid"
                        ),
                    )
                )
                continue
            file_name = candidate.get("file")
            if not isinstance(file_name, str) or not file_name:
                refusals.append(
                    _refusal_record(
                        candidate,
                        index,
                        reason=f"FEM air volume {name!r} must name its separate STEP file",
                    )
                )
                continue
            record = _scope_identity(candidate, index)
            record.update(
                {
                    "file": file_name,
                    "n_bodies_expected": 1,
                    "reason": (
                        "declared FEM air volume is exported as a separate "
                        "one-solid member" + external_note
                    ),
                    "severity": "info",
                }
            )
            # Evidence only. ``validate_return_manifest`` derives
            # ``scope.status`` from the skipped and included records alone, so
            # degrading the plan on a FEM record nothing counts would make the
            # manifest fail its own status check.
            if external != "none":
                record["external_reference"] = external
            fem_air_volumes.append(record)
            continue

        managed = candidate.get("wglink_managed") is True
        managed_role = candidate.get("wglink_role")
        if managed and managed_role not in {"waveguide", "enclosure"}:
            # This skip is the one rule that drops a B-rep body without asking
            # whether it is visible, and Fusion's STEP export asks nothing
            # else: ``STEPExportOptions`` takes a Component, so every visible
            # body under it reaches the file whatever this inventory says. A
            # document inserted by an older WGLink still has its leftover
            # shell visible, and nothing here can retroactively hide it -- so
            # the best available outcome for that document is a refusal that
            # NAMES the body in the way, mirroring the 'exclude' rule above.
            # A new insertion hides its own helpers and never reaches this.
            if (
                body_kind in {"solid", "surface"}
                and candidate.get("visible") is not False
            ):
                refusals.append(
                    _refusal_record(
                        candidate,
                        index,
                        reason=(
                            f"WGLink helper body {name!r} is left out of the return "
                            "inventory but is still visible, and Fusion's STEP export "
                            "writes every visible body of the exported component, so "
                            "it would reach the solver as a second radiating surface; "
                            "hide the body itself in the Fusion browser and send "
                            "again, or run Insert again to have WGLink hide it. "
                            "Hiding a folder that contains it will not work: Autodesk "
                            "documents that objects invisible only because their group "
                            "is hidden are exported as if they were visible"
                        ),
                    )
                )
                continue
            record = _skipped_record(
                candidate,
                index,
                kind="wglink_helper",
                reason="WGLink-managed helpers are excluded because the final body carries the solve exterior",
                severity="info",
            )
            skipped.append((record, candidate))
            continue

        if body_kind in {"solid", "surface"} and candidate.get("visible") is False:
            painted = tuple(candidate.get("source_face_roles") or ())
            if painted:
                # Hiding a body is how a user leaves it out, but a painted
                # source on it would silently vanish from the solve. That is
                # missing required geometry, so it refuses rather than
                # becoming a finding that Approve could wave through.
                refusals.append(
                    _refusal_record(
                        candidate,
                        index,
                        reason=(
                            f"hidden body {name!r} carries painted source face(s) "
                            f"{', '.join(painted)}; show the body to solve with "
                            "them, or clear the paint with Set WG Source…"
                        ),
                    )
                )
                continue
            record = _skipped_record(
                candidate,
                index,
                kind="hidden_body",
                reason="hidden bodies are excluded by policy",
                severity="info",
            )
            skipped.append((record, candidate))
            continue

        if body_kind == "solid" and candidate.get("visible") is True:
            included.append(
                _included_record(
                    candidate,
                    index,
                    external_reference=external,
                    reason=(
                        "visible B-rep solids are included in the exterior assembly"
                        + external_note
                    ),
                    severity="degraded" if stale_external else "info",
                )
            )
            degraded = degraded or stale_external
            continue

        declared_shell = candidate.get("declaration") == "exterior-shell"
        managed_shell = managed and managed_role in {"waveguide", "enclosure"}
        if (
            body_kind == "surface"
            and candidate.get("visible") is True
            and (declared_shell or managed_shell)
        ):
            included.append(
                _included_record(
                    candidate,
                    index,
                    external_reference=external,
                    reason=(
                        (
                            "visible surface body is declared as an exterior shell "
                            "and is included"
                            if declared_shell
                            else "visible WGLink-managed surface body is included"
                        )
                        + external_note
                    ),
                    severity="degraded" if stale_external else "info",
                )
            )
            degraded = degraded or stale_external
            continue

        if body_kind == "surface" and candidate.get("visible") is True:
            if (
                candidate.get("only_enclosing_exterior") is True
                and not body_role_declared
            ):
                included.append(
                    _included_record(
                        candidate,
                        index,
                        external_reference=external,
                        reason=(
                            "the only visible body is taken as the exterior shell"
                            + external_note
                        ),
                        severity="degraded" if stale_external else "info",
                    )
                )
                degraded = degraded or stale_external
                continue
            refusals.append(
                _refusal_record(
                    candidate,
                    index,
                    reason=(
                        f"visible surface body {name!r} is not classified. "
                        "If it is a modelling or cutting helper, hide the body "
                        "itself in the browser; if it is part of the acoustic "
                        f"exterior, select it and use {MANAGE_DROPDOWN_NAME} → "
                        "Declare Body… → Exterior shell" + external_note
                    ),
                    reason_code=SCOPE_REASON_UNCLASSIFIED_VISIBLE_SURFACE,
                )
            )
            continue

        refusals.append(
            _refusal_record(
                candidate,
                index,
                reason=(
                    f"{name!r} cannot be classified for export; describe it as "
                    "a construction entity, FEM volume, mesh body, or visible "
                    "B-rep solid/surface"
                ),
            )
        )

    if construction is not None:
        skipped.append((construction, {}))

    # Currency as evidence on a skip the ordinary rules already decided. A
    # stale link is still a degraded reason -- the signal the old
    # short-circuit carried -- but it no longer decides whether the body is
    # in the inventory.
    for record, candidate in skipped:
        external = candidate.get("external_reference", "none")
        if external not in {"resolved-stale", "resolved-current"}:
            continue
        record["external_reference"] = external
        record["reason"] += _EXTERNAL_NOTES[external]
        if external == "resolved-stale" and record["severity"] != "degraded":
            record["severity"] = "degraded"
            degraded = True

    for record, candidate in skipped:
        dependency = _dependency_reason(candidate)
        if dependency:
            name = _display_name(candidate, 0)
            refusals.append(
                _refusal_record(
                    candidate,
                    0,
                    reason=f"cannot skip {name!r} because {dependency}",
                )
            )

    return ScopePlan(
        selection=selection_name,
        included=tuple(included),
        skipped=tuple(record for record, _candidate in skipped),
        fem_air_volumes=tuple(fem_air_volumes),
        refusals=tuple(refusals),
        status="degraded" if degraded else "clean",
    )


def _reject_json_constant(value: str) -> None:
    raise WgReturnError(f"wgreturn JSON contains non-finite constant {value!r}")


def validate_domain_record(
    value: object, *, automatic_feature: bool = False
) -> tuple[str, ...]:
    """Validate a writer domain declaration under the original refusal contract."""
    try:
        return wglink_protocol.validate_domain_record(
            value, automatic_feature=automatic_feature
        )
    except wglink_protocol.ProtocolValidationError as exc:
        raise WgReturnError(str(exc)) from exc


def validate_return_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate the writer structure with its exact endpoint profile."""
    try:
        wglink_protocol.validate_structure(manifest, wglink_protocol.ADDIN_WRITER)
    except wglink_protocol.ProtocolValidationError as exc:
        raise WgReturnError(str(exc)) from exc




def build_return_manifest(
    *,
    return_record: Mapping[str, Any],
    generator: Mapping[str, Any],
    document: Mapping[str, Any],
    coordinate_system: Mapping[str, Any],
    assembly: Mapping[str, Any],
    files: Mapping[str, Any],
    scope: Mapping[str, Any] | ScopePlan,
    instances: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    acoustics: None = None,
    wgreturn_version: str = "1.1",
    required_features: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a detached manifest and validate it before returning it."""

    scope_record = scope.manifest_scope() if isinstance(scope, ScopePlan) else dict(scope)
    fem_present = bool(scope_record.get("fem_air_volumes"))
    features = list(
        BASE_RETURN_FEATURES if required_features is None else required_features
    )
    if fem_present and "fem-air-volume-v1" not in features:
        features.append("fem-air-volume-v1")
    domain = dict(assembly).get("domain")
    if (
        isinstance(domain, Mapping)
        and domain.get("cut_planes")
        and REDUCED_DOMAIN_FEATURE not in features
    ):
        features.append(REDUCED_DOMAIN_FEATURE)
    manifest = {
        "wgreturn_version": wgreturn_version,
        "required_features": features,
        "return": dict(return_record),
        "generator": dict(generator),
        "document": dict(document),
        "coordinate_system": dict(coordinate_system),
        "assembly": dict(assembly),
        "files": dict(files),
        "scope": scope_record,
        "instances": list(instances),
        "sources": list(sources),
        "acoustics": acoustics,
    }
    detached = deepcopy(manifest)
    validate_return_manifest(detached)
    return detached


def dumps_return_manifest(manifest: Mapping[str, Any]) -> str:
    """Serialize validated evidence as deterministic UTF-8 JSON text."""

    validate_return_manifest(manifest)
    try:
        return json.dumps(
            manifest,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise WgReturnError(f"wgreturn manifest is not JSON serializable: {exc}") from exc


def loads_return_manifest(data: str | bytes) -> dict[str, Any]:
    """Parse JSON text and validate it without accepting NaN or Infinity."""

    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        manifest = json.loads(data, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WgReturnError(f"could not parse wgreturn JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise WgReturnError("wgreturn JSON root must be an object")
    validate_return_manifest(manifest)
    return manifest
