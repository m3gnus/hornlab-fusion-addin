"""Observe a Fusion assembly and publish an immutable WG return bundle.

The policy module owns classification and manifest validation.  This module is
the deliberately thin Fusion boundary: it reads live entities, exports STEP,
checks that Fusion wrote the promised inventory, and publishes without ever
editing the open design.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from typing import Any
import uuid

import adsk.core
import adsk.fusion

import wglink_core
from wglink_return import (
    WgReturnError,
    build_return_manifest,
    dumps_return_manifest,
    plan_export_scope,
)


DECLARATION_ATTRIBUTE = "return_declaration"
DECLARATIONS = frozenset({"exterior-shell", "exclude"})
FEM_COMPONENT_NAME = "FEM_MF_AIR"
SOURCE_ROLES = ("LF", "MF", "HF", "PORT_EXIT")
SOURCE_RESOLUTION_MM = {"HF": 4.0, "MF": 15.0, "LF": 30.0, "PORT_EXIT": 25.0}
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_STEP_BODY = re.compile(r"\b(?:MANIFOLD_SOLID_BREP|SHELL_BASED_SURFACE_MODEL)\s*\(", re.I)


def _adapter_version() -> str:
    try:
        data = json.loads((Path(__file__).with_name("WGLink.manifest")).read_text("utf-8"))
        value = str(data.get("version", "")).strip()
        if value:
            return value
    except Exception:  # noqa: BLE001 - packaging metadata has a safe fallback
        pass
    return "unknown"


ADAPTER_VERSION = _adapter_version()


def declare_body(body: object, declaration: str) -> None:
    """Set or replace the explicit return classification on one body."""

    value = str(declaration).strip().lower()
    if value not in DECLARATIONS:
        choices = ", ".join(sorted(DECLARATIONS))
        raise wglink_core.WgLinkError(f"body declaration must be one of: {choices}")
    wglink_core._set_attribute(body, DECLARATION_ATTRIBUTE, value)


def read_declaration(body: object) -> str | None:
    """Read a valid explicit return classification without repairing it."""

    value = wglink_core._attribute_value(body, DECLARATION_ATTRIBUTE)
    if value is None:
        return None
    value = value.strip().lower()
    return value if value in DECLARATIONS else None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def generate_return_id(timestamp_ms: int | None = None) -> str:
    """Mint a prefixed ULID using Fusion's standard-library-only runtime."""

    if timestamp_ms is None:
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise ValueError("ULID timestamp must be an integer number of milliseconds")
    if timestamp_ms < 0 or timestamp_ms >= 1 << 48:
        raise ValueError("ULID timestamp is outside the 48-bit range")
    value = (timestamp_ms << 80) | secrets.randbits(80)
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "wgr_" + "".join(encoded)


def _selection_items(value: object) -> list[object]:
    if value is None or value == "root":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return [value.item(index) for index in range(value.count)]
    except Exception:  # noqa: BLE001
        return [value]


def _occurrence_path(occurrence: object) -> str:
    for name in ("fullPathName", "name"):
        try:
            value = str(getattr(occurrence, name)).strip()
            if value:
                return value
        except Exception:  # noqa: BLE001
            pass
    return "unnamed occurrence"


def _selection(design: object, value: object) -> tuple[object, object, object]:
    root = design.rootComponent
    selected = _selection_items(value)
    if not selected or selected == [root]:
        return "root", root, root
    if len(selected) != 1:
        raise wglink_core.WgLinkError(
            "Select the root or exactly one occurrence subtree; several selections are not supported."
        )
    entity = getattr(selected[0], "entity", selected[0])
    kind = wglink_core._kind(entity)
    if kind != "Occurrence" and not (
        getattr(entity, "component", None) is not None
        and (hasattr(entity, "transform2") or hasattr(entity, "transform"))
    ):
        label = (kind or type(entity).__name__).lower()
        raise wglink_core.WgLinkError(
            f"Cannot export a selected {label}; select the root or exactly one occurrence subtree."
        )
    path = _occurrence_path(entity)
    return {"kind": "occurrence", "path": path}, entity, entity


def _component_name(component: object) -> str:
    try:
        value = str(component.name).strip()
        return value or "unnamed component"
    except Exception:  # noqa: BLE001
        return "unnamed component"


def _object_id(entity: object, fallback: str) -> str:
    token = wglink_core._entity_token(entity)
    return token or fallback


def _bool(entity: object, names: tuple[str, ...], default: bool) -> bool:
    for name in names:
        try:
            value = getattr(entity, name)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(value, bool):
            return value
    return default


