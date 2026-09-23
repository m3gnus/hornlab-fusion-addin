"""Observe a Fusion assembly and publish an immutable WG return bundle.

The policy module owns classification and manifest validation.  This module is
the deliberately thin Fusion boundary: it reads live entities, exports STEP,
checks that Fusion wrote the promised inventory, and publishes without ever
editing the open design.
"""

from __future__ import annotations

from collections.abc import Callable
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
    from . import wglink_activity, wglink_author, wglink_core
    from .wglink_return import (
        BASE_RETURN_FEATURES,
        DOCUMENT_UP_FEATURE,
        DOMAIN_AUTOMATIC_FEATURE,
        DOMAIN_KIND_FOR_PLANES,
        MANAGE_DROPDOWN_NAME,
        SCOPE_REASON_UNCLASSIFIED_VISIBLE_SURFACE,
        WgReturnError,
        build_return_manifest,
        canonical_domain_planes,
        classify_cut_provenance,
        dumps_return_manifest,
        plan_export_scope,
    )
else:
    import wglink_activity
    import wglink_author
    import wglink_core
    from wglink_return import (
        BASE_RETURN_FEATURES,
        DOCUMENT_UP_FEATURE,
        DOMAIN_AUTOMATIC_FEATURE,
        DOMAIN_KIND_FOR_PLANES,
        MANAGE_DROPDOWN_NAME,
        SCOPE_REASON_UNCLASSIFIED_VISIBLE_SURFACE,
        WgReturnError,
        build_return_manifest,
        canonical_domain_planes,
        classify_cut_provenance,
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
# One top-level Part 21 body entity, in all three spellings a CAD kernel writes.
# BREP_WITH_VOIDS is the ISO 10303-42 subtype of MANIFOLD_SOLID_BREP used for a
# solid with an enclosed internal void, and Open CASCADE's writer emits it as a
# plain entity rather than as a complex instance -- so a hollow body was counted
# as zero bodies here, and every export of one failed the count gate as a body
# that is not there.
_STEP_BODY = re.compile(
    r"\b(?:MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS|SHELL_BASED_SURFACE_MODEL)\s*\(",
    re.I,
)


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


@wglink_activity.counted(wglink_activity.MUTATION_BODY_DECLARATION)
def declare_body(body: object, declaration: str) -> None:
    """Set or replace the explicit return classification on one body."""

    value = str(declaration).strip().lower()
    if value not in DECLARATIONS:
        choices = ", ".join(sorted(DECLARATIONS))
        raise wglink_core.WgLinkError(f"body declaration must be one of: {choices}")
    wglink_core._set_attribute(body, DECLARATION_ATTRIBUTE, value)


@wglink_activity.counted(wglink_activity.MUTATION_BODY_DECLARATION)
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


def _source_face_roles(body: object) -> tuple[str, ...]:
    """The distinct painted source roles a body carries, in the order found."""

    roles: list[str] = []
    for face in wglink_core._items(getattr(body, "faces", None)):
        role = _face_role(face)
        if role is not None and role not in roles:
            roles.append(role)
    return tuple(roles)


def _is_managed_helper(candidate: dict[str, Any]) -> bool:
    """Is this a WGLink-managed body that is not the final exterior body?

    Every entity ``_stamp_managed`` touches carries the instance id, including
    the cut tool, the throat patch and the stitched shell that ``_close_and_thicken``
    leaves behind.  ``plan_export_scope`` skips those by role, so they must not
    also claim to hold the anchor, or a required source that an exported body
    already carries -- a dependency flag on a skipped body is a terminal refusal.
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


def _exports_its_faces(candidate: dict[str, Any]) -> bool:
    """Do this candidate's painted faces reach the STEP file?

    Only the rules that can keep a body out of the export are asked -- helper
    role, suppression, visibility and an ``exclude`` declaration -- because a
    body WG never receives cannot stand in for one that is dropped.
    """

    return (
        candidate.get("kind") == "body"
        and candidate.get("body_kind") in {"solid", "surface"}
        and not _is_managed_helper(candidate)
        and not candidate.get("suppressed")
        and candidate.get("visible") is not False
        and candidate.get("declaration") != "exclude"
    )


def _display_body_name(record: dict[str, Any]) -> str:
    """Name a body the way ``wglink_return._display_name`` names it.

    Deliberately the same order of keys: a body that both refusals can talk
    about has to be called the same thing by both, or the user reads two
    messages about what looks like two bodies.
    """

    for key in ("path", "name", "object_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return "unnamed body"


def _fusion_would_export(candidate: dict[str, Any]) -> bool:
    """Would Fusion write this body into ``assembly.step``?

    This is Fusion's own predicate and nothing else: visible, unsuppressed,
    B-rep. ``ExportManager.createSTEPExportOptions`` takes a filename and a
    Component, so there is no per-body scoping to consult -- no role, no
    declaration, no inventory. It deliberately does NOT reuse
    :func:`_exports_its_faces`, which asks WGLink's policy questions as well;
    the whole point of this one is to be the other side of the comparison.

    LIMITATION, and it is not closable from inside this module. Autodesk
    documents that hiding a body GROUP does not exclude its members: "such
    objects while invisible will be exported as if they were visible."
    ``_scope_walk`` reads ``isVisible`` first (``isLightBulbOn`` as fallback),
    and ``isVisible`` reads False for exactly such a body. So this predicate is
    right for an individually-hidden body and wrong for a group-hidden one --
    it under-predicts the file rather than over-predicting it. Treat it as a
    named check that catches what it can, not as an exhaustive one; the count
    gate after the export is still what catches the rest, and the refusal text
    says so rather than promising a completeness this cannot deliver.
    """

    return (
        candidate.get("kind") == "body"
        and candidate.get("body_kind") in {"solid", "surface"}
        and not candidate.get("suppressed")
        and candidate.get("visible") is not False
    )


def _refuse_inventory_disagreement(
    candidates: list[dict[str, Any]], included: list[dict[str, Any]]
) -> None:
    """Compare the two filters by name before the export, not by count after.

    Two independent filters run over the same document -- ``plan_export_scope``
    decides the inventory and Fusion decides the file -- and until they were
    compared the only symptom of a disagreement was the count gate's
    arithmetic, which names nothing a user can act on. Bodies on each side are
    named here instead.
    """

    predicted = {
        str(candidate.get("object_id")): _display_body_name(candidate)
        for candidate in candidates
        if _fusion_would_export(candidate)
    }
    inventoried = {
        str(record.get("object_id")): _display_body_name(record)
        for record in included
    }
    if predicted.keys() == inventoried.keys():
        return
    parts = []
    unlisted = sorted(
        predicted[key] for key in predicted.keys() - inventoried.keys()
    )
    unexported = sorted(
        inventoried[key] for key in inventoried.keys() - predicted.keys()
    )
    if unlisted:
        parts.append(
            "Fusion will export "
            + ", ".join(repr(name) for name in unlisted)
            + ", which the return inventory does not list"
        )
    if unexported:
        parts.append(
            "the return inventory lists "
            + ", ".join(repr(name) for name in unexported)
            + ", which Fusion will not export"
        )
    raise wglink_core.WgLinkError(
        "The return inventory and Fusion's STEP export disagree about this "
        "document: "
        + "; ".join(parts)
        + ". Fusion writes every visible, unsuppressed B-rep body of the "
        "exported component and offers no other way to narrow a STEP export, "
        "so hide the body itself in the browser -- hiding a folder that "
        "contains it will not work, because Autodesk exports a group-hidden "
        "body as if it were visible, and that case is one this check cannot "
        "see."
    )


def _fem_bodies_fusion_would_export(
    component: object, path: str, occurrence: object | None, *, left_out: bool
) -> list[str]:
    """Name the B-rep bodies under a FEM air component that Fusion still writes.

    ``_scope_walk`` stops at a FEM component, so none of its bodies becomes an
    exterior candidate and :func:`_refuse_inventory_disagreement` never sees
    them. The exported Component is written whole all the same, and a FEM
    component inside it adds every visible, unsuppressed B-rep body to
    ``assembly.step``: the air solid, and anything in occurrences below it.
    The predicate is :func:`_fusion_would_export`'s, and so is its
    limitation. A body hidden only through a hidden body GROUP reads as
    hidden here, yet Fusion exports it, so the count gate after the export
    stays the backstop for that case. Visibility is read the way ``add_body``
    reads it, from the body and from its occurrence. ``left_out`` is True
    when a suppressed or hidden occurrence above (or at) this one already
    keeps the whole subtree out of the file. An unresolved external reference
    has no bodies to read, so it is passed over here.
    """

    if left_out or _bool(occurrence, ("isSuppressed",), False):
        return []
    if not _bool(occurrence, ("isVisible", "isLightBulbOn"), True):
        return []
    owner = occurrence if _collection(occurrence, "bRepBodies") else component
    names: list[str] = []
    for body in _collection(owner, "bRepBodies"):
        if _bool(body, ("isSuppressed",), False):
            continue
        if not _bool(body, ("isVisible", "isLightBulbOn"), True):
            continue
        names.append(f"{path}/{getattr(body, 'name', '') or 'unnamed body'}")
    children = (
        _collection(occurrence, "childOccurrences") if occurrence is not None else []
    )
    if not children:
        children = _collection(component, "occurrences")
    for child in children:
        child_component = getattr(child, "component", None)
        if child_component is None or _external_reference(child) == "unresolved":
            continue
        names.extend(
            _fem_bodies_fusion_would_export(
                child_component, _occurrence_path(child), child, left_out=False
            )
        )
    return names


def _refuse_fem_bodies_in_export(fem_exported: list[dict[str, Any]]) -> None:
    """Refuse by name a FEM air body that the exterior STEP would carry.

    Counting such a body into the exterior inventory is not an alternative.
    WG's ingest requires ``scope.included`` to hold exactly
    ``assembly.n_bodies_expected`` bodies and imports ``assembly.step`` as the
    exterior geometry, so the air volume would be meshed as a radiating
    surface. Fusion's only per-body export control is visibility, and the
    separate FEM member is exported from the FEM component's own body, which a
    hidden body would empty as well. So the remedy offered is to hide the
    occurrence. It is not measured here whether Fusion still writes the FEM
    component's body when only its occurrence is hidden. If it does not, the
    one-solid gate on the FEM member refuses the empty file by name.

    When the FEM component is itself the export scope, hiding it is no remedy,
    so that case asks for a scope that contains it instead.
    """

    if not fem_exported:
        return
    bodies = sorted({item["body"] for item in fem_exported})
    listed = ("body " if len(bodies) == 1 else "bodies ") + ", ".join(
        repr(name) for name in bodies
    )
    itself = sorted(
        {item["occurrence"] for item in fem_exported if item["is_export_scope"]}
    )
    if itself:
        raise wglink_core.WgLinkError(
            f"The export scope {itself[0]!r} is itself a FEM air volume, so "
            f"assembly.step would carry its {listed} as exterior geometry. "
            "Send the assembly that contains the FEM air volume instead: leave "
            "Assembly scope empty, or select an occurrence that contains it."
        )
    occurrences = sorted({item["occurrence"] for item in fem_exported})
    one = len(occurrences) == 1
    noun = "occurrence" if one else "occurrences"
    raise wglink_core.WgLinkError(
        "Fusion writes every visible body of the exported component into "
        f"assembly.step, so the {listed} under a FEM air volume would reach "
        f"WG as exterior geometry. Hide the FEM {noun} "
        + ", ".join(repr(name) for name in occurrences)
        + f" in the browser before Send. Hide the {noun} rather than the "
        f"bodies inside {'it' if one else 'them'}: Fusion exports visible "
        "bodies only, so hidden bodies would leave the separate FEM file empty "
        "as well. If Fusion leaves that file empty anyway, Send refuses it by "
        "name."
    )


def _mark_uncovered_helper_sources(candidates: list[dict[str, Any]]) -> None:
    """Let a helper claim only the source roles its own insertion loses.

    ``_close_and_thicken`` stitches the loft and the throat patch into one
    surface body before thickening it, so on a throat-opened model the leftover
    shell carries the very painted face the final solid carries. That duplicate
    is no evidence of a loss: skipping it costs WG nothing WG is not already
    being handed. Cover only counts from an exported body of the *same*
    instance -- another insertion's HF sits somewhere else entirely -- and a
    role nothing exported carries still flags, so the refusal that stops WG
    losing a source silently stays. Coverage spans the whole walk, so this
    cannot be decided while one candidate is being built.
    """

    covered: dict[str, set[str]] = {}
    for candidate in candidates:
        instance_id = candidate.get("wglink_instance_id")
        if instance_id and _exports_its_faces(candidate):
            covered.setdefault(instance_id, set()).update(
                candidate.get("source_face_roles", ())
            )
    for candidate in candidates:
        if _is_managed_helper(candidate):
            kept = covered.get(candidate.get("wglink_instance_id"), ())
            candidate["contains_required_source"] = any(
                role not in kept for role in candidate.get("source_face_roles", ())
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


@wglink_activity.counted(wglink_activity.SOURCE_INVENTORY)
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
    fem_exported_bodies: list[dict[str, Any]] = []
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
            "source_face_roles": _source_face_roles(body),
            "contains_required_source": False,
            "contains_solver_anchor": False,
            "only_enclosing_exterior": False,
            "requested_fem_air_volume": False,
        }
        # A helper's claim is settled by _mark_uncovered_helper_sources once the
        # walk knows what else is exported.
        candidate["contains_required_source"] = (
            bool(instance_id) and not _is_managed_helper(candidate)
        ) or bool(candidate["source_face_roles"])
        candidates.append(candidate)
        bodies[object_id] = body
        measured[object_id] = native if placed else body

    def walk_component(
        component: object,
        path: str,
        occurrence: object | None,
        suppressed: bool,
        hidden: bool = False,
        *,
        export_scope: bool = False,
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
            # The walk stops here, but the exported component does not: every
            # visible body under this one still lands in assembly.step.
            fem_exported_bodies.extend(
                {
                    "occurrence": path,
                    "body": name,
                    # Passed down by the top-level call, not ``component is
                    # geometry``: Fusion can mint a fresh wrapper per read,
                    # so identity says nothing about which Component it is.
                    "is_export_scope": export_scope,
                }
                for name in _fem_bodies_fusion_would_export(
                    component, path, occurrence, left_out=suppressed or hidden
                )
            )
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
            # Only the FEM check reads this; ``add_body`` keeps its own reading.
            child_hidden = hidden or not _bool(
                child, ("isVisible", "isLightBulbOn"), True
            )
            walk_component(
                child_component, child_path, child, child_suppressed, child_hidden
            )

    if selection == "root":
        walk_component(
            design.rootComponent,
            _component_name(design.rootComponent),
            None,
            False,
            export_scope=True,
        )
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
                export_scope=True,
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
        and item.get("visible") is not False
        and item.get("declaration") != "exclude"
        and not _is_managed_helper(item)
    ]
    declared_roles = any(
        item.get("body_kind") in {"solid", "surface"}
        and item.get("declaration") is not None
        for item in candidates
    )
    if len(exterior) == 1 and not declared_roles:
        exterior[0]["only_enclosing_exterior"] = True
    _mark_uncovered_helper_sources(candidates)
    return {
        "selection": selection,
        "geometry": geometry,
        "selected_occurrence": None if selection == "root" else selected_entity,
        "export_frame": export_frame,
        "candidates": candidates,
        "bodies": bodies,
        "measured": measured,
        "fem_components": fem_components,
        "fem_exported_bodies": fem_exported_bodies,
        "components": components,
    }


def _record_body(record: dict[str, Any]) -> object | None:
    bodies = [
        entity
        for entity in record.get("entities", [])
        if _role(entity) in EXTERIOR_ROLES
    ]
    if len(bodies) > 1:
        labels = ", ".join(wglink_core._entity_label(entity) for entity in bodies)
        raise wglink_core.WgLinkError(
            f"WGLink instance {record['instance_id']!r} belongs to several "
            f"managed bodies: {labels}. Send cannot choose between them. "
            "Detach removes WGLink metadata and no geometry, and it does run "
            "on this state: pass options['entity_token'] or options['entity'] "
            "naming the copy you are giving up to unmanage only that one, "
            "options['preview'] to see what that removes first, or no target "
            "to unmanage every copy."
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
        "scope_reason_codes": [],
        "has_unclassified_visible_surface_refusal": False,
        "manage_dropdown_name": MANAGE_DROPDOWN_NAME,
        "source_error": None,
        "domain": None,
        "domain_error": None,
        "cut_provenance": [],
        "bounds_mm": None,
        "source_bounds_mm": None,
    }
    _mark_solver_anchor(walk["candidates"], anchor)
    try:
        scope = plan_export_scope(walk["selection"], walk["candidates"]).manifest_scope()
    except WgReturnError as exc:
        report["scope_error"] = str(exc)
        report["scope_reason_codes"] = list(dict.fromkeys(
            str(reason["reason_code"])
            for reason in exc.reasons
            if reason.get("reason_code")
        ))
        report["has_unclassified_visible_surface_refusal"] = (
            SCOPE_REASON_UNCLASSIFIED_VISIBLE_SURFACE
            in report["scope_reason_codes"]
        )
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
        # The preview predicts the export, so it resolves the export-frame
        # placements the same way and only once -- the second derivation below
        # reuses these fractions instead of asking again outside this guard.
        fractions = _retained_fractions(
            design,
            records,
            resolve_domain_planes(opts.get("domain")),
            selected_occurrence=walk["selected_occurrence"],
            transforms=_assembly_transforms(
                design, records, selected_occurrence=walk["selected_occurrence"]
            ),
        )
        sources = _sources(
            records,
            included_bodies,
            fractions,
            source_identity=bool(opts.get("source_identity")),
            design=design,
        )
    except wglink_core.WgLinkError as exc:
        report["source_error"] = str(exc)
        sources = []
        fractions = {}
    for source in sources:
        observed = source.get("observed", {})
        report["sources"].append({
            "role": str(source.get("role") or "?"),
            "area_mm2": float(observed.get("total_area_mm2") or 0.0),
            "face_count": int(observed.get("face_count") or 0),
            "instance_id": source.get("instance_id"),
        })
    if bool(opts.get("automatic_domain")):
        report["domain"] = {"kind": "automatic"}
        report["cut_provenance"] = read_cut_provenance(
            design, included_pairs, walk["export_frame"], walk["geometry"]
        )
    elif bool(opts.get("display_automatic_domain")):
        # An older WG still performs its established automatic cut from an
        # absent domain. The preview describes that user-facing behaviour but
        # records no new field and reads no timeline evidence for that WG.
        report["domain"] = {"kind": "automatic"}
    else:
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
    for face in _source_faces(records, included_bodies, fractions):
        source_bounds = _merge_bounds(source_bounds, face)
    report["source_bounds_mm"] = source_bounds
    return report


@wglink_activity.counted(wglink_activity.DOCUMENT_SIGNATURE)
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
    # The fingerprint has to be about the frame the export writes, so the
    # placements are resolved here, once, with the same selected occurrence the
    # manifest uses -- not a second time in the root-relative frame.
    try:
        transforms = _assembly_transforms(
            design, records, selected_occurrence=walk["selected_occurrence"]
        )
    except wglink_core.WgLinkError as exc:
        return {"hash": None, "reason": str(exc)}
    try:
        sources = _sources(
            records,
            [body for _item, body in included_pairs],
            _retained_fractions(
                design,
                records,
                resolve_domain_planes(opts.get("domain")),
                selected_occurrence=walk["selected_occurrence"],
                transforms=transforms,
            ),
            source_identity=bool(opts.get("source_identity")),
            design=design,
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
            "assembly_from_link": transforms[str(record["instance_id"])][0],
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
    automatic_domain = bool(opts.get("automatic_domain"))
    cut_state = (
        read_cut_provenance(
            design, included_pairs, walk["export_frame"], walk["geometry"]
        )
        if automatic_domain
        else []
    )
    document_up = fusion_document_up(app) if bool(opts.get("document_up")) else None
    state = {
        "selection": scope["selection"],
        "bodies": bodies,
        "fem_air_volumes": scope["fem_air_volumes"],
        "instances": instances,
        "sources": source_state,
        **({"domain": {"kind": "automatic"}, "cut_provenance": cut_state} if automatic_domain else {}),
        **({"document_up": document_up} if document_up is not None else {}),
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


def _same_entity(left: object, right: object) -> bool:
    """True when two handles denote the same Fusion entity.

    Fusion hands out equivalent but *distinct* Python wrappers for one entity:
    the occurrence a token lookup returns need not be the object the UI
    selection produced, even though both name the same placement. Comparing
    them with ``is`` therefore makes a refusal depend on whether a given Fusion
    build happened to reuse the wrapper, which is not something this add-in can
    observe. ``entityToken`` is Fusion's own published identity for exactly
    this comparison, so it is what is compared.

    An entity with no readable token falls back to object identity rather than
    to an empty-string match -- two different unreadable entities must never
    look equal, because equality here is what grants the identity transform.
    """

    if left is None or right is None:
        return False
    if left is right:
        return True
    left_token = wglink_core._entity_token(left)
    if not left_token:
        return False
    return left_token == wglink_core._entity_token(right)


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
        if len(occurrences) == 1 and _same_entity(occurrences[0], selected_occurrence):
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
        # Refused, never defaulted: an identity placement would solve the horn
        # at the wrong position. What makes the component unfindable is not
        # established (FIELD-FINDINGS F3; nesting was ruled out -- it has its
        # own refusal below), so the message names no cause, only what the
        # user can check and do.
        payload = record.get("payload") or {}
        label = str(payload.get("link_name") or payload.get("design_name") or "").strip()
        named = f"the WG waveguide {label!r}" if label else "a WG waveguide"
        raise wglink_core.WgLinkError(
            f"WGLink cannot find the component that holds {named} in this "
            "assembly, so it cannot tell where the waveguide sits. It will not "
            "guess a position, because a guessed position would be solved as if "
            "it were real.\n\n"
            "Check that the waveguide's component is still in the design. If it "
            "was deleted or replaced, undo that and send again. If it is there "
            "and this keeps happening, do not insert a second copy; report it, "
            "with this message, so the cause can be found.\n\n"
            f"(WGLink link {instance_id}: no wrapper occurrence could be resolved.)"
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


def _assembly_transforms(
    design: object,
    records: list[dict[str, Any]],
    *,
    selected_occurrence: object | None = None,
) -> dict[str, tuple[list[list[float]], str | None]]:
    """Resolve every in-scope link's export-frame placement ONCE.

    The manifest's ``assembly_from_link``, the declared-domain disc reduction
    and the return-state fingerprint published beside them all have to describe
    the frame the STEP was actually written in. They used to call the strict
    resolver separately, and ``return_state`` called it *without* the selected
    occurrence: an occurrence-scope send then recorded the identity in the
    manifest while its own signature hash carried the root-relative placement,
    and a nested wrapper the manifest accepted was refused as nested by the
    fingerprint -- so "select that instance's own occurrence" was not the
    complete recovery the guide advertises.

    Resolving here and handing the answer down removes the opportunity to
    disagree: one selection argument, one resolution, one frame.
    """

    resolved: dict[str, tuple[list[list[float]], str | None]] = {}
    for record in records:
        instance_id = str(record["instance_id"])
        if instance_id in resolved:
            continue
        resolved[instance_id] = _strict_assembly_from_link(
            design, record, selected_occurrence=selected_occurrence
        )
    return resolved


def _resolved_transform(
    design: object,
    record: dict[str, Any],
    *,
    selected_occurrence: object | None,
    transforms: dict[str, tuple[list[list[float]], str | None]] | None,
) -> tuple[list[list[float]], str | None]:
    """One record's placement, taken from the shared resolution when there is one."""

    if transforms is not None:
        resolved = transforms.get(str(record["instance_id"]))
        if resolved is not None:
            return resolved
    return _strict_assembly_from_link(
        design, record, selected_occurrence=selected_occurrence
    )


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


# The datums a source contract reads: Fusion's object type for each, and the
# component collection a legacy name lookup searches.
_CONTRACT_DATUM_KINDS = {
    "WG_THROAT_PLANE": ("ConstructionPlane", "constructionPlanes"),
    "WG_AXIS": ("ConstructionAxis", "constructionAxes"),
}


_DATUM_KINDS = frozenset(kind for kind, _collection in _CONTRACT_DATUM_KINDS.values())


def _same_live_entity(left: object, right: object) -> bool:
    """Whether two Fusion handles wrap one entity.

    Fusion mints a Python wrapper per lookup, and Autodesk documents that the
    token strings two reads report for one entity may differ -- compare the
    entities, it says, not the strings. Fusion's own wrapper equality does
    that. ``_same_entity``'s token comparison is only the fallback for a handle
    that is not a Fusion object.
    """

    if left is None or right is None:
        return False
    if left is right:
        return True
    base = getattr(adsk.core, "Base", None)
    if isinstance(base, type) and isinstance(left, base) and isinstance(right, base):
        try:
            return bool(left == right)
        except Exception:  # noqa: BLE001 - fall back to the entity token
            pass
    return _same_entity(left, right)


def _distinct_entities(entities: list[object]) -> list[object]:
    """Drop repeated handles on one Fusion entity, keeping the first of each."""

    unique: list[object] = []
    for entity in entities:
        if not any(_same_live_entity(entity, seen) for seen in unique):
            unique.append(entity)
    return unique


def _named_all(collection: object, name: str) -> list[object]:
    """Every item of a Fusion collection whose browser name is ``name``."""

    matches: list[object] = []
    for item in wglink_core._items(collection):
        try:
            if str(item.name) == name:
                matches.append(item)
        except Exception:  # noqa: BLE001 - an unreadable name is not a match
            continue
    if not matches:
        found = _named(collection, name)
        if found is not None:
            matches.append(found)
    return _distinct_entities(matches)


def _link_components(record: dict[str, Any]) -> list[object]:
    """Every component one raw link record lives in."""

    found = [
        entity
        for entity in record.get("wrappers", [])
        if wglink_core._kind(entity) == "Component" or hasattr(entity, "bRepBodies")
    ]
    for entity in record.get("entities", []):
        parent = wglink_core._parent_component(entity)
        if parent is None and wglink_core._kind(entity) in _DATUM_KINDS:
            # A construction plane or axis names its owner ``component``.
            try:
                parent = entity.component
            except Exception:  # noqa: BLE001
                parent = None
        if parent is not None:
            found.append(parent)
    return _distinct_entities(found)


def _shares_component(design: object, record: dict[str, Any], component: object) -> bool:
    """Whether another WG link lives in ``component`` beside this one."""

    instance_id = str(record["instance_id"])
    for other_id, other in wglink_core._link_records(design).items():
        if other_id == instance_id:
            continue
        if any(_same_live_entity(component, owner) for owner in _link_components(other)):
            return True
    return False


def _datum_refusal(instance_id: str, name: str, reason: str) -> wglink_core.WgLinkError:
    return wglink_core.WgLinkError(
        f"WGLink instance {instance_id!r}: {reason}, so Send cannot tell which "
        f"datum is this link's {name} and will not guess. Delete the extra copy, "
        "or re-insert the link from WG, and send again."
    )


def _contract_datum(
    design: object, record: dict[str, Any], component: object | None, name: str
) -> object | None:
    """This link's own ``name`` datum, found by ownership rather than by name.

    Insert stamps each datum it builds with its owner (``instance_id``) and its
    role (``datum``), and records the datum's entity token in the link's
    ``entity_tokens`` under ``datum:<name>``. Those records are the identity.
    The browser name is presentation a user may change, and a Part Design
    document keeps every root-fallback link's datums in one component under
    the same names, so a name lookup there answers for whichever link it meets
    first.

    The stored token is resolved with ``findEntityByToken`` and the entity it
    names is compared with the stamped one -- never the token strings, which
    Fusion documents may differ over time for one entity. Where the two records
    disagree, or two datums claim one role, Send refuses by name rather than
    choose. A datum Insert recorded that is now gone gives no contract, never a
    namesake. Only a link with neither record is read by name, and only while
    it is the only link in its component.
    """

    kind, collection_name = _CONTRACT_DATUM_KINDS[name]
    instance_id = str(record["instance_id"])
    stamped = _distinct_entities([
        entity
        for entity in record.get("entities", [])
        if wglink_core._kind(entity) == kind
        and wglink_core._attribute_value(entity, "datum") == name
        and wglink_core._attribute_value(entity, "instance_id") == instance_id
    ])
    token = wglink_core._token_table(record.get("payload", {})).get(f"datum:{name}", "")
    recorded = _distinct_entities([
        entity
        for entity in wglink_core._find_by_token(design, token)
        if wglink_core._kind(entity) == kind
    ])
    for entity in recorded:
        owner = wglink_core._attribute_value(entity, "instance_id")
        role = wglink_core._attribute_value(entity, "datum")
        if owner not in (None, "", instance_id) or role not in (None, "", name):
            raise wglink_core.WgLinkError(
                f"WGLink instance {instance_id!r} recorded its {name} as a datum "
                f"WGLink stamped as {role or 'a datum'} of instance {owner or instance_id!r}. "
                "The two ownership records disagree, so Send will not guess which "
                "datum is this link's. Re-insert the link from WG and send again."
            )
    if stamped and recorded:
        agreed = [
            entity
            for entity in stamped
            if any(_same_live_entity(entity, match) for match in recorded)
        ]
        if len(agreed) == 1:
            return agreed[0]
        if not agreed:
            raise _datum_refusal(
                instance_id,
                name,
                f"the datum stamped as its {name} is not the one Insert recorded for it",
            )
        raise _datum_refusal(
            instance_id, name, f"{len(agreed)} datums answer to its recorded {name}"
        )
    if stamped:
        if len(stamped) == 1:
            return stamped[0]
        raise _datum_refusal(
            instance_id, name, f"{len(stamped)} datums are stamped as its {name}"
        )
    if recorded:
        if len(recorded) == 1:
            return recorded[0]
        raise _datum_refusal(
            instance_id, name, f"its recorded {name} names {len(recorded)} datums"
        )
    if token:
        # Insert recorded this datum and it is gone. A datum of the same name
        # is somebody else's, or the user's.
        return None
    named = (
        _named_all(getattr(component, collection_name, None), name)
        if component is not None
        else []
    )
    if not named:
        return None
    if _shares_component(design, record, component):
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} has no record of which {name} is its "
            "own, and its component holds another WG link whose datums share that "
            "name, so Send will not guess between them. Re-insert this link from "
            "WG, which records its datums, or give each link its own component."
        )
    if len(named) > 1:
        raise _datum_refusal(
            instance_id, name, f"{len(named)} datums in its component are named {name}"
        )
    return named[0]


