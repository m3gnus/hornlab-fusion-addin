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
import math
from pathlib import PurePosixPath
import re
from typing import Any


SUPPORTED_RETURN_FEATURES = frozenset(
    {
        "checksummed-files-v1",
        "assembly-frame-v1",
        "instance-records-v1",
        "fem-air-volume-v1",
    }
)
BASE_RETURN_FEATURES = (
    "checksummed-files-v1",
    "assembly-frame-v1",
    "instance-records-v1",
)

_ULID = re.compile(r"^wgr_[0-9A-HJKMNP-TV-Z]{26}$")
_VERSION = re.compile(r"^(\d+)\.(\d+)$")
_SHA256 = re.compile(r"^sha256:[0-9A-Fa-f]+$")
_FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "freshness",
        "freshness_verdict",
        "healing",
        "healing_record",
        "physical_tag",
        "physical_tags",
        "solver_readiness",
        "solver_ready",
        "symmetry",
        "tag",
        "tag_map",
        "tag_namespace",
        "tags",
    }
)


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


def _refusal_record(
    candidate: Mapping[str, Any], index: int, *, reason: str
) -> dict[str, Any]:
    record = _scope_identity(candidate, index)
    record.update({"decision": "refuse", "reason": reason})
    return record


def plan_export_scope(
    selection: object,
    candidates: Sequence[object],
) -> ScopePlan:
    """Apply §3's first-terminal-rule policy to Fusion-free descriptors.

    Body descriptors use ``body_kind`` (``solid``, ``surface``, or ``mesh``),
    ``visible``, and optional occurrence evidence such as ``suppressed`` and
    ``external_reference``.  Declarations use ``declaration`` with
    ``exterior-shell`` or ``fem-air-volume``.  The four dependency booleans
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

        if external in {"resolved-stale", "resolved-current"}:
            stale = external == "resolved-stale"
            included.append(
                _included_record(
                    candidate,
                    index,
                    external_reference=external,
                    reason=(
                        "resolved external link is stale; included the current "
                        "local snapshot"
                        if stale
                        else "resolved external link is current and included"
                    ),
                    severity="degraded" if stale else "info",
                )
            )
            degraded = degraded or stale
            continue

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
                    "reason": "declared FEM air volume is exported as a separate one-solid member",
                    "severity": "info",
                }
            )
            fem_air_volumes.append(record)
            continue

        managed = candidate.get("wglink_managed") is True
        managed_role = candidate.get("wglink_role")
        if managed and managed_role not in {"waveguide", "enclosure"}:
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
            record = _skipped_record(
                candidate,
                index,
                kind="hidden_body",
                reason="hidden bodies are excluded by policy",
                severity="degraded",
            )
            skipped.append((record, candidate))
            degraded = True
            continue

        if body_kind == "solid" and candidate.get("visible") is True:
            included.append(
                _included_record(
                    candidate,
                    index,
                    external_reference="none",
                    reason="visible B-rep solids are included in the exterior assembly",
                )
            )
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
                    external_reference="none",
                    reason=(
                        "visible surface body is declared as an exterior shell "
                        "and is included"
                        if declared_shell
                        else "visible WGLink-managed surface body is included"
                    ),
                )
            )
            continue

        if body_kind == "surface" and candidate.get("visible") is True:
            refusals.append(
                _refusal_record(
                    candidate,
                    index,
                    reason=(
                        f"visible surface body {name!r} is unclassified; mark it "
                        "'exterior-shell' or exclude it"
                    ),
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


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WgReturnError(f"{label} must be an object")
    return value


def _list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise WgReturnError(f"{label} must be a list")
    return value


def _required(record: Mapping[str, Any], keys: Sequence[str], *, label: str) -> None:
    missing = [key for key in keys if key not in record]
    if missing:
        raise WgReturnError(f"{label} is missing required field(s): {', '.join(missing)}")


def _string(value: object, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        suffix = " or null" if nullable else ""
        raise WgReturnError(f"{label} must be a non-empty string{suffix}")
    return value


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WgReturnError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, *, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WgReturnError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise WgReturnError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise WgReturnError(f"{label} must be a finite number")
    if minimum is not None and number < minimum:
        raise WgReturnError(f"{label} must be >= {minimum}")
    return number


def _point(value: object, *, label: str) -> list[Any]:
    point = _list(value, label=label)
    if len(point) != 3:
        raise WgReturnError(f"{label} must contain three coordinates")
    for axis, coordinate in enumerate(point):
        _number(coordinate, label=f"{label}[{axis}]")
    return point


def _matrix(value: object, *, label: str) -> None:
    rows = _list(value, label=label)
    if len(rows) != 4:
        raise WgReturnError(f"{label} must be a finite 4x4 matrix in millimetres")
    converted: list[list[float]] = []
    for row_index, raw_row in enumerate(rows):
        row = _list(raw_row, label=f"{label}[{row_index}]")
        if len(row) != 4:
            raise WgReturnError(f"{label}[{row_index}] must contain four values")
        converted.append(
            [
                _number(value, label=f"{label}[{row_index}][{column}]")
                for column, value in enumerate(row)
            ]
        )
    if converted[3] != [0.0, 0.0, 0.0, 1.0]:
        raise WgReturnError(f"{label} last row must be [0, 0, 0, 1]")


def _timestamp(value: object, *, label: str) -> None:
    text = _string(value, label=label)
    if not text.endswith("Z"):
        raise WgReturnError(f"{label} must be an RFC-3339 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise WgReturnError(f"{label} must be an RFC-3339 UTC timestamp") from exc


def _member_name(value: object, *, label: str) -> str:
    name = _string(value, label=label)
    if "\\" in name:
        raise WgReturnError(f"{label} must use forward slashes")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or name != path.as_posix():
        raise WgReturnError(f"{label} must be a normalized relative path")
    return name


def _check_forbidden_fields(value: object, *, path: str = "wgreturn") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_EVIDENCE_KEYS:
                raise WgReturnError(
                    f"{path}.{key} is WG-authored verdict data and must not appear "
                    "in CAD evidence"
                )
            _check_forbidden_fields(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_forbidden_fields(child, path=f"{path}[{index}]")


def _validate_fingerprint(value: object, *, label: str) -> None:
    record = _mapping(value, label=label)
    _required(record, ("is_solid", "volume_mm3", "bbox_mm"), label=label)
    if not isinstance(record["is_solid"], bool):
        raise WgReturnError(f"{label}.is_solid must be boolean")
    _number(record["volume_mm3"], label=f"{label}.volume_mm3", minimum=0.0)
    bbox = _list(record["bbox_mm"], label=f"{label}.bbox_mm")
    if len(bbox) != 6:
        raise WgReturnError(f"{label}.bbox_mm must contain six values")
    for index, coordinate in enumerate(bbox):
        _number(coordinate, label=f"{label}.bbox_mm[{index}]")


def _validate_instance(value: object, *, index: int) -> str:
    label = f"instances[{index}]"
    record = _mapping(value, label=label)
    _required(
        record,
        (
            "instance_id",
            "design_id",
            "export_id",
            "export_sequence",
            "build_mode",
            "parameter_prefix",
            "assembly_from_link",
            "chirality",
            "body_evidence",
        ),
        label=label,
    )
    instance_id = _string(record["instance_id"], label=f"{label}.instance_id")
    _string(record["design_id"], label=f"{label}.design_id")
    _string(record["export_id"], label=f"{label}.export_id")
    _integer(record["export_sequence"], label=f"{label}.export_sequence", minimum=1)
    if record["build_mode"] not in {"enclosure", "freestanding"}:
        raise WgReturnError(
            f"{label}.build_mode must be 'enclosure' or 'freestanding'"
        )
    _string(record["parameter_prefix"], label=f"{label}.parameter_prefix")
    _matrix(record["assembly_from_link"], label=f"{label}.assembly_from_link")
    if record["chirality"] != "original":
        raise WgReturnError(
            f"{label}.chirality {record['chirality']!r} is unsupported; mirrored "
            "links have no producer and must be recreated without mirroring"
        )

    optional_strings = (
        "lineage_id",
        "design_hash",
        "geometry_hash",
        "origin_bundle_id",
        "occurrence_path",
    )
    for key in optional_strings:
        if key in record:
            _string(record[key], label=f"{label}.{key}", nullable=True)
    if "edit_version" in record and record["edit_version"] is not None:
        _integer(record["edit_version"], label=f"{label}.edit_version", minimum=1)

    evidence = _mapping(record["body_evidence"], label=f"{label}.body_evidence")
    _required(evidence, ("local_body_state", "observed_at"), label=f"{label}.body_evidence")
    if evidence["local_body_state"] not in {
        "unmodified",
        "modified",
        "missing",
        "unknown",
    }:
        raise WgReturnError(f"{label}.body_evidence.local_body_state is invalid")
    _timestamp(evidence["observed_at"], label=f"{label}.body_evidence.observed_at")
    for key in ("baseline_fingerprint", "observed_fingerprint"):
        if key in evidence and evidence[key] is not None:
            _validate_fingerprint(evidence[key], label=f"{label}.body_evidence.{key}")

    if "source_contract" in record and record["source_contract"] is not None:
        contract = _mapping(record["source_contract"], label=f"{label}.source_contract")
        _required(
            contract,
            (
                "role",
                "throat_z_mm",
                "throat_plane_link",
                "axis_link",
                "throat_diameter_mm",
                "expected_disc_area_mm2",
            ),
            label=f"{label}.source_contract",
        )
        _string(contract["role"], label=f"{label}.source_contract.role")
        _number(contract["throat_z_mm"], label=f"{label}.source_contract.throat_z_mm")
        plane = _mapping(
            contract["throat_plane_link"],
            label=f"{label}.source_contract.throat_plane_link",
        )
        _required(plane, ("origin_mm", "normal"), label=f"{label}.source_contract.throat_plane_link")
        _point(plane["origin_mm"], label=f"{label}.source_contract.throat_plane_link.origin_mm")
        _point(plane["normal"], label=f"{label}.source_contract.throat_plane_link.normal")
        axis = _mapping(contract["axis_link"], label=f"{label}.source_contract.axis_link")
        _required(axis, ("origin_mm", "direction"), label=f"{label}.source_contract.axis_link")
        _point(axis["origin_mm"], label=f"{label}.source_contract.axis_link.origin_mm")
        _point(axis["direction"], label=f"{label}.source_contract.axis_link.direction")
        _number(
            contract["throat_diameter_mm"],
            label=f"{label}.source_contract.throat_diameter_mm",
            minimum=0.0,
        )
        _number(
            contract["expected_disc_area_mm2"],
            label=f"{label}.source_contract.expected_disc_area_mm2",
            minimum=0.0,
        )
    return instance_id


def _validate_source(
    value: object, *, index: int, instance_ids: set[str]
) -> tuple[str, str]:
    label = f"sources[{index}]"
    record = _mapping(value, label=label)
    _required(
        record,
        (
            "id",
            "role",
            "required",
            "default_drive_channel_id",
            "patch_policy",
            "expected_connected_components",
            "selectors",
            "observed",
        ),
        label=label,
    )
    source_id = _string(record["id"], label=f"{label}.id")
    role = _string(record["role"], label=f"{label}.role")
    if "instance_id" in record and record["instance_id"] is not None:
        linked_instance = _string(record["instance_id"], label=f"{label}.instance_id")
        if linked_instance not in instance_ids:
            raise WgReturnError(
                f"{label}.instance_id {linked_instance!r} does not name an instance"
            )
    if not isinstance(record["required"], bool):
        raise WgReturnError(f"{label}.required must be boolean")
    channel_id = _string(
        record["default_drive_channel_id"],
        label=f"{label}.default_drive_channel_id",
    )
    if record["patch_policy"] not in {
        "single-connected",
        "explicit-disconnected",
    }:
        raise WgReturnError(f"{label}.patch_policy is invalid")
    _integer(
        record["expected_connected_components"],
        label=f"{label}.expected_connected_components",
        minimum=1,
    )

    selectors = _mapping(record["selectors"], label=f"{label}.selectors")
    mechanisms = 0
    linked = selectors.get("linked_throat")
    if linked is not None:
        linked_record = _mapping(linked, label=f"{label}.selectors.linked_throat")
        _required(linked_record, ("instance_id",), label=f"{label}.selectors.linked_throat")
        linked_id = _string(
            linked_record["instance_id"],
            label=f"{label}.selectors.linked_throat.instance_id",
        )
        if linked_id not in instance_ids:
            raise WgReturnError(
                f"{label}.selectors.linked_throat.instance_id {linked_id!r} "
                "does not name an instance"
            )
        mechanisms += 1
    for key in ("appearance_labels", "shell_names"):
        if key in selectors:
            values = _list(selectors[key], label=f"{label}.selectors.{key}")
            if not all(isinstance(item, str) and item for item in values):
                raise WgReturnError(f"{label}.selectors.{key} must contain strings")
            mechanisms += bool(values)
    if "advanced_face_indices" in selectors:
        indices = _list(
            selectors["advanced_face_indices"],
            label=f"{label}.selectors.advanced_face_indices",
        )
        for face_index, value in enumerate(indices):
            _integer(
                value,
                label=f"{label}.selectors.advanced_face_indices[{face_index}]",
            )
        mechanisms += bool(indices)
    if not mechanisms:
        raise WgReturnError(f"{label}.selectors must contain at least one mechanism")

    observed = _mapping(record["observed"], label=f"{label}.observed")
    _required(
        observed,
        ("face_count", "total_area_mm2", "per_face_area_mm2", "bodies"),
        label=f"{label}.observed",
    )
    face_count = _integer(
        observed["face_count"], label=f"{label}.observed.face_count", minimum=1
    )
    _number(
        observed["total_area_mm2"],
        label=f"{label}.observed.total_area_mm2",
        minimum=0.0,
    )
    areas = _list(
        observed["per_face_area_mm2"], label=f"{label}.observed.per_face_area_mm2"
    )
    if len(areas) != face_count:
        raise WgReturnError(
            f"{label}.observed.per_face_area_mm2 must contain face_count entries"
        )
    for area_index, area in enumerate(areas):
        _number(
            area,
            label=f"{label}.observed.per_face_area_mm2[{area_index}]",
            minimum=0.0,
        )
    bodies = _list(observed["bodies"], label=f"{label}.observed.bodies")
    if not bodies or not all(isinstance(body, str) and body for body in bodies):
        raise WgReturnError(f"{label}.observed.bodies must contain body names")
    if "suggested_resolution_mm" in record and record["suggested_resolution_mm"] is not None:
        _number(
            record["suggested_resolution_mm"],
            label=f"{label}.suggested_resolution_mm",
            minimum=0.0,
        )
    return source_id, channel_id


def validate_return_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate the §2 schema and evidence/verdict ownership boundary."""

    root = _mapping(manifest, label="wgreturn")
    _check_forbidden_fields(root)
    _required(
        root,
        (
            "wgreturn_version",
            "required_features",
            "return",
            "generator",
            "document",
            "coordinate_system",
            "assembly",
            "files",
            "scope",
            "instances",
            "sources",
        ),
        label="wgreturn",
    )
    version = root["wgreturn_version"]
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None or int(match.group(1)) != 1:
        raise WgReturnError(
            f"unsupported wgreturn_version {version!r}; reader supports major 1"
        )

    features = _list(root["required_features"], label="required_features")
    if not all(isinstance(feature, str) and feature for feature in features):
        raise WgReturnError("required_features must contain feature names")
    if len(features) != len(set(features)):
        raise WgReturnError("required_features must contain unique feature names")
    missing_features = sorted(set(BASE_RETURN_FEATURES).difference(features))
    if missing_features:
        raise WgReturnError(
            "required_features is missing launch feature(s): "
            + ", ".join(missing_features)
        )
    unknown = sorted(set(features).difference(SUPPORTED_RETURN_FEATURES))
    if unknown:
        raise WgReturnError(
            "unsupported required_features: " + ", ".join(repr(item) for item in unknown)
        )

    return_record = _mapping(root["return"], label="return")
    _required(return_record, ("id", "created_at"), label="return")
    return_id = _string(return_record["id"], label="return.id")
    if not _ULID.fullmatch(return_id):
        raise WgReturnError("return.id must be a wgr_-prefixed ULID")
    _timestamp(return_record["created_at"], label="return.created_at")

    generator = _mapping(root["generator"], label="generator")
    _required(
        generator,
        ("adapter", "adapter_version", "cad_app", "cad_version"),
        label="generator",
    )
    for key in ("adapter", "adapter_version", "cad_app", "cad_version"):
        _string(generator[key], label=f"generator.{key}")

    document = _mapping(root["document"], label="document")
    _required(document, ("name",), label="document")
    _string(document["name"], label="document.name")
    if "native_id" in document:
        _string(document["native_id"], label="document.native_id", nullable=True)

    coordinate = _mapping(root["coordinate_system"], label="coordinate_system")
    _required(
        coordinate,
        ("length_unit", "handedness", "matrix_convention"),
        label="coordinate_system",
    )
    expected_coordinate = {
        "length_unit": "mm",
        "handedness": "right",
        "matrix_convention": "row-major-local-to-parent",
    }
    for key, expected in expected_coordinate.items():
        if coordinate[key] != expected:
            raise WgReturnError(
                f"coordinate_system.{key} must be {expected!r}, got {coordinate[key]!r}"
            )

    assembly = _mapping(root["assembly"], label="assembly")
    _required(assembly, ("file", "n_bodies_expected", "bbox_mm"), label="assembly")
    assembly_file = _member_name(assembly["file"], label="assembly.file")
    expected_bodies = _integer(
        assembly["n_bodies_expected"], label="assembly.n_bodies_expected"
    )
    bbox = _list(assembly["bbox_mm"], label="assembly.bbox_mm")
    if len(bbox) != 2:
        raise WgReturnError("assembly.bbox_mm must contain minimum and maximum points")
    low = _point(bbox[0], label="assembly.bbox_mm[0]")
    high = _point(bbox[1], label="assembly.bbox_mm[1]")
    if any(float(left) > float(right) for left, right in zip(low, high)):
        raise WgReturnError("assembly.bbox_mm minimum exceeds maximum")

    files = _mapping(root["files"], label="files")
    if not files:
        raise WgReturnError("files must contain the complete artifact inventory")
    file_names: set[str] = set()
    for raw_name, raw_record in files.items():
        name = _member_name(raw_name, label="files key")
        if name in file_names:
            raise WgReturnError(f"files contains duplicate path {name!r}")
        file_names.add(name)
        record = _mapping(raw_record, label=f"files[{name!r}]")
        _required(
            record,
            ("sha256", "size_bytes", "media_type", "purpose"),
            label=f"files[{name!r}]",
        )
        digest = _string(record["sha256"], label=f"files[{name!r}].sha256")
        if not _SHA256.fullmatch(digest):
            raise WgReturnError(f"files[{name!r}].sha256 must use sha256:<hex>")
        _integer(record["size_bytes"], label=f"files[{name!r}].size_bytes")
        _string(record["media_type"], label=f"files[{name!r}].media_type")
        if record["purpose"] not in {"exterior-assembly", "fem-air-volume"}:
            raise WgReturnError(f"files[{name!r}].purpose is invalid")
    if assembly_file not in files:
        raise WgReturnError("assembly.file is not present in files")
    if files[assembly_file].get("purpose") != "exterior-assembly":
        raise WgReturnError("assembly.file must have purpose 'exterior-assembly'")

    scope = _mapping(root["scope"], label="scope")
    _required(
        scope,
        ("selection", "included", "skipped", "fem_air_volumes", "status"),
        label="scope",
    )
    _string(scope["selection"], label="scope.selection")
    included = _list(scope["included"], label="scope.included")
    skipped = _list(scope["skipped"], label="scope.skipped")
    fem_volumes = _list(scope["fem_air_volumes"], label="scope.fem_air_volumes")
    if scope["status"] not in {"clean", "degraded"}:
        raise WgReturnError("scope.status must be 'clean' or 'degraded'")
    for index, raw_record in enumerate(included):
        record = _mapping(raw_record, label=f"scope.included[{index}]")
        _required(
            record,
            ("object_id", "name", "component", "body_kind", "visible", "external_reference"),
            label=f"scope.included[{index}]",
        )
        if record["body_kind"] not in {"solid", "surface"}:
            raise WgReturnError(f"scope.included[{index}].body_kind is invalid")
        if record["visible"] is not True:
            raise WgReturnError(f"scope.included[{index}].visible must be true")
        if record["external_reference"] not in {
            "none",
            "resolved-stale",
            "resolved-current",
        }:
            raise WgReturnError(
                f"scope.included[{index}].external_reference is invalid"
            )
    if len(included) != expected_bodies:
        raise WgReturnError(
            "scope.included count must equal assembly.n_bodies_expected"
        )
    degraded_reasons = 0
    for index, raw_record in enumerate(skipped):
        record = _mapping(raw_record, label=f"scope.skipped[{index}]")
        _required(record, ("kind", "reason", "severity"), label=f"scope.skipped[{index}]")
        _string(record["reason"], label=f"scope.skipped[{index}].reason")
        if record["severity"] not in {"info", "degraded"}:
            raise WgReturnError(f"scope.skipped[{index}].severity is invalid")
        degraded_reasons += record["severity"] == "degraded"
    for record in included:
        degraded_reasons += record.get("severity") == "degraded"
        degraded_reasons += record.get("external_reference") == "resolved-stale"
    if bool(degraded_reasons) != (scope["status"] == "degraded"):
        raise WgReturnError(
            "scope.status must be degraded exactly when a recorded scope reason is degraded"
        )

    fem_files: set[str] = set()
    for index, raw_record in enumerate(fem_volumes):
        record = _mapping(raw_record, label=f"scope.fem_air_volumes[{index}]")
        _required(
            record,
            ("file", "n_bodies_expected"),
            label=f"scope.fem_air_volumes[{index}]",
        )
        name = _member_name(record["file"], label=f"scope.fem_air_volumes[{index}].file")
        if record["n_bodies_expected"] != 1:
            raise WgReturnError(
                f"scope.fem_air_volumes[{index}] must expect exactly one solid"
            )
        if name not in files or files[name].get("purpose") != "fem-air-volume":
            raise WgReturnError(
                f"scope.fem_air_volumes[{index}].file must name a FEM file inventory entry"
            )
        fem_files.add(name)
    inventoried_fem = {
        name for name, record in files.items() if record.get("purpose") == "fem-air-volume"
    }
    if fem_files != inventoried_fem:
        raise WgReturnError("scope FEM volumes and files inventory must agree exactly")
    has_fem_feature = "fem-air-volume-v1" in features
    if bool(fem_volumes) != has_fem_feature:
        raise WgReturnError(
            "fem-air-volume-v1 is required exactly when a FEM air volume is present"
        )

    instances = _list(root["instances"], label="instances")
    instance_ids: list[str] = []
    for index, instance in enumerate(instances):
        instance_ids.append(_validate_instance(instance, index=index))
    if len(instance_ids) != len(set(instance_ids)):
        raise WgReturnError("instances must have unique instance_id values")
    anchor = coordinate.get("solver_anchor_instance_id")
    if anchor is not None:
        anchor = _string(anchor, label="coordinate_system.solver_anchor_instance_id")
    if not instances and anchor is not None:
        raise WgReturnError("an unlinked return must not name a solver anchor instance")
    if len(instances) > 1 and anchor is None:
        raise WgReturnError(
            "multiple linked instances require solver_anchor_instance_id"
        )
    if anchor is not None and anchor not in instance_ids:
        raise WgReturnError(
            "coordinate_system.solver_anchor_instance_id does not name an instance"
        )

    sources = _list(root["sources"], label="sources")
    if not sources:
        raise WgReturnError("sources must contain at least one drivable patch")
    source_keys = [
        _validate_source(source, index=index, instance_ids=set(instance_ids))
        for index, source in enumerate(sources)
    ]
    source_ids = [source_id for source_id, _channel in source_keys]
    channel_ids = [channel for _source_id, channel in source_keys]
    if len(source_ids) != len(set(source_ids)):
        raise WgReturnError("sources must have unique id values")
    if len(channel_ids) != len(set(channel_ids)):
        raise WgReturnError("sources must have unique default_drive_channel_id values")
    if root.get("acoustics") is not None:
        raise WgReturnError("acoustics must be null in wgreturn_version 1.0")


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
    wgreturn_version: str = "1.0",
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
