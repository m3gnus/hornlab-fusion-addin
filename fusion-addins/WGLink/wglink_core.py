"""Head-less Fusion adapter for Waveguide Generator links.

This module deliberately contains no dialogs and no command UI.  Every public
operation either returns a JSON-serialisable report or raises ``WgLinkError``
with an actionable refusal suitable for the command shell or an unattended
harness.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any
import uuid

import adsk.core
import adsk.fusion

from wglink_bundle import (
    IDENTITY_MATRIX,
    TAG_AREA_TOLERANCE,
    WgLinkError,
    attribute_payload,
    effective_parameters,
    enclosure_plan,
    format_expression,
    fusion_matrix_to_mm,
    health_regressions,
    instance_parameter_prefix,
    link_state,
    mm_to_internal,
    parameter_slug,
    plan_sections,
    read_bundle,
    refreshed_body_evidence,
    rollback_target,
    tag_verdict,
    throat_area_mm2,
    transform_points,
)


ATTRIBUTE_GROUP = "WGLink"
ADDIN_DIR = Path(__file__).resolve().parent
MOUTH_OVERSHOOT_SUFFIX = "mouth_overshoot"
LINK_FRAME_TOLERANCE_MM = 0.01
BODY_NAMES = {
    "cut_tool": "WGLink waveguide cut tool",
    "enclosure": "WGLink enclosure",
    "throat_patch": "WGLink throat patch body",
    "stitched_waveguide": "WGLink stitched waveguide body",
    "waveguide": "WGLink freestanding waveguide",
    "waveguide_surface": "WGLink waveguide surface",
}
ROLE_COLOURS = {
    "HF": (255, 0, 0, 255),
    "MF": (255, 187, 0, 255),
    "LF": (0, 76, 255, 255),
    "DAMP": (21, 255, 0, 255),
    "PORT_EXIT": (68, 150, 72, 0),
}
_PAYLOAD_KEYS = {
    "assembly_from_link",
    "build_mode",
    "bundle_id",
    "bundle_path",
    "chirality",
    "design_hash",
    "design_id",
    "design_name",
    "edit_version",
    "export_id",
    "export_sequence",
    "geometry_hash",
    "instance_id",
    "lineage_id",
    "local_body_state",
    "parameter_prefix",
    "schema",
    "slug",
    "thicken_sign",
    "topology",
    "parameter_expressions",
    "expected_throat_area_mm2",
    "body_fingerprint",
    "occurrence_token",
    "wrapper",
    "source_role",
    "throat_z_mm",
    "entity_tokens",
    "timeline_start",
    "timeline_end",
    "last_managed_sketch_index",
}


def _options(options: dict[str, Any] | None) -> dict[str, Any]:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise WgLinkError(f"options must be an object, got {type(options).__name__}")
    return dict(options)


def _read_owned_bundle(path: str | os.PathLike[str]) -> object:
    """Load validated JSON, then release any owned zip extraction immediately."""

    bundle = read_bundle(path)
    bundle.close()
    return bundle


def _design(app: adsk.core.Application) -> adsk.fusion.Design:
    design = adsk.fusion.Design.cast(getattr(app, "activeProduct", None))
    if design is None:
        raise WgLinkError("No active Fusion design. Open or create a design and try again.")
    if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
        raise WgLinkError(
            "WGLink requires a parametric design with a timeline. In the Browser, "
            "right-click the root component and choose 'Capture Design History', "
            "then try again."
        )
    return design


def _items(collection: object) -> list[object]:
    if collection is None:
        return []
    try:
        return [collection.item(index) for index in range(collection.count)]
    except Exception:  # noqa: BLE001 - Fusion exposes both lists and collections
        try:
            return list(collection)
        except Exception:  # noqa: BLE001
            return []


def _kind(entity: object) -> str | None:
    try:
        value = entity.objectType
        return value.split("::")[-1] if value else None
    except Exception:  # noqa: BLE001 - identity diagnostics must be non-fatal
        return None


def _json(value: object) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _attributes(entity: object) -> object | None:
    try:
        return entity.attributes
    except Exception:  # noqa: BLE001
        return None


def _attribute(entity: object, name: str) -> object | None:
    attributes = _attributes(entity)
    if attributes is None:
        return None
    try:
        return attributes.itemByName(ATTRIBUTE_GROUP, name)
    except Exception:  # noqa: BLE001
        return None


def _attribute_value(entity: object, name: str) -> str | None:
    attribute = _attribute(entity, name)
    if attribute is None:
        return None
    try:
        return str(attribute.value)
    except Exception:  # noqa: BLE001
        return None


def _set_attribute(entity: object, name: str, value: object) -> None:
    attributes = _attributes(entity)
    if attributes is None:
        raise WgLinkError(f"Fusion {_kind(entity) or 'entity'} cannot carry attributes")
    text = str(value)
    existing = _attribute(entity, name)
    if existing is not None:
        try:
            existing.value = text
            return
        except Exception:  # noqa: BLE001 - replace a stale attribute instance
            try:
                existing.deleteMe()
            except Exception:  # noqa: BLE001
                pass
    attributes.add(ATTRIBUTE_GROUP, name, text)


def _entity_token(entity: object) -> str:
    try:
        return str(entity.entityToken)
    except Exception:  # noqa: BLE001
        return ""


def _stamp_managed(entity: object, instance_id: str, name: str, value: object) -> None:
    _set_attribute(entity, "instance_id", instance_id)
    _set_attribute(entity, name, _json(value) if isinstance(value, (dict, list)) else value)
    token = _entity_token(entity)
    if token:
        _set_attribute(entity, "entity_token", token)


def _name_body(body: object, role: str) -> object:
    """Apply a browser name without making that presentation name identity."""

    body.name = BODY_NAMES[role]
    return body


def _stamp_payload(entity: object, payload: dict[str, str]) -> None:
    for name, value in payload.items():
        _set_attribute(entity, name, value)
    token = _entity_token(entity)
    if token:
        _set_attribute(entity, "entity_token", token)


def _wrapper_attribute_name(instance_id: str) -> str:
    safe = "".join(character if character.isalnum() else "_" for character in instance_id)
    return f"link_{safe}"


def _stamp_wrapper(component: object, payload: dict[str, str]) -> None:
    _set_attribute(component, _wrapper_attribute_name(payload["instance_id"]), _json(payload))
    token = _entity_token(component)
    if token:
        _set_attribute(component, "entity_token", token)


def _direct_attributes(entity: object) -> dict[str, str]:
    result: dict[str, str] = {}
    for attribute in _items(_attributes(entity)):
        try:
            if attribute.groupName == ATTRIBUTE_GROUP:
                result[str(attribute.name)] = str(attribute.value)
        except Exception:  # noqa: BLE001
            continue
    return result


def _all_link_attributes(design: adsk.fusion.Design) -> list[object]:
    try:
        return _items(design.findAttributes(ATTRIBUTE_GROUP, ""))
    except Exception as exc:  # noqa: BLE001
        raise WgLinkError(f"Could not inspect WGLink attributes: {exc}") from exc


def _parent(attribute: object) -> object | None:
    try:
        return attribute.parent
    except Exception:  # noqa: BLE001
        return None


def _link_records(design: adsk.fusion.Design) -> dict[str, dict[str, Any]]:
    by_parent: dict[int, dict[str, Any]] = {}
    for attribute in _all_link_attributes(design):
        parent = _parent(attribute)
        if parent is None:
            continue
        entry = by_parent.setdefault(id(parent), {"entity": parent, "attrs": {}})
        try:
            entry["attrs"][str(attribute.name)] = str(attribute.value)
        except Exception:  # noqa: BLE001
            continue

    records: dict[str, dict[str, Any]] = {}
    for entry in by_parent.values():
        attrs = entry["attrs"]
        wrapper_payload = None
        for name, value in attrs.items():
            if not name.startswith("link_"):
                continue
            try:
                candidate = json.loads(value)
            except (TypeError, ValueError):
                continue
            if isinstance(candidate, dict) and candidate.get("instance_id"):
                wrapper_payload = {str(key): str(item) for key, item in candidate.items()}
                break
        instance_id = attrs.get("instance_id")
        if wrapper_payload is not None:
            instance_id = wrapper_payload["instance_id"]
        if not instance_id:
            continue
        record = records.setdefault(
            instance_id,
            {"instance_id": instance_id, "entities": [], "payload": {}},
        )
        record["entities"].append(entry["entity"])
        if wrapper_payload:
            record["payload"].update(wrapper_payload)
            record.setdefault("wrappers", []).append(entry["entity"])
        direct_payload = {key: value for key, value in attrs.items() if key in _PAYLOAD_KEYS}
        if "topology" in direct_payload:
            record["payload"].update(direct_payload)
    return records


def _body_role(entity: object) -> str | None:
    return _attribute_value(entity, "role")


def _token_table(payload: dict[str, Any]) -> dict[str, str]:
    try:
        table = json.loads(str(payload.get("entity_tokens", "{}")))
    except (TypeError, ValueError):
        return {}
    if not isinstance(table, dict):
        return {}
    return {str(name): str(token) for name, token in table.items() if token}


def _entity_label(entity: object) -> str:
    try:
        return f"{getattr(entity, 'name', '?')} ({_kind(entity) or 'unknown'})"
    except Exception:  # noqa: BLE001
        return _kind(entity) or "unknown entity"


def _resolve_link(
    design: adsk.fusion.Design,
    options: dict[str, Any],
    *,
    allow_missing_body: bool = False,
) -> dict[str, Any]:
    records = _link_records(design)
    if not records:
        raise WgLinkError(
            "No complete WGLink attributes were found. Use Relink to recover a "
            "known link, or Detach to keep the geometry unmanaged."
        )
    requested = options.get("instance_id")
    if requested is not None:
        requested = str(requested)
        if requested not in records:
            raise WgLinkError(
                f"WGLink instance {requested!r} was not found. Audit the document "
                "and choose an existing instance id."
            )
        record = records[requested]
    elif len(records) == 1:
        record = next(iter(records.values()))
    else:
        names = ", ".join(sorted(records))
        raise WgLinkError(
            f"The document contains multiple WGLink instances ({names}). Pass "
            "options['instance_id'] to choose one."
        )

    bodies = [
        entity
        for entity in record["entities"]
        if _body_role(entity) in {"enclosure", "waveguide"}
    ]
    wrappers = [entity for entity in record.get("wrappers", []) if _kind(entity) == "Component"]
    token_table = _token_table(record["payload"])
    if not bodies:
        bodies = [
            entity
            for entity in _find_by_token(design, token_table.get("body:final", ""))
            if _kind(entity) == "BRepBody"
        ]
    if not wrappers:
        wrappers = [
            entity
            for entity in _find_by_token(
                design, token_table.get("wrapper:component", "")
            )
            if _kind(entity) == "Component"
        ]
    if len(bodies) > 1 or len(wrappers) > 1:
        clashes = bodies if len(bodies) > 1 else wrappers
        labels = ", ".join(_entity_label(entity) for entity in clashes)
        raise WgLinkError(
            f"Duplicate WGLink instance id {record['instance_id']!r} belongs to "
            f"multiple managed objects: {labels}. Give the instances unique ids "
            "or Detach one; WGLink refuses to guess."
        )
    record["body"] = bodies[0] if bodies else None
    record["wrapper_component"] = wrappers[0] if wrappers else None
    if not record["payload"].get("topology"):
        raise WgLinkError(
            f"WGLink instance {record['instance_id']!r} is missing its topology "
            "attributes. Use Relink if the bundle is known, or Detach the geometry."
        )
    if record["body"] is None and not allow_missing_body:
        raise WgLinkError(
            f"The managed body for WGLink instance {record['instance_id']!r} was "
            "deleted. Recreate the link; Update cannot rebuild a missing body."
        )
    return record


def _find_by_token(design: adsk.fusion.Design, token: str) -> list[object]:
    if not token:
        return []
    try:
        return _items(design.findEntityByToken(token))
    except Exception:  # noqa: BLE001
        return []


def _component_for_record(design: adsk.fusion.Design, record: dict[str, Any]) -> object:
    wrapper = record.get("wrapper_component")
    if wrapper is not None:
        return wrapper
    body = record.get("body")
    try:
        return body.parentComponent
    except Exception:  # noqa: BLE001
        return design.rootComponent


def _matrix_rows(matrix: object) -> list[list[float]]:
    """The occurrence placement as the contract's row-major MILLIMETRE matrix."""

    try:
        values = [float(value) for value in matrix.asArray()]
    except Exception:  # noqa: BLE001
        return [list(row) for row in IDENTITY_MATRIX]
    try:
        return fusion_matrix_to_mm(values)
    except WgLinkError:
        return [list(row) for row in IDENTITY_MATRIX]


def _assembly_from_link(design: adsk.fusion.Design, record: dict[str, Any]) -> list[list[float]]:
    if record.get("payload", {}).get("wrapper") == "root":
        return [list(row) for row in IDENTITY_MATRIX]
    token = record.get("payload", {}).get("occurrence_token", "")
    for entity in _find_by_token(design, token):
        if _kind(entity) == "Occurrence":
            transform = getattr(entity, "transform2", None) or getattr(entity, "transform", None)
            return _matrix_rows(transform)
    component = _component_for_record(design, record)
    occurrences = []
    for occurrence in _items(getattr(design.rootComponent, "allOccurrences", None)):
        try:
            if occurrence.component == component:
                occurrences.append(occurrence)
        except Exception:  # noqa: BLE001
            continue
    if len(occurrences) == 1:
        transform = getattr(occurrences[0], "transform2", None) or occurrences[0].transform
        return _matrix_rows(transform)
    if len(occurrences) > 1:
        raise WgLinkError(
            f"WGLink instance {record['instance_id']!r} has multiple wrapper "
            "occurrences. Detach copies or give each insertion its own component."
        )
    return [list(row) for row in IDENTITY_MATRIX]


def _parameter_map(bundle: object, prefix: str) -> dict[str, object]:
    """Index by SUFFIX, anchored on the link's slug -- see parameters_by_suffix."""

    return effective_parameters(bundle, prefix)