def _external_reference(occurrence: object | None) -> str:
    if occurrence is None:
        return "none"
    try:
        direct = str(occurrence.external_reference)
        if direct in {"none", "resolved-current", "resolved-stale", "unresolved"}:
            return direct
    except Exception:  # noqa: BLE001
        pass
    referenced = _bool(
        occurrence, ("isReferencedComponent", "isExternalReference"), False
    )
    if not referenced:
        return "none"
    reference = None
    for name in ("documentReference", "externalReference", "dataFile"):
        try:
            reference = getattr(occurrence, name)
        except Exception:  # noqa: BLE001
            continue
        if reference is not None:
            break
    if not _bool(occurrence, ("isValid",), True) or (
        reference is not None and not _bool(reference, ("isValid",), True)
    ):
        return "unresolved"
    if getattr(occurrence, "component", None) is None:
        return "unresolved"
    if _bool(occurrence, ("isOutOfDate", "isStale"), False) or (
        reference is not None
        and _bool(reference, ("isOutOfDate", "isStale"), False)
    ):
        return "resolved-stale"
    return "resolved-current"


def _role(body: object) -> str | None:
    value = wglink_core._body_role(body)
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _instance_id(body: object) -> str | None:
    value = wglink_core._attribute_value(body, "instance_id")
    return value if value else None


def _face_role(face: object) -> str | None:
    value = wglink_core._appearance_name(face)
    if not isinstance(value, str):
        return None
    canonical = value.strip().upper()
    return canonical if canonical in SOURCE_ROLES else None


def _has_source_face(body: object) -> bool:
    return any(_face_role(face) is not None for face in wglink_core._items(getattr(body, "faces", None)))


def _collection(entity: object, name: str) -> list[object]:
    try:
        return wglink_core._items(getattr(entity, name))
    except Exception:  # noqa: BLE001
        return []


def _is_fem_component(component: object) -> bool:
    return _component_name(component).casefold() == FEM_COMPONENT_NAME.casefold()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "unnamed"


def _fem_slug(component: object) -> str:
    name = _component_name(component)
    if name.upper().startswith("FEM_"):
        name = name[4:]
    return _slug(name)