# WG's rigid-placement tolerance: ``rigid_inverse`` (server/mesh/imported.py)
# refuses a mirrored or non-rigid solver anchor by it. Send applies the same rule
# to every instance, before anything is written, so no placement it labels
# ``original`` is one WG would refuse.
_RIGID_TOLERANCE = 1.0e-6


def _chirality(matrix: list[list[float]], instance_id: str) -> str:
    """The manifest's ``chirality`` for one placement, measured, or a refusal.

    ``original`` is the only value the contract has, and it is true only of a
    proper rotation. Send used to write it for every instance, which labelled a
    mirrored wrapper as the one thing it is not; the determinant says which it
    is.
    """

    rotation = [[float(matrix[row][column]) for column in range(3)] for row in range(3)]
    (a, b, c), (d, e, f), (g, h, i) = rotation
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if determinant < 0.0:
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} is placed mirrored (its placement's "
            f"determinant is {determinant:.6g}). The return contract's only "
            "chirality, 'original', is true of an unmirrored placement alone, so "
            "Send refuses it rather than mislabel it. "
            "Place an unmirrored copy instead: insert it from WG and move or "
            "rotate it into position."
        )
    orthonormal = all(
        abs(
            sum(rotation[k][p] * rotation[k][q] for k in range(3))
            - (1.0 if p == q else 0.0)
        )
        <= _RIGID_TOLERANCE
        for p in range(3)
        for q in range(3)
    )
    if not orthonormal or abs(determinant - 1.0) > _RIGID_TOLERANCE:
        raise wglink_core.WgLinkError(
            f"WGLink instance {instance_id!r} is not a rigid placement: its rotation "
            f"is not orthonormal within {_RIGID_TOLERANCE:g} (determinant "
            f"{determinant:.9g}). The return contract places a link by a rotation "
            "plus a translation only, so Send refuses it. Remove any scale or "
            "skew from the wrapper's "
            "placement and send again."
        )
    return "original"


