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
import time
from typing import Any
import uuid

import adsk.core
import adsk.fusion

if __package__:
    from . import wglink_author, wglink_core
    from .wglink_return import (
        DOMAIN_KIND_FOR_PLANES,
        WgReturnError,
        build_return_manifest,
        canonical_domain_planes,
        dumps_return_manifest,
        plan_export_scope,
    )
else:
    import wglink_author
    import wglink_core
    from wglink_return import (
        DOMAIN_KIND_FOR_PLANES,
        WgReturnError,
        build_return_manifest,
        canonical_domain_planes,
        dumps_return_manifest,
        plan_export_scope,
    )


DECLARATION_ATTRIBUTE = "return_declaration"
# The two managed roles that name a final exterior body. Every other managed
# role is a helper WGLink built on the way there; wglink_return skips those, so
# nothing here may mark one as a body the export depends on.
EXTERIOR_ROLES = frozenset({"waveguide", "enclosure"})
DECLARATIONS = frozenset(wglink_author.BODY_DECLARATIONS)
FEM_COMPONENT_NAME = "FEM_MF_AIR"
# One definition, shared with the authoring commands: the dialog that paints a
# role and the export that reads it back must never drift apart. SOURCE_ROLES
# is what a *new* paint offers; RECOGNISED_SOURCE_ROLES is what an *existing*
# painted face is accepted as, which also covers retired spellings such as
# PORT_EXIT so an old export keeps recognising -- and reporting -- its
# original role.
SOURCE_ROLES = wglink_author.SOURCE_ROLES
RECOGNISED_SOURCE_ROLES = wglink_author.RECOGNISED_SOURCE_ROLES
SOURCE_RESOLUTION_MM = {
    "HF": 4.0,
    "MF": 15.0,
    "LF": 30.0,
    "PASSIVE_CARDIOID": 25.0,
    "PORT_EXIT": 25.0,  # legacy spelling; same physical role
}
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


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _shape_fingerprint(body: object) -> dict[str, Any]:
    """A deterministic, transform-aware shape summary for live change detection."""

    faces = []
    for face in wglink_core._items(getattr(body, "faces", None)):
        try:
            box = face.boundingBox
            face_box = [
                float(box.minPoint.x) * 10.0,
                float(box.minPoint.y) * 10.0,
                float(box.minPoint.z) * 10.0,
                float(box.maxPoint.x) * 10.0,
                float(box.maxPoint.y) * 10.0,
                float(box.maxPoint.z) * 10.0,
            ]
            faces.append({
                "area_mm2": float(face.area) * 100.0,
                "bbox_mm": face_box,
                "source_role": _face_role(face),
            })
        except Exception:  # noqa: BLE001 - one unreadable face degrades the token
            faces.append({"unreadable": True})
    faces.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return {
        **wglink_core._body_fingerprint(body),
        "revision_id": str(getattr(body, "revisionId", "") or "") or None,
        "face_count": len(faces),
        "edge_count": len(wglink_core._items(getattr(body, "edges", None))),
        "faces": faces,
    }


def declare_body(body: object, declaration: str) -> None:
    """Set or replace the explicit return classification on one body."""

    value = str(declaration).strip().lower()
    if value not in DECLARATIONS:
        choices = ", ".join(sorted(DECLARATIONS))
        raise wglink_core.WgLinkError(f"body declaration must be one of: {choices}")
    wglink_core._set_attribute(body, DECLARATION_ATTRIBUTE, value)


def clear_declaration(body: object) -> None:
    """Remove an explicit classification, restoring automatic scoping.

    The Declare Body command needs an undo for itself: a body left declared
    ``exclude`` by mistake is invisible to every later export, and there was no
    way to take the declaration back off.
    """

    attribute = wglink_core._attribute(body, DECLARATION_ATTRIBUTE)
    if attribute is None:
        return
    try:
        attribute.deleteMe()
    except Exception as exc:  # noqa: BLE001 - surfaced as an actionable refusal
        raise wglink_core.WgLinkError(
            f"Could not clear the WG declaration on this body: {exc}."
        ) from exc


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


def _occurrence_placement(occurrence: object) -> list[list[float]] | None:
    """The occurrence's parent-relative placement, in the contract's mm rows."""

    for name in ("transform2", "transform"):
        try:
            matrix = getattr(occurrence, name)
        except Exception:  # noqa: BLE001 - try the other spelling
            continue
        if matrix is None:
            continue
        try:
            return wglink_core.fusion_matrix_to_mm(
                [float(component) for component in matrix.asArray()]
            )
        except Exception:  # noqa: BLE001 - an unreadable transform is not identity
            return None
    return None


def _is_identity_placement(
    rows: list[list[float]] | None,
    *,
    rotation_tolerance: float = 1.0e-9,
    translation_tolerance_mm: float = 1.0e-6,
) -> bool:
    if rows is None:
        return False
    for index, row in enumerate(rows):
        for column, value in enumerate(row):
            target = 1.0 if index == column else 0.0
            tolerance = (
                translation_tolerance_mm
                if column == 3 and index < 3
                else rotation_tolerance
            )
            if abs(float(value) - target) > tolerance:
                return False
    return True


ROOT_EXPORT_FRAME = "root-component"
OCCURRENCE_EXPORT_FRAME = "selected-occurrence-component"


def _child_occurrences(occurrence: object, component: object) -> list[object]:
    """The children the scope walk would descend into, resolved its way."""

    children = _collection(occurrence, "childOccurrences")
    if not children:
        children = _collection(component, "occurrences")
    return children