def _parameter_by_suffix(parameters: dict[str, object], suffix: str) -> object:
    parameter = parameters.get(suffix)
    if parameter is None:
        raise WgLinkError(
            f"this link has no '{suffix}' interface parameter; re-export the "
            "design from Waveguide Generator"
        )
    return parameter


def _push_parameters(
    design: adsk.fusion.Design,
    bundle: object,
    prefix: str,
) -> dict[str, list[str]]:
    created: list[str] = []
    updated: list[str] = []
    design_name = str(bundle.manifest.get("design", {}).get("name", "waveguide"))
    for parameter in effective_parameters(bundle, prefix).values():
        existing = design.userParameters.itemByName(parameter.name)
        if existing is None:
            design.userParameters.add(
                parameter.name,
                adsk.core.ValueInput.createByString(parameter.expression),
                "mm",
                f"WGLink interface parameter for {design_name}",
            )
            created.append(parameter.name)
        else:
            existing.expression = parameter.expression
            updated.append(parameter.name)
    return {"created": created, "updated": updated}


def _push_mouth_overshoot_parameter(
    design: adsk.fusion.Design,
    prefix: str,
    overshoot_mm: float,
) -> tuple[str, dict[str, list[str]]]:
    name = f"{prefix}{MOUTH_OVERSHOOT_SUFFIX}"
    expression = format_expression(overshoot_mm)
    existing = design.userParameters.itemByName(name)
    if existing is None:
        design.userParameters.add(
            name,
            adsk.core.ValueInput.createByString(expression),
            "mm",
            "WGLink mouth punch-through depth",
        )
        return name, {"created": [name], "updated": []}
    existing.expression = expression
    return name, {"created": [], "updated": [name]}


def _merge_parameter_reports(
    target: dict[str, list[str]], source: dict[str, list[str]]
) -> None:
    for action in ("created", "updated"):
        target[action].extend(source[action])


def _managed_parameter_expressions(
    bundle: object,
    prefix: str,
    *,
    mouth_overshoot_mm: float | None = None,
) -> dict[str, str]:
    expressions = {
        parameter.name: parameter.expression
        for parameter in effective_parameters(bundle, prefix).values()
    }
    if mouth_overshoot_mm is not None:
        expressions[f"{prefix}{MOUTH_OVERSHOOT_SUFFIX}"] = format_expression(
            mouth_overshoot_mm
        )
    return expressions


def _point(xyz_mm: list[float] | tuple[float, float, float]) -> object:
    return adsk.core.Point3D.create(*(mm_to_internal(float(value)) for value in xyz_mm))


def _selected_sections(
    grid: dict[str, Any],
    plan: object,
    *,
    wall: str = "inner",
) -> list[list[list[float]]]:
    source = grid[f"{wall}_points"]
    rays = list(range(0, grid["n_phi"], plan.phi_stride))
    return [
        [[float(value) for value in source[ray][station]] for ray in rays]
        for station in plan.ring_indices
    ]


def _add_fit_sketch(
    component: object,
    points: list[list[float]],
    *,
    name: str,
    instance_id: str,
    marker_name: str,
    marker: object,
) -> tuple[object, object]:
    sketch = component.sketches.add(component.xYConstructionPlane)
    sketch.name = name
    visible = sketch.isLightBulbOn
    try:
        sketch.isComputeDeferred = True
        collection = adsk.core.ObjectCollection.create()
        for xyz in points:
            collection.add(_point(xyz))
        spline = sketch.sketchCurves.sketchFittedSplines.add(collection)
        spline.isClosed = True
    finally:
        try:
            sketch.isComputeDeferred = False
        except Exception:  # noqa: BLE001
            pass
        try:
            sketch.isLightBulbOn = False if visible else visible
        except Exception:  # noqa: BLE001
            pass
    _stamp_managed(sketch, instance_id, marker_name, marker)
    return sketch, spline