def _source_contract(design: object, record: dict[str, Any]) -> dict[str, Any] | None:
    payload = record.get("payload", {})
    component = record.get("wrapper_component") or getattr(record.get("body"), "parentComponent", None)
    plane = _contract_datum(design, record, component, "WG_THROAT_PLANE")
    axis = _contract_datum(design, record, component, "WG_AXIS")
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
    transforms: dict[str, tuple[list[list[float]], str | None]] | None = None,
) -> dict[str, Any]:
    payload = record.get("payload", {})
    matrix, occurrence_path = _resolved_transform(
        design,
        record,
        selected_occurrence=selected_occurrence,
        transforms=transforms,
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
        "chirality": _chirality(matrix, str(record["instance_id"])),
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


# ------------------------------------------------------------ source identity
#
# ``source-identity-v1`` (WG ``docs/reference/MULTI-INSTANCE-CAD-IDENTITY.md``,
# "Cross-export source identity"): when WG advertises it, every ``sources[].id``
# is the CAD-authored identity of that logical source, the same in every later
# export. It is resolved to faces here, and a missing, split, copied, removed or
# ambiguous mapping is refused -- WG cannot tell from an opaque string which
# face was meant, so neither side may guess. No Fusion entity token and no face
# stamp ever enters the manifest.
#
# Two kinds of source, two kinds of identity:
#
# * A linked throat source is one-to-one with its link, whose ``instance_id``
#   this add-in minted at Insert. Its identity is derived from that id, so it
#   needs no document write and cannot be missing; ``_throat_faces`` already
#   refuses a throat face that is removed or split.
# * A painted source is every unclaimed face painted one role (WG selects those
#   faces by appearance name, so one role is one source). Set WG Source... stamps
#   each native face with ``source_identity``: the source id, the role it was
#   painted, a nonce for that face, and how many faces the source was marked on.
#   Fusion copies an attribute onto both halves of a split face and onto a pasted
#   or patterned copy, which is what makes a split or copy visible here.

SOURCE_IDENTITY_FEATURE = "source-identity-v1"
SOURCE_IDENTITY_ATTRIBUTE = "source_identity"
SOURCE_IDENTITY_SCHEMA = 1
SOURCE_IDENTITY_PREFIX = "wgs-"
# 20 Crockford base32 characters carry 100 bits; with the prefix an id is 24
# ASCII bytes, inside WG's 25-byte ceiling.
SOURCE_IDENTITY_CHARACTERS = 20
# WG's bounds (``server/cadlink/wgreturn.py``): the id itself, and the whole gmsh
# physical name ingestion writes for the source with the worst-case tag.
SOURCE_IDENTITY_MAX_BYTES = 25
GMSH_PHYSICAL_NAME_MAX_BYTES = 128
WORST_CASE_SOURCE_TAG = 9999
_THROAT_IDENTITY_NAMESPACE = "wglink-throat-source-v1|"


def _base32_identity(value: int) -> str:
    characters = []
    for _index in range(SOURCE_IDENTITY_CHARACTERS):
        characters.append(_CROCKFORD[value & 31])
        value >>= 5
    return SOURCE_IDENTITY_PREFIX + "".join(reversed(characters))


def _mint_source_identity() -> str:
    return _base32_identity(secrets.randbits(5 * SOURCE_IDENTITY_CHARACTERS))


def _throat_source_identity(instance_id: str) -> str:
    digest = hashlib.sha256(
        (_THROAT_IDENTITY_NAMESPACE + str(instance_id)).encode("utf-8")
    ).digest()
    return _base32_identity(
        int.from_bytes(digest, "big") >> (256 - 5 * SOURCE_IDENTITY_CHARACTERS)
    )


def source_physical_name(tag: int, source_id: str, instance_id: object, role: str) -> str:
    """The mesh physical name WG's ingestion gives a source (WG ``_physical_name``)."""

    instance = "null" if instance_id is None else str(instance_id)
    return (
        f"wg-import-v1|tag={tag}|source_id={source_id}|"
        f"instance_id={instance}|role={role}"
    )


def _check_source_identities(sources: list[dict[str, Any]]) -> None:
    """Refuse, before anything is written, what WG would refuse on reading."""

    seen: set[str] = set()
    for source in sources:
        source_id = source.get("id")
        role = str(source.get("role") or "")
        if not isinstance(source_id, str) or not source_id:
            raise wglink_core.WgLinkError(f"The {role} source has no source identity.")
        if source_id != source_id.strip():
            raise wglink_core.WgLinkError(
                f"The {role} source identity {source_id!r} must be trimmed."
            )
        if len(source_id.encode("utf-8")) > SOURCE_IDENTITY_MAX_BYTES:
            raise wglink_core.WgLinkError(
                f"The {role} source identity {source_id!r} is longer than "
                f"{SOURCE_IDENTITY_MAX_BYTES} UTF-8 bytes."
            )
        if source_id in seen:
            raise wglink_core.WgLinkError(
                f"Two sources claim the source identity {source_id!r}; identities must "
                "be unique within a return, so WGLink will not choose between them. "
                "A copied link or face carries its original's identity: remove the "
                "copy, or insert a fresh link from WG."
            )
        seen.add(source_id)
        name = source_physical_name(
            WORST_CASE_SOURCE_TAG, source_id, source.get("instance_id"), role
        )
        size = len(name.encode("utf-8"))
        if size > GMSH_PHYSICAL_NAME_MAX_BYTES:
            raise wglink_core.WgLinkError(
                f"The {role} source {source_id!r} would need a {size}-byte mesh name; "
                f"WG keeps at most {GMSH_PHYSICAL_NAME_MAX_BYTES} bytes."
            )


def _native_face(face: object) -> object:
    # An occurrence proxy carries no attributes of its own; the stamp lives on
    # the native face, which is also what two placements of one component share.
    return getattr(face, "nativeObject", None) or face


def _parse_source_stamp(value: object) -> dict[str, Any] | None:
    try:
        stamp = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    if not isinstance(stamp, dict) or stamp.get("schema") != SOURCE_IDENTITY_SCHEMA:
        return None
    faces = stamp.get("faces")
    if (
        not all(isinstance(stamp.get(key), str) and stamp.get(key) for key in ("id", "role", "face"))
        or isinstance(faces, bool)
        or not isinstance(faces, int)
        or faces < 1
    ):
        return None
    return stamp


def read_source_stamp(face: object) -> dict[str, Any] | None:
    """The identity stamp a face carries, or None when it carries none that is readable."""

    value = wglink_core._attribute_value(_native_face(face), SOURCE_IDENTITY_ATTRIBUTE)
    return None if value is None else _parse_source_stamp(value)


def _canonical_source_role(value: object) -> str | None:
    """A painted or stamped role with the retired ``PORT_EXIT`` spelling resolved."""

    literal = wglink_author._canonical_role(value)
    if literal is None:
        return None
    return wglink_author.LEGACY_SOURCE_ROLE_ALIASES.get(literal, literal)


def _painted_role(face: object) -> str | None:
    return _canonical_source_role(_face_role(face))


def _run_again(canonical: str) -> str:
    return (
        f"Select every face that should drive {canonical} and run Set WG Source… "
        f"{canonical} again. Because this source no longer resolves, that gives it a "
        "new source identity, and WG asks for its setup again."
    )


def _unresolved_paint(role: str, canonical: str, missing: int, total: int) -> str:
    """The refusal for a role group WGLink cannot resolve to one source.

    Held in one place so the ambiguous case and the pre-stamp case cannot drift
    apart: the ambiguous one must keep saying exactly this.
    """

    return (
        f"{missing} of {total} face(s) painted {role} carry no WG source "
        f"identity for {canonical} (painted by hand, repainted, or marked before "
        f"source identities existed). Select them and run Set WG Source… "
        f"{canonical}; a face added to a source that still resolves keeps that "
        "source's identity."
    )


def _painted_source_identity(
    role: str,
    faces: list[object],
    design: object | None = None,
    *,
    adopt: Callable[[str, int], bool] | None = None,
) -> str:
    """Resolve one painted role group to its single authored identity, or refuse.

    ``faces`` are the faces this export sees painted ``role``. ``design``, when
    given, lets a refusal tell a face that is gone from one that is only outside
    what is being sent -- the remedies differ.

    ``adopt`` is the caller's way of asking the user one question, and only the
    export supplies one. Without it nothing here writes to the document, which
    is what keeps the preview and the fingerprint read-only by construction
    rather than by convention.
    """

    canonical = _canonical_source_role(role) or role
    natives = _distinct_entities([_native_face(face) for face in faces])
    stamps = [(native, read_source_stamp(native)) for native in natives]
    missing = [
        native
        for native, stamp in stamps
        if stamp is None or _canonical_source_role(stamp["role"]) != canonical
    ]
    if missing:
        # One condition covered two different situations, and only one of them
        # is ambiguous.
        #
        # When *some* face of the group already carries an identity, nothing
        # here can tell whether the unidentified faces belong to that source or
        # were painted separately; binding them would silently solve the wrong
        # thing. That refusal is unchanged, and no question is put, because no
        # answer to it would be safe.
        #
        # When *no* face carries the attribute at all, the group is paint that
        # predates source identities. There is no competing identity to
        # mis-bind to and the paint is the only evidence and is unanimous, so
        # the user is asked once and the group is adopted. "Carries the
        # attribute" is deliberately stricter than "parses": a value that will
        # not parse is a corruption, and it establishes nothing about whether
        # the face is already claimed.
        refusal = _unresolved_paint(role, canonical, len(missing), len(natives))
        pre_stamp = bool(natives) and all(
            wglink_core._attribute(native, SOURCE_IDENTITY_ATTRIBUTE) is None
            for native in natives
        )
        if pre_stamp and adopt is not None and design is not None:
            if adopt(canonical, len(natives)):
                return _adopt_painted_source(design, natives, canonical)
            # Declined: nothing was written, and the refusal stands as before.
        elif pre_stamp:
            refusal = (
                f"{refusal} These faces predate WG source identities, so Send "
                f"offers to adopt them as this document's {canonical} source."
            )
        raise wglink_core.WgLinkError(refusal)
    identities = sorted({stamp["id"] for _native, stamp in stamps})
    if len(identities) != 1:
        raise wglink_core.WgLinkError(
            f"The faces painted {role} carry {len(identities)} different WG source "
            f"identities, so which source they are is ambiguous. {_run_again(canonical)}"
        )
    by_nonce: dict[str, list[object]] = {}
    for native, stamp in stamps:
        by_nonce.setdefault(stamp["face"], []).append(native)
    copied = max(len(group) for group in by_nonce.values())
    if copied > 1:
        raise wglink_core.WgLinkError(
            f"A face of the {canonical} source was split or copied: {copied} faces carry "
            f"one face's identity. {_run_again(canonical)}"
        )
    counts = {stamp["faces"] for _native, stamp in stamps}
    if counts == {len(natives)}:
        return identities[0]
    expected = max(counts)
    if design is not None:
        painted_members = [
            member
            for member, _attribute, _stamp in _identity_members(design, identities[0])
            if _painted_role(member) == canonical
        ]
        outside = [
            member
            for member in _distinct_entities(painted_members)
            if not any(_same_live_entity(member, native) for native in natives)
        ]
        if outside:
            raise wglink_core.WgLinkError(
                f"{len(outside)} face(s) of the {canonical} source are outside what is "
                "being sent (in a hidden or excluded body, or another component), so "
                "this export would carry only part of that source. Include them in the "
                f"export, or select them and Clear their WG source."
            )
    raise wglink_core.WgLinkError(
        f"The {canonical} source was marked on {expected} face(s), but only "
        f"{len(natives)} still carry it: a face was removed or repainted. "
        f"{_run_again(canonical)}"
    )


def _identity_members(design: object, identity: str) -> list[tuple[object, object, dict[str, Any]]]:
    """Every (face, attribute, stamp) in the design carrying ``identity``."""

    members = []
    for attribute, stamp in _stamp_attributes(design):
        if stamp["id"] == identity:
            members.append((attribute.parent, attribute, stamp))
    return members


def _stamp_attributes(design: object) -> list[tuple[object, dict[str, Any]]]:
    try:
        found = wglink_core._items(
            design.findAttributes(wglink_core.ATTRIBUTE_GROUP, SOURCE_IDENTITY_ATTRIBUTE)
        )
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(
            f"Could not read the WG source identities in this design: {exc}"
        ) from exc
    result = []
    for attribute in found:
        try:
            if str(attribute.name) != SOURCE_IDENTITY_ATTRIBUTE or attribute.parent is None:
                continue
            stamp = _parse_source_stamp(attribute.value)
        except Exception:  # noqa: BLE001 - an unreadable stamp is no stamp
            continue
        if stamp is not None:
            result.append((attribute, stamp))
    return result


class _StampEdit:
    """Every stamp write of one command, undone together if any of them fails.

    A face in an externally referenced (read-only) component refuses the write;
    without this, the faces written before it would keep a half-applied source.
    """

    def __init__(self) -> None:
        self._originals: list[tuple[object, str | None]] = []

    def _remember(self, face: object) -> None:
        if any(face is seen for seen, _value in self._originals):
            return
        self._originals.append(
            (face, wglink_core._attribute_value(face, SOURCE_IDENTITY_ATTRIBUTE))
        )

    def write(self, face: object, stamp: dict[str, Any]) -> None:
        self._remember(face)
        wglink_core._set_attribute(face, SOURCE_IDENTITY_ATTRIBUTE, wglink_core._json(stamp))

    def delete(self, face: object) -> None:
        attribute = wglink_core._attribute(face, SOURCE_IDENTITY_ATTRIBUTE)
        if attribute is None:
            return
        self._remember(face)
        if attribute.deleteMe() is False:
            raise wglink_core.WgLinkError("Fusion refused to remove a WG source identity.")

    def rollback(self) -> list[str]:
        problems: list[str] = []
        for face, value in reversed(self._originals):
            try:
                if value is None:
                    attribute = wglink_core._attribute(face, SOURCE_IDENTITY_ATTRIBUTE)
                    if attribute is not None and attribute.deleteMe() is False:
                        problems.append("Fusion refused to remove a stamp this command wrote")
                else:
                    wglink_core._set_attribute(face, SOURCE_IDENTITY_ATTRIBUTE, value)
            except Exception as exc:  # noqa: BLE001 - report every face we could not restore
                problems.append(str(exc))
        return problems


def _run_edit(action: str, body) -> Any:
    edit = _StampEdit()
    try:
        return body(edit)
    except Exception as exc:  # noqa: BLE001 - roll back, then refuse with the cause
        problems = edit.rollback()
        detail = str(exc)
        if problems:
            detail += (
                f"; {len(problems)} face(s) could not be restored, so undo this "
                "command in Fusion"
            )
        raise wglink_core.WgLinkError(
            f"Could not {action}: {detail}. Every source identity stamp was restored."
            if not problems
            else f"Could not {action}: {detail}."
        ) from exc


def _is_managed_throat(face: object) -> bool:
    return wglink_core._attribute_value(face, "face_role") is not None


def _detach_from_identity(edit: _StampEdit, design: object, face: object) -> None:
    """Take ``face`` out of the source it was stamped into.

    The others keep their identity and their count drops by one. A decrement,
    never a recount: a source that had already lost a face must still read as
    short afterwards, not be made to add up by this edit.
    """

    stamp = read_source_stamp(face)
    edit.delete(face)
    if stamp is None:
        return
    for member, _attribute, member_stamp in _identity_members(design, stamp["id"]):
        if _same_live_entity(member, face):
            continue
        edit.write(member, dict(member_stamp, faces=max(1, member_stamp["faces"] - 1)))


def _valid_group(
    members: list[tuple[object, object, dict[str, Any]]],
    canonical: str,
    selected: list[object],
) -> bool:
    """Whether a role's existing stamps still form exactly the source they record.

    Every member must still be painted that role -- a face whose paint was
    removed or changed by hand is a stale member, which makes the source one
    that no longer resolves. The faces just painted by this command count as
    painted whatever their proxy/native appearance reads back.
    """

    natives = _distinct_entities([member for member, _attribute, _stamp in members])
    if not natives or len(natives) != len(members):
        return False
    for member in natives:
        just_painted = any(_same_live_entity(member, native) for native in selected)
        if not just_painted and _painted_role(member) != canonical:
            return False
    counts = {stamp["faces"] for _member, _attribute, stamp in members}
    nonces = [stamp["face"] for _member, _attribute, stamp in members]
    return counts == {len(natives)} and len(set(nonces)) == len(nonces)


@wglink_activity.counted(wglink_activity.MUTATION_SOURCE_IDENTITY)
def assign_source_identity(design: object, faces: list[object], role: str) -> dict[str, Any]:
    """Stamp the selected faces, just painted ``role``, with that source's identity.

    Adding faces to a source that still resolves keeps its identity, so WG keeps
    the setup it recorded for it. When the design's existing ``role`` stamps do
    not resolve -- a member's paint removed or changed, a face removed, split or
    copied, or two identities -- this is the reassignment WGLink asked for: a new
    identity is minted for the selected faces and the stale stamps are removed
    from every other face, so WG does not carry a setup across a remapping nobody
    confirmed. All writes succeed together or none remain.

    A managed throat face is left alone; its identity comes from its link.
    """

    canonical = _canonical_source_role(role) or role
    selected = [
        native
        for native in _distinct_entities([_native_face(face) for face in faces])
        if not _is_managed_throat(native)
    ]
    if not selected:
        return {"identity": None, "stamped": 0, "kept": False}

    def body(edit: _StampEdit) -> dict[str, Any]:
        for native in selected:
            stamp = read_source_stamp(native)
            if stamp is not None and _canonical_source_role(stamp["role"]) != canonical:
                _detach_from_identity(edit, design, native)

        groups: dict[str, list[tuple[object, object, dict[str, Any]]]] = {}
        for attribute, stamp in _stamp_attributes(design):
            if _canonical_source_role(stamp["role"]) == canonical:
                groups.setdefault(stamp["id"], []).append((attribute.parent, attribute, stamp))
        if len(groups) == 1:
            identity, members = next(iter(groups.items()))
            if _valid_group(members, canonical, selected):
                joining = [
                    native
                    for native in selected
                    if not any(_same_live_entity(native, member) for member, _a, _s in members)
                ]
                total = len(members) + len(joining)
                for member, _attribute, stamp in members:
                    if stamp["faces"] != total:
                        edit.write(member, dict(stamp, faces=total))
                for native in joining:
                    edit.write(native, _new_stamp(identity, canonical, total))
                return {"identity": identity, "stamped": len(joining), "kept": True}

        identity = _mint_source_identity()
        for group in groups.values():
            for member, _attribute, _stamp in group:
                if not any(_same_live_entity(member, native) for native in selected):
                    edit.delete(member)
        for native in selected:
            edit.write(native, _new_stamp(identity, canonical, len(selected)))
        return {"identity": identity, "stamped": len(selected), "kept": False}

    return _run_edit(f"stamp the {canonical} source identity", body)


def _new_stamp(identity: str, role: str, faces: int) -> dict[str, Any]:
    return {
        "schema": SOURCE_IDENTITY_SCHEMA,
        "id": identity,
        "role": role,
        "face": uuid.uuid4().hex,
        "faces": faces,
    }


def _adopt_painted_source(design: object, natives: list[object], canonical: str) -> str:
    """Give paint that predates source identities one identity, once, together.

    Minted and stamped exactly as ``assign_source_identity`` mints and stamps --
    one identity, one nonce per face, and the same transaction -- so there is
    one identity scheme and one stamp shape, not two. A face in an externally
    referenced (read-only) component refuses the write, and ``_StampEdit`` puts
    back every face written before it, so a document is never left with half a
    source.

    ``faces`` records the number adopted, which is what this group *is*: a later
    export that sees fewer faces then reads as short, exactly as it does for a
    source marked by hand. Adoption binds the faces this export carries and goes
    looking for no others; paint of the same role outside the export is not
    adopted, and a wider export later says so.

    The caller has already established that no face here carries the attribute,
    and has already asked.
    """

    def body(edit: _StampEdit) -> str:
        identity = _mint_source_identity()
        for native in natives:
            edit.write(native, _new_stamp(identity, canonical, len(natives)))
        return identity

    return _run_edit(f"adopt the {canonical} source identity", body)


@wglink_activity.counted(wglink_activity.MUTATION_SOURCE_IDENTITY)
def clear_source_identity(design: object, faces: list[object]) -> int:
    """Remove the source identity from every selected face, painted or not.

    A face whose paint was already removed by hand still carries its stamp;
    Clear is how that stale member is taken out of its source.
    """

    targets = [
        native
        for native in _distinct_entities([_native_face(face) for face in faces])
        if wglink_core._attribute(native, SOURCE_IDENTITY_ATTRIBUTE) is not None
    ]
    if not targets:
        return 0

    def body(edit: _StampEdit) -> int:
        for native in targets:
            _detach_from_identity(edit, design, native)
        return len(targets)

    return _run_edit("clear the WG source identity", body)


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
    transforms: dict[str, tuple[list[list[float]], str | None]] | None = None,
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
        matrix, _path = _resolved_transform(
            design,
            record,
            selected_occurrence=selected_occurrence,
            transforms=transforms,
        )
        fractions[instance_id] = _declared_disc_reduction(
            instance_id, _source_contract(design, record), matrix, planes
        )
    return fractions


def _sources(
    records: list[dict[str, Any]],
    included_bodies: list[object],
    retained_fractions: dict[str, float] | None = None,
    *,
    source_identity: bool = False,
    design: object | None = None,
    adopt: Callable[[str, int], bool] | None = None,
) -> list[dict[str, Any]]:
    """Every drivable source the return carries.

    ``source_identity`` is on only when WG advertises ``source-identity-v1``;
    off, every id is the legacy role-derived one, byte for byte.

    ``adopt`` is passed only by the export. The preview and the fingerprint call
    this to read, so they leave it out and cannot write a stamp.
    """
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
        if source_identity:
            source_id = _throat_source_identity(str(record["instance_id"]))
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
        if source_identity:
            source_id = _painted_source_identity(role, faces, design, adopt=adopt)
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
    if source_identity:
        _check_source_identities(sources)
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
_ORIGIN_PLANE_ATTRIBUTES = {
    "YZ": "yZConstructionPlane",
    "XZ": "xZConstructionPlane",
    "XY": "xYConstructionPlane",
}
_AXIS_FOR_ORIGIN_PLANE = {"YZ": 0, "XZ": 1, "XY": 2}


def _same_fusion_entity(left: object, right: object, design: object) -> bool:
    """Best-effort identity check across Fusion's fresh Python wrappers."""

    left = getattr(left, "nativeObject", None) or left
    right = getattr(right, "nativeObject", None) or right
    # Autodesk documents that one entity's token string may change. Resolve
    # each token and compare the entities returned; never compare the strings.
    resolved: list[list[object]] = []
    for candidate in (left, right):
        aliases = [candidate]
        token = wglink_core._entity_token(candidate)
        if token:
            aliases.extend(wglink_core._find_by_token(design, token))
        resolved.append(aliases)
    for candidate in resolved[0]:
        for target in resolved[1]:
            try:
                if candidate is target or candidate == target:
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _origin_plane_name(entity: object, component: object, design: object) -> str | None:
    for name, attribute in _ORIGIN_PLANE_ATTRIBUTES.items():
        try:
            origin = getattr(component, attribute)
        except Exception:  # noqa: BLE001
            continue
        if _same_fusion_entity(entity, origin, design):
            return name
    # Exact browser names are a fallback for old wrappers that expose neither
    # stable equality nor an entity token for origin construction geometry.
    shown = str(getattr(entity, "name", "") or "").strip().upper()
    return next((name for name in _ORIGIN_PLANE_ATTRIBUTES if shown == f"{name} PLANE"), None)


def _parameter_value(parameter: object) -> float | None:
    try:
        value = parameter.value
    except Exception:  # noqa: BLE001
        value = parameter
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _cut_tool_descriptor(
    entity: object, component: object, design: object
) -> tuple[str, str, bool] | None:
    """Describe an origin plane or a zero-offset construction plane."""

    direct = _origin_plane_name(entity, component, design)
    if direct is not None:
        return "origin-plane", direct, True
    definition = getattr(entity, "definition", None)
    if definition is None:
        return None
    base = getattr(definition, "planarEntity", None)
    origin = _origin_plane_name(base, component, design) if base is not None else None
    if origin is None:
        return None
    kind = str(getattr(definition, "objectType", "") or "")
    offset = _parameter_value(getattr(definition, "offset", None))
    coincident = offset is not None and abs(offset) <= 1.0e-9
    if "ByPlaneDefinition" in kind:
        coincident = True
    return "construction-plane", origin, coincident


def _extrude_profile_plane(
    feature: object, component: object, design: object
) -> tuple[str, str, bool] | None:
    profiles = wglink_core._items(getattr(feature, "profile", None))
    if not profiles and getattr(feature, "profile", None) is not None:
        profiles = [feature.profile]
    found: list[tuple[str, str, bool]] = []
    for profile in profiles:
        sketch = getattr(profile, "parentSketch", None)
        reference = getattr(sketch, "referencePlane", None)
        descriptor = (
            _cut_tool_descriptor(reference, component, design)
            if reference is not None
            else None
        )
        if descriptor is not None:
            found.append(descriptor)
    return found[0] if found and all(item == found[0] for item in found) else None


def _feature_kind(feature: object) -> str | None:
    object_type = str(getattr(feature, "objectType", "") or "")
    if object_type.endswith("::SplitBodyFeature") or object_type.endswith("SplitBodyFeature"):
        return "split-body"
    if object_type.endswith("::ExtrudeFeature") or object_type.endswith("ExtrudeFeature"):
        operation = getattr(feature, "operation", None)
        cut_value = getattr(getattr(adsk.fusion, "FeatureOperations", None), "CutFeatureOperation", 1)
        if operation == cut_value or "CutFeatureOperation" in str(operation):
            return "extrude-cut"
    return None


def _kept_side(body: object, origin_plane: str) -> str | None:
    values = wglink_core._bbox_values(body)
    axis = _AXIS_FOR_ORIGIN_PLANE[origin_plane]
    minimum, maximum = float(values[axis]), float(values[axis + 3])
    tolerance = max(DOMAIN_TOLERANCE_FLOOR_MM, DOMAIN_TOLERANCE_REL * math.dist(values[:3], values[3:]))
    if minimum >= -tolerance and maximum > tolerance:
        return "positive"
    if maximum <= tolerance and minimum < -tolerance:
        return "negative"
    return None


def _fusion_cut_descriptors(
    design: object,
    included_pairs: list[tuple[dict[str, Any], object]],
    export_component: object,
) -> list[dict[str, Any]]:
    """Thin Fusion adapter: inspect the active timeline into plain records."""

    timeline = getattr(design, "timeline", None)
    if timeline is None:
        return []
    try:
        marker = int(timeline.markerPosition)
    except Exception:  # noqa: BLE001
        marker = len(wglink_core._items(timeline))
    sides: dict[tuple[str, str], str] = {}
    bodies_by_id: dict[str, object] = {}
    for record, body in included_pairs:
        object_id = str(record["object_id"])
        bodies_by_id[object_id] = body
        for plane in _ORIGIN_PLANE_ATTRIBUTES:
            side = _kept_side(body, plane)
            if side is not None:
                sides[(object_id, plane)] = side

    snapshots: list[tuple[object, dict[str, Any], set[str]]] = []
    for fallback_index, item in enumerate(wglink_core._items(timeline)):
        try:
            feature = item.entity
        except Exception:  # noqa: BLE001 - one unreadable entry is no evidence
            continue
        kind = _feature_kind(feature) if feature is not None else None
        if kind is None:
            continue
        try:
            index = int(getattr(item, "index"))
        except Exception:  # noqa: BLE001
            index = fallback_index
        affected: set[str] = set()
        for candidate in wglink_core._items(getattr(feature, "bodies", None)):
            affected.update(
                object_id
                for object_id, body in bodies_by_id.items()
                if _same_fusion_entity(candidate, body, design)
            )
        snapshots.append((feature, {
            "timeline_index": index,
            "marker_position": marker,
            "suppressed": bool(getattr(feature, "isSuppressed", False)),
            "feature_kind": kind,
            "feature_name": str(getattr(feature, "name", "") or ""),
        }, affected))

    descriptors: list[dict[str, Any]] = []
    try:
        for feature, descriptor, affected in snapshots:
            if descriptor["suppressed"] or descriptor["timeline_index"] >= marker:
                continue
            timeline_object = getattr(feature, "timelineObject", None)
            try:
                if timeline_object is not None:
                    timeline_object.rollTo(True)
            except Exception:  # noqa: BLE001
                pass
            component = getattr(feature, "parentComponent", None)
            if component is None:
                continue
            if descriptor["feature_kind"] == "split-body":
                tool = _cut_tool_descriptor(
                    getattr(feature, "splittingTool", None), component, design
                )
                participants = getattr(feature, "splitBodies", None)
            else:
                tool = _extrude_profile_plane(feature, component, design)
                participants = getattr(feature, "participantBodies", None)
            for candidate in wglink_core._items(participants):
                affected.update(
                    object_id
                    for object_id, body in bodies_by_id.items()
                    if _same_fusion_entity(candidate, body, design)
                )
            # Contract planes are in the STEP export frame. A feature in a
            # child component has that meaning only while the occurrence is
            # untransformed; otherwise its local x0/y0/z0 is a different plane.
            if not _same_fusion_entity(component, export_component, design):
                affected = {
                    object_id
                    for object_id in affected
                    if _is_identity_placement(
                        _occurrence_placement(
                            getattr(bodies_by_id[object_id], "assemblyContext", None)
                        )
                    )
                }
            if tool is None:
                continue
            tool_kind, origin_plane, coincident = tool
            kept_sides = {
                object_id: sides[(object_id, origin_plane)]
                for object_id in affected
                if (object_id, origin_plane) in sides
            }
            descriptors.append({
                **descriptor,
                "tool_kind": tool_kind,
                "origin_plane": origin_plane,
                "coincident": coincident,
                "body_object_ids": sorted(affected),
                "kept_sides": kept_sides,
            })
    finally:
        try:
            timeline.markerPosition = marker
            restored = int(timeline.markerPosition)
        except Exception as exc:  # noqa: BLE001
            raise wglink_core.WgLinkError(
                "Fusion could not restore the design timeline after reading cut provenance; "
                "the export was cancelled. Restore the timeline marker, then try again."
            ) from exc
        if restored != marker:
            raise wglink_core.WgLinkError(
                "Fusion did not restore the design timeline after reading cut provenance; "
                "the export was cancelled. Restore the timeline marker, then try again."
            )
    return descriptors


def read_cut_provenance(
    design: object,
    included_pairs: list[tuple[dict[str, Any], object]],
    export_frame: str,
    export_component: object,
) -> list[dict[str, Any]]:
    descriptors = _fusion_cut_descriptors(design, included_pairs, export_component)
    return classify_cut_provenance(
        descriptors,
        {str(record["object_id"]) for record, _body in included_pairs},
        export_frame,
    )


def fusion_document_up(app: object) -> str:
    """Read Fusion's general modelling-orientation preference at export."""

    try:
        orientation = app.preferences.generalPreferences.defaultModelingOrientation
    except Exception as exc:  # noqa: BLE001
        raise wglink_core.WgLinkError(
            f"Could not read Fusion's modelling orientation: {exc}."
        ) from exc
    orientations = getattr(adsk.core, "DefaultModelingOrientations", None)
    y_up = getattr(orientations, "YUpModelingOrientation", 0)
    z_up = getattr(orientations, "ZUpModelingOrientation", 1)
    if orientation == y_up and not isinstance(orientation, bool):
        return "+y"
    if orientation == z_up and not isinstance(orientation, bool):
        return "+z"
    raise wglink_core.WgLinkError(
        f"Fusion reported an unsupported modelling orientation: {orientation!r}."
    )


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


#: Windows answers an exclusive create on a name another process holds open, or
#: has just deleted (delete-pending), with PermissionError -- not
#: FileExistsError. Held is not taken: the name is tried again a few times over
#: about a tenth of a second before WGLink moves on to the next one.
_RESERVE_HELD_ATTEMPTS = 5
_RESERVE_HELD_PAUSE_SECONDS = 0.025
#: How many names in a row may be held before WGLink stops and says so. A
#: folder that refuses every name is a permission problem, not a busy name.
_RESERVE_HELD_NAMES = 8


def _create_reservation(reservation: Path) -> int | None:
    """The exclusive create; None when Windows keeps the name held past the retries.

    ``FileExistsError`` propagates: the name is taken. On POSIX a
    ``PermissionError`` is a real permission problem with the folder, so it
    becomes a visible refusal at once and is never retried.
    """

    for attempt in range(_RESERVE_HELD_ATTEMPTS):
        if attempt:
            time.sleep(_RESERVE_HELD_PAUSE_SECONDS)
        try:
            return os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except PermissionError as exc:
            if os.name != "nt":
                raise wglink_core.WgLinkError(
                    f"WGLink cannot create files in the return folder {reservation.parent}: "
                    f"{exc}. Check that the folder is writable, then send again."
                ) from exc
    return None


def _reserve_target(target: Path, *, overwrite: bool) -> tuple[Path, Path]:
    """Atomically reserve an immutable bundle name across Fusion processes."""

    stem = target.name.removesuffix(".wgreturn")
    index = 1
    held = 0
    while True:
        candidate = target if index == 1 else target.with_name(f"{stem}-{index}.wgreturn")
        reservation = _reservation_path(candidate)
        try:
            descriptor = _create_reservation(reservation)
        except FileExistsError:
            descriptor = None
            held = 0
        else:
            if descriptor is None:
                held += 1
                if held >= _RESERVE_HELD_NAMES:
                    raise wglink_core.WgLinkError(
                        f"WGLink could not reserve a return name in {target.parent}: "
                        f"{held} names in a row were held by another program. Check "
                        "that the folder is writable and not locked, then send again."
                    )
        if descriptor is None:
            if overwrite:
                raise wglink_core.WgLinkError(
                    f"Another WGLink export is already publishing {candidate.name}."
                )
            index += 1
            continue
        held = 0
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


@wglink_activity.counted(wglink_activity.EXPORT_RETURN)
def send(
    app: object,
    options: dict[str, Any],
    *,
    confirm_adoption: Callable[[str, int], bool] | None = None,
) -> dict[str, Any]:
    """Write one return bundle and return a JSON-serialisable export report.

    ``confirm_adoption(role, faces)`` is how the add-in's UI asks the one
    question this export may put to the user: whether a role group painted
    before WG source identities existed should be adopted as this document's
    source for that role. It is a callback rather than a dialog raised here
    because this module stays head-less; a caller that supplies none -- a
    script, a test, a shell without Fusion's modal API -- gets the refusal it
    always got.
    """

    if not isinstance(options, dict):
        raise wglink_core.WgLinkError("options must be an object")
    output_value = options.get("output_folder")
    if not isinstance(output_value, (str, os.PathLike)) or not str(output_value).strip():
        raise wglink_core.WgLinkError("options['output_folder'] must name the return output folder.")
    automatic_domain = bool(options.get("automatic_domain"))
    domain_planes = () if automatic_domain else resolve_domain_planes(options.get("domain"))
    # Declared only when WG advertises it (WGLink.py reads the capability file).
    source_identity = bool(options.get("source_identity"))
    design = wglink_core._design(app)
    walk = _scope_walk(design, options.get("selection"))
    # First, and before anything is written: a FEM air body inside the exported
    # component would land in assembly.step, and no inventory rule can account
    # for it there.
    _refuse_fem_bodies_in_export(walk["fem_exported_bodies"])
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

    # Before anything is written: the inventory and the file must be about the
    # same set of bodies. Run it here, ahead of the temp bundle and the export,
    # so a disagreement costs nothing and is reported by name.
    _refuse_inventory_disagreement(walk["candidates"], scope["included"])

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
    domain = {"kind": "automatic"} if automatic_domain else plan_domain(
        domain_planes, measured_bodies
    )
    cut_provenance = (
        read_cut_provenance(
            design, included_pairs, walk["export_frame"], walk["geometry"]
        )
        if automatic_domain
        else []
    )
    observed_at = _utc_timestamp()
    # One resolution of the export-frame placements, shared by the manifest's
    # instance records and by the declared-domain reduction beside them.
    transforms = _assembly_transforms(
        design, records, selected_occurrence=walk["selected_occurrence"]
    )
    instance_records = [
        _instance_record(
            design,
            record,
            observed_at,
            selected_occurrence=walk["selected_occurrence"],
            transforms=transforms,
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
            transforms=transforms,
        ),
        source_identity=source_identity,
        design=design,
        adopt=confirm_adoption,
    )
    # After ``_sources``, deliberately: an adoption above has already written
    # its stamps, so the fingerprint is taken of the document as it now is
    # rather than of one that could not be fingerprinted at all.
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
        document_up = fusion_document_up(app) if bool(options.get("document_up")) else None
        if document_up is not None:
            coordinate["document_up"] = document_up
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
                **({"cut_provenance": cut_provenance} if cut_provenance else {}),
            },
            files=files,
            scope=scope,
            instances=instance_records,
            sources=sources,
            required_features=[
                *BASE_RETURN_FEATURES,
                *([SOURCE_IDENTITY_FEATURE] if source_identity else []),
                *([DOMAIN_AUTOMATIC_FEATURE] if automatic_domain else []),
                *([DOCUMENT_UP_FEATURE] if document_up is not None else []),
            ],
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