def _selection(design: object, value: object) -> tuple[object, object, object, str]:
    """Resolve the selection into (scope record, export Component, occurrence, frame).

    The second value is what Fusion's STEP export is given, so it is always a
    Component -- see :func:`_export_step`. A Component exports in its OWN
    frame, and the fourth value names that frame so nothing downstream has to
    infer it: ``assembly.bbox_mm``, every ``assembly_from_link``, and the
    declared-domain measurement are all in the frame of the file that was
    written.

    Occurrence scope is therefore *component-local*, not assembly-local. That
    is exact and needs no arithmetic in one shape: when the selected
    occurrence's subtree contains no child occurrences, every exported body is
    a native body of that one component, so each frame-dependent value can be
    read from the native object Fusion already keeps "outside the context of an
    assembly". A placed occurrence that *does* contain children is refused --
    its children's bodies are native to their own components and reaching the
    exported frame from there means composing the placement chain, which is the
    unverified arithmetic ``_strict_assembly_from_link`` refuses for the same
    reason. An identity placement is accepted either way, because then the two
    frames are the same frame.
    """

    root = design.rootComponent
    selected = _selection_items(value)
    if not selected or selected == [root]:
        return "root", root, root, ROOT_EXPORT_FRAME
    if len(selected) != 1:
        raise wglink_core.WgLinkError(
            "Select the root or exactly one occurrence subtree; several selections are not supported."
        )
    entity = getattr(selected[0], "entity", selected[0])
    kind = wglink_core._kind(entity)
    component = getattr(entity, "component", None)
    if kind != "Occurrence" and not (
        component is not None
        and (hasattr(entity, "transform2") or hasattr(entity, "transform"))
    ):
        label = (kind or type(entity).__name__).lower()
        raise wglink_core.WgLinkError(
            f"Cannot export a selected {label}; select the root or exactly one occurrence subtree."
        )
    path = _occurrence_path(entity)
    if component is None:
        # An unresolved external link has no component to export. The scope walk
        # turns this into the actionable "unresolved" refusal, so hand the
        # occurrence back unchanged and let it get there.
        return (
            {"kind": "occurrence", "path": path},
            entity,
            entity,
            OCCURRENCE_EXPORT_FRAME,
        )
    if not _is_identity_placement(
        _occurrence_placement(entity)
    ) and _child_occurrences(entity, component):
        raise wglink_core.WgLinkError(
            f"Occurrence {path!r} is placed away from the assembly origin and "
            "contains sub-assemblies, and Fusion can only export a component in "
            "its own frame. Leave Assembly scope empty to send the whole root "
            "assembly, select one of the sub-assemblies on its own, or move the "
            "occurrence back onto the assembly origin (edit or delete the joint "
            "or Move feature that placed it) and send again."
        )
    return {"kind": "occurrence", "path": path}, component, entity, OCCURRENCE_EXPORT_FRAME


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
    """The role a painted face carries, as its literal appearance name.

    Accepts every recognised role, current or retired, but never rewrites the
    name: a face painted ``PORT_EXIT`` reports ``PORT_EXIT`` here, which is
    what keeps an unchanged return's exported role identical across the
    rename.
    """

    value = wglink_core._appearance_name(face)
    if not isinstance(value, str):
        return None
    canonical = value.strip().upper()
    return canonical if canonical in RECOGNISED_SOURCE_ROLES else None


def _has_source_face(body: object) -> bool:
    return any(_face_role(face) is not None for face in wglink_core._items(getattr(body, "faces", None)))


def _is_managed_helper(candidate: dict[str, Any]) -> bool:
    """Is this a WGLink-managed body that is not the final exterior body?

    Every entity ``_stamp_managed`` touches carries the instance id, including
    the cut tool, the throat patch and the stitched shell that ``_close_and_thicken``
    leaves behind.  ``plan_export_scope`` skips those by role, so they must not
    also claim to hold the anchor or a required source -- a dependency flag on a
    skipped body is a terminal refusal.
    """

    return bool(candidate.get("wglink_managed")) and candidate.get(
        "wglink_role"
    ) not in EXTERIOR_ROLES


def _mark_solver_anchor(candidates: list[dict[str, Any]], anchor: str | None) -> None:
    """Flag the one body the anchor instance solves through, not its helpers."""

    for candidate in candidates:
        candidate["contains_solver_anchor"] = bool(
            anchor
            and candidate.get("wglink_instance_id") == anchor
            and not _is_managed_helper(candidate)
        )


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
    selection, geometry, selected_entity, export_frame = _selection(design, selection_value)
    # Every frame-dependent measurement is taken from the handle that lives in
    # the frame of the file being written, and which handle that is depends on
    # the selected occurrence's placement, not on the export scope alone.
    #
    # A body proxy reports geometry in the ROOT document frame; a native body
    # reports it in its own component's frame. Under root scope the proxy is
    # already right. Under occurrence scope:
    #
    # * placement identity -- the selected component's frame IS the root frame,
    #   so the proxies are right for every body, including bodies inside child
    #   occurrences, whose natives would be in their own child components'
    #   frames and therefore wrong;
    # * placement moved or rotated -- ``_selection`` has already refused any
    #   child occurrence, so every body belongs to the selected component
    #   itself and its native is exactly the exported frame.
    placed = selection != "root" and not _is_identity_placement(
        _occurrence_placement(selected_entity)
    )
    candidates: list[dict[str, Any]] = []
    bodies: dict[str, object] = {}
    measured: dict[str, object] = {}
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
            "contains_required_source": False,
            "contains_solver_anchor": False,
            "only_enclosing_exterior": False,
            "requested_fem_air_volume": False,
        }
        candidate["contains_required_source"] = (
            bool(instance_id) and not _is_managed_helper(candidate)
        ) or _has_source_face(body)
        candidates.append(candidate)
        bodies[object_id] = body
        measured[object_id] = native if placed else body

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
        if children and placed:
            # ``_selection`` refuses this combination, so reaching it means the
            # document changed under the dialog. Refusing again here is cheap,
            # and the alternative is measuring a child's native geometry in the
            # wrong frame.
            raise wglink_core.WgLinkError(
                "The selected occurrence is placed away from the assembly origin "
                "and contains sub-assemblies; its bodies cannot be measured in "
                "the frame the STEP is written in. Leave Assembly scope empty to "
                "send the whole root assembly."
            )
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
        and not _is_managed_helper(item)
    ]
    if len(exterior) == 1:
        exterior[0]["only_enclosing_exterior"] = True
    return {
        "selection": selection,
        "geometry": geometry,
        "selected_occurrence": None if selection == "root" else selected_entity,
        "export_frame": export_frame,
        "candidates": candidates,
        "bodies": bodies,
        "measured": measured,
        "fem_components": fem_components,
        "components": components,
    }