def _build_loft(
    component: object,
    section_points: list[list[list[float]]],
    *,
    planar: bool,
    solid: bool,
    instance_id: str,
) -> tuple[object, object, list[object]]:
    sections = adsk.core.ObjectCollection.create()
    sketches: list[object] = []
    for index, points in enumerate(section_points):
        sketch, spline = _add_fit_sketch(
            component,
            points,
            name=f"WG_RING_{index:03d}",
            instance_id=instance_id,
            marker_name="ring",
            marker={"section": index, "wall": "inner"},
        )
        sketches.append(sketch)
        if planar and sketch.profiles.count:
            sections.add(sketch.profiles.item(0))
        else:
            sections.add(spline)
        adsk.doEvents()
    if sections.count < 2:
        raise WgLinkError(f"Only {sections.count} ring sections were built; at least 2 are required.")
    lofts = component.features.loftFeatures
    loft_input = lofts.createInput(adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    for index in range(sections.count):
        loft_input.loftSections.add(sections.item(index))
    loft_input.isSolid = bool(solid and planar)
    loft = lofts.add(loft_input)
    loft.name = "WGLink waveguide loft"
    _stamp_managed(loft, instance_id, "feature", "loft")
    if not loft.bodies.count:
        raise WgLinkError("The waveguide loft produced no body.")
    body = loft.bodies.item(0)
    _name_body(body, "cut_tool" if solid else "waveguide_surface")
    return body, loft, sketches


def _mouth_cap_face(body: object, mouth_z_mm: float) -> object:
    target = mm_to_internal(mouth_z_mm)
    position_tolerance = mm_to_internal(0.0001)
    direction_tolerance = 1.0e-6
    candidates: list[object] = []
    for face in _items(body.faces):
        try:
            plane = adsk.core.Plane.cast(face.geometry)
        except Exception:  # noqa: BLE001 - malformed/non-planar faces are not candidates
            plane = None
        if plane is None:
            continue
        box = face.boundingBox
        if (
            abs(box.minPoint.z - target) > position_tolerance
            or abs(box.maxPoint.z - target) > position_tolerance
        ):
            continue
        normal = plane.normal
        sign = -1.0 if bool(face.isParamReversed) else 1.0
        nx = sign * float(normal.x)
        ny = sign * float(normal.y)
        nz = sign * float(normal.z)
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 0.0:
            continue
        if (
            abs(nx / length) <= direction_tolerance
            and abs(ny / length) <= direction_tolerance
            and nz / length >= 1.0 - direction_tolerance
        ):
            candidates.append(face)
    if len(candidates) != 1:
        raise WgLinkError(
            "Expected exactly one planar loft cap face at the mouth plane "
            f"z={mouth_z_mm:g} mm with outward normal +z; Fusion found "
            f"{len(candidates)}. Refusing to extrude an ambiguous face."
        )
    return candidates[0]


def _extrude_mouth_overshoot(
    component: object,
    loft_body: object,
    *,
    mouth_z_mm: float,
    overshoot_parameter: str,
    instance_id: str,
) -> tuple[object, object]:
    face = _mouth_cap_face(loft_body, mouth_z_mm)
    extrudes = component.features.extrudeFeatures
    extrude_input = extrudes.createInput(
        face, adsk.fusion.FeatureOperations.JoinFeatureOperation
    )
    extrude_input.setOneSideExtent(
        adsk.fusion.DistanceExtentDefinition.create(
            adsk.core.ValueInput.createByString(overshoot_parameter)
        ),
        adsk.fusion.ExtentDirections.PositiveExtentDirection,
    )
    extrude = extrudes.add(extrude_input)
    extrude.name = "WGLink mouth overshoot"
    _stamp_managed(extrude, instance_id, "feature", "mouth_overshoot")
    if not extrude.bodies.count:
        raise WgLinkError("The mouth overshoot join extrude produced no body.")
    return _name_body(extrude.bodies.item(0), "cut_tool"), extrude


def _throat_edge(body: object, throat_z_mm: float, tolerance_mm: float = 0.5) -> object | None:
    best = None
    best_delta = None
    for edge in _items(body.edges):
        box = edge.boundingBox
        delta = abs(box.minPoint.z * 10.0 - throat_z_mm) + abs(
            box.maxPoint.z * 10.0 - throat_z_mm
        )
        if best_delta is None or delta < best_delta:
            best = edge
            best_delta = delta
    if best_delta is None or best_delta >= tolerance_mm * 2.0:
        return None
    return best


def _bbox_values(body: object) -> list[float]:
    box = body.boundingBox
    return [
        box.minPoint.x * 10.0,
        box.minPoint.y * 10.0,
        box.minPoint.z * 10.0,
        box.maxPoint.x * 10.0,
        box.maxPoint.y * 10.0,
        box.maxPoint.z * 10.0,
    ]


def _containment_name(value: object) -> str:
    states = getattr(adsk.fusion, "PointContainment", None)
    for name in (
        "PointInsidePointContainment",
        "PointOutsidePointContainment",
        "PointOnPointContainment",
        "UnknownPointContainment",
    ):
        try:
            if value == getattr(states, name):
                return name.removesuffix("PointContainment")
        except Exception:  # noqa: BLE001
            continue
    return str(value)


def _wall_side_probe(
    solid: object,
    throat_axis_mm: tuple[float, float, float],
    wall_t_mm: float,
) -> tuple[bool, bool, bool, str, str]:
    x_value, y_value, throat_z_mm = throat_axis_mm
    cavity_point = _point(
        [x_value, y_value, throat_z_mm + max(0.25, wall_t_mm / 4.0)]
    )
    backing_point = _point(
        [x_value, y_value, throat_z_mm - wall_t_mm / 2.0]
    )
    cavity = solid.pointContainment(cavity_point)
    backing = solid.pointContainment(backing_point)
    states = adsk.fusion.PointContainment
    cavity_outside = cavity == states.PointOutsidePointContainment
    backing_inside = backing == states.PointInsidePointContainment
    return (
        cavity_outside,
        backing_inside,
        cavity == states.PointInsidePointContainment
        and backing == states.PointOutsidePointContainment,
        _containment_name(cavity),
        _containment_name(backing),
    )


def _close_and_thicken(
    component: object,
    surface_body: object,
    *,
    throat_axis_mm: tuple[float, float, float],
    wall_t_mm: float,
    thickness_parameter: str,
    instance_id: str,
) -> tuple[object, int, list[object]]:
    features = component.features
    made: list[object] = []
    throat_z_mm = throat_axis_mm[2]
    edge = _throat_edge(surface_body, throat_z_mm)
    if edge is None:
        raise WgLinkError(f"No closed loft edge was found at the throat plane z={throat_z_mm:g} mm.")

    patch_input = features.patchFeatures.createInput(
        edge, adsk.fusion.FeatureOperations.NewBodyFeatureOperation
    )
    patch = features.patchFeatures.add(patch_input)
    patch.name = "WGLink throat patch"
    _stamp_managed(patch, instance_id, "feature", "patch")
    patch_body = _name_body(patch.bodies.item(0), "throat_patch")
    _stamp_managed(patch_body, instance_id, "role", "cut_tool")
    made.append(patch)

    surfaces = adsk.core.ObjectCollection.create()
    surfaces.add(surface_body)
    surfaces.add(patch_body)
    stitch_input = features.stitchFeatures.createInput(
        surfaces,
        adsk.core.ValueInput.createByReal(mm_to_internal(0.01)),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    stitch = features.stitchFeatures.add(stitch_input)
    stitch.name = "WGLink stitched waveguide"
    _stamp_managed(stitch, instance_id, "feature", "stitch")
    made.append(stitch)
    stitched = _name_body(stitch.bodies.item(0), "stitched_waveguide")
    _stamp_managed(stitched, instance_id, "role", "cut_tool")

    for sign in (1, -1):
        shells = adsk.core.ObjectCollection.create()
        shells.add(stitched)
        expression = thickness_parameter if sign > 0 else f"-1 * {thickness_parameter}"
        thicken_input = features.thickenFeatures.createInput(
            shells,
            adsk.core.ValueInput.createByString(expression),
            False,
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
            True,
        )
        thicken = features.thickenFeatures.add(thicken_input)
        solid = _name_body(thicken.bodies.item(0), "waveguide")
        cavity_ok, backing_ok, exact_opposite, cavity_state, backing_state = _wall_side_probe(
            solid, throat_axis_mm, wall_t_mm
        )
        if cavity_ok and backing_ok:
            # MEASURED 2026-08-10: bbox growth selected the wrong sign because
            # the curved mouth grows radially whichever material side wins. It
            # produced z=0..176.5 instead of about -6..170.5 mm and +5.65%
            # volume. Axis containment is the unambiguous material-side test.
            # The mesher STEP intentionally has throat_opened=true, while this
            # patch/stitch path retains the driver-seat disc; roughly
            # pi * 12.7^2 * wall_t mm^3 residual volume is therefore expected.
            thicken.name = "WGLink outward wall"
            _stamp_managed(thicken, instance_id, "feature", "thicken")
            made.append(thicken)
            return solid, sign, made
        if not exact_opposite:
            disagreements = []
            if not cavity_ok:
                disagreements.append(
                    f"cavity probe was {cavity_state}, expected PointOutside"
                )
            if not backing_ok:
                disagreements.append(
                    f"driver-backing probe was {backing_state}, expected PointInside"
                )
            try:
                thicken.deleteMe()
            except Exception:  # noqa: BLE001
                pass
            raise WgLinkError(
                "Could not establish the freestanding wall material side: "
                + "; ".join(disagreements)
                + ". Refusing to keep an ambiguous solid; Undo this insertion."
            )
        try:
            thicken.deleteMe()
        except Exception as exc:  # noqa: BLE001
            raise WgLinkError(
                "The first thicken direction shrank inward and Fusion could not "
                f"remove it before retrying: {exc}. Undo this insertion."
            ) from exc
    raise WgLinkError(
        "Neither thicken direction put the cavity outside and driver backing inside."
    )


def _add_offset_plane(
    component: object,
    base_plane: object,
    expression: str,
    name: str,
    instance_id: str,
) -> object:
    plane_input = component.constructionPlanes.createInput()
    plane_input.setByOffset(base_plane, adsk.core.ValueInput.createByString(expression))
    plane = component.constructionPlanes.add(plane_input)
    plane.name = name
    _stamp_managed(plane, instance_id, "datum", name)
    return plane


def _build_datums(
    component: object,
    bundle: object,
    parameters: dict[str, object],
    instance_id: str,
) -> tuple[dict[str, object], dict[str, list[str]]]:
    mode = bundle.manifest["design"]["build_mode"]
    depth = _parameter_by_suffix(parameters, "depth").name
    vertical = _parameter_by_suffix(parameters, "vertical_offset").name
    baffle_expression = depth
    if mode == "enclosure":
        baffle_expression = _parameter_by_suffix(parameters, "enc_z_front").name
    planes: dict[str, object] = {}
    created: list[str] = []
    skipped: list[str] = []

    definitions = [
        ("WG_THROAT_PLANE", component.xYConstructionPlane, "0 mm"),
        ("WG_GEOM_MIDPLANE_Y", component.xZConstructionPlane, vertical),
        ("WG_SOLVER_CUT_PLANE_Y", component.xZConstructionPlane, "0 mm"),
        ("WG_SOLVER_CUT_PLANE_X", component.yZConstructionPlane, "0 mm"),
    ]
    if mode == "enclosure":
        definitions.append(
            ("WG_BAFFLE_PLANE", component.xYConstructionPlane, baffle_expression)
        )
        front = _parameter_by_suffix(parameters, "enc_z_front").name
        depth_param = _parameter_by_suffix(parameters, "enc_depth").name
        definitions.append(
            ("WG_ENC_BACK_PLANE", component.xYConstructionPlane, f"{front} - {depth_param}")
        )
    mouth = bundle.manifest.get("datums", {}).get("WG_MOUTH_PLANE")
    if isinstance(mouth, dict) and mouth.get("exact") is True:
        definitions.append(("WG_MOUTH_PLANE", component.xYConstructionPlane, baffle_expression))
    else:
        skipped.append("WG_MOUTH_PLANE: manifest rim is not exactly planar")

    for name, base, expression in definitions:
        planes[name] = _add_offset_plane(component, base, expression, name, instance_id)
        created.append(name)

    axis_input = component.constructionAxes.createInput()
    axis_input.setByTwoPlanes(
        planes["WG_SOLVER_CUT_PLANE_X"], planes["WG_GEOM_MIDPLANE_Y"]
    )
    axis = component.constructionAxes.add(axis_input)
    axis.name = "WG_AXIS"
    _stamp_managed(axis, instance_id, "datum", "WG_AXIS")
    created.append("WG_AXIS")
    planes["WG_AXIS"] = axis
    return planes, {"created": created, "skipped": skipped}


def _build_interface_sketches(
    component: object,
    bundle: object,
    plan: object,
    instance_id: str,
) -> dict[str, object]:
    rays = list(range(0, bundle.grid["n_phi"], plan.phi_stride))
    throat = [bundle.grid["inner_points"][ray][0] for ray in rays]
    mouth_datum = bundle.manifest.get("datums", {}).get("WG_MOUTH_OUTLINE_INNER", {})
    mouth_points = mouth_datum.get("points_mm") if isinstance(mouth_datum, dict) else None
    if not isinstance(mouth_points, list) or len(mouth_points) != bundle.grid["n_phi"]:
        raise WgLinkError(
            "Manifest datum WG_MOUTH_OUTLINE_INNER does not match the point-grid rays."
        )
    mouth = [mouth_points[ray] for ray in rays]
    result: dict[str, object] = {}
    for role, name, points in (
        ("throat", "WGI_THROAT_SKETCH", throat),
        ("mouth", "WGI_MOUTH_SKETCH", mouth),
    ):
        sketch, _spline = _add_fit_sketch(
            component,
            points,
            name=name,
            instance_id=instance_id,
            marker_name="interface",
            marker=role,
        )
        result[role] = sketch
    return result


def _line_midpoint(line: object) -> tuple[float, float]:
    start = line.startSketchPoint.geometry
    end = line.endSketchPoint.geometry
    return ((start.x + end.x) * 0.5, (start.y + end.y) * 0.5)


def _rectangle_lines(lines: list[object]) -> dict[str, object]:
    horizontal = [
        line
        for line in lines
        if abs(line.startSketchPoint.geometry.y - line.endSketchPoint.geometry.y) < 1.0e-7
    ]
    vertical = [
        line
        for line in lines
        if abs(line.startSketchPoint.geometry.x - line.endSketchPoint.geometry.x) < 1.0e-7
    ]
    if len(horizontal) != 2 or len(vertical) != 2:
        raise WgLinkError("Fusion's two-point rectangle did not produce two horizontal and two vertical lines.")
    return {
        "bottom": min(horizontal, key=lambda line: _line_midpoint(line)[1]),
        "top": max(horizontal, key=lambda line: _line_midpoint(line)[1]),
        "left": min(vertical, key=lambda line: _line_midpoint(line)[0]),
        "right": max(vertical, key=lambda line: _line_midpoint(line)[0]),
    }


def _dimension_enclosure_rectangle(
    sketch: object,
    lines: dict[str, object],
    parameters: dict[str, object],
) -> None:
    dimensions = sketch.sketchDimensions
    horizontal = adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation
    vertical = adsk.fusion.DimensionOrientations.VerticalDimensionOrientation
    width = _parameter_by_suffix(parameters, "enc_w").name
    height = _parameter_by_suffix(parameters, "enc_h").name
    x0 = _parameter_by_suffix(parameters, "enc_x0").name
    y0 = _parameter_by_suffix(parameters, "enc_y0").name

    bottom = lines["bottom"]
    right = lines["right"]
    left = lines["left"]
    width_dimension = dimensions.addDistanceDimension(
        bottom.startSketchPoint,
        bottom.endSketchPoint,
        horizontal,
        _point([0.0, _line_midpoint(bottom)[1] * 10.0 - 20.0, 0.0]),
    )
    width_dimension.parameter.expression = width
    height_dimension = dimensions.addDistanceDimension(
        right.startSketchPoint,
        right.endSketchPoint,
        vertical,
        _point([_line_midpoint(right)[0] * 10.0 + 20.0, 0.0, 0.0]),
    )
    height_dimension.parameter.expression = height
    x_dimension = dimensions.addDistanceDimension(
        sketch.originPoint,
        left.startSketchPoint,
        horizontal,
        _point([_line_midpoint(left)[0] * 5.0, 20.0, 0.0]),
    )
    x_dimension.parameter.expression = f"-1 * {x0}"
    y_dimension = dimensions.addDistanceDimension(
        sketch.originPoint,
        bottom.startSketchPoint,
        vertical,
        _point([20.0, _line_midpoint(bottom)[1] * 5.0, 0.0]),
    )
    y_dimension.parameter.expression = f"-1 * {y0}"


def _enclosure_sketch(
    component: object,
    bundle: object,
    parameters: dict[str, object],
    parameter_prefix: str,
    instance_id: str,
) -> tuple[object, bool, bool]:
    plan = enclosure_plan(bundle, parameter_prefix)
    if plan is None:
        raise WgLinkError("An enclosure sketch was requested for a freestanding bundle.")
    x0, y0, x1, y1 = plan.rectangle_mm
    sketch = component.sketches.add(component.xYConstructionPlane)
    sketch.name = "WGI_ENCLOSURE_ENVELOPE"
    rectangle = sketch.sketchCurves.sketchLines.addTwoPointRectangle(
        _point([x0, y0, 0.0]), _point([x1, y1, 0.0])
    )
    lines = _rectangle_lines(_items(rectangle))
    try:
        constraints = sketch.geometricConstraints
        constraints.addHorizontal(lines["top"])
        constraints.addHorizontal(lines["bottom"])
        constraints.addVertical(lines["left"])
        constraints.addVertical(lines["right"])
        # MEASURED 2026-08-10 in Fusion 2704.1.53: addTwoPointRectangle
        # supplied zero geometric constraints. Four dimensions alone left the
        # 328 x 565 mm update at 335.7 x 572.5 mm. These four explicit
        # constraints made isFullyConstrained true and landed every bound.
        _dimension_enclosure_rectangle(sketch, lines, parameters)
        if not bool(sketch.isFullyConstrained):
            raise WgLinkError(
                "Fusion did not fully constrain the driven enclosure rectangle"
            )
        parametric = True
    except Exception:  # noqa: BLE001 - the measured fixed-geometry fallback
        try:
            sketch.deleteMe()
        except Exception:  # noqa: BLE001
            pass
        sketch = component.sketches.add(component.xYConstructionPlane)
        sketch.name = "WGI_ENCLOSURE_ENVELOPE_FIXED"
        sketch.sketchCurves.sketchLines.addTwoPointRectangle(
            _point([x0, y0, 0.0]), _point([x1, y1, 0.0])
        )
        parametric = False
    _stamp_managed(
        sketch,
        instance_id,
        "enclosure_sketch",
        {"parametric": parametric},
    )
    try:
        fully_constrained = bool(sketch.isFullyConstrained)
    except Exception:  # noqa: BLE001
        fully_constrained = False
    return sketch, parametric, fully_constrained


def _edges_spanning_z(body: object) -> list[object]:
    box = body.boundingBox
    span = box.maxPoint.z - box.minPoint.z
    return [
        edge
        for edge in _items(body.edges)
        if edge.boundingBox.maxPoint.z - edge.boundingBox.minPoint.z >= span - 1.0e-6
    ]


def _face_at_z(body: object, z_mm: float) -> object | None:
    target = mm_to_internal(z_mm)
    candidates = []
    for face in _items(body.faces):
        box = face.boundingBox
        if abs(box.minPoint.z - target) < 1.0e-5 and abs(box.maxPoint.z - target) < 1.0e-5:
            candidates.append(face)
    return max(candidates, key=lambda face: face.area * 100.0) if candidates else None


def _add_chamfer(
    component: object,
    edges: list[object],
    expression: str,
    instance_id: str,
    role: str,
) -> object:
    collection = adsk.core.ObjectCollection.create()
    for edge in edges:
        collection.add(edge)
    chamfers = component.features.chamferFeatures
    chamfer_input = chamfers.createInput2()
    chamfer_input.chamferEdgeSets.addEqualDistanceChamferEdgeSet(
        collection,
        adsk.core.ValueInput.createByString(expression),
        True,
    )
    feature = chamfers.add(chamfer_input)
    feature.name = f"WGLink {role} chamfer"
    _stamp_managed(feature, instance_id, "feature", role)
    return feature


def _add_fillet(
    component: object,
    edges: list[object],
    expression: str,
    instance_id: str,
    role: str,
) -> object:
    collection = adsk.core.ObjectCollection.create()
    for edge in edges:
        collection.add(edge)
    fillets = component.features.filletFeatures
    fillet_input = fillets.createInput()
    fillet_input.edgeSetInputs.addConstantRadiusEdgeSet(
        collection,
        adsk.core.ValueInput.createByString(expression),
        True,
    )
    feature = fillets.add(fillet_input)
    feature.name = f"WGLink {role} fillet"
    _stamp_managed(feature, instance_id, "feature", role)
    return feature


def _add_edge_treatment(
    component: object,
    edges: list[object],
    expression: str,
    edge_type: int,
    instance_id: str,
    role: str,
) -> object:
    if edge_type == 1:
        return _add_fillet(component, edges, expression, instance_id, role)
    if edge_type == 2:
        return _add_chamfer(component, edges, expression, instance_id, role)
    raise WgLinkError(f"Unsupported enclosure edge_type {edge_type!r}")


def _build_enclosure(
    component: object,
    bundle: object,
    parameters: dict[str, object],
    parameter_prefix: str,
    cut_tool: object,
    instance_id: str,
) -> tuple[object, dict[str, Any], list[object]]:
    plan = enclosure_plan(bundle, parameter_prefix)
    if plan is None:
        raise WgLinkError("Cannot build an enclosure from a freestanding bundle.")
    sketch, parametric, fully_constrained = _enclosure_sketch(
        component, bundle, parameters, parameter_prefix, instance_id
    )
    made: list[object] = [sketch]
    profile = sketch.profiles.item(0)
    extrudes = component.features.extrudeFeatures
    extrude_input = extrudes.createInput(
        profile, adsk.fusion.FeatureOperations.NewBodyFeatureOperation
    )
    extrude_input.setTwoSidesExtent(
        adsk.fusion.DistanceExtentDefinition.create(
            adsk.core.ValueInput.createByString(plan.front_extent_expression)
        ),
        adsk.fusion.DistanceExtentDefinition.create(
            adsk.core.ValueInput.createByString(plan.back_extent_expression)
        ),
    )
    extrude = extrudes.add(extrude_input)
    extrude.name = "WGLink enclosure block"
    _stamp_managed(extrude, instance_id, "feature", "extrude")
    made.append(extrude)
    box = _name_body(extrude.bodies.item(0), "enclosure")
    _stamp_managed(box, instance_id, "role", "enclosure")

    vertical_edges = _edges_spanning_z(box)
    if len(vertical_edges) != 4:
        raise WgLinkError(
            f"Expected 4 vertical enclosure edges before edge treatment; Fusion found {len(vertical_edges)}."
        )
    front = _face_at_z(box, plan.front_extent_mm)
    back = _face_at_z(box, -plan.back_extent_mm)
    if front is None or back is None:
        raise WgLinkError("Could not identify both enclosure perimeter faces before edge treatment.")
    perimeter_edges = _items(front.edges) + _items(back.edges)
    if len(perimeter_edges) != 8:
        raise WgLinkError(
            f"Expected 8 front/back perimeter edges before edge treatment; Fusion found {len(perimeter_edges)}."
        )
    edges = vertical_edges + perimeter_edges
    if len({id(edge) for edge in edges}) != 12:
        raise WgLinkError("The enclosure edge set did not contain 12 distinct prism edges.")
    edge_treatment = _add_edge_treatment(
        component,
        edges,
        plan.edge_expression,
        plan.edge_type,
        instance_id,
        "plan-corner",
    )
    made.append(edge_treatment)

    tools = adsk.core.ObjectCollection.create()
    tools.add(cut_tool)
    combines = component.features.combineFeatures
    combine_input = combines.createInput(box, tools)
    combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    combine_input.isKeepToolBodies = False
    combine = combines.add(combine_input)
    combine.name = "WGLink enclosure minus waveguide"
    _stamp_managed(combine, instance_id, "feature", "combine")
    made.append(combine)
    # Fusion mutates the target BRepBody in place for Combine.  Keeping that
    # handle also avoids relying on a feature-specific ``bodies`` collection.
    return box, {
        "parametric": parametric,
        "edge_type": plan.edge_type,
        "sketch_fully_constrained": fully_constrained,
        "edges": len(edges),
    }, made


def _library_appearance(app: object, name: str) -> object | None:
    """Find the user's own role appearance, with Favorites searched first.

    Fusion material libraries raise ``invalid name`` for ordinary misses, so
    each lookup is independently guarded; a miss in the first library must not
    prevent reaching the library that owns the appearance.
    """

    libraries = _items(app.materialLibraries)
    libraries.sort(key=lambda library: 0 if "Favorites" in library.name else 1)
    for library in libraries:
        try:
            appearance = library.appearances.itemByName(name)
        except Exception:  # noqa: BLE001 - measured Fusion behavior
            continue
        if appearance:
            return appearance
    return None


def _colour_property(appearance: object) -> object | None:
    try:
        properties = appearance.appearanceProperties
    except Exception:  # noqa: BLE001
        return None
    for prop in _items(properties):
        try:
            if prop.objectType == adsk.core.ColorProperty.classType():
                return prop
        except Exception:  # noqa: BLE001
            continue
    return None


def _named_appearance(app: object, design: object, name: str) -> object:
    source = _library_appearance(app, name)
    try:
        existing = design.appearances.itemByName(name)
    except Exception:  # noqa: BLE001
        existing = None
    if existing:
        # Do not mutate a shared appearance already painted onto managed faces.
        # Fusion invalidates every existing face binding when its colour asset
        # changes; repairing only the current instance left other links untagged.
        return existing
    if source:
        return design.appearances.addByCopy(source, name)

    # No user-owned role exists.  Copy a real material asset, then apply the
    # owner's measured role colour; never leave a grey lookalike under a source
    # role name.
    for library in _items(app.materialLibraries):
        try:
            base = library.appearances.itemByName("Steel - Satin")
        except Exception:  # noqa: BLE001
            continue
        if not base:
            continue
        minted = design.appearances.addByCopy(base, name)
        rgba = ROLE_COLOURS.get(name.upper())
        prop = _colour_property(minted) if rgba else None
        if prop and rgba:
            prop.value = adsk.core.Color.create(*rgba)
        return minted
    raise WgLinkError(
        f"Could not find or create a visible {name!r} appearance. Add it to a "
        "Fusion material library and retry."
    )


def _appearance_name(face: object) -> str | None:
    # Both fetching the appearance and reading its name can throw assetInst for
    # an unpainted face or after an applied appearance's colour was repaired.
    try:
        appearance = face.appearance
        return appearance.name if appearance else None
    except Exception:  # noqa: BLE001 - measured Fusion behavior
        return None


def _face_descriptor(face: object, index: int, throat_z_mm: float) -> dict[str, object]:
    box = face.boundingBox
    planar = abs(box.maxPoint.z - box.minPoint.z) * 10.0 < 1.0e-3
    at_throat = (
        abs(box.minPoint.z * 10.0 - throat_z_mm) < 0.5
        and abs(box.maxPoint.z * 10.0 - throat_z_mm) < 0.5
    )
    return {
        "id": index,
        "planar": planar,
        "at_throat_plane": at_throat,
        "area_mm2": face.area * 100.0,
    }


def _tag_report(
    app: object,
    design: object,
    body: object,
    *,
    instance_id: str,
    role: str,
    throat_z_mm: float,
    expected_area_mm2: float,
    repair: bool,
) -> dict[str, object]:
    faces = _items(body.faces)
    tagged: list[dict[str, object]] = []
    by_index: dict[int, object] = {}
    for index, face in enumerate(faces):
        if _appearance_name(face) != role:
            continue
        descriptor = _face_descriptor(face, index, throat_z_mm)
        tagged.append(descriptor)
        by_index[index] = face
    verdict = tag_verdict(
        tagged,
        expected_area_mm2=expected_area_mm2,
        tolerance=TAG_AREA_TOLERANCE,
    )
    stripped = 0
    intended = by_index.get(verdict.keep) if verdict.keep is not None else None
    if repair:
        all_descriptors = []
        all_by_index = {}
        for index, face in enumerate(faces):
            descriptor = _face_descriptor(face, index, throat_z_mm)
            all_descriptors.append(descriptor)
            all_by_index[index] = face
        geometric = tag_verdict(
            all_descriptors,
            expected_area_mm2=expected_area_mm2,
            tolerance=TAG_AREA_TOLERANCE,
        )
        if len(geometric.candidates) != 1:
            raise WgLinkError(
                f"Expected exactly one planar throat face near {expected_area_mm2:.3f} mm^2; "
                f"Fusion found {len(geometric.candidates)}. Undo and inspect the source geometry."
            )
        geometric_candidate = all_by_index[int(geometric.candidates[0])]
        appearance = _named_appearance(app, design, role)
        for index in verdict.strip:
            face = by_index.get(int(index))
            if face is None:
                continue
            try:
                face.appearance = None
                stripped += 1
            except Exception as exc:  # noqa: BLE001
                raise WgLinkError(f"Could not strip stray {role} face {index}: {exc}") from exc
        if intended is None:
            intended = geometric_candidate
            intended.appearance = appearance
        # Read-back is also the repair after correcting an already-painted
        # appearance's colour invalidates the face's asset binding.
        try:
            if intended.appearance.name != role:
                intended.appearance = appearance
        except Exception:  # noqa: BLE001
            intended.appearance = appearance
        _stamp_managed(intended, instance_id, "face_role", role)

    current = [face for face in faces if _appearance_name(face) == role]
    area = intended.area * 100.0 if intended is not None else 0.0
    return {
        "area_mm2": area,
        "tagged_faces": len(current),
        "strays_stripped": stripped,
        "repainted": bool(repair and verdict.repaint),
        "role": role,
    }


def _health_names() -> dict[object, str]:
    states = adsk.fusion.FeatureHealthStates
    result = {}
    for name, attribute in (
        ("Healthy", "HealthyFeatureHealthState"),
        ("Error", "ErrorFeatureHealthState"),
        ("Warning", "WarningFeatureHealthState"),
        ("Suppressed", "SuppressedFeatureHealthState"),
        ("RolledBack", "RolledBackFeatureHealthState"),
        ("Unknown", "UnknownFeatureHealthState"),
    ):
        try:
            result[getattr(states, attribute)] = name
        except Exception:  # noqa: BLE001
            continue
    return result


def _walk_timeline(
    node: object,
    rows: list[dict[str, object]],
    skipped: list[dict[str, object]],
) -> None:
    names = _health_names()
    for index, item in enumerate(_items(node)):
        # Fusion's timeline collection already exposes expanded group children
        # at top level. Recursing into the group double-counts their health.
        if getattr(item, "isGroup", False):
            continue
        try:
            entity = item.entity
            if entity is None or not hasattr(entity, "healthState"):
                continue
            state = entity.healthState
            rows.append(
                {
                    "name": getattr(entity, "name", "?"),
                    "type": _kind(entity) or "unknown",
                    "health": names.get(state, str(state)),
                }
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics never abort a build
            skipped.append({"index": index, "why": str(exc)})


def _feature_health(design: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    _walk_timeline(design.timeline, rows, skipped)
    return rows, skipped


def _expand_groups(timeline: object) -> None:
    for _depth in range(8):
        changed = False
        for item in _items(timeline):
            if getattr(item, "isGroup", False) and getattr(item, "isCollapsed", False):
                item.isCollapsed = False
                changed = True
                break
        if not changed:
            return


def _timeline_entries(timeline: object) -> list[dict[str, object]]:
    entries = []
    for index, item in enumerate(_items(timeline)):
        if getattr(item, "isGroup", False):
            continue
        kind = None
        try:
            kind = _kind(item.entity)
        except Exception:  # noqa: BLE001 - unreadable is an explicit pure input
            pass
        entries.append({"index": index, "kind": kind})
    return entries


def _measurable(body: object) -> object:
    """Return the body in the ROOT assembly context, which is the only one
    ``measureManager`` accepts.

    MEASURED 2026-08-10: handed a native body that lives inside a component --
    which every managed body does, because D2 puts them in the wrapper
    occurrence -- ``measureMinimumDistance`` raises
    ``3 : invalid argument geometryTwo`` for every single point. The predecessor
    spike never saw this because it built in the root component, so the
    deviation gate would have reported "0 measured" forever and the surface
    accuracy check would have quietly measured nothing.
    """

    context = None
    try:
        context = body.assemblyContext
    except Exception:  # noqa: BLE001
        context = None
    if context is not None:
        return body
    try:
        component = body.parentComponent
        design = component.parentDesign
        if component == design.rootComponent:
            return body
        for occurrence in _items(design.rootComponent.allOccurrences):
            if occurrence.component == component:
                return body.createForAssemblyContext(occurrence)
    except Exception:  # noqa: BLE001 - fall back to the native body and report
        return body
    return body


def _last_managed_sketch_index(*sketch_groups: object) -> int | None:
    """Where this instance's own sketches sit in the timeline RIGHT NOW.

    The index recorded at insert cannot be trusted: insert collapses its range
    into one timeline group, a later insert numbers its own entries against
    that collapsed view, and Update expands every group again -- so a stored
    absolute index refers to a different entry by the time it is read.

    MEASURED 2026-08-10: with two links in one document, the stored index put
    the rollback target before the SECOND link's ring sketches, and the rebuild
    died on the first ring with
    ``2 : InternalValidationError : Utils::findObjectPath(this, logicalPath)``
    -- exactly the failure the scoped-rollback rule exists to prevent. Asking
    the document where the sketches are now is stable by construction.
    """

    best: int | None = None
    for sketch in sketch_groups:
        try:
            index = int(sketch.timelineObject.index)
        except Exception:  # noqa: BLE001 - an unreadable sketch cannot anchor
            continue
        if best is None or index > best:
            best = index
    return best


def _warn_unmeasured(deviation: object, warnings: list[str]) -> None:
    """A deviation that measured nothing must never read as a passing check."""

    if not isinstance(deviation, dict):
        return
    if deviation.get("measured"):
        return
    reason = deviation.get("error") or "no check points were available"
    warnings.append(
        "LOUD: the surface deviation check measured nothing "
        f"({deviation.get('failures', 0)} failures: {reason}). "
        "The build was NOT verified against the mesher's own grid."
    )


def _deviation(app: object, body: object, check_points: list[list[float]], limit: int) -> dict[str, object]:
    if limit <= 0:
        raise WgLinkError(f"max_checks must be positive, got {limit}")
    stride = max(1, len(check_points) // limit)
    sampled = check_points[::stride][:limit]
    target = _measurable(body)
    distances: list[float] = []
    failures = 0
    first_error = None
    for xyz in sampled:
        try:
            result = app.measureManager.measureMinimumDistance(_point(xyz), target)
            distances.append(float(result.value) * 10.0)
        except Exception as exc:  # noqa: BLE001 - report individual measurement failures
            failures += 1
            if first_error is None:
                first_error = str(exc)
    if not distances:
        # Silence here would read as "the surface is perfect". Say what broke.
        return {
            "frame": "root-assembly",
            "measured": 0,
            "failures": failures,
            "error": first_error or "no check points",
            "mean_mm": None,
            "max_mm": None,
            "p95_mm": None,
        }
    distances.sort()
    count = len(distances)
    return {
        "frame": "root-assembly",
        "measured": count,
        "failures": failures,
        "mean_mm": sum(distances) / count,
        "max_mm": distances[-1],
        "p95_mm": distances[min(count - 1, int(count * 0.95))],
    }


def _insert_check_points(bundle: object, plan: object, maximum: int) -> list[list[float]]:
    selected = set(plan.ring_indices)
    candidates = [
        [float(value) for value in bundle.grid["inner_points"][ray][station]]
        for ray in range(bundle.grid["n_phi"])
        for station in range(bundle.grid["n_length"])
        if station not in selected
    ]
    if not candidates:
        return []
    stride = max(1, len(candidates) // maximum)
    return candidates[::stride][:maximum]


def _body_fingerprint(body: object) -> dict[str, object]:
    return {
        "bbox_mm": _bbox_values(body),
        "is_solid": bool(body.isSolid),
        "volume_mm3": float(body.volume) * 1000.0 if body.isSolid else None,
    }


def _body_measurement(body: object, manifest: dict[str, Any]) -> dict[str, object]:
    fingerprint = _body_fingerprint(body)
    expected = manifest.get("body", {}).get("volume_mm3")
    observed = fingerprint["volume_mm3"]
    difference = None
    if isinstance(expected, (int, float)) and expected and isinstance(observed, float):
        difference = (observed - float(expected)) / float(expected) * 100.0
    return {
        **fingerprint,
        "manifest_volume_mm3": expected,
        "volume_difference_percent": difference,
        "manifest_bbox_mm": manifest.get("body", {}).get("bbox_mm"),
    }


def _fingerprints_match(stored: object, body: object) -> bool | None:
    try:
        expected = json.loads(str(stored))
        observed = _body_fingerprint(body)
        if bool(expected["is_solid"]) != bool(observed["is_solid"]):
            return False
        left_volume = expected.get("volume_mm3")
        right_volume = observed.get("volume_mm3")
        if left_volume is not None and not math.isclose(
            float(left_volume), float(right_volume), rel_tol=1.0e-6, abs_tol=1.0e-3
        ):
            return False
        return all(
            math.isclose(float(left), float(right), rel_tol=1.0e-7, abs_tol=1.0e-4)
            for left, right in zip(expected["bbox_mm"], observed["bbox_mm"])
        )
    except Exception:  # noqa: BLE001
        return None


def _local_body_state(record: dict[str, Any]) -> str:
    if record.get("payload", {}).get("local_body_state") == "modified":
        return "modified"
    body = record.get("body")
    if body is None:
        return "missing"
    matches = _fingerprints_match(record.get("payload", {}).get("body_fingerprint"), body)
    if matches is None:
        return "unknown"
    return "unmodified" if matches else "modified"


def link_frame_verdict(
    throat_center_mm: tuple[float, float] | list[float],
    throat_plane_z_mm: float,
    expected_vertical_offset_mm: float,
    expected_throat_z_mm: float,
    tolerance_mm: float = LINK_FRAME_TOLERANCE_MM,
) -> dict[str, object]:
    """Compare native body geometry with the component-local link frame."""

    center_x = float(throat_center_mm[0])
    center_y = float(throat_center_mm[1])
    plane_z = float(throat_plane_z_mm)
    expected_y = float(expected_vertical_offset_mm)
    expected_z = float(expected_throat_z_mm)
    tolerance = float(tolerance_mm)
    offset = [center_x, center_y - expected_y, plane_z - expected_z]
    in_frame = all(abs(value) <= tolerance for value in offset)
    return {
        "verdict": "in_frame" if in_frame else "moved",
        "center_mm": [center_x, center_y],
        "expected_center_mm": [0.0, expected_y],
        "plane_z_mm": plane_z,
        "expected_plane_z_mm": expected_z,
        "offset_mm": offset,
        "distance_mm": math.sqrt(sum(value * value for value in offset)),
        "tolerance_mm": tolerance,
    }


def _frame_face(design: object, record: dict[str, Any]) -> object:
    """Resolve the tagged throat face by attributes/token, never by its name."""

    payload = record.get("payload", {})
    role = str(payload.get("source_role", "HF"))
    instance_id = str(record.get("instance_id", ""))

    body = record.get("body")
    faces = _items(getattr(body, "faces", None)) if body is not None else []
    attributed = [
        face
        for face in faces
        if _attribute_value(face, "face_role") == role
        and _attribute_value(face, "instance_id") == instance_id
    ]
    if len(attributed) == 1:
        return attributed[0]

    painted = [face for face in faces if _appearance_name(face) == role]
    if len(painted) == 1:
        return painted[0]

    token = _token_table(payload).get(f"face:{role}", "")
    token_faces = [
        entity
        for entity in _find_by_token(design, token)
        if _kind(entity) == "BRepFace"
    ]
    if len(token_faces) == 1:
        return token_faces[0]
    raise WgLinkError(
        "could not resolve exactly one tagged throat face "
        f"(token={len(token_faces)}, attributed={len(attributed)}, painted={len(painted)})"
    )


def _link_frame_report(design: object, record: dict[str, Any]) -> dict[str, object]:
    """Measure in native component coordinates, intentionally ignoring occurrence moves.

    This catches a body moved relative to its own managed datums. A whole
    component moved together with those datums is a legitimate placement and
    therefore remains in frame here.
    """

    payload = record.get("payload", {})
    prefix = str(
        payload.get("parameter_prefix") or f"wg_{payload.get('slug', '')}_"
    )
    parameter_name = f"{prefix}vertical_offset"
    expected_z = float(payload.get("throat_z_mm", 0.0) or 0.0)
    try:
        parameter = design.userParameters.itemByName(parameter_name)
        if parameter is None:
            raise WgLinkError(f"managed parameter {parameter_name!r} is missing")
        expected_y = float(parameter.value) * 10.0
        face = _frame_face(design, record)
        center = face.centroid
        plane = adsk.core.Plane.cast(face.geometry)
        if plane is None:
            raise WgLinkError("the tagged throat face is not planar")
        report = link_frame_verdict(
            [float(center.x) * 10.0, float(center.y) * 10.0],
            float(plane.origin.z) * 10.0,
            expected_y,
            expected_z,
        )
        report["vertical_offset_parameter"] = parameter_name
        return report
    except Exception as exc:  # noqa: BLE001 - Audit must report, never refuse
        return {
            "verdict": "unknown",
            "expected_center_mm": [0.0, None],
            "expected_plane_z_mm": expected_z,
            "vertical_offset_parameter": parameter_name,
            "error": str(exc),
        }


def _refuse_bad_link_frame(frame: dict[str, object], *, force: bool) -> None:
    if frame.get("verdict") == "in_frame" or force:
        return
    if frame.get("verdict") == "moved":
        center = frame["center_mm"]
        expected = frame["expected_center_mm"]
        offset = frame["offset_mm"]
        raise WgLinkError(
            "WGLink refuses Update because the managed body has left the link frame: "
            f"tagged throat centre is ({center[0]:.3f}, {center[1]:.3f}) mm, "
            f"expected ({expected[0]:.3f}, {expected[1]:.3f}) mm; measured "
            f"offset is ({offset[0]:+.3f}, {offset[1]:+.3f}, {offset[2]:+.3f}) mm "
            "in x/y/plane-z. Undo the body Move, or Detach if the body is now "
            "genuinely yours. A caller that intentionally accepts this can pass force=True."
        )
    raise WgLinkError(
        "WGLink refuses Update because it could not verify the managed body's link "
        f"frame ({frame.get('error', 'unknown reason')}). Restore/recreate the managed "
        "link, or Detach if the body is now yours. A caller may pass force=True only "
        "if proceeding intentionally."
    )


def _repo_root(options: dict[str, Any]) -> Path:
    explicit = options.get("repo_root") or os.environ.get("HORNLAB_FUSION_ADDIN_REPO")
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.extend([ADDIN_DIR.parents[1], Path.cwd(), *Path.cwd().parents])
    for candidate in candidates:
        if (candidate / "scripts" / "wglink_resample.py").is_file():
            return candidate.resolve()
    raise WgLinkError(
        "Could not locate scripts/wglink_resample.py. Install WGLink by symlink "
        "from the repository, or set HORNLAB_FUSION_ADDIN_REPO to the checkout."
    )


def _python_for_resampler(repo_root: Path, options: dict[str, Any]) -> Path:
    explicit = options.get("python_path")
    candidates = []
    if explicit:
        candidates.append(Path(str(explicit)).expanduser())
    candidates.extend(
        [
            repo_root / ".venv" / "bin" / "python",
            repo_root / ".venv" / "Scripts" / "python.exe",
        ]
    )
    located = shutil.which("python3")
    if located:
        candidates.append(Path(located))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise WgLinkError(
        "No Python interpreter for WGLink resampling was found. Create the "
        "repository .venv or pass options['python_path']."
    )


def _subprocess_env() -> dict[str, str]:
    """Hand the child interpreter an environment Fusion has not poisoned.

    MEASURED 2026-08-10: the repo venv's python died at startup with
    ``Fatal Python error: Failed to import encodings module`` because Fusion
    exports its own ``PYTHONHOME``/``PYTHONPATH`` and the child inherited them,
    so it looked for its standard library inside Fusion's runtime. Same list
    WGMetalPipeline already strips for the same reason.
    """

    env = dict(os.environ)
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONEXECUTABLE",
        "PYTHONNOUSERSITE",
        "PYTHONUSERBASE",
        "__PYVENV_LAUNCHER__",
    ):
        env.pop(key, None)
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _resample_payload(
    bundle_path: str | os.PathLike[str],
    topology: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    repo_root = _repo_root(options)
    python = _python_for_resampler(repo_root, options)
    timeout = float(options.get("timeout_seconds", 180.0))
    if timeout <= 0.0:
        raise WgLinkError(f"timeout_seconds must be positive, got {timeout:g}")
    with tempfile.TemporaryDirectory(prefix="wglink-resample-") as temporary:
        root = Path(temporary)
        topology_path = root / "topology.json"
        output_path = root / "payload.json"
        topology_path.write_text(_json(topology) + "\n", encoding="utf-8")
        command = [
            str(python),
            str(repo_root / "scripts" / "wglink_resample.py"),
            "--bundle",
            str(bundle_path),
            "--topology",
            str(topology_path),
            "--out",
            str(output_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(repo_root),
                env=_subprocess_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WgLinkError(
                f"WGLink resampling exceeded {timeout:g} seconds; retry or increase "
                "options['timeout_seconds']."
            ) from exc
        if completed.returncode:
            machine_error = completed.stdout.strip()
            if machine_error:
                raise WgLinkError(machine_error)
            raise WgLinkError(
                "WGLink resampling failed without a JSON error. stderr: "
                + completed.stderr.strip()
            )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WgLinkError(f"Could not read WGLink resampling output: {exc}") from exc
    if not isinstance(payload, dict):
        raise WgLinkError("WGLink resampling output was not a JSON object.")
    return payload


def _managed_entities(design: object, instance_id: str) -> list[object]:
    found: dict[int, object] = {}
    for attribute in _all_link_attributes(design):
        parent = _parent(attribute)
        if parent is None:
            continue
        try:
            if attribute.name == "instance_id" and str(attribute.value) == instance_id:
                found[id(parent)] = parent
            elif attribute.name == _wrapper_attribute_name(instance_id):
                found[id(parent)] = parent
        except Exception:  # noqa: BLE001
            continue
    return list(found.values())


def _update_payload_attributes(
    design: object,
    instance_id: str,
    updates: dict[str, str],
) -> None:
    for entity in _managed_entities(design, instance_id):
        attrs = _direct_attributes(entity)
        if "topology" in attrs:
            for name, value in updates.items():
                _set_attribute(entity, name, value)
        wrapper_name = _wrapper_attribute_name(instance_id)
        if wrapper_name in attrs:
            try:
                payload = json.loads(attrs[wrapper_name])
            except (TypeError, ValueError):
                payload = {}
            payload.update(updates)
            _set_attribute(entity, wrapper_name, _json(payload))


def _progress_path(instance_id: str, options: dict[str, Any]) -> Path:
    configured = options.get("progress_path")
    if configured:
        return Path(str(configured)).expanduser()
    return Path(tempfile.gettempdir()) / f"wglink-update-{instance_id}.json"


def _write_progress(path: Path, report: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise WgLinkError(f"Could not write rebuild progress report {path}: {exc}") from exc


def _ring_sketches(
    design: object,
    instance_id: str,
    payload: dict[str, Any],
) -> list[tuple[str, int, object, object]]:
    result = []
    try:
        attributes = _items(design.findAttributes(ATTRIBUTE_GROUP, "ring"))
    except Exception as exc:  # noqa: BLE001
        raise WgLinkError(f"Could not resolve managed ring sketches: {exc}") from exc
    for attribute in attributes:
        sketch = _parent(attribute)
        if sketch is None or _attribute_value(sketch, "instance_id") != instance_id:
            continue
        try:
            marker = json.loads(str(attribute.value))
            wall = str(marker["wall"])
            section = int(marker["section"])
            splines = sketch.sketchCurves.sketchFittedSplines
            if splines.count != 1 or not splines.item(0).isClosed:
                raise ValueError("expected one closed fitted spline")
            result.append((wall, section, sketch, splines.item(0)))
        except Exception as exc:  # noqa: BLE001
            raise WgLinkError(f"Managed ring sketch is malformed: {exc}") from exc
    observed = {(wall, section) for wall, section, _sketch, _spline in result}
    for name, token in _token_table(payload).items():
        pieces = name.split(":")
        if len(pieces) != 3 or pieces[0] != "ring":
            continue
        wall = pieces[1]
        try:
            section = int(pieces[2])
        except ValueError:
            continue
        if (wall, section) in observed:
            continue
        sketches = [
            entity for entity in _find_by_token(design, token) if _kind(entity) == "Sketch"
        ]
        if len(sketches) != 1:
            continue
        sketch = sketches[0]
        marker_attribute = _attribute(sketch, "ring")
        if (
            _attribute_value(sketch, "instance_id") != instance_id
            or marker_attribute is None
        ):
            raise WgLinkError(
                "A stored ring entity token resolved to a sketch that no longer "
                "carries this instance's ring attributes. Recreate the link."
            )
        try:
            stored_marker = json.loads(str(marker_attribute.value))
        except (TypeError, ValueError) as exc:
            raise WgLinkError(
                "A token-resolved ring marker is invalid. Recreate the link."
            ) from exc
        if stored_marker != {"wall": wall, "section": section}:
            raise WgLinkError(
                "A stored ring entity token resolved to the wrong managed ring. "
                "Recreate the link."
            )
        splines = sketch.sketchCurves.sketchFittedSplines
        if splines.count == 1 and splines.item(0).isClosed:
            result.append((wall, section, sketch, splines.item(0)))
    order = {"inner": 0, "outer": 1}
    result.sort(key=lambda row: (order.get(row[0], 99), row[1]))
    return result


def _interface_sketches(
    design: object,
    instance_id: str,
    payload: dict[str, Any],
) -> dict[str, tuple[object, object]]:
    result = {}
    try:
        attributes = _items(design.findAttributes(ATTRIBUTE_GROUP, "interface"))
    except Exception as exc:  # noqa: BLE001
        raise WgLinkError(f"Could not resolve managed interface sketches: {exc}") from exc
    for attribute in attributes:
        sketch = _parent(attribute)
        if sketch is None or _attribute_value(sketch, "instance_id") != instance_id:
            continue
        role = str(attribute.value)
        splines = sketch.sketchCurves.sketchFittedSplines
        if splines.count != 1 or not splines.item(0).isClosed:
            raise WgLinkError(f"Managed {role} interface sketch is not one closed fitted spline.")
        result[role] = (sketch, splines.item(0))
    for role in ("throat", "mouth"):
        if role in result:
            continue
        token = _token_table(payload).get(f"interface:{role}", "")
        sketches = [
            entity for entity in _find_by_token(design, token) if _kind(entity) == "Sketch"
        ]
        if len(sketches) != 1:
            continue
        sketch = sketches[0]
        if (
            _attribute_value(sketch, "instance_id") != instance_id
            or _attribute_value(sketch, "interface") != role
        ):
            raise WgLinkError(
                f"The stored {role} entity token resolved to a sketch without this "
                "instance's interface attributes. Recreate the link."
            )
        splines = sketch.sketchCurves.sketchFittedSplines
        if splines.count == 1 and splines.item(0).isClosed:
            result[role] = (sketch, splines.item(0))
    return result


def _validate_rebuild_topology(
    rings: list[tuple[str, int, object, object]],
    interfaces: dict[str, tuple[object, object]],
    payload: dict[str, Any],
    topology: dict[str, Any],
) -> tuple[int, int]:
    sections = int(payload.get("sections", -1))
    points = int(payload.get("points_per_ring", -1))
    walls = int(topology.get("walls", -1))
    expected_rings = sections * walls
    actual_counts = sorted({spline.fitPoints.count for _wall, _section, _sketch, spline in rings})
    if (
        float(topology.get("overshoot_mm", 0.0)) > 0.0
        and len(rings) == expected_rings + walls
        and actual_counts == [points]
    ):
        raise WgLinkError(
            "This WGLink link predates the mouth face-extrude change and still "
            f"contains {walls} synthetic overshoot ring sketch(es). The current "
            f"topology requires {expected_rings} real ring sketches. Recreate the "
            "link before updating."
        )
    if len(rings) != expected_rings or actual_counts != [points]:
        count_text = actual_counts if actual_counts else [0]
        raise WgLinkError(
            f"Rebuild topology mismatch: document has {len(rings)} ring sketches "
            f"with point counts {count_text}; payload requires {expected_rings} "
            f"ring sketches ({sections} sections x {walls} walls) of {points} points. "
            "Recreate the link with the desired topology."
        )
    expected_markers = [
        (wall, section)
        for wall in (["inner", "outer"] if walls == 2 else ["inner"])
        for section in range(sections)
    ]
    if [(wall, section) for wall, section, _sketch, _spline in rings] != expected_markers:
        raise WgLinkError("Managed ring section markers are missing or duplicated; Recreate the link.")
    if set(interfaces) != {"throat", "mouth"}:
        raise WgLinkError(
            "Managed throat/mouth interface sketches are missing. Recreate the link "
            "before updating."
        )
    interface_counts = {spline.fitPoints.count for _sketch, spline in interfaces.values()}
    if interface_counts != {points}:
        raise WgLinkError(
            f"Interface sketch point counts {sorted(interface_counts)} do not match payload {points}."
        )
    if walls == 2 and payload.get("outer_points") is None:
        raise WgLinkError("Document topology has two walls but the update payload has no outer_points.")
    return sections, points


def _move_spline(sketch: object, spline: object, points: list[list[float]]) -> int:
    visible = sketch.isLightBulbOn
    moved = 0
    try:
        if not visible:
            sketch.isLightBulbOn = True
        sketch.isComputeDeferred = True
        fit_points = spline.fitPoints
        for index, xyz in enumerate(points):
            current = fit_points.item(index).geometry
            target = _point(xyz)
            vector = adsk.core.Vector3D.create(
                target.x - current.x,
                target.y - current.y,
                target.z - current.z,
            )
            if vector.length > 1.0e-5:
                fit_points.item(index).move(vector)
                moved += 1
    finally:
        try:
            sketch.isComputeDeferred = False
        except Exception:  # noqa: BLE001
            pass
        try:
            sketch.isLightBulbOn = visible
        except Exception:  # noqa: BLE001
            pass
    return moved


def _rebuild_interface_points(payload: dict[str, Any]) -> dict[str, list[list[float]]]:
    points = payload["points"]
    mouth_section = int(payload["sections"]) - 1
    return {
        "throat": [points[index][0] for index in range(len(points))],
        "mouth": [points[index][mouth_section] for index in range(len(points))],
    }


def _fixed_enclosure_sketch(design: object, instance_id: str) -> object | None:
    try:
        attributes = _items(design.findAttributes(ATTRIBUTE_GROUP, "enclosure_sketch"))
    except Exception:  # noqa: BLE001
        return None
    for attribute in attributes:
        sketch = _parent(attribute)
        if sketch is None or _attribute_value(sketch, "instance_id") != instance_id:
            continue
        try:
            marker = json.loads(str(attribute.value))
        except (TypeError, ValueError):
            continue
        if marker.get("parametric") is False:
            return sketch
    return None


def _move_fixed_enclosure(sketch: object, bundle: object) -> int:
    plan = enclosure_plan(bundle)
    if plan is None:
        return 0
    x0, y0, x1, y1 = plan.rectangle_mm
    target = sorted(
        [
            (mm_to_internal(x0), mm_to_internal(y0)),
            (mm_to_internal(x0), mm_to_internal(y1)),
            (mm_to_internal(x1), mm_to_internal(y0)),
            (mm_to_internal(x1), mm_to_internal(y1)),
        ]
    )
    unique: dict[int, object] = {}
    for line in _items(sketch.sketchCurves.sketchLines):
        unique[id(line.startSketchPoint)] = line.startSketchPoint
        unique[id(line.endSketchPoint)] = line.endSketchPoint
    points = sorted(unique.values(), key=lambda point: (point.geometry.x, point.geometry.y))
    if len(points) != 4:
        raise WgLinkError(
            f"Fixed enclosure sketch has {len(points)} corner points; expected 4. Recreate the link."
        )
    moved = 0
    visible = sketch.isLightBulbOn
    try:
        sketch.isLightBulbOn = True
        sketch.isComputeDeferred = True
        for point, (x_value, y_value) in zip(points, target):
            current = point.geometry
            vector = adsk.core.Vector3D.create(x_value - current.x, y_value - current.y, 0.0)
            if vector.length > 1.0e-5:
                point.move(vector)
                moved += 1
    finally:
        try:
            sketch.isComputeDeferred = False
        except Exception:  # noqa: BLE001
            pass
        try:
            sketch.isLightBulbOn = visible
        except Exception:  # noqa: BLE001
            pass
    return moved


def _next_wrapper_name(root: object, slug: str) -> str:
    existing = set()
    for occurrence in _items(root.occurrences):
        try:
            existing.add(occurrence.component.name)
        except Exception:  # noqa: BLE001
            continue
    index = 1
    while f"WGLink_{slug}_{index}" in existing:
        index += 1
    return f"WGLink_{slug}_{index}"


def _create_wrapper(
    design: object,
    slug: str,
    options: dict[str, Any],
    warnings: list[str],
) -> tuple[object, object | None, str]:
    root = design.rootComponent
    name = _next_wrapper_name(root, slug)
    identity = adsk.core.Matrix3D.create()
    try:
        occurrence = root.occurrences.addNewComponent(identity)
    except RuntimeError as exc:
        part_design_error = (
            "Part Design documents can only contain one component" in str(exc)
            and "Failed to create component" in str(exc)
        )
        if not part_design_error:
            raise
        if options.get("allow_root_fallback", True) is False:
            raise WgLinkError(
                "Fusion refused the managed wrapper because this is a Part Design "
                "document. Create an Assembly document, or allow the explicit root "
                "fallback."
            ) from exc
        warnings.append(
            "LOUD: Fusion Part Design permits only one component, so WGLink was "
            "placed in the root component. This link cannot be moved or jointed as "
            "a unit; use an Assembly document for a movable wrapper."
        )
        return root, None, "root"
    try:
        # MEASURED Fusion 2704.1.53: the first component is grounded to its
        # parent at creation, and that separate flag silently discards moves.
        occurrence.isGroundToParent = False
    except Exception as exc:  # noqa: BLE001 - a grounded link remains usable
        warnings.append(
            "LOUD: Fusion refused to clear Ground to Parent on the WGLink wrapper: "
            f"{exc}. It cannot be moved or jointed as a unit until you right-click "
            "the component in the browser and choose Unground From Parent."
        )
    component = occurrence.component
    component.name = name
    return component, occurrence, "occurrence"


def _validate_enclosure_placement(bundle: object) -> None:
    plan = enclosure_plan(bundle)
    if plan is None:
        return
    x0, y0, _x1, _y1 = plan.rectangle_mm
    if x0 >= 0.0 or y0 >= 0.0:
        raise WgLinkError(
            "WGLink refuses enclosure placement because the realized origin values "
            f"do not straddle the waveguide axis: enc_x0={x0:g} mm and "
            f"enc_y0={y0:g} mm; both must be negative. Correct the WG enclosure "
            "bounds and export again."
        )


def _validate_mouth_outline(bundle: object) -> None:
    datum = bundle.manifest.get("datums", {}).get("WG_MOUTH_OUTLINE_INNER", {})
    points = datum.get("points_mm") if isinstance(datum, dict) else None
    if not isinstance(points, list) or len(points) != bundle.grid["n_phi"]:
        raise WgLinkError(
            "Manifest datum WG_MOUTH_OUTLINE_INNER does not match the point-grid "
            "rays; re-export the bundle from WG."
        )


def _timeline_index(entity: object) -> int | None:
    try:
        value = entity.timelineObject.index
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, int) and value >= 0 else None


def _stored_parameter_prefixes(records: dict[str, dict[str, Any]]) -> list[str]:
    prefixes = []
    for record in records.values():
        payload = record.get("payload", {})
        prefix = payload.get("parameter_prefix")
        if prefix:
            prefixes.append(str(prefix))
            continue
        slug = payload.get("slug")
        if slug:
            prefixes.append(f"wg_{slug}_")
    return prefixes


def _group_timeline(design: object, start: int, name: str, warnings: list[str]) -> None:
    end = design.timeline.count - 1
    if end < start:
        return
    try:
        group = design.timeline.timelineGroups.add(start, end)
        group.name = name
    except Exception as exc:  # noqa: BLE001 - grouping is presentation, not geometry
        warnings.append(f"Could not group the WGLink timeline range: {exc}")


def insert(
    app: adsk.core.Application,
    bundle_path: str | os.PathLike[str],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize one full Waveguide Generator viewport model."""

    opts = _options(options)
    design = _design(app)
    bundle = _read_owned_bundle(bundle_path)
    mode = bundle.manifest["design"]["build_mode"]
    _validate_enclosure_placement(bundle)
    _validate_mouth_outline(bundle)
    if mode == "enclosure" and not bundle.grid.get("all_rings_planar"):
        raise WgLinkError(
            "An enclosure cut tool requires a planar point grid, but this bundle "
            "contains non-planar rings. Export a valid enclosure build from WG."
        )
    plan = plan_sections(
        bundle.grid["n_phi"],
        bundle.grid["n_length"],
        inner_points=bundle.grid["inner_points"],
    )
    slug = parameter_slug(bundle)
    overshoot_mm = float(opts.get("overshoot_mm", 5.0)) if mode == "enclosure" else 0.0
    if overshoot_mm <= 0.0 and mode == "enclosure":
        raise WgLinkError("enclosure overshoot_mm must be positive to avoid a zero-thickness cut")
    instance_id = str(opts.get("instance_id") or uuid.uuid4())
    existing_records = _link_records(design)
    if instance_id in existing_records:
        raise WgLinkError(
            f"WGLink instance id {instance_id!r} already exists. Choose a unique id."
        )
    parameter_prefix = instance_parameter_prefix(
        slug, _stored_parameter_prefixes(existing_records)
    )
    parameters = _parameter_map(bundle, parameter_prefix)
    wall_parameter = None
    if mode == "freestanding":
        wall_parameter = _parameter_by_suffix(parameters, "wall_t")
        if wall_parameter.value <= 0.0:
            raise WgLinkError(
                f"Freestanding wall_t must be positive, got {wall_parameter.value:g} mm."
            )
    role = str(opts.get("role", "HF"))
    expected_area = throat_area_mm2(bundle)
    throat_axis = tuple(
        sum(
            float(bundle.grid["inner_points"][ray][0][axis])
            for ray in range(bundle.grid["n_phi"])
        )
        / bundle.grid["n_phi"]
        for axis in range(3)
    )
    throat_z = throat_axis[2]
    warnings: list[str] = []
    report: dict[str, Any] = {
        "wrapper": "occurrence",
        "assembly_from_link": [list(row) for row in IDENTITY_MATRIX],
        "warnings": warnings,
    }
    timeline_start = design.timeline.count
    started = time.time()
    mutated = False
    made: list[object] = []
    try:
        component, occurrence, wrapper_mode = _create_wrapper(design, slug, opts, warnings)
        mutated = True
        report["wrapper"] = wrapper_mode
        parameter_report = _push_parameters(design, bundle, parameter_prefix)
        mouth_overshoot_parameter = None
        if mode == "enclosure":
            mouth_overshoot_parameter, overshoot_parameter_report = (
                _push_mouth_overshoot_parameter(
                    design, parameter_prefix, overshoot_mm
                )
            )
            _merge_parameter_reports(parameter_report, overshoot_parameter_report)
        interfaces = _build_interface_sketches(component, bundle, plan, instance_id)
        made.extend(interfaces.values())
        section_points = _selected_sections(bundle.grid, plan)
        cut_or_surface, loft, ring_sketches = _build_loft(
            component,
            section_points,
            planar=bool(bundle.grid.get("all_rings_planar")),
            solid=mode == "enclosure",
            instance_id=instance_id,
        )
        made.extend(ring_sketches)
        made.append(loft)
        if mode == "enclosure":
            assert mouth_overshoot_parameter is not None
            cut_or_surface, mouth_extrude = _extrude_mouth_overshoot(
                component,
                cut_or_surface,
                mouth_z_mm=float(bundle.grid["ring_z_mm"][-1]),
                overshoot_parameter=mouth_overshoot_parameter,
                instance_id=instance_id,
            )
            made.append(mouth_extrude)
        _stamp_managed(cut_or_surface, instance_id, "role", "cut_tool")
        _name_body(
            cut_or_surface,
            "cut_tool" if mode == "enclosure" else "waveguide_surface",
        )
        # Keep every managed fit-point sketch before the first non-Sketch /
        # non-Occurrence timeline entry.  The pure rollback rule can then land
        # on the loft without suppressing captured sketch references.  Datum
        # features are independent siblings and can safely follow the loft.
        _datums, datum_report = _build_datums(
            component, bundle, parameters, instance_id
        )
        thicken_sign = None
        enclosure_report = None
        if mode == "freestanding":
            assert wall_parameter is not None
            final_body, thicken_sign, close_features = _close_and_thicken(
                component,
                cut_or_surface,
                throat_axis_mm=throat_axis,
                wall_t_mm=wall_parameter.value,
                thickness_parameter=wall_parameter.name,
                instance_id=instance_id,
            )
            made.extend(close_features)
            _stamp_managed(final_body, instance_id, "role", "waveguide")
            _name_body(final_body, "waveguide")
        else:
            final_body, enclosure_report, enclosure_features = _build_enclosure(
                component,
                bundle,
                parameters,
                parameter_prefix,
                cut_or_surface,
                instance_id,
            )
            made.extend(enclosure_features)
            _stamp_managed(final_body, instance_id, "role", "enclosure")
            _name_body(final_body, "enclosure")

        tag = _tag_report(
            app,
            design,
            final_body,
            instance_id=instance_id,
            role=role,
            throat_z_mm=throat_z,
            expected_area_mm2=expected_area,
            repair=True,
        )
        assembly = [list(row) for row in IDENTITY_MATRIX]
        payload = attribute_payload(
            bundle,
            instance_id=instance_id,
            plan=plan,
            overshoot_mm=overshoot_mm,
            walls=1,
            assembly_from_link=assembly,
            thicken_sign=thicken_sign,
            parameter_prefix=parameter_prefix,
        )
        payload["bundle_path"] = str(Path(bundle_path).expanduser().resolve())
        payload["parameter_expressions"] = _json(
            _managed_parameter_expressions(
                bundle,
                parameter_prefix,
                mouth_overshoot_mm=(overshoot_mm if mode == "enclosure" else None),
            )
        )
        payload["expected_throat_area_mm2"] = repr(expected_area)
        payload["body_fingerprint"] = _json(_body_fingerprint(final_body))
        payload["occurrence_token"] = _entity_token(occurrence) if occurrence else ""
        payload["wrapper"] = wrapper_mode
        payload["source_role"] = role
        payload["throat_z_mm"] = repr(throat_z)
        managed_sketch_indices = [
            index
            for entity in (*interfaces.values(), *ring_sketches, *made)
            if _kind(entity) == "Sketch" and (index := _timeline_index(entity)) is not None
        ]
        if not managed_sketch_indices:
            raise WgLinkError(
                "Fusion exposed no timeline index for this link's managed sketches."
            )
        payload["timeline_start"] = str(timeline_start)
        payload["timeline_end"] = str(design.timeline.count - 1)
        payload["last_managed_sketch_index"] = str(max(managed_sketch_indices))
        token_entities = {
            "body:final": final_body,
            "wrapper:component": component,
            **{f"interface:{name}": entity for name, entity in interfaces.items()},
            **{
                f"ring:inner:{index}": entity
                for index, entity in enumerate(ring_sketches)
            },
            **{f"datum:{name}": entity for name, entity in _datums.items()},
            **{
                f"managed:{index}:{_kind(entity) or 'entity'}": entity
                for index, entity in enumerate(made)
            },
        }
        if occurrence is not None:
            token_entities["wrapper:occurrence"] = occurrence
        for attribute in _items(design.findAttributes(ATTRIBUTE_GROUP, "face_role")):
            face = _parent(attribute)
            if face is not None and _attribute_value(face, "instance_id") == instance_id:
                token_entities[f"face:{role}"] = face
        payload["entity_tokens"] = _json(
            {
                name: token
                for name, entity in token_entities.items()
                if (token := _entity_token(entity))
            }
        )
        _stamp_wrapper(component, payload)
        _stamp_payload(final_body, payload)
        for entity in made:
            if _kind(entity) not in {"Sketch", "ConstructionPlane", "ConstructionAxis"}:
                _stamp_payload(entity, payload)

        design_name = str(bundle.manifest.get("design", {}).get("name") or f"WGLink {slug}")
        _group_timeline(design, timeline_start, design_name, warnings)
        checks = _insert_check_points(bundle, plan, int(opts.get("max_checks", 400)))
        report.update(
            {
                "assembly_from_link": assembly,
                "body": _body_measurement(final_body, bundle.manifest),
                "build_seconds": time.time() - started,
                "datums": datum_report,
                "deviation": _deviation(
                    app, final_body, checks, int(opts.get("max_checks", 400))
                ),
                "enclosure": enclosure_report,
                "instance_id": instance_id,
                # Which document-global namespace this insertion took. A second
                # copy of one design gets wg_<slug>2_, and the user needs to
                # know which set of parameters drives which waveguide.
                "parameter_prefix": parameter_prefix,
                "parameters": parameter_report,
                "section_plan": {
                    "phi_stride": plan.phi_stride,
                    "ring_stride": plan.ring_stride,
                    "points_per_ring": plan.points_per_ring,
                    "ring_indices": list(plan.ring_indices),
                    "sections": len(plan.ring_indices),
                    "overshoot_mm": overshoot_mm,
                },
                "tag": tag,
            }
        )
        if mouth_overshoot_parameter is not None:
            report["mouth_overshoot_parameter"] = mouth_overshoot_parameter
        if mode == "enclosure":
            cabinet = enclosure_plan(bundle, parameter_prefix)
            if cabinet is not None:
                x0, y0, x1, y1 = cabinet.rectangle_mm
                report["body"]["enclosure_bounds_mm"] = [
                    [x0, y0, -cabinet.back_extent_mm],
                    [x1, y1, cabinet.front_extent_mm],
                ]
        if enclosure_report and enclosure_report["parametric"] is False:
            warnings.append(
                "LOUD: Fusion rejected the driven enclosure dimensions. The fixed-geometry "
                "fallback is active; Update will move its four corners explicitly."
            )
        _warn_unmeasured(report.get("deviation"), warnings)
        return report
    except WgLinkError as exc:
        if mutated:
            raise WgLinkError(
                f"{exc} A partial WGLink insertion exists; Undo will clear it."
            ) from exc
        raise
    except Exception as exc:  # noqa: BLE001 - one head-less Fusion boundary
        remedy = " Use Undo to recover the partial insertion." if mutated else ""
        raise WgLinkError(f"Fusion refused WGLink Insert: {exc}.{remedy}") from exc


def _bundle_path_for_update(record: dict[str, Any], bundle_path: object) -> Path:
    raw = bundle_path if bundle_path is not None else record.get("payload", {}).get("bundle_path")
    if not raw:
        raise WgLinkError(
            "This link has no bundle path. Use Relink to locate the .wglink bundle, "
            "then run Update again."
        )
    path = Path(str(raw)).expanduser()
    if not path.exists():
        raise WgLinkError(
            f"The linked bundle path is missing: {path}. Use Relink to select its "
            "new or renamed location."
        )
    return path


def _no_op_update_report(
    app: object,
    design: object,
    record: dict[str, Any],
    bundle: object,
    options: dict[str, Any],
    link_frame: dict[str, object],
) -> dict[str, Any]:
    body = record["body"]
    role = record["payload"].get("source_role", "HF")
    throat_z = float(record["payload"].get("throat_z_mm", bundle.grid["ring_z_mm"][0]))
    expected = float(record["payload"].get("expected_throat_area_mm2", throat_area_mm2(bundle)))
    count = design.timeline.count
    report = {
        "wrapper": record["payload"].get("wrapper", "root"),
        "assembly_from_link": _assembly_from_link(design, record),
        "link_frame": link_frame,
        "tag": _tag_report(
            app,
            design,
            body,
            instance_id=record["instance_id"],
            role=role,
            throat_z_mm=throat_z,
            expected_area_mm2=expected,
            repair=True,
        ),
        "deviation": _deviation(app, body, [], int(options.get("max_checks", 400))),
        "warnings": ["The incoming bundle is already the linked export; Update was a no-op."],
        "fit_points_moved": 0,
        "sections_done": 0,
        "regressed": [],
        "timeline": {
            "count": count,
            "rolled_back_to": None,
            "rolled_back_to_kind": None,
            "restored": design.timeline.markerPosition,
        },
    }
    if record["payload"].get("build_mode") == "enclosure":
        prefix = str(
            record["payload"].get("parameter_prefix")
            or f"wg_{record['payload'].get('slug', '')}_"
        )
        report["mouth_overshoot_parameter"] = (
            f"{prefix}{MOUTH_OVERSHOOT_SUFFIX}"
        )
    return report


def update(
    app: adsk.core.Application,
    bundle_path: str | os.PathLike[str] | None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild a managed link in place by moving existing fit points only."""

    opts = _options(options)
    design = _design(app)
    record = _resolve_link(design, opts)
    link_frame = _link_frame_report(design, record)
    _refuse_bad_link_frame(link_frame, force=bool(opts.get("force")))
    source_path = _bundle_path_for_update(record, bundle_path)
    bundle = _read_owned_bundle(source_path)
    state = link_state(record["payload"], bundle)
    stored_mode = record["payload"].get("build_mode")
    incoming_mode = bundle.manifest["design"]["build_mode"]
    if incoming_mode == "enclosure" and not bundle.grid.get("all_rings_planar"):
        raise WgLinkError(
            "An enclosure cut tool requires a planar point grid, but this bundle "
            "contains non-planar rings. Export a valid enclosure build from WG."
        )
    if stored_mode != incoming_mode:
        raise WgLinkError(
            f"WGLink build mode mismatch: the document is {stored_mode!r}, while "
            f"the bundle is {incoming_mode!r}. Recreate the link in the intended "
            "mode; force cannot make these topologies interchangeable."
        )
    stored_slug = record["payload"].get("slug")
    incoming_slug = parameter_slug(bundle)
    if stored_slug != incoming_slug:
        raise WgLinkError(
            f"WGLink parameter namespace mismatch: the document owns {stored_slug!r}, "
            f"while the bundle owns {incoming_slug!r}. Recreate the link; force cannot "
            "retarget existing datum and enclosure expressions to another namespace."
        )
    parameter_prefix = str(
        record["payload"].get("parameter_prefix") or f"wg_{stored_slug}_"
    )
    _validate_enclosure_placement(bundle)
    _validate_mouth_outline(bundle)
    if state.verdict == "corrupt_export":
        raise WgLinkError(
            "WGLink export identity is corrupt: stored sequence "
            f"{state.stored_sequence} names export {state.stored_export_id!r}, but "
            f"the selected bundle names {state.bundle_export_id!r}. One sequence "
            "must identify one export; Relink to the correct bundle or re-export from WG."
        )
    if state.verdict in {"different_design", "different_lineage", "older_export"} and not opts.get("force"):
        remedy = (
            "Choose the bundle exported from this design or use Relink; pass force=True "
            "only after confirming the replacement."
        )
        raise WgLinkError(f"WGLink refuses {state.verdict}: {remedy}")
    no_op = state.verdict == "same_export" and not opts.get("force")
    try:
        topology = json.loads(record["payload"]["topology"])
    except (TypeError, ValueError, KeyError) as exc:
        raise WgLinkError("Stored WGLink topology is invalid. Recreate the link.") from exc
    payload = _resample_payload(source_path, topology, opts)
    rings = _ring_sketches(design, record["instance_id"], record["payload"])
    interfaces = _interface_sketches(
        design, record["instance_id"], record["payload"]
    )
    sections, _points = _validate_rebuild_topology(rings, interfaces, payload, topology)
    if no_op:
        return _no_op_update_report(
            app, design, record, bundle, opts, link_frame
        )

    _expand_groups(design.timeline)
    entries = _timeline_entries(design.timeline)
    if "last_managed_sketch_index" not in record["payload"]:
        raise WgLinkError(
            "This link predates scoped timeline metadata. Recreate it before Update; "
            "WGLink will not roll back across unrelated document features."
        )
    last_sketch_index = _last_managed_sketch_index(
        *(entry[2] for entry in rings),
        *(pair[0] for pair in interfaces.values()),
    )
    if last_sketch_index is None:
        raise WgLinkError(
            "WGLink could not locate its own sketches in the timeline, so it cannot "
            "tell which features depend on them. Refusing to edit; Recreate the link."
        )
    target_index = rollback_target(entries, after_index=last_sketch_index)
    target_entry = next(
        (entry for entry in entries if entry["index"] == target_index),
        None,
    )
    if target_index is None or target_entry is None:
        raise WgLinkError(
            "WGLink could not find a timeline rollback target after the managed "
            "Sketch and Occurrence entries. Refusing to edit; Recreate the link "
            "or inspect the timeline."
        )
    before_health, before_skipped = _feature_health(design)
    timeline_report = {
        "count": design.timeline.count,
        "rolled_back_to": target_index,
        "rolled_back_to_kind": target_entry["kind"],
        "restored": None,
    }
    progress_path = _progress_path(record["instance_id"], opts)
    report: dict[str, Any] = {
        "wrapper": record["payload"].get("wrapper", "root"),
        "assembly_from_link": _assembly_from_link(design, record),
        "link_frame": link_frame,
        "warnings": [],
        "fit_points_moved": 0,
        "sections_done": 0,
        "regressed": [],
        "timeline": timeline_report,
        "progress_path": str(progress_path),
        "progress": [],
    }
    mouth_overshoot = float(payload.get("overshoot_mm", 0.0))
    if incoming_mode == "enclosure":
        report["mouth_overshoot_parameter"] = (
            f"{parameter_prefix}{MOUTH_OVERSHOOT_SUFFIX}"
        )
    if before_skipped:
        report["warnings"].append(
            f"Health diagnostics skipped {len(before_skipped)} unreadable timeline entries."
        )
    local_state = _local_body_state(record)
    marker_moved = False
    failure: Exception | None = None
    try:
        design.timeline.markerPosition = target_index
        marker_moved = True
        report["parameters"] = _push_parameters(design, bundle, parameter_prefix)
        if incoming_mode == "enclosure":
            _mouth_overshoot_parameter, overshoot_parameter_report = (
                _push_mouth_overshoot_parameter(
                    design, parameter_prefix, mouth_overshoot
                )
            )
            _merge_parameter_reports(
                report["parameters"], overshoot_parameter_report
            )
        for wall, section, sketch, spline in rings:
            grid = payload["points"] if wall == "inner" else payload["outer_points"]
            points = [grid[index][section] for index in range(len(grid))]
            began = time.time()
            moved = _move_spline(sketch, spline, points)
            report["fit_points_moved"] += moved
            report["sections_done"] += 1
            report["progress"].append(
                {
                    "wall": wall,
                    "section": section,
                    "moved": moved,
                    "seconds": time.time() - began,
                }
            )
            _write_progress(progress_path, report)
            adsk.doEvents()

        interface_points = _rebuild_interface_points(payload)
        for role, (sketch, spline) in interfaces.items():
            report["fit_points_moved"] += _move_spline(
                sketch, spline, interface_points[role]
            )
        fixed = _fixed_enclosure_sketch(design, record["instance_id"])
        if fixed is not None:
            report["fit_points_moved"] += _move_fixed_enclosure(fixed, bundle)
        _write_progress(progress_path, report)
    except Exception as exc:  # noqa: BLE001 - marker restoration happens below
        failure = exc
    finally:
        if marker_moved:
            try:
                design.timeline.moveToEnd()
                timeline_report["restored"] = design.timeline.markerPosition
            except Exception as exc:  # noqa: BLE001
                if failure is None:
                    failure = exc
                report["warnings"].append(f"Timeline marker restoration failed: {exc}")
    if failure is not None:
        tag_failure = None
        try:
            failed_body_record = _resolve_link(
                design, {"instance_id": record["instance_id"]}
            )
            _tag_report(
                app,
                design,
                failed_body_record["body"],
                instance_id=record["instance_id"],
                role=record["payload"].get("source_role", "HF"),
                throat_z_mm=float(
                    payload.get(
                        "throat_z_mm", record["payload"].get("throat_z_mm", 0.0)
                    )
                ),
                expected_area_mm2=throat_area_mm2(bundle),
                repair=True,
            )
        except Exception as exc:  # noqa: BLE001 - never mask the rebuild failure
            tag_failure = str(exc)
        tag_note = f" Tag repair also failed: {tag_failure}." if tag_failure else ""
        raise WgLinkError(
            f"WGLink rebuild failed after {report['sections_done']} sections: {failure}. "
            f"Fusion has no transaction for this operation; use Undo to recover.{tag_note}"
        ) from failure

    body_record = _resolve_link(design, {"instance_id": record["instance_id"]})
    body = body_record["body"]
    role = record["payload"].get("source_role", "HF")
    throat_z = float(payload.get("throat_z_mm", record["payload"].get("throat_z_mm", 0.0)))
    expected = throat_area_mm2(bundle)
    tag = _tag_report(
        app,
        design,
        body,
        instance_id=record["instance_id"],
        role=role,
        throat_z_mm=throat_z,
        expected_area_mm2=expected,
        repair=True,
    )
    after_health, after_skipped = _feature_health(design)
    regressions = health_regressions(before_health, after_health)
    if after_skipped:
        report["warnings"].append(
            f"Post-update health diagnostics skipped {len(after_skipped)} unreadable entries."
        )
    assembly = _assembly_from_link(design, body_record)
    report.update(
        {
            "assembly_from_link": assembly,
            "body": _body_measurement(body, bundle.manifest),
            "deviation": _deviation(
                app,
                body,
                transform_points(list(payload.get("check_points") or []), assembly),
                int(opts.get("max_checks", 400)),
            ),
            "regressed": regressions,
            "tag": tag,
        }
    )
    if regressions:
        names = ", ".join(
            f"{row.get('name', '?')} ({row.get('health', '?')})"
            for row in regressions
        )
        _write_progress(progress_path, report)
        raise WgLinkError(
            "WGLink update caused feature-health regressions: "
            f"{names}. Freshness evidence was not committed; Undo the update or "
            "repair every named feature before trying again."
        )
    evidence_state, evidence_fingerprint = refreshed_body_evidence(
        local_state,
        record["payload"].get("body_fingerprint"),
        _json(_body_fingerprint(body)),
    )
    refresh = {
        "assembly_from_link": _json(assembly),
        "bundle_id": str(bundle.manifest.get("bundle", {}).get("id", "")),
        "bundle_path": str(source_path.resolve()),
        "design_hash": str(bundle.manifest.get("design", {}).get("design_hash", "")),
        "design_id": bundle.identity.design_id,
        "design_name": str(bundle.manifest.get("design", {}).get("name", "")),
        "edit_version": str(bundle.manifest.get("design", {}).get("edit_version", "")),
        "export_id": bundle.identity.export_id,
        "export_sequence": str(bundle.identity.export_sequence),
        "geometry_hash": str(bundle.manifest.get("export", {}).get("geometry_hash", "")),
        "lineage_id": bundle.identity.lineage_id or "",
        "local_body_state": evidence_state,
        "parameter_prefix": parameter_prefix,
        "parameter_expressions": _json(
            _managed_parameter_expressions(
                bundle,
                parameter_prefix,
                mouth_overshoot_mm=(
                    mouth_overshoot if incoming_mode == "enclosure" else None
                ),
            )
        ),
        "expected_throat_area_mm2": repr(expected),
        "throat_z_mm": repr(throat_z),
        "slug": incoming_slug,
    }
    if evidence_fingerprint is not None:
        refresh["body_fingerprint"] = evidence_fingerprint
    _update_payload_attributes(design, record["instance_id"], refresh)
    _warn_unmeasured(report.get("deviation"), report.setdefault("warnings", []))
    _write_progress(progress_path, report)
    return report


def _stored_json(payload: dict[str, Any], name: str, default: object) -> object:
    raw = payload.get(name)
    if raw is None:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return default


def _parameter_drift(design: object, record: dict[str, Any]) -> list[dict[str, object]]:
    expected = _stored_json(record["payload"], "parameter_expressions", {})
    if not isinstance(expected, dict):
        return [{"name": "(metadata)", "expected": "parameter map", "actual": "invalid"}]
    drift = []
    for name, expression in sorted(expected.items()):
        parameter = design.userParameters.itemByName(str(name))
        if parameter is None:
            drift.append({"name": name, "expected": expression, "actual": None})
            continue
        try:
            actual = str(parameter.expression)
        except Exception as exc:  # noqa: BLE001
            actual = f"(unreadable: {exc})"
        if actual != str(expression):
            drift.append({"name": name, "expected": expression, "actual": actual})
    return drift


def _ground_to_parent(design: object, record: dict[str, Any]) -> bool:
    if record.get("payload", {}).get("wrapper") == "root":
        return False
    token = record.get("payload", {}).get("occurrence_token", "")
    occurrences = [
        entity
        for entity in _find_by_token(design, token)
        if _kind(entity) == "Occurrence"
    ]
    if not occurrences:
        component = _component_for_record(design, record)
        for occurrence in _items(getattr(design.rootComponent, "allOccurrences", None)):
            try:
                if occurrence.component == component:
                    occurrences.append(occurrence)
            except Exception:  # noqa: BLE001
                continue
    if len(occurrences) == 1:
        try:
            return bool(occurrences[0].isGroundToParent)
        except Exception:  # noqa: BLE001 - Audit evidence must remain best-effort
            return False
    return False


def audit(
    app: adsk.core.Application,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report observable link evidence without claiming reference correctness."""

    opts = _options(options)
    design = _design(app)
    record = _resolve_link(design, opts, allow_missing_body=True)
    payload = record["payload"]
    body = record.get("body")
    warnings: list[str] = []
    stored_path = payload.get("bundle_path")
    state: str
    bundle = None
    if not stored_path:
        state = "missing_bundle_path"
        warnings.append("The bundle path is missing. Use Relink to locate the bundle.")
    elif not Path(stored_path).expanduser().exists():
        state = "missing_bundle"
        warnings.append(
            f"The bundle path no longer exists: {stored_path}. Use Relink to select its new location."
        )
    else:
        bundle = _read_owned_bundle(Path(stored_path).expanduser())
        state = link_state(payload, bundle).verdict

    health, skipped = _feature_health(design)
    unhealthy = [row for row in health if row.get("health") != "Healthy"]
    unsupported = [
        {
            "name": row.get("name"),
            "type": row.get("type"),
            "observed": f"feature health is {row.get('health')}",
        }
        for row in unhealthy
    ]
    if skipped:
        warnings.append(f"Health diagnostics skipped {len(skipped)} unreadable timeline entries.")
    link_frame = _link_frame_report(design, record)
    ground_to_parent = _ground_to_parent(design, record)
    if ground_to_parent:
        warnings.append(
            "The WGLink wrapper is grounded to its parent and cannot be moved or "
            "jointed as a unit. Right-click the component in the browser and choose "
            "Unground From Parent, or re-run Insert."
        )
    local_state = _local_body_state(record)
    if link_frame.get("verdict") == "moved":
        local_state = "modified"
        offset = link_frame.get("offset_mm", [])
        warnings.append(
            "The managed body has left its link frame; tagged throat offset "
            f"is {offset} mm in x/y/plane-z. Undo the body Move, or Detach if "
            "the body is now yours."
        )
    elif link_frame.get("verdict") == "unknown":
        warnings.append(
            "The managed body link frame could not be checked: "
            f"{link_frame.get('error', 'unknown reason')}."
        )
    if local_state == "missing":
        warnings.append("The managed body is missing. Recreate the link; Audit cannot restore it.")
    role = payload.get("source_role", "HF")
    expected = float(payload.get("expected_throat_area_mm2", 0.0) or 0.0)
    throat_z = float(payload.get("throat_z_mm", 0.0) or 0.0)
    if body is None or expected <= 0.0:
        tag = {
            "area_mm2": 0.0,
            "tagged_faces": 0,
            "strays_stripped": 0,
            "repainted": False,
            "role": role,
        }
    else:
        tag = _tag_report(
            app,
            design,
            body,
            instance_id=record["instance_id"],
            role=role,
            throat_z_mm=throat_z,
            expected_area_mm2=expected,
            repair=False,
        )
    return {
        "instance_id": record["instance_id"],
        "link_state": state,
        "parameter_drift": _parameter_drift(design, record),
        "tag": tag,
        "health": health,
        "regressed": unhealthy,
        "unsupported_references": unsupported,
        "local_body_state": local_state,
        "link_frame": link_frame,
        "ground_to_parent": ground_to_parent,
        "direct_reference_limit": (
            "Fusion exposes no face-to-feature reverse index. Audit can name unhealthy "
            "features, but cannot detect a reference that silently rebound to the wrong face."
        ),
        "bundle": str(stored_path or ""),
        "bundle_design_name": (
            str(bundle.manifest.get("design", {}).get("name", "")) if bundle else ""
        ),
        "warnings": warnings,
    }


def relink(
    app: adsk.core.Application,
    bundle_path: str | os.PathLike[str],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Point one existing link at a moved or renamed bundle without rebuilding."""

    opts = _options(options)
    design = _design(app)
    record = _resolve_link(design, opts, allow_missing_body=True)
    path = Path(bundle_path).expanduser()
    if not path.exists():
        raise WgLinkError(f"Relink bundle path does not exist: {path}")
    bundle = _read_owned_bundle(path)
    stored_design = record["payload"].get("design_id")
    if stored_design != bundle.identity.design_id and not opts.get("force"):
        raise WgLinkError(
            f"Relink requires design.id {stored_design!r}, but the selected bundle is "
            f"{bundle.identity.design_id!r}. Select this design's moved bundle, or "
            "pass force=True only if intentionally repointing the link."
        )
    resolved = str(path.resolve())
    _update_payload_attributes(
        design,
        record["instance_id"],
        {"bundle_path": resolved},
    )
    return {
        "instance_id": record["instance_id"],
        "bundle_path": resolved,
        "design_id": bundle.identity.design_id,
        "forced": bool(opts.get("force") and stored_design != bundle.identity.design_id),
        "warnings": [],
    }


def _delete_attribute(attribute: object) -> bool:
    try:
        attribute.deleteMe()
        return True
    except Exception:  # noqa: BLE001
        return False


def detach(
    app: adsk.core.Application,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove only WGLink metadata, leaving all Fusion geometry untouched."""

    opts = _options(options)
    design = _design(app)
    record = _resolve_link(design, opts, allow_missing_body=True)
    instance_id = record["instance_id"]
    entities = _managed_entities(design, instance_id)
    removed: list[dict[str, str]] = []
    wrapper_name = _wrapper_attribute_name(instance_id)
    for entity in entities:
        attrs = _direct_attributes(entity)
        direct_owner = attrs.get("instance_id") == instance_id
        names = list(attrs) if direct_owner else [wrapper_name]
        for name in names:
            if name not in attrs:
                continue
            attribute = _attribute(entity, name)
            if attribute is not None and _delete_attribute(attribute):
                removed.append({"entity": _entity_label(entity), "name": name})
        if not direct_owner and wrapper_name in attrs:
            # A root component may host more than one fallback link.  Keep its
            # shared entity-token cross-check while any wrapper record remains.
            remaining = _direct_attributes(entity)
            if not any(name.startswith("link_") for name in remaining):
                token_attribute = _attribute(entity, "entity_token")
                if token_attribute is not None and _delete_attribute(token_attribute):
                    removed.append(
                        {"entity": _entity_label(entity), "name": "entity_token"}
                    )
    return {
        "instance_id": instance_id,
        "attributes_removed": len(removed),
        "entities_touched": len({row["entity"] for row in removed}),
        "removed": removed,
        "bodies_changed": 0,
        "features_changed": 0,
        "warnings": [],
    }


__all__ = ["WgLinkError", "audit", "detach", "insert", "relink", "update"]