def _scope_walk(design: object, selection_value: object) -> dict[str, Any]:
    selection, geometry, selected_entity = _selection(design, selection_value)
    candidates: list[dict[str, Any]] = []
    bodies: dict[str, object] = {}
    fem_components: dict[str, object] = {}
    components: list[object] = []
    construction_count = 0
    serial = 0

    def add_body(
        body: object,
        component: object,
        path: str,
        occurrence: object | None,
        *,
        mesh: bool = False,
        suppressed: bool = False,
    ) -> None:
        nonlocal serial
        serial += 1
        name = str(getattr(body, "name", "") or f"body {serial}")
        object_id = _object_id(body, f"body-{serial:04d}")
        if object_id in bodies:
            object_id = f"{object_id}@{path}"
        visible = _bool(body, ("isVisible", "isLightBulbOn"), True)
        if occurrence is not None:
            visible = visible and _bool(
                occurrence, ("isVisible", "isLightBulbOn"), True
            )
        # An occurrence PROXY exposes an empty attribute collection (measured
        # in Fusion 2704: the probe read zero attributes off the proxy while
        # the native body carried the whole payload), so identity is read from
        # the native object; the proxy stays the geometry/claim handle.
        native = getattr(body, "nativeObject", None) or body
        instance_id = _instance_id(native)
        managed_role = _role(native)
        candidate = {
            "kind": "mesh_body" if mesh else "body",
            "body_kind": "mesh" if mesh else ("solid" if bool(getattr(body, "isSolid", False)) else "surface"),
            "visible": visible,
            "suppressed": suppressed or _bool(body, ("isSuppressed",), False),
            "external_reference": _external_reference(occurrence),
            "declaration": read_declaration(native),
            "component": _component_name(component),
            "name": name,
            "path": f"{path}/{name}" if path else name,
            "object_id": object_id,
            "wglink_managed": bool(instance_id or managed_role),
            "wglink_role": managed_role,
            "wglink_instance_id": instance_id,
            "contains_required_source": bool(instance_id) or _has_source_face(body),
            "contains_solver_anchor": False,
            "only_enclosing_exterior": False,
            "requested_fem_air_volume": False,
        }
        candidates.append(candidate)
        bodies[object_id] = body

    def walk_component(
        component: object,
        path: str,
        occurrence: object | None,
        suppressed: bool,
    ) -> None:
        nonlocal construction_count, serial
        components.append(component)
        if _is_fem_component(component):
            serial += 1
            solids = [body for body in _collection(component, "bRepBodies") if bool(getattr(body, "isSolid", False))]
            object_id = _object_id(component, f"fem-{serial:04d}")
            file_name = f"fem/{_fem_slug(component)}.step"
            candidates.append(
                {
                    "kind": "fem_air_volume",
                    "name": _component_name(component),
                    "component": _component_name(component),
                    "path": path,
                    "object_id": object_id,
                    "solid_count": len(solids),
                    "file": file_name,
                    "visible": True,
                    "external_reference": _external_reference(occurrence),
                    "requested_fem_air_volume": True,
                }
            )
            fem_components[object_id] = component
            return

        body_owner = occurrence if _collection(occurrence, "bRepBodies") else component
        for body in _collection(body_owner, "bRepBodies"):
            add_body(body, component, path, occurrence, suppressed=suppressed)
        mesh_owner = occurrence if _collection(occurrence, "meshBodies") else component
        for body in _collection(mesh_owner, "meshBodies"):
            add_body(body, component, path, occurrence, mesh=True, suppressed=suppressed)
        construction_count += sum(
            len(_collection(component, name))
            for name in ("constructionPlanes", "constructionAxes", "sketches")
        )

        children = _collection(occurrence, "childOccurrences") if occurrence is not None else _collection(component, "occurrences")
        if occurrence is not None and not children:
            children = _collection(component, "occurrences")
        for child in children:
            child_path = _occurrence_path(child)
            child_suppressed = suppressed or _bool(child, ("isSuppressed",), False)
            external = _external_reference(child)
            child_component = getattr(child, "component", None)
            if external == "unresolved" or child_component is None:
                serial += 1
                candidates.append(
                    {
                        "kind": "body",
                        "body_kind": "solid",
                        "visible": True,
                        "suppressed": child_suppressed,
                        "external_reference": "unresolved" if external != "none" else external,
                        "component": _component_name(child_component) if child_component else child_path,
                        "name": child_path,
                        "path": child_path,
                        "object_id": _object_id(child, f"occurrence-{serial:04d}"),
                    }
                )
                continue
            walk_component(child_component, child_path, child, child_suppressed)

    if selection == "root":
        walk_component(design.rootComponent, _component_name(design.rootComponent), None, False)
    else:
        occurrence = selected_entity
        component = getattr(occurrence, "component", None)
        external = _external_reference(occurrence)
        if component is None or external == "unresolved":
            candidates.append(
                {
                    "kind": "body",
                    "body_kind": "solid",
                    "visible": True,
                    "external_reference": "unresolved",
                    "component": _occurrence_path(occurrence),
                    "name": _occurrence_path(occurrence),
                    "path": _occurrence_path(occurrence),
                    "object_id": _object_id(occurrence, "occurrence-0001"),
                }
            )
        else:
            walk_component(
                component,
                _occurrence_path(occurrence),
                occurrence,
                _bool(occurrence, ("isSuppressed",), False),
            )

    if construction_count:
        candidates.append(
            {
                "kind": "construction",
                "object_id": "construction-aggregate",
                "name": "construction entities",
                "count": construction_count,
            }
        )
    exterior = [
        item
        for item in candidates
        if item.get("body_kind") in {"solid", "surface"}
        and item.get("kind") == "body"
        and item.get("declaration") != "exclude"
        and not (
            item.get("wglink_managed")
            and item.get("wglink_role") not in {"waveguide", "enclosure"}
        )
    ]
    if len(exterior) == 1:
        exterior[0]["only_enclosing_exterior"] = True
    return {
        "selection": selection,
        "geometry": geometry,
        "candidates": candidates,
        "bodies": bodies,
        "fem_components": fem_components,
        "components": components,
    }


def _record_body(record: dict[str, Any]) -> object | None:
    bodies = [
        entity
        for entity in record.get("entities", [])
        if _role(entity) in {"waveguide", "enclosure"}
    ]
    if len(bodies) > 1:
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} belongs to several managed bodies; Detach the duplicate."
        )
    return bodies[0] if bodies else None


def _records_in_scope(design: object, walk: dict[str, Any]) -> list[dict[str, Any]]:
    records = wglink_core._link_records(design)
    if walk["selection"] == "root":
        chosen = list(records.values())
    else:
        component_ids = {id(component) for component in walk["components"]}
        chosen = []
        candidate_ids = {
            item.get("wglink_instance_id")
            for item in walk["candidates"]
            if item.get("wglink_instance_id")
        }
        for instance_id, record in records.items():
            entities = record.get("entities", [])
            in_component = any(
                id(entity) in component_ids
                or id(getattr(entity, "parentComponent", None)) in component_ids
                for entity in entities
            )
            if instance_id in candidate_ids or in_component:
                chosen.append(record)
    for record in chosen:
        record["body"] = _record_body(record)
        wrappers = [
            entity
            for entity in record.get("wrappers", [])
            if wglink_core._kind(entity) == "Component" or hasattr(entity, "bRepBodies")
        ]
        if len(wrappers) > 1:
            raise wglink_core.WgLinkError(
                f"WGLink instance {record['instance_id']!r} has several wrapper components."
            )
        record["wrapper_component"] = wrappers[0] if wrappers else getattr(record.get("body"), "parentComponent", None)
    return sorted(chosen, key=lambda item: str(item["instance_id"]))