def _record_body(record: dict[str, Any]) -> object | None:
    bodies = [
        entity
        for entity in record.get("entities", [])
        if _role(entity) in EXTERIOR_ROLES
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


def _merge_bounds(
    bounds: dict[str, list[float]] | None, entity: object
) -> dict[str, list[float]] | None:
    """Grow a millimetre bounding box by one body's or face's own box."""

    try:
        box = entity.boundingBox
        low = [
            float(box.minPoint.x) * 10.0,
            float(box.minPoint.y) * 10.0,
            float(box.minPoint.z) * 10.0,
        ]
        high = [
            float(box.maxPoint.x) * 10.0,
            float(box.maxPoint.y) * 10.0,
            float(box.maxPoint.z) * 10.0,
        ]
    except Exception:  # noqa: BLE001 - an unreadable box only narrows the advice
        return bounds
    if not all(math.isfinite(value) for value in (*low, *high)):
        return bounds
    if bounds is None:
        return {"min": low, "max": high}
    return {
        "min": [min(bounds["min"][axis], low[axis]) for axis in range(3)],
        "max": [max(bounds["max"][axis], high[axis]) for axis in range(3)],
    }


def _source_faces(
    records: list[dict[str, Any]],
    included_bodies: list[object],
    retained_fractions: dict[str, float] | None = None,
) -> list[object]:
    """Every face the export would treat as a source, linked or painted."""

    fractions = retained_fractions or {}
    faces: list[object] = []
    seen: set[tuple[str, object]] = set()
    for record in records:
        try:
            claimed = _throat_faces(
                record, fractions.get(str(record["instance_id"]), 1.0)
            )
        except wglink_core.WgLinkError:
            continue
        for face in claimed:
            key = _face_key(face)
            if key not in seen:
                seen.add(key)
                faces.append(face)
    for body in included_bodies:
        for face in wglink_core._items(getattr(body, "faces", None)):
            if _face_role(face) is None:
                continue
            key = _face_key(face)
            if key not in seen:
                seen.add(key)
                faces.append(face)
    return faces


def preflight_scope(app: object, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Gather what the Send and Solve dialogs preview, without exporting.

    Deliberately fail-soft: a scope or source problem is reported as text for
    the dialog to warn about *before* OK, rather than raised as the dead-end
    modal the user used to meet only after asking for an export.  Everything
    returned is plain JSON-ish data -- ``wglink_author.preflight_summary``
    composes the wording and never sees a Fusion object.
    """

    opts = dict(options or {})
    design = wglink_core._design(app)
    walk = _scope_walk(design, opts.get("selection"))
    records = _records_in_scope(design, walk)
    instance_ids = [str(record["instance_id"]) for record in records]
    requested_anchor = _nullable(opts.get("anchor_instance_id"))
    if len(instance_ids) == 1:
        anchor = instance_ids[0]
    elif instance_ids:
        # The dialog's anchor dropdown may not have been touched yet; a preview
        # picks the first rather than refusing the way an export has to.
        anchor = (
            str(requested_anchor)
            if requested_anchor is not None and str(requested_anchor) in instance_ids
            else instance_ids[0]
        )
    else:
        anchor = None
    report: dict[str, Any] = {
        "selection": walk["selection"],
        "instance_ids": instance_ids,
        "included": [],
        "sources": [],
        "scope_error": None,
        "source_error": None,
        "domain": None,
        "domain_error": None,
        "bounds_mm": None,
        "source_bounds_mm": None,
    }
    _mark_solver_anchor(walk["candidates"], anchor)
    try:
        scope = plan_export_scope(walk["selection"], walk["candidates"]).manifest_scope()
    except WgReturnError as exc:
        report["scope_error"] = str(exc)
        return report

    included_pairs = [
        (item, walk["bodies"][item["object_id"]])
        for item in scope["included"]
        if item["object_id"] in walk["bodies"]
    ]
    included_bodies = [body for _item, body in included_pairs]
    measured_bodies = [
        walk["measured"][item["object_id"]] for item, _body in included_pairs
    ]
    report["included"] = [
        {
            "name": str(item.get("path") or item.get("name") or "unnamed body"),
            "body_kind": str(item.get("body_kind") or "solid"),
        }
        for item, _body in included_pairs
    ]
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
    try:
        sources = _sources(
            records,
            included_bodies,
            _retained_fractions(
                design,
                records,
                resolve_domain_planes(opts.get("domain")),
                selected_occurrence=walk["selected_occurrence"],
            ),
        )
    except wglink_core.WgLinkError as exc:
        report["source_error"] = str(exc)
        sources = []
    for source in sources:
        observed = source.get("observed", {})
        report["sources"].append({
            "role": str(source.get("role") or "?"),
            "area_mm2": float(observed.get("total_area_mm2") or 0.0),
            "face_count": int(observed.get("face_count") or 0),
            "instance_id": source.get("instance_id"),
        })
    try:
        report["domain"] = plan_domain(
            resolve_domain_planes(opts.get("domain")), measured_bodies
        )
    except wglink_core.WgLinkError as exc:
        # The preview's whole job is to say this before OK rather than after.
        report["domain_error"] = str(exc)
    bounds = None
    for body in measured_bodies:
        bounds = _merge_bounds(bounds, body)
    report["bounds_mm"] = bounds
    source_bounds = None
    for face in _source_faces(
        records,
        included_bodies,
        _retained_fractions(
            design,
            records,
            resolve_domain_planes(opts.get("domain")),
            selected_occurrence=walk["selected_occurrence"],
        ),
    ):
        source_bounds = _merge_bounds(source_bounds, face)
    report["source_bounds_mm"] = source_bounds
    return report


def return_state(app: object, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fingerprint the same root assembly, sources, and parameters a return exports.

    This deliberately does not write STEP. It runs on Fusion's main thread and
    gives WG a cheap optimistic-concurrency token for the live CAD document.
    """

    opts = dict(options or {})
    design = wglink_core._design(app)
    walk = _scope_walk(design, opts.get("selection"))
    records = _records_in_scope(design, walk)
    instance_ids = [str(record["instance_id"]) for record in records]
    requested_anchor = _nullable(opts.get("anchor_instance_id"))
    if len(instance_ids) == 1:
        anchor = instance_ids[0]
    elif len(instance_ids) > 1:
        if requested_anchor is None or str(requested_anchor) not in instance_ids:
            return {"hash": None, "reason": "ambiguous-link-anchor"}
        anchor = str(requested_anchor)
    else:
        anchor = None
    _mark_solver_anchor(walk["candidates"], anchor)
    try:
        scope = plan_export_scope(
            walk["selection"], walk["candidates"]
        ).manifest_scope()
    except WgReturnError as exc:
        return {"hash": None, "reason": str(exc)}
    included_pairs = [
        (item, walk["bodies"][item["object_id"]])
        for item in scope["included"]
        if item["object_id"] in walk["bodies"]
    ]
    if len(included_pairs) != len(scope["included"]):
        return {"hash": None, "reason": "unresolved-included-body"}
    for record in records:
        source_body = next(
            (
                body
                for included, body in included_pairs
                if included.get("wglink_instance_id") == record["instance_id"]
            ),
            None,
        )
        if source_body is not None:
            record["source_body"] = source_body
    try:
        sources = _sources(
            records,
            [body for _item, body in included_pairs],
            _retained_fractions(
                design,
                records,
                resolve_domain_planes(opts.get("domain")),
                selected_occurrence=walk["selected_occurrence"],
            ),
        )
    except wglink_core.WgLinkError as exc:
        return {"hash": None, "reason": str(exc)}
    bodies = []
    for item, body in included_pairs:
        bodies.append({
            "object_id": item["object_id"],
            "path": item.get("path"),
            "body_kind": item.get("body_kind"),
            "wglink_instance_id": item.get("wglink_instance_id"),
            "fingerprint": _shape_fingerprint(body),
        })
    instances = []
    for record in records:
        instances.append({
            "instance_id": str(record["instance_id"]),
            "design_id": str(record.get("payload", {}).get("design_id") or ""),
            "assembly_from_link": _strict_assembly_from_link(design, record)[0],
            "observed_parameters": wglink_core._observed_parameters(design, record),
        })
    source_state = [{
        "id": source["id"],
        "role": source["role"],
        "instance_id": source.get("instance_id"),
        "expected_connected_components": source["expected_connected_components"],
        "observed": source["observed"],
    } for source in sources]
    instance_identities = _instance_identity_summaries(
        included_pairs=included_pairs,
        instances=instances,
        sources=sources,
    )
    state = {
        "selection": scope["selection"],
        "bodies": bodies,
        "fem_air_volumes": scope["fem_air_volumes"],
        "instances": instances,
        "sources": source_state,
    }
    return {
        "hash": _canonical_hash(state),
        "body_count": len(bodies),
        "source_hash": _canonical_hash(source_state),
        "state": state,
        # These are the exact identities the next return export would carry.
        # Keep them beside, rather than inside, ``state`` so adding advisory
        # heartbeat detail never changes the established return-state hash.
        "instance_identities": instance_identities,
    }


def _instance_identity_summaries(
    *,
    included_pairs: list[tuple[dict[str, Any], object]],
    instances: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Summarize only identities that the live return path actually resolved.

    Fusion entity tokens are published only when the scope walk's object id is
    that same token. A fake or fallback ``body-0001`` label is useful inside a
    one-shot export plan, but it must not be advertised as persistent CAD
    identity. Transform, source, and drive ids come directly from the strict
    return records; this helper never derives plausible substitutes.
    """

    body_ids: dict[str, list[str]] = {}
    incomplete_body_ids: set[str] = set()
    for item, body in included_pairs:
        instance_id = str(item.get("wglink_instance_id") or "")
        if not instance_id:
            continue
        token = wglink_core._entity_token(body)
        if not token or str(item.get("object_id") or "") != token:
            incomplete_body_ids.add(instance_id)
            continue
        body_ids.setdefault(instance_id, []).append(token)

    source_pairs: dict[str, list[tuple[str, str]]] = {}
    incomplete_sources: set[str] = set()
    for source in sources:
        instance_id = str(source.get("instance_id") or "")
        if not instance_id:
            continue
        source_id = str(source.get("id") or "")
        drive_id = str(source.get("default_drive_channel_id") or "")
        if not source_id or not drive_id:
            incomplete_sources.add(instance_id)
            continue
        source_pairs.setdefault(instance_id, []).append((source_id, drive_id))

    summaries: dict[str, dict[str, Any]] = {}
    for instance in instances:
        instance_id = str(instance.get("instance_id") or "")
        if not instance_id:
            continue
        summary: dict[str, Any] = {}
        matrix = instance.get("assembly_from_link")
        if (
            isinstance(matrix, list)
            and len(matrix) == 4
            and all(isinstance(row, list) and len(row) == 4 for row in matrix)
        ):
            try:
                values = [[float(value) for value in row] for row in matrix]
                if all(math.isfinite(value) for row in values for value in row):
                    summary["transform_hash"] = _canonical_hash(values)
            except (TypeError, ValueError, OverflowError):
                pass
        if instance_id not in incomplete_body_ids and body_ids.get(instance_id):
            summary["body_object_ids"] = sorted(set(body_ids[instance_id]))
        if instance_id not in incomplete_sources and source_pairs.get(instance_id):
            pairs = source_pairs[instance_id]
            summary["source_ids"] = sorted({source_id for source_id, _drive_id in pairs})
            summary["drive_channel_ids"] = sorted({drive_id for _source_id, drive_id in pairs})
        if summary:
            summaries[instance_id] = summary
    return summaries


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


def _strict_assembly_from_link(
    design: object,
    record: dict[str, Any],
    *,
    selected_occurrence: object | None = None,
) -> tuple[list[list[float]], str | None]:
    """The transform from this link's own frame into the exported file's frame.

    Under occurrence scope the exported file *is* the selected occurrence's
    component, so for the instance whose wrapper is that very occurrence the
    answer is the identity -- definitionally, not by composing anything. Any
    other in-scope wrapper would need the placement chain and is refused, which
    keeps this function's rule intact: never a plausible default.
    """

    instance_id = str(record["instance_id"])
    if selected_occurrence is not None:
        occurrences = _matching_occurrences(design, record)
        if len(occurrences) == 1 and occurrences[0] is selected_occurrence:
            return [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ], _occurrence_path(selected_occurrence)
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} is not the selected occurrence, so "
            "its placement inside the exported component cannot be recorded "
            "faithfully. Leave Assembly scope empty to send the whole root "
            "assembly, or select that instance's own occurrence."
        )
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


def _instance_record(
    design: object,
    record: dict[str, Any],
    observed_at: str,
    *,
    selected_occurrence: object | None = None,
) -> dict[str, Any]:
    payload = record.get("payload", {})
    matrix, occurrence_path = _strict_assembly_from_link(
        design, record, selected_occurrence=selected_occurrence
    )
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
        "observed_parameters": wglink_core._observed_parameters(design, record),
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


def _throat_faces(record: dict[str, Any], retained_fraction: float = 1.0) -> list[object]:
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
    if role not in RECOGNISED_SOURCE_ROLES or expected is None or throat_z is None:
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} lacks a complete required throat selector."
        )
    # What the declared domain leaves of the contract's disc, decided before any
    # face is looked at. Both candidate branches are measured against it: the
    # geometric one and the painted one match the same physical face, so a
    # required source cut in half used to fail *both* gates and refuse the
    # export -- the linked reduced model WG's own ingest already accepts.
    retained = float(retained_fraction)
    if not math.isfinite(retained) or not 0.0 < retained <= 1.0:
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} has a retained source "
            f"fraction of {retained!r}, which is not a share of a disc."
        )
    expected = expected * retained
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
        share = (
            ""
            if retained >= 1.0
            else f" at the {retained:g} of its disc a declared cut leaves ({expected:.4g} mm2)"
        )
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} required throat source{share} "
            f"resolved to {len(matches)} faces; expected exactly one."
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


#: How far from perpendicular a throat axis may sit to its own throat plane
#: before the disc a declared cut would halve stops being defined. Matches the
#: reader's own limit, because the two decide the same thing about the same
#: contract and a disagreement would be a return one side accepts and the other
#: refuses.
THROAT_NORMAL_ANGLE_DEG = 0.1


def _placed_point(matrix: list[list[float]], point: list[float]) -> list[float]:
    """A link-local millimetre point in the frame the STEP is written in."""

    return [
        sum(matrix[row][column] * point[column] for column in range(3)) + matrix[row][3]
        for row in range(3)
    ]


def _placed_direction(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """A link-local direction in the exported frame: rotation only."""

    return [
        sum(matrix[row][column] * vector[column] for column in range(3))
        for row in range(3)
    ]


def _unit(vector: list[float]) -> tuple[list[float], float]:
    length = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(length) or length <= 0.0:
        return vector, 0.0
    return [value / length for value in vector], length


def _declared_disc_reduction(
    instance_id: str,
    contract: dict[str, Any] | None,
    matrix: list[list[float]] | None,
    planes: tuple[str, ...],
) -> float:
    """What a declared cut leaves of this throat's disc, or a refusal.

    A declared plane does one of exactly two supported things to a throat disc.
    It can miss it -- the ordinary shape of a source whose mirror twin the cut
    removed, which stays whole -- or it can pass through the disc's centre, and
    then exactly half survives because a disc is symmetric about its centre.
    Anything else is refused with the measurement rather than guessed: an
    off-centre clip leaves a circular segment of no fixed fraction, and a plane
    past the far edge leaves no source at all.

    The decision is made once per contract, from the contract's own geometry
    placed in the exported frame, before any face is looked at -- so a face
    cannot argue itself into a different expectation than the one it must meet.
    ``server/mesh/imported.declared_disc_reduction`` decides the same thing on
    the reading side from the same contract, which is why this does not write
    the answer into the manifest: two recorded fractions could disagree, and
    the one that drifted would be the one nobody re-derived.
    """

    if not planes:
        return 1.0
    if contract is None or matrix is None:
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} declares a reduced domain but has no "
            "readable throat contract, so what the cut leaves of its source cannot "
            "be derived. Send the full model, or repair the link's throat datums."
        )
    plane_link = contract.get("throat_plane_link") or {}
    axis_link = contract.get("axis_link") or {}
    normal, normal_length = _unit(
        _placed_direction(matrix, [float(value) for value in plane_link["normal"]])
    )
    direction, direction_length = _unit(
        _placed_direction(matrix, [float(value) for value in axis_link["direction"]])
    )
    diameter = float(contract["throat_diameter_mm"])
    if normal_length <= 0.0 or direction_length <= 0.0 or diameter <= 0.0:
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} has a degenerate throat plane, axis or "
            "diameter, so a declared reduced domain cannot be applied to its source."
        )
    plane_origin = _placed_point(
        matrix, [float(value) for value in plane_link["origin_mm"]]
    )
    axis_origin = _placed_point(
        matrix, [float(value) for value in axis_link["origin_mm"]]
    )
    along = sum(direction[axis] * normal[axis] for axis in range(3))
    if abs(along) < math.cos(math.radians(THROAT_NORMAL_ANGLE_DEG)):
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} has a throat axis that is not "
            "perpendicular to its own throat plane, so the disc a declared reduced "
            "domain would cut is undefined."
        )
    # Where the axis pierces the throat plane: the centre of the contract's disc.
    reach_along = (
        sum((plane_origin[axis] - axis_origin[axis]) * normal[axis] for axis in range(3))
        / along
    )
    centre = [axis_origin[axis] + direction[axis] * reach_along for axis in range(3)]
    radius = 0.5 * diameter

    fraction = 1.0
    cuts: list[list[float]] = []
    for plane in planes:
        axis_index = DOMAIN_AXIS_FOR_PLANE.get(plane)
        if axis_index is None:
            raise wglink_core.WgLinkError(
                f"Declared cut plane {plane!r} is not one a throat source understands."
            )
        world = [0.0, 0.0, 0.0]
        world[axis_index] = 1.0
        projection = sum(world[axis] * normal[axis] for axis in range(3))
        in_plane = [world[axis] - projection * normal[axis] for axis in range(3)]
        span = math.sqrt(sum(value * value for value in in_plane))
        # How far the disc reaches along this world axis, and where its centre
        # sits relative to the plane at coordinate zero.
        reach = radius * span
        offset = centre[axis_index]
        if offset - reach >= -DOMAIN_TOLERANCE_FLOOR_MM:
            continue
        if abs(offset) <= DOMAIN_TOLERANCE_FLOOR_MM and reach > DOMAIN_TOLERANCE_FLOOR_MM:
            fraction *= 0.5
            cuts.append([value / span for value in in_plane])
            continue
        raise wglink_core.WgLinkError(
            f"The export was declared a reduced domain about {DOMAIN_PLANE_LABEL[plane]}, "
            f"but that plane crosses the throat of WGLink instance {instance_id!r} "
            f"{offset:+.4g} mm off its centre (disc reach {reach:.4g} mm). A declared "
            "cut is supported only where it misses a source or passes through its "
            "centre; this one would leave a partial disc of no known area."
        )
    if len(cuts) == 2:
        skew = abs(sum(cuts[0][axis] * cuts[1][axis] for axis in range(3)))
        if skew > 1.0e-3:
            raise wglink_core.WgLinkError(
                f"The two declared cut planes are not perpendicular within the throat "
                f"plane of WGLink instance {instance_id!r}, so the retained wedge is "
                "not a quarter of its disc."
            )
    return fraction


def _retained_fractions(
    design: object,
    records: list[dict[str, Any]],
    planes: tuple[str, ...],
    *,
    selected_occurrence: object | None = None,
) -> dict[str, float]:
    """The share of each link's throat disc a declared cut leaves.

    One derivation, used by the export, by the preview that predicts it and by
    the return-state fingerprint, so a declaration cannot mean one thing in the
    dialog and another in the bundle.
    """

    if not planes:
        return {}
    fractions: dict[str, float] = {}
    for record in records:
        instance_id = str(record["instance_id"])
        matrix, _path = _strict_assembly_from_link(
            design, record, selected_occurrence=selected_occurrence
        )
        fractions[instance_id] = _declared_disc_reduction(
            instance_id, _source_contract(design, record), matrix, planes
        )
    return fractions


def _sources(
    records: list[dict[str, Any]],
    included_bodies: list[object],
    retained_fractions: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    claimed: set[tuple[str, object]] = set()
    used: set[str] = set()
    face_bodies: dict[tuple[str, object], str] = {}
    for body in included_bodies:
        name = str(getattr(body, "name", "unnamed body") or "unnamed body")
        for face in wglink_core._items(getattr(body, "faces", None)):
            face_bodies[_face_key(face)] = name

    fractions = retained_fractions or {}
    for record in records:
        role = str(record.get("payload", {}).get("source_role") or "").upper()
        faces = _throat_faces(record, fractions.get(str(record["instance_id"]), 1.0))
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

    painted: dict[str, list[object]] = {role: [] for role in RECOGNISED_SOURCE_ROLES}
    for body in included_bodies:
        for face in wglink_core._items(getattr(body, "faces", None)):
            role = _face_role(face)
            if role and _face_key(face) not in claimed:
                painted[role].append(face)
    for role in RECOGNISED_SOURCE_ROLES:
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
            "Return export has no drivable source. Paint an included face LF, MF, HF, or PASSIVE_CARDIOID and try again."
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


# A declared domain is measured against the bodies actually being exported,
# with a tolerance that scales with the model: 1e-4 of the bounding diagonal,
# floored so a small model still tolerates the round-off of a CAD cut that
# lands on the plane.
DOMAIN_TOLERANCE_REL = 1.0e-4
DOMAIN_TOLERANCE_FLOOR_MM = 0.05
DOMAIN_AXIS_FOR_PLANE = {"x0": 0, "y0": 1}
DOMAIN_PLANE_LABEL = {"x0": "x = 0", "y0": "y = 0"}


def resolve_domain_planes(value: object) -> tuple[str, ...]:
    """Read ``options['domain']`` as an ordered, checked set of planes."""

    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "full"}:
            return ()
        planes = [part for part in re.split(r"[+,\s]+", text) if part]
    elif isinstance(value, (list, tuple)):
        planes = [str(part).strip().lower() for part in value]
    else:
        raise wglink_core.WgLinkError(
            "options['domain'] must be 'full', a plane name, or a list of plane names."
        )
    try:
        return canonical_domain_planes(planes)
    except WgReturnError as exc:
        raise wglink_core.WgLinkError(str(exc)) from exc


def plan_domain(planes: tuple[str, ...], bodies: list[object]) -> dict[str, Any] | None:
    """Measure the exported bodies before letting them claim a reduced domain.

    A bounding box settles one-sidedness exactly -- a box that stays on the
    positive side of a plane cannot contain geometry on the negative side -- so
    this is a proof, not a heuristic. It is deliberately the only thing checked
    here: whether the *cut face* is open, and whether the rest of the boundary
    leaks, are properties of the meshed surface, and WG re-derives both from the
    mesh it builds. Declaring is the CAD's job; believing is not.
    """

    if not planes:
        return None
    boxes = [wglink_core._bbox_values(body) for body in bodies]
    if not boxes:
        raise wglink_core.WgLinkError(
            "A reduced domain cannot be declared for an export with no included bodies."
        )
    low = [min(box[axis] for box in boxes) for axis in range(3)]
    high = [max(box[axis + 3] for box in boxes) for axis in range(3)]
    diagonal = math.sqrt(sum((high[axis] - low[axis]) ** 2 for axis in range(3)))
    tolerance = max(DOMAIN_TOLERANCE_FLOOR_MM, DOMAIN_TOLERANCE_REL * diagonal)
    evidence: dict[str, Any] = {}
    for plane in planes:
        axis = DOMAIN_AXIS_FOR_PLANE[plane]
        minimum, maximum = float(low[axis]), float(high[axis])
        label = DOMAIN_PLANE_LABEL[plane]
        # Order matters for the remedy, not for the verdict: a model sitting
        # wholly on the wrong side is a mirror away from being right, while one
        # that straddles is not a half at all.
        if maximum <= tolerance:
            raise wglink_core.WgLinkError(
                f"The export was declared a reduced domain about {label}, but the "
                "included bodies have no extent on the positive side of it. WG keeps "
                f"the positive half, so mirror the model onto {label[0]} >= 0 first."
            )
        if minimum < -tolerance:
            raise wglink_core.WgLinkError(
                f"The export was declared a reduced domain about {label}, but the "
                f"included bodies reach {abs(minimum):.4g} mm onto the negative side "
                f"of it (tolerance {tolerance:.4g} mm). Cut the model in CAD, or "
                "declare the full model."
            )
        evidence[plane] = {
            "min_mm": minimum,
            "max_mm": maximum,
            "tolerance_mm": tolerance,
        }
    return {
        "kind": DOMAIN_KIND_FOR_PLANES[planes],
        "cut_planes": list(planes),
        "declared_by": "cad-author",
        "evidence": evidence,
    }


def _export_step(design: object, path: Path, geometry: object) -> None:
    """Export one Component to STEP, which is the only geometry Fusion takes.

    ``ExportManager.createSTEPExportOptions`` documents its second argument as
    "the geometry to export. Valid geometry for this is currently a Component
    object". Handing it an Occurrence is what Fusion refuses with
    ``3 : invlid argument geometry`` -- error code 3 is its invalid-argument
    code and "invlid" is Autodesk's own spelling, so the reported message is
    Fusion rejecting the argument type rather than anything about the model.

    The Component is always passed explicitly, including for the root scope
    where the argument is optional. That is what every Autodesk sample does,
    and it removes one whole class of question about what an omitted optional
    argument resolves to inside Fusion.
    """

    kind = wglink_core._kind(geometry)
    if kind is not None and kind != "Component":
        raise wglink_core.WgLinkError(
            f"Cannot export {path.name} from a {kind}; Fusion's STEP export takes "
            "a Component."
        )
    manager = design.exportManager
    try:
        options = manager.createSTEPExportOptions(str(path), geometry)
        ok = manager.execute(options)
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(f"Fusion STEP export failed for {path.name}: {exc}.") from exc
    if not ok or not path.is_file():
        raise wglink_core.WgLinkError(f"Fusion STEP export failed for {path.name}.")


CAD_DOCUMENT_MEMBER = "document.f3d"
CAD_DOCUMENT_MEDIA_TYPE = "application/vnd.autodesk.fusion360"


def _file_record(
    path: Path, purpose: str, media_type: str = "model/step"
) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "sha256": "sha256:" + digest.hexdigest(),
        "size_bytes": path.stat().st_size,
        "media_type": media_type,
        "purpose": purpose,
    }


def _export_fusion_archive(design: object, path: Path) -> str | None:
    """Export the whole document as a native Fusion archive.

    STEP records the geometry a solve needs; it does not record the timeline,
    the parameters, or anything a person would reopen and edit. The archive is
    the user's own copy of the model a run was solved from, which is why WG
    files it in the run archive rather than treating it as solver input.

    It is a convenience, not evidence, so a failure returns its reason instead
    of costing the user an otherwise complete return. The caller reports the
    reason rather than dropping it: a capture that quietly never happens is
    worse than one that says why.
    """

    manager = getattr(design, "exportManager", None)
    create = getattr(manager, "createFusionArchiveExportOptions", None)
    if create is None:
        return "this Fusion build has no archive export"
    try:
        options = create(str(path))
        ok = options is not None and manager.execute(options)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    if not ok or not path.is_file():
        return "Fusion reported no archive file"
    return None


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


_STALE_PUBLISH_SECONDS = 24 * 60 * 60


def _reservation_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.reserve")


def _reserve_target(target: Path, *, overwrite: bool) -> tuple[Path, Path]:
    """Atomically reserve an immutable bundle name across Fusion processes."""

    stem = target.name.removesuffix(".wgreturn")
    index = 1
    while True:
        candidate = target if index == 1 else target.with_name(f"{stem}-{index}.wgreturn")
        reservation = _reservation_path(candidate)
        try:
            descriptor = os.open(
                reservation,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            if overwrite:
                raise wglink_core.WgLinkError(
                    f"Another WGLink export is already publishing {candidate.name}."
                ) from None
            index += 1
            continue
        try:
            os.write(
                descriptor,
                f"pid={os.getpid()} created={_utc_timestamp()}\n".encode("ascii"),
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if overwrite or not candidate.exists():
            return candidate, reservation
        reservation.unlink(missing_ok=True)
        index += 1


def _cleanup_stale_publish_artifacts(
    output: Path,
    *,
    now: float | None = None,
    stale_after_seconds: float = _STALE_PUBLISH_SECONDS,
) -> None:
    """Recover or remove publish debris that cannot belong to a live export."""

    cutoff = (time.time() if now is None else now) - stale_after_seconds
    try:
        candidates = list(output.iterdir())
    except OSError:
        return
    for candidate in candidates:
        name = candidate.name
        if not name.startswith("."):
            continue
        try:
            if candidate.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if name.endswith(".reserve"):
            candidate.unlink(missing_ok=True)
            continue
        backup_match = re.match(r"^\.(.+\.wgreturn)\.old-[^.]+$", name)
        if backup_match and candidate.is_dir():
            target = output / backup_match.group(1)
            if target.exists():
                shutil.rmtree(candidate, ignore_errors=True)
            else:
                try:
                    os.replace(candidate, target)
                except OSError:
                    pass
            continue
        if ".wgreturn.tmp-" in name and candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=True)


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
    domain_planes = resolve_domain_planes(options.get("domain"))
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

    _mark_solver_anchor(walk["candidates"], anchor)
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
    # The same bodies, as handles in the frame the STEP is written in.
    measured_bodies = [
        walk["measured"][record["object_id"]] for record, _body in included_pairs
    ]
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
    domain = plan_domain(domain_planes, measured_bodies)
    observed_at = _utc_timestamp()
    instance_records = [
        _instance_record(
            design,
            record,
            observed_at,
            selected_occurrence=walk["selected_occurrence"],
        )
        for record in records
    ]
    sources = _sources(
        records,
        included_bodies,
        _retained_fractions(
            design,
            records,
            domain_planes,
            selected_occurrence=walk["selected_occurrence"],
        ),
    )
    return_state_snapshot = return_state(app, options)
    return_state_hash = return_state_snapshot.get("hash")
    if not return_state_hash:
        raise wglink_core.WgLinkError(
            f"Could not fingerprint the Fusion return state: {return_state_snapshot.get('reason', 'unknown error')}."
        )
    document_name, native_id = _document(app, design)
    output = Path(output_value).expanduser()
    try:
        output.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(
            f"Could not create return output folder {output}: {exc}."
        ) from exc
    _cleanup_stale_publish_artifacts(output)
    request_id = _nullable(options.get("request_id"))
    suffix = f"-{_safe_document_name(str(request_id))}" if request_id else ""
    target = output / f"{_safe_document_name(document_name)}{suffix}.wgreturn"
    overwrite = bool(options.get("overwrite", False))
    # Capturing the document costs seconds and tens of megabytes per return, so
    # the caller decides. Default on: a run whose model cannot be reopened is
    # the gap the archive exists to close.
    capture_document = bool(options.get("capture_document", True))
    document_capture_error: str | None = None
    target, reservation = _reserve_target(target, overwrite=overwrite)
    temp: Path | None = None
    try:
        if target.exists() and not target.is_dir():
            raise wglink_core.WgLinkError(
                f"Return target exists but is not a bundle folder: {target}."
            )
        try:
            temp = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=output))
        except Exception as exc:  # noqa: BLE001
            raise wglink_core.WgLinkError(
                f"Could not create a temporary return bundle beside {target}: {exc}."
            ) from exc
        assembly_path = temp / "assembly.step"
        _export_step(design, assembly_path, walk["geometry"])
        observed_count = count_step_bodies(assembly_path)
        expected_count = len(included_bodies)
        if observed_count != expected_count:
            raise wglink_core.WgLinkError(
                f"STEP body count gate refused the export: inventory expects {expected_count}, but assembly.step contains {observed_count}."
            )
        files = {"assembly.step": _file_record(assembly_path, "exterior-assembly")}
        if capture_document:
            document_path = temp / CAD_DOCUMENT_MEMBER
            document_capture_error = _export_fusion_archive(design, document_path)
            if document_capture_error is None:
                files[CAD_DOCUMENT_MEMBER] = _file_record(
                    document_path, "cad-document", CAD_DOCUMENT_MEDIA_TYPE
                )
            else:
                document_path.unlink(missing_ok=True)
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
            # Which component's own frame assembly.step is written in. Fusion
            # exports a Component in its own coordinates and offers no way to
            # export one in its assembly placement, so the file's frame is a
            # fact about the export scope. Stating it keeps every other
            # coordinate in this manifest -- the bounding box, each
            # assembly_from_link -- readable without inferring anything.
            "export_frame": walk["export_frame"],
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
            document={
                "name": document_name,
                "native_id": native_id,
                **({"request_id": request_id} if request_id else {}),
            },
            coordinate_system=coordinate,
            assembly={
                "file": "assembly.step",
                "n_bodies_expected": expected_count,
                "bbox_mm": _bbox(measured_bodies),
                "signature_hash": return_state_hash,
                **({"domain": domain} if domain is not None else {}),
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
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        raise wglink_core.WgLinkError(str(exc)) from exc
    except wglink_core.WgLinkError:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - one head-less filesystem boundary
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        raise wglink_core.WgLinkError(
            f"Could not publish return bundle {target}: {exc}."
        ) from exc
    finally:
        reservation.unlink(missing_ok=True)

    return {
        "return_id": manifest["return"]["id"],
        "bundle_path": str(target),
        "domain": domain,
        "document_captured": capture_document and document_capture_error is None,
        "document_capture_error": document_capture_error,
        "scope": manifest["scope"],
        "instances": manifest["instances"],
        "sources": manifest["sources"],
        "files": manifest["files"],
        "manifest": manifest,
    }