def inspect_scope(app: object, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return selection and linked-instance choices without exporting anything."""

    opts = dict(options or {})
    design = wglink_core._design(app)
    walk = _scope_walk(design, opts.get("selection"))
    records = _records_in_scope(design, walk)
    return {
        "selection": walk["selection"],
        "instance_ids": [record["instance_id"] for record in records],
    }


def _strict_matrix_rows(matrix: object, instance_id: str) -> list[list[float]]:
    if matrix is None:
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} has no readable occurrence transform."
        )
    try:
        values = [float(value) for value in matrix.asArray()]
        return wglink_core.fusion_matrix_to_mm(values)
    except Exception as exc:  # noqa: BLE001 - identity would be a dangerous fallback
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} has an unreadable occurrence transform: {exc}."
        ) from exc


def _matching_occurrences(design: object, record: dict[str, Any]) -> list[object]:
    payload = record.get("payload", {})
    token = str(payload.get("occurrence_token") or "")
    matches = [
        entity
        for entity in wglink_core._find_by_token(design, token)
        if wglink_core._kind(entity) == "Occurrence" or hasattr(entity, "component")
    ]
    if matches:
        return matches
    component = record.get("wrapper_component")
    result = []
    for occurrence in wglink_core._items(getattr(design.rootComponent, "allOccurrences", None)):
        try:
            if occurrence.component == component:
                result.append(occurrence)
        except Exception:  # noqa: BLE001
            continue
    return result


def _strict_assembly_from_link(design: object, record: dict[str, Any]) -> tuple[list[list[float]], str | None]:
    instance_id = str(record["instance_id"])
    if record.get("payload", {}).get("wrapper") == "root":
        return [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ], None
    occurrences = _matching_occurrences(design, record)
    if not occurrences:
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} has no resolvable wrapper occurrence; placement was not defaulted to identity."
        )
    if len(occurrences) > 1:
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} has multiple wrapper occurrences; placement is ambiguous."
        )
    occurrence = occurrences[0]
    # transform2 is PARENT-relative. For a root-level wrapper that is the
    # assembly transform; for a wrapper nested inside another occurrence it is
    # not, and composing the chain is unverified arithmetic on the trust path
    # (C4). Refusing is the R19 rule: never a plausible default.
    if getattr(occurrence, "assemblyContext", None) is not None:
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} wrapper is nested inside another "
            "occurrence; its assembly placement cannot be recorded faithfully. "
            "Move the wrapper to the root level and send again."
        )
    try:
        matrix = getattr(occurrence, "transform2", None)
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} has an unreadable transform2: {exc}."
        ) from exc
    if matrix is None:
        try:
            matrix = occurrence.transform
        except Exception as exc:  # noqa: BLE001
            raise wglink_core.WgLinkError(
                f"WGLink instance {instance_id!r} has an unreadable occurrence transform: {exc}."
            ) from exc
    return _strict_matrix_rows(matrix, instance_id), _occurrence_path(occurrence)


def _nullable(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _integer_echo(payload: dict[str, Any], key: str, *, required: bool) -> int | None:
    value = _nullable(payload.get(key))
    if value is None:
        if required:
            raise wglink_core.WgLinkError(f"WGLink payload is missing required {key!r}.")
        return None
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise wglink_core.WgLinkError(f"WGLink payload {key!r} is not an integer: {value!r}.") from exc
    return result


def _float_echo(payload: dict[str, Any], key: str) -> float | None:
    value = _nullable(payload.get(key))
    if value is None:
        return None
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _stored_fingerprint(value: object) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    try:
        parsed = json.loads(str(value)) if not isinstance(value, dict) else value
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _stored_config(value: object) -> dict[str, Any] | None:
    """Decode the exact WG config snapshot stored on a managed instance."""

    if value is None or value == "":
        return None
    try:
        parsed = json.loads(str(value)) if not isinstance(value, dict) else value
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, dict) and parsed else None


def _named(collection: object, name: str) -> object | None:
    try:
        found = collection.itemByName(name)
        if found is not None:
            return found
    except Exception:  # noqa: BLE001
        pass
    for item in wglink_core._items(collection):
        try:
            if str(item.name) == name:
                return item
        except Exception:  # noqa: BLE001
            continue
    return None


def _xyz(value: object, *, scale: float) -> list[float]:
    return [float(value.x) * scale, float(value.y) * scale, float(value.z) * scale]


def _source_contract(design: object, record: dict[str, Any]) -> dict[str, Any] | None:
    payload = record.get("payload", {})
    component = record.get("wrapper_component") or getattr(record.get("body"), "parentComponent", None)
    if component is None:
        return None
    plane = _named(getattr(component, "constructionPlanes", None), "WG_THROAT_PLANE")
    axis = _named(getattr(component, "constructionAxes", None), "WG_AXIS")
    prefix = _nullable(payload.get("parameter_prefix"))
    role = _nullable(payload.get("source_role"))
    throat_z = _float_echo(payload, "throat_z_mm")
    expected_area = _float_echo(payload, "expected_throat_area_mm2")
    if None in (plane, axis, prefix, role, throat_z, expected_area):
        return None
    parameter = _named(getattr(design, "userParameters", None), f"{prefix}throat_dia")
    if parameter is None:
        return None
    try:
        if hasattr(parameter, "value_mm"):
            diameter = float(parameter.value_mm)
        else:
            diameter = float(parameter.value) * 10.0
        plane_geometry = getattr(plane, "geometry", plane)
        axis_geometry = getattr(axis, "geometry", axis)
        plane_origin = _xyz(plane_geometry.origin, scale=10.0)
        plane_normal = _xyz(plane_geometry.normal, scale=1.0)
        axis_origin = _xyz(axis_geometry.origin, scale=10.0)
        axis_direction = _xyz(axis_geometry.direction, scale=1.0)
    except Exception:  # noqa: BLE001 - a partial datum contract is no contract
        return None
    if diameter <= 0.0:
        return None
    disc_area = math.pi * diameter * diameter / 4.0
    if expected_area <= 0.0 or not math.isclose(expected_area, disc_area, rel_tol=0.01):
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} has a throat diameter whose disc area contradicts the stored expected area."
        )
    return {
        "role": str(role),
        "throat_z_mm": throat_z,
        "throat_plane_link": {"origin_mm": plane_origin, "normal": plane_normal},
        "axis_link": {"origin_mm": axis_origin, "direction": axis_direction},
        "throat_diameter_mm": diameter,
        "expected_disc_area_mm2": expected_area,
    }


def _instance_record(design: object, record: dict[str, Any], observed_at: str) -> dict[str, Any]:
    payload = record.get("payload", {})
    matrix, occurrence_path = _strict_assembly_from_link(design, record)
    body = record.get("body")
    baseline = _stored_fingerprint(payload.get("body_fingerprint"))
    observed = wglink_core._body_fingerprint(body) if body is not None else None
    result = {
        "instance_id": str(record["instance_id"]),
        "design_id": _nullable(payload.get("design_id")),
        "lineage_id": _nullable(payload.get("lineage_id")),
        "edit_version": _integer_echo(payload, "edit_version", required=False),
        "design_hash": _nullable(payload.get("design_hash")),
        "formula": _nullable(payload.get("formula")),
        "config": _stored_config(payload.get("config_json")),
        "export_id": _nullable(payload.get("export_id")),
        "export_sequence": _integer_echo(payload, "export_sequence", required=True),
        "geometry_hash": _nullable(payload.get("geometry_hash")),
        "origin_bundle_id": _nullable(payload.get("bundle_id")),
        "build_mode": _nullable(payload.get("build_mode")),
        "parameter_prefix": _nullable(payload.get("parameter_prefix")),
        "occurrence_path": occurrence_path,
        "assembly_from_link": matrix,
        "chirality": "original",
        "body_evidence": {
            "local_body_state": wglink_core._local_body_state(record),
            "baseline_fingerprint": baseline,
            "observed_fingerprint": observed,
            "observed_at": observed_at,
        },
        "source_contract": _source_contract(design, record),
    }
    for key in ("design_id", "export_id", "build_mode", "parameter_prefix"):
        if result[key] is None:
            raise wglink_core.WgLinkError(
                f"WGLink instance {record['instance_id']!r} is missing required stored field {key!r}."
            )
    return result


def _face_key(face: object) -> tuple[str, object]:
    token = wglink_core._entity_token(face)
    return ("token", token) if token else ("object", id(face))


def _face_area(face: object) -> float:
    try:
        area = float(face.area) * 100.0
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(f"Could not read a source face area: {exc}.") from exc
    if not math.isfinite(area) or area <= 0.0:
        raise wglink_core.WgLinkError(f"Source face area must be positive, got {area!r}.")
    return area


def _throat_faces(record: dict[str, Any]) -> list[object]:
    # Prefer the body wrapper the scope walk observed (an occurrence proxy in
    # an assembly). Fusion mints distinct Python wrappers -- with distinct
    # entity tokens -- for native and proxy views of ONE face, so the claim
    # keys and the painted-source pass must come from the SAME collection or
    # the throat disc double-counts as a user source (measured in E2E).
    body = record.get("source_body") or record.get("body")
    if body is None:
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} has no managed body for its required throat source."
        )
    payload = record.get("payload", {})
    role = str(_nullable(payload.get("source_role")) or "").upper()
    expected = _float_echo(payload, "expected_throat_area_mm2")
    throat_z = _float_echo(payload, "throat_z_mm")
    if role not in SOURCE_ROLES or expected is None or throat_z is None:
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} lacks a complete required throat selector."
        )
    faces = wglink_core._items(getattr(body, "faces", None))
    geometric = []
    painted = []
    for face in faces:
        area = _face_area(face)
        area_ok = math.isclose(area, expected, rel_tol=0.01)
        if _face_role(face) == role and area_ok:
            painted.append(face)
        try:
            box = face.boundingBox
            planar = abs(float(box.maxPoint.z) - float(box.minPoint.z)) * 10.0 <= 0.05
            at_plane = abs(float(box.minPoint.z) * 10.0 - throat_z) <= 0.05 and abs(float(box.maxPoint.z) * 10.0 - throat_z) <= 0.05
            if area_ok and planar and at_plane:
                geometric.append(face)
        except Exception:  # noqa: BLE001 - appearance remains useful test evidence
            pass
    # The geometric gates read link-local coordinates, so they only bind on an
    # unmoved wrapper; on a moved or rotated one the painted+area branch is
    # the one that still resolves. Either way the faces returned here belong
    # to the observed body wrapper, so their keys claim correctly downstream.
    matches = geometric if len(geometric) == 1 else painted
    if len(matches) != 1:
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} required throat source resolved to {len(matches)} faces; expected exactly one."
        )
    return matches


def _edge_key(edge: object) -> tuple[str, object]:
    token = wglink_core._entity_token(edge)
    return ("token", token) if token else ("object", id(edge))


def _connected_components(faces: list[object]) -> int:
    edge_to_faces: dict[tuple[str, object], list[int]] = {}
    for index, face in enumerate(faces):
        for edge in wglink_core._items(getattr(face, "edges", None)):
            edge_to_faces.setdefault(_edge_key(edge), []).append(index)
    neighbours = [set() for _face in faces]
    for indices in edge_to_faces.values():
        for index in indices:
            neighbours[index].update(other for other in indices if other != index)
    remaining = set(range(len(faces)))
    count = 0
    while remaining:
        count += 1
        stack = [remaining.pop()]
        while stack:
            current = stack.pop()
            linked = neighbours[current].intersection(remaining)
            remaining.difference_update(linked)
            stack.extend(linked)
    return count


def _source_ids(role: str, used: set[str]) -> tuple[str, str]:
    base = _slug(role)
    suffix = ""
    index = 2
    while f"source-{base}{suffix}" in used:
        suffix = f"-{index}"
        index += 1
    source_id = f"source-{base}{suffix}"
    used.add(source_id)
    return source_id, f"drive-{base}{suffix}"


def _observed(faces: list[object], face_bodies: dict[tuple[str, object], str]) -> dict[str, Any]:
    areas = [_face_area(face) for face in faces]
    return {
        "face_count": len(faces),
        "total_area_mm2": sum(areas),
        "per_face_area_mm2": areas,
        "bodies": sorted({face_bodies[_face_key(face)] for face in faces}),
    }


def _sources(records: list[dict[str, Any]], included_bodies: list[object]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    claimed: set[tuple[str, object]] = set()
    used: set[str] = set()
    face_bodies: dict[tuple[str, object], str] = {}
    for body in included_bodies:
        name = str(getattr(body, "name", "unnamed body") or "unnamed body")
        for face in wglink_core._items(getattr(body, "faces", None)):
            face_bodies[_face_key(face)] = name

    for record in records:
        role = str(record.get("payload", {}).get("source_role") or "").upper()
        faces = _throat_faces(record)
        for face in faces:
            key = _face_key(face)
            claimed.add(key)
            face_bodies.setdefault(key, str(getattr(record.get("body"), "name", "unnamed body")))
        source_id, drive_id = _source_ids(role, used)
        sources.append(
            {
                "id": source_id,
                "role": role,
                "instance_id": str(record["instance_id"]),
                "required": True,
                "default_drive_channel_id": drive_id,
                "patch_policy": "single-connected",
                "expected_connected_components": 1,
                "selectors": {
                    "linked_throat": {"instance_id": str(record["instance_id"])},
                    "appearance_labels": [role],
                },
                "observed": _observed(faces, face_bodies),
                "suggested_resolution_mm": SOURCE_RESOLUTION_MM[role],
            }
        )

    painted: dict[str, list[object]] = {role: [] for role in SOURCE_ROLES}
    for body in included_bodies:
        for face in wglink_core._items(getattr(body, "faces", None)):
            role = _face_role(face)
            if role and _face_key(face) not in claimed:
                painted[role].append(face)
    for role in SOURCE_ROLES:
        faces = painted[role]
        if not faces:
            continue
        source_id, drive_id = _source_ids(role, used)
        sources.append(
            {
                "id": source_id,
                "role": role,
                "instance_id": None,
                "required": True,
                "default_drive_channel_id": drive_id,
                "patch_policy": "explicit-disconnected",
                "expected_connected_components": _connected_components(faces),
                "selectors": {"appearance_labels": [role]},
                "observed": _observed(faces, face_bodies),
                "suggested_resolution_mm": SOURCE_RESOLUTION_MM[role],
            }
        )
    if not sources:
        raise wglink_core.WgLinkError(
            "Return export has no drivable source. Paint an included face LF, MF, HF, or PORT_EXIT and try again."
        )
    return sources


def _step_text(value: str | bytes | os.PathLike[str]) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, os.PathLike):
        return Path(value).read_text(encoding="utf-8", errors="replace")
    text = str(value)
    if "\n" not in text and "ISO-10303" not in text and Path(text).is_file():
        return Path(text).read_text(encoding="utf-8", errors="replace")
    return text


def count_step_bodies(value: str | bytes | os.PathLike[str]) -> int:
    """Count the two top-level STEP body entities emitted by Fusion."""

    text = re.sub(r"/\*.*?\*/", "", _step_text(value), flags=re.S)
    text = re.sub(r"'(?:''|[^'])*'", "''", text)
    return len(_STEP_BODY.findall(text))


def _export_step(design: object, path: Path, geometry: object | None) -> None:
    manager = design.exportManager
    try:
        options = (
            manager.createSTEPExportOptions(str(path))
            if geometry is None
            else manager.createSTEPExportOptions(str(path), geometry)
        )
        ok = manager.execute(options)
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(f"Fusion STEP export failed for {path.name}: {exc}.") from exc
    if not ok or not path.is_file():
        raise wglink_core.WgLinkError(f"Fusion STEP export failed for {path.name}.")


def _file_record(path: Path, purpose: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "sha256": "sha256:" + digest.hexdigest(),
        "size_bytes": path.stat().st_size,
        "media_type": "model/step",
        "purpose": purpose,
    }


def _bbox(bodies: list[object]) -> list[list[float]]:
    boxes = [wglink_core._bbox_values(body) for body in bodies]
    if not boxes:
        raise wglink_core.WgLinkError("The exterior assembly contains no included B-rep bodies.")
    return [
        [min(box[index] for box in boxes) for index in range(3)],
        [max(box[index] for box in boxes) for index in range(3, 6)],
    ]


def _document(app: object, design: object) -> tuple[str, str | None]:
    document = getattr(app, "activeDocument", None) or getattr(design, "parentDocument", None)
    name = str(getattr(document, "name", "") or getattr(design.rootComponent, "name", "Untitled")).strip()
    native_id = None
    try:
        value = str(document.dataFile.id).strip()
        native_id = value or None
    except Exception:  # noqa: BLE001 - unsaved/local documents are normal
        pass
    return name or "Untitled", native_id


def _safe_document_name(name: str) -> str:
    value = re.sub(r"[\\/:\x00-\x1f]", "_", name).strip().rstrip(".")
    return value or "Untitled"


def _publish(temp: Path, target: Path, overwrite: bool) -> None:
    if target.exists() and not overwrite:
        raise wglink_core.WgLinkError(
            f"Return bundle already exists: {target}. Pass options['overwrite']=true to replace it."
        )
    if not target.exists():
        os.replace(temp, target)
        return
    backup = target.with_name(f".{target.name}.old-{uuid.uuid4().hex}")
    os.replace(target, backup)
    try:
        os.replace(temp, target)
    except Exception:
        os.replace(backup, target)
        raise
    shutil.rmtree(backup)


def send(app: object, options: dict[str, Any]) -> dict[str, Any]:
    """Write one return bundle and return a JSON-serialisable export report."""

    if not isinstance(options, dict):
        raise wglink_core.WgLinkError("options must be an object")
    output_value = options.get("output_folder")
    if not isinstance(output_value, (str, os.PathLike)) or not str(output_value).strip():
        raise wglink_core.WgLinkError("options['output_folder'] must name the return output folder.")
    design = wglink_core._design(app)
    walk = _scope_walk(design, options.get("selection"))
    records = _records_in_scope(design, walk)
    instance_ids = [str(record["instance_id"]) for record in records]
    requested_anchor = _nullable(options.get("anchor_instance_id"))
    if len(instance_ids) == 1:
        anchor = instance_ids[0]
    elif len(instance_ids) > 1:
        if requested_anchor is None:
            raise wglink_core.WgLinkError(
                "More than one WGLink instance is in scope; choose options['anchor_instance_id']."
            )
        anchor = str(requested_anchor)
        if anchor not in instance_ids:
            raise wglink_core.WgLinkError(
                f"Anchor instance {anchor!r} is not one of the in-scope instances: {', '.join(instance_ids)}."
            )
    else:
        if requested_anchor is not None:
            raise wglink_core.WgLinkError("An unlinked return cannot name an anchor instance.")
        anchor = None

    for candidate in walk["candidates"]:
        candidate["contains_solver_anchor"] = bool(
            anchor and candidate.get("wglink_instance_id") == anchor
        )
    try:
        scope_plan = plan_export_scope(walk["selection"], walk["candidates"])
        scope = scope_plan.manifest_scope()
    except WgReturnError as exc:
        raise wglink_core.WgLinkError(str(exc)) from exc

    included_pairs = [
        (record, walk["bodies"][record["object_id"]])
        for record in scope["included"]
        if record["object_id"] in walk["bodies"]
    ]
    included_bodies = [body for _record, body in included_pairs]
    if len(included_pairs) != len(scope["included"]):
        raise wglink_core.WgLinkError("Could not resolve every included body back to live Fusion geometry.")
    for record in records:
        instance_body = next(
            (
                body
                for included, body in included_pairs
                if included.get("wglink_instance_id") == record["instance_id"]
            ),
            None,
        )
        if instance_body is not None:
            record["source_body"] = instance_body
    observed_at = _utc_timestamp()
    instance_records = [_instance_record(design, record, observed_at) for record in records]
    sources = _sources(records, included_bodies)
    document_name, native_id = _document(app, design)
    output = Path(output_value).expanduser()
    try:
        output.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(
            f"Could not create return output folder {output}: {exc}."
        ) from exc
    target = output / f"{_safe_document_name(document_name)}.wgreturn"
    overwrite = bool(options.get("overwrite", False))
    if target.exists() and not target.is_dir():
        raise wglink_core.WgLinkError(
            f"Return target exists but is not a bundle folder: {target}."
        )
    if target.exists() and not overwrite:
        raise wglink_core.WgLinkError(
            f"Return bundle already exists: {target}. Pass options['overwrite']=true to replace it."
        )

    try:
        temp = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=output))
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(
            f"Could not create a temporary return bundle beside {target}: {exc}."
        ) from exc
    try:
        assembly_path = temp / "assembly.step"
        export_geometry = None if walk["selection"] == "root" else walk["geometry"]
        _export_step(design, assembly_path, export_geometry)
        observed_count = count_step_bodies(assembly_path)
        expected_count = len(included_bodies)
        if observed_count != expected_count:
            raise wglink_core.WgLinkError(
                f"STEP body count gate refused the export: inventory expects {expected_count}, but assembly.step contains {observed_count}."
            )
        files = {"assembly.step": _file_record(assembly_path, "exterior-assembly")}
        for fem in scope["fem_air_volumes"]:
            component = walk["fem_components"].get(fem.get("object_id"))
            if component is None:
                raise wglink_core.WgLinkError(f"Could not resolve FEM component {fem.get('name', '?')!r}.")
            member = str(fem["file"])
            fem_path = temp / Path(member)
            fem_path.parent.mkdir(parents=True, exist_ok=True)
            _export_step(design, fem_path, component)
            fem_count = count_step_bodies(fem_path)
            if fem_count != 1:
                raise wglink_core.WgLinkError(
                    f"FEM STEP {member!r} contains {fem_count} bodies; exactly one solid is required."
                )
            files[member] = _file_record(fem_path, "fem-air-volume")

        coordinate = {
            "length_unit": "mm",
            "handedness": "right",
            "matrix_convention": "row-major-local-to-parent",
        }
        if anchor is not None:
            coordinate["solver_anchor_instance_id"] = anchor
        manifest = build_return_manifest(
            return_record={"id": generate_return_id(), "created_at": observed_at},
            generator={
                "adapter": "hornlab-fusion-addin/WGLink",
                "adapter_version": ADAPTER_VERSION,
                "cad_app": "fusion360",
                "cad_version": str(getattr(app, "version", "unknown") or "unknown"),
            },
            document={"name": document_name, "native_id": native_id},
            coordinate_system=coordinate,
            assembly={
                "file": "assembly.step",
                "n_bodies_expected": expected_count,
                "bbox_mm": _bbox(included_bodies),
            },
            files=files,
            scope=scope,
            instances=instance_records,
            sources=sources,
        )
        (temp / "wgreturn.json").write_text(
            dumps_return_manifest(manifest), encoding="utf-8"
        )
        _publish(temp, target, overwrite)
    except WgReturnError as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise wglink_core.WgLinkError(str(exc)) from exc
    except wglink_core.WgLinkError:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - one head-less filesystem boundary
        shutil.rmtree(temp, ignore_errors=True)
        raise wglink_core.WgLinkError(
            f"Could not publish return bundle {target}: {exc}."
        ) from exc

    return {
        "return_id": manifest["return"]["id"],
        "bundle_path": str(target),
        "scope": manifest["scope"],
        "instances": manifest["instances"],
        "sources": manifest["sources"],
        "files": manifest["files"],
        "manifest": manifest,
    }
