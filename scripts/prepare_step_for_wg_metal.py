#!/usr/bin/env python3
"""Prepare a named-source STEP surface model for Waveguide Generator Metal BEM.

The expected CAD pattern is:

* one acoustic boundary made from stitched/sewn surfaces,
* named source patches exported as STEP shell/surface model names,
* quarter-domain models aligned to WG Metal's quadrant convention when
  ``--quadrants`` is not ``1234``.

The script writes:

* ``tagged_sources.msh`` in the STEP units, carrying all named sources,
* one WG-compatible metre-unit ``<source>_source_tag2_m.msh`` per source,
* ``manifest.json`` with topology and source mapping diagnostics.

It intentionally refuses to report solver-ready output when the mesh has free
edges away from the declared symmetry planes. Use ``--allow-leaks`` only for
debugging bad exports.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass
import json
import re
import sys
from pathlib import Path
from typing import Iterable

import gmsh
import meshio
import numpy as np

# Shared pure-Python sizing/cost predictor, also imported by the Fusion add-in.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wg_mesh_sizing as sizing  # noqa: E402


SOURCE_TAG_BASE = 2
RIGID_TAG = 1
SPEED_OF_SOUND_M_S = 343.0
FREQUENCY_ELEMENTS_PER_WAVELENGTH = 6.0
DEFAULT_TOPOLOGY_TOL = 1e-5
GENERIC_PORT_EXIT_SOURCE = "PORT_EXIT"
LEGACY_PORT_EXIT_SOURCES = frozenset({"PORT_EXIT_L", "PORT_EXIT_R"})

# Acoustic-role element-per-wavelength defaults (see wg_mesh_sizing). The
# radiating flare/source is the main accuracy/size lever; shadowed rear/outer
# surfaces ride near Nyquist; the near-field baffle is graded by distance.
DEFAULT_RADIATING_EPW = sizing.DEFAULT_RADIATING_EPW
DEFAULT_SHADOW_EPW = sizing.DEFAULT_SHADOW_EPW
DEFAULT_THROAT_EPW = sizing.DEFAULT_THROAT_EPW
WELD_TOLERANCE_MM = 5.0e-3  # 5 micrometres; closes near-duplicate OCC patch nodes
DEGENERATE_MIN_QUALITY = 1.0e-4  # drops needle slivers that make dense solves singular


@dataclass(frozen=True)
class SourceSpec:
    name: str
    resolution_mm: float
    tag: int


def _parse_source_spec(raw: str, index: int) -> SourceSpec:
    parts = [part.strip() for part in raw.split(":")]
    if len(parts) not in (2, 3) or not parts[0]:
        raise argparse.ArgumentTypeError(
            "--source expects NAME:RES_MM or NAME:RES_MM:TAG"
        )
    try:
        resolution = float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid source resolution: {raw!r}") from exc
    if resolution <= 0.0:
        raise argparse.ArgumentTypeError(f"source resolution must be positive: {raw!r}")
    tag = SOURCE_TAG_BASE + index if len(parts) == 2 else int(parts[2])
    if tag <= RIGID_TAG:
        raise argparse.ArgumentTypeError("source physical tags must be > 1")
    return SourceSpec(name=parts[0], resolution_mm=resolution, tag=tag)


# Role keyword -> the acoustic role its refine group adopts.
_REFINE_ROLE_KEYWORDS = {
    "radiating": sizing.ROLE_RADIATING,
    "flare": sizing.ROLE_RADIATING,
    "mouth": sizing.ROLE_RADIATING,
    "throat": sizing.ROLE_THROAT,
    "shadow": sizing.ROLE_SHADOW,
    "rear": sizing.ROLE_SHADOW,
    "near": sizing.ROLE_NEAR_FIELD,
    "nearfield": sizing.ROLE_NEAR_FIELD,
    "baffle": sizing.ROLE_NEAR_FIELD,
}


@dataclass(frozen=True)
class RefineSpec:
    """Per-face mesh-size override painted via a Fusion appearance/shell name.

    A refine group stays physically rigid (tag 1); it only restricts the local
    mesh size. The size is either an explicit ``RES_MM`` ceiling or an
    elements-per-wavelength target resolved against the band top at mesh time.
    """

    name: str
    epw: float | None
    size_mm: float | None
    role: str

    def size_for_band(self, f_max_hz: float | None, *, fallback_mm: float) -> float:
        if self.size_mm is not None:
            return float(self.size_mm)
        ceiling = sizing.frequency_ceiling_mm(self.epw or DEFAULT_RADIATING_EPW, f_max_hz)
        return float(ceiling) if ceiling is not None else float(fallback_mm)


def _parse_refine_spec(
    raw: str,
    *,
    radiating_epw: float,
    shadow_epw: float,
    throat_epw: float,
) -> RefineSpec:
    """Parse ``--refine NAME:VALUE``.

    ``VALUE`` is one of: a role keyword (``radiating``/``shadow``/``throat``/
    ``near``) which adopts that role's elements-per-wavelength; ``<num>mm`` for
    an explicit size ceiling in millimetres; or a bare ``<num>`` read as
    elements-per-wavelength (the primary size lever).
    """
    parts = [part.strip() for part in raw.split(":")]
    if len(parts) != 2 or not parts[0]:
        raise argparse.ArgumentTypeError("--refine expects NAME:EPW, NAME:RES_MMmm, or NAME:ROLE")
    name, value = parts[0], parts[1].lower()
    if value in _REFINE_ROLE_KEYWORDS:
        role = _REFINE_ROLE_KEYWORDS[value]
        epw = {
            sizing.ROLE_RADIATING: radiating_epw,
            sizing.ROLE_THROAT: throat_epw,
            sizing.ROLE_SHADOW: shadow_epw,
            sizing.ROLE_NEAR_FIELD: radiating_epw,
        }[role]
        return RefineSpec(name=name, epw=float(epw), size_mm=None, role=role)
    if value.endswith("mm"):
        try:
            size_mm = float(value[:-2])
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid --refine size: {raw!r}") from exc
        if size_mm <= 0.0:
            raise argparse.ArgumentTypeError(f"--refine size must be positive: {raw!r}")
        return RefineSpec(name=name, epw=None, size_mm=size_mm, role="custom")
    try:
        epw = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --refine value: {raw!r}") from exc
    if epw <= 0.0:
        raise argparse.ArgumentTypeError(f"--refine elements-per-wavelength must be positive: {raw!r}")
    return RefineSpec(name=name, epw=epw, size_mm=None, role="custom")


def _auto_radiating_surfaces(
    shell_to_surfaces: dict[str, list[int]],
    source_surface_set: set[int],
) -> set[int]:
    """Classify rigid surfaces sharing a body/shell with a source as radiating.

    A waveguide flare modeled as its own STEP shell/body and carrying the
    source patch is the primary radiator even where it is far from the throat,
    so it must stay fine. We only auto-promote when the model splits into more
    than one named shell; a single-shell export carries no body signal and is
    left to the distance-graded near-field fallback.
    """
    if len(shell_to_surfaces) <= 1:
        return set()
    radiating: set[int] = set()
    for surfaces in shell_to_surfaces.values():
        surface_set = set(surfaces)
        if surface_set & source_surface_set:
            radiating |= surface_set
    return radiating - source_surface_set


def _step_records(step_text: str) -> dict[int, str]:
    records: dict[int, str] = {}
    for match in re.finditer(r"#(\d+)\s*=\s*(.*?);", step_text, flags=re.S):
        records[int(match.group(1))] = " ".join(match.group(2).split())
    return records


def _step_refs(record: str) -> list[int]:
    return [int(value) for value in re.findall(r"#(\d+)", record)]


def _first_step_string(record: str) -> str | None:
    match = re.search(r"'((?:[^']|'')*)'", record)
    if match is None:
        return None
    return match.group(1).replace("''", "'")


def _parse_named_shell_faces(step_path: Path) -> dict[str, list[int]]:
    """Return STEP shell/surface model name -> ADVANCED_FACE ids.

    Fusion STEP exports commonly encode named surface bodies as
    ``SHELL_BASED_SURFACE_MODEL('name', (#open_shell))``. Gmsh often drops
    those names on import, so we recover them from STEP text and map the face
    order onto imported OCC surface tags.
    """
    records = _step_records(step_path.read_text(encoding="ascii", errors="replace"))
    shell_to_faces: dict[int, list[int]] = {}
    for rec_id, record in records.items():
        if record.startswith(("OPEN_SHELL", "CLOSED_SHELL")):
            shell_to_faces[rec_id] = [
                ref for ref in _step_refs(record)
                if records.get(ref, "").startswith("ADVANCED_FACE")
            ]

    out: dict[str, list[int]] = {}
    for record in records.values():
        if not record.startswith("SHELL_BASED_SURFACE_MODEL"):
            continue
        name = _first_step_string(record)
        if not name:
            continue
        faces: list[int] = []
        for ref in _step_refs(record):
            faces.extend(shell_to_faces.get(ref, []))
        if faces:
            out[name] = faces
    return out


def _parse_styled_face_groups(step_path: Path) -> dict[str, list[int]]:
    """Return STEP presentation/appearance label -> ADVANCED_FACE ids.

    Fusion split faces cannot be named directly in the Browser, but they can
    carry per-face appearance overrides. STEP exports those overrides through
    presentation styles. This parser follows ``STYLED_ITEM`` records to either
    direct ``ADVANCED_FACE`` targets or named shell/surface targets.
    """
    records = _step_records(step_path.read_text(encoding="ascii", errors="replace"))
    shell_faces: dict[int, list[int]] = {}
    model_faces: dict[int, list[int]] = {}
    for rec_id, record in records.items():
        if record.startswith(("OPEN_SHELL", "CLOSED_SHELL")):
            shell_faces[rec_id] = [
                ref for ref in _step_refs(record)
                if records.get(ref, "").startswith("ADVANCED_FACE")
            ]
    for rec_id, record in records.items():
        if record.startswith("SHELL_BASED_SURFACE_MODEL"):
            faces: list[int] = []
            for ref in _step_refs(record):
                faces.extend(shell_faces.get(ref, []))
            if faces:
                model_faces[rec_id] = faces

    def _collect_labels(ref: int, seen: set[int] | None = None) -> set[str]:
        if seen is None:
            seen = set()
        if ref in seen:
            return set()
        seen.add(ref)
        record = records.get(ref, "")
        labels = set()
        label = _first_step_string(record)
        if label:
            labels.add(label)
        for child in _step_refs(record):
            labels.update(_collect_labels(child, seen))
        return labels

    out: dict[str, list[int]] = {}
    for record in records.values():
        if not record.startswith("STYLED_ITEM"):
            continue
        refs = _step_refs(record)
        if len(refs) < 2:
            continue
        target = refs[-1]
        target_record = records.get(target, "")
        if target_record.startswith("ADVANCED_FACE"):
            faces = [target]
        elif target in model_faces:
            faces = model_faces[target]
        elif target in shell_faces:
            faces = shell_faces[target]
        else:
            continue

        labels: set[str] = set()
        styled_name = _first_step_string(record)
        if styled_name:
            labels.add(styled_name)
        for style_ref in refs[:-1]:
            labels.update(_collect_labels(style_ref))
        for label in labels:
            if not label:
                continue
            out.setdefault(label, [])
            out[label].extend(faces)

    return {label: sorted(set(faces)) for label, faces in out.items()}


def _advanced_face_order(step_path: Path) -> list[int]:
    records = _step_records(step_path.read_text(encoding="ascii", errors="replace"))
    return [
        rec_id for rec_id, record in records.items()
        if record.startswith("ADVANCED_FACE")
    ]


def _map_step_faces_to_gmsh_surfaces(
    step_path: Path,
    source_specs: list[SourceSpec],
    *,
    skip_missing_sources: bool = False,
) -> dict[str, list[int]]:
    named_faces = _parse_named_shell_faces(step_path)
    styled_faces = _parse_styled_face_groups(step_path)
    face_order = _advanced_face_order(step_path)
    face_to_index = {face_id: index for index, face_id in enumerate(face_order)}

    gmsh_surfaces = [tag for dim, tag in sorted(gmsh.model.getEntities(2))]
    if len(gmsh_surfaces) < len(face_order):
        raise RuntimeError(
            f"STEP has {len(face_order)} ADVANCED_FACE records but gmsh imported "
            f"only {len(gmsh_surfaces)} surfaces"
        )

    def _lookup_source_faces(source_name: str) -> tuple[str, list[int]] | None:
        for origin, groups in (("named shell/surface", named_faces), ("appearance/style", styled_faces)):
            if source_name in groups:
                return origin, groups[source_name]
        lower_name = source_name.lower()
        for origin, groups in (("named shell/surface", named_faces), ("appearance/style", styled_faces)):
            for label, faces in groups.items():
                if label.lower() == lower_name:
                    return origin, faces
        return None

    requested_source_names = {spec.name.strip().upper() for spec in source_specs}
    requested_legacy_port_exits = [
        spec
        for spec in source_specs
        if spec.name.strip().upper() in LEGACY_PORT_EXIT_SOURCES
    ]
    allow_generic_port_exit_alias = (
        GENERIC_PORT_EXIT_SOURCE not in requested_source_names
        and len(requested_legacy_port_exits) == 1
    )

    def _lookup_generic_port_exit_alias(spec: SourceSpec) -> tuple[str, list[int]] | None:
        if not allow_generic_port_exit_alias:
            return None
        if spec.name.strip().upper() not in LEGACY_PORT_EXIT_SOURCES:
            return None
        lookup = _lookup_source_faces(GENERIC_PORT_EXIT_SOURCE)
        if lookup is None:
            return None
        origin, faces = lookup
        return f"{origin} ({GENERIC_PORT_EXIT_SOURCE} alias for {spec.name})", faces

    def _missing_message(source_name: str) -> str:
        available_named = ", ".join(sorted(named_faces)) or "(none)"
        available_styles = ", ".join(sorted(styled_faces)) or "(none)"
        return (
            f"source {source_name!r} not found as a named STEP shell/surface "
            f"or face appearance/style. Available shell names: {available_named}. "
            f"Available style names: {available_styles}"
        )

    mapping: dict[str, list[int]] = {}
    origins: dict[str, str] = {}
    missing: dict[str, dict[str, object]] = {}
    for spec in source_specs:
        lookup = _lookup_source_faces(spec.name)
        if lookup is None:
            lookup = _lookup_generic_port_exit_alias(spec)
        if lookup is None:
            if not skip_missing_sources:
                raise RuntimeError(_missing_message(spec.name))
            missing[spec.name] = {
                "tag": spec.tag,
                "resolution_mm": spec.resolution_mm,
                "reason": _missing_message(spec.name),
            }
            continue
        origin, face_ids = lookup
        surface_tags: list[int] = []
        for face_id in face_ids:
            if face_id not in face_to_index:
                raise RuntimeError(f"face #{face_id} for source {spec.name!r} is not an ADVANCED_FACE")
            surface_tags.append(gmsh_surfaces[face_to_index[face_id]])
        mapping[spec.name] = surface_tags
        origins[spec.name] = origin
    if not mapping:
        requested = ", ".join(spec.name for spec in source_specs)
        raise RuntimeError(
            f"none of the requested sources were found in the STEP export: {requested}. "
            f"{_missing_message(source_specs[0].name)}"
        )
    setattr(_map_step_faces_to_gmsh_surfaces, "last_origins", origins)
    setattr(_map_step_faces_to_gmsh_surfaces, "last_missing", missing)
    return mapping


def _gmsh_surface_tags() -> list[int]:
    return [tag for dim, tag in sorted(gmsh.model.getEntities(2))]


def _named_shell_gmsh_surfaces(step_path: Path, gmsh_surfaces: list[int]) -> dict[str, list[int]]:
    """Map each STEP named shell/body to its imported gmsh surface tags."""
    named_faces = _parse_named_shell_faces(step_path)
    face_order = _advanced_face_order(step_path)
    face_to_index = {face_id: index for index, face_id in enumerate(face_order)}
    out: dict[str, list[int]] = {}
    for name, faces in named_faces.items():
        out[name] = sorted(
            {gmsh_surfaces[face_to_index[f]] for f in faces if f in face_to_index}
        )
    return out


def _map_refine_groups_to_gmsh_surfaces(
    step_path: Path,
    refine_specs: list[RefineSpec],
    gmsh_surfaces: list[int],
) -> tuple[dict[str, list[int]], dict[str, str]]:
    """Resolve refine group names to gmsh surfaces (case-insensitive lookup).

    Missing refine names are skipped (they are optional overrides, unlike
    sources). Returns ``(name -> surfaces, name -> origin)``.
    """
    named_faces = _parse_named_shell_faces(step_path)
    styled_faces = _parse_styled_face_groups(step_path)
    face_order = _advanced_face_order(step_path)
    face_to_index = {face_id: index for index, face_id in enumerate(face_order)}

    def _lookup(name: str) -> tuple[str, list[int]] | None:
        for origin, groups in (("named shell/surface", named_faces), ("appearance/style", styled_faces)):
            if name in groups:
                return origin, groups[name]
        lower = name.lower()
        for origin, groups in (("named shell/surface", named_faces), ("appearance/style", styled_faces)):
            for label, faces in groups.items():
                if label.lower() == lower:
                    return origin, faces
        return None

    mapping: dict[str, list[int]] = {}
    origins: dict[str, str] = {}
    for spec in refine_specs:
        lookup = _lookup(spec.name)
        if lookup is None:
            continue
        origin, face_ids = lookup
        surfaces = sorted(
            {gmsh_surfaces[face_to_index[f]] for f in face_ids if f in face_to_index}
        )
        if surfaces:
            mapping[spec.name] = surfaces
            origins[spec.name] = origin
    return mapping, origins


def _mesh_triangle_data(mesh: meshio.Mesh) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if "triangle" not in mesh.cells_dict:
        raise RuntimeError("mesh has no triangle cells")
    triangles = np.asarray(mesh.cells_dict["triangle"], dtype=np.int64)
    points = np.asarray(mesh.points, dtype=np.float64)
    try:
        tags = np.asarray(mesh.cell_data_dict["gmsh:physical"]["triangle"], dtype=np.int32)
    except KeyError as exc:
        raise RuntimeError("mesh has no gmsh:physical triangle tags") from exc
    return points, triangles, tags


def _triangle_area2(points: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    p0 = points[triangles[:, 0]]
    p1 = points[triangles[:, 1]]
    p2 = points[triangles[:, 2]]
    return np.linalg.norm(np.cross(p1 - p0, p2 - p0), axis=1)


def _remove_degenerate_triangles(
    points: np.ndarray,
    triangles: np.ndarray,
    tags: np.ndarray,
    *,
    eps: float = 1e-18,
    min_quality: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Drop zero-area triangles and, optionally, needle slivers.

    ``min_quality`` is a scale-invariant shape threshold: a triangle whose area
    falls below ``min_quality * longest_edge**2`` is removed. Fine OCC meshes
    carry micrometre-wide needles bridging near-duplicate patch-boundary nodes
    whose quadrature-degenerate rows make the dense metal-bem solve singular
    (LAPACK info > 0). Ported from hornlab_mesher.normals (commit a5539de).
    """
    if len(triangles) == 0:
        return triangles, tags, 0
    p0 = points[triangles[:, 0]]
    p1 = points[triangles[:, 1]]
    p2 = points[triangles[:, 2]]
    area2 = np.linalg.norm(np.cross(p1 - p0, p2 - p0), axis=1)
    keep = area2 > eps
    if min_quality > 0.0:
        longest_sq = np.maximum(
            np.maximum(
                np.sum((p1 - p0) ** 2, axis=1),
                np.sum((p2 - p1) ** 2, axis=1),
            ),
            np.sum((p0 - p2) ** 2, axis=1),
        )
        keep &= (0.5 * area2) > (min_quality * longest_sq)
    return triangles[keep], tags[keep], int(np.count_nonzero(~keep))


def _weld_near_duplicate_vertices(
    points: np.ndarray,
    triangles: np.ndarray,
    *,
    tol_mm: float = WELD_TOLERANCE_MM,
) -> np.ndarray:
    """Remap triangles so vertices closer than ``tol_mm`` coincide.

    Spatial hash with cells of the weld tolerance; clusters merge to the lowest
    vertex index via union-find. Closes the near-duplicate boundary nodes
    (micrometres apart) that OCC leaves between sewn patches on fine meshes,
    which otherwise seed singular slivers and spurious free edges. Ported from
    hornlab_mesher.mesher (commit a8c2648).
    """
    if len(points) == 0 or len(triangles) == 0:
        return triangles
    cells = np.floor(points / tol_mm).astype(np.int64)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for index, key in enumerate(map(tuple, cells)):
        buckets.setdefault(key, []).append(index)

    parent = np.arange(len(points))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = int(parent[a])
        return a

    tol_sq = tol_mm * tol_mm
    neighbor_offsets = [
        (dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
    ]
    for key, indices in buckets.items():
        candidates: list[int] = []
        for dx, dy, dz in neighbor_offsets:
            candidates.extend(buckets.get((key[0] + dx, key[1] + dy, key[2] + dz), ()))
        for i in indices:
            pi = points[i]
            for j in candidates:
                if j <= i:
                    continue
                delta = points[j] - pi
                if float(delta @ delta) <= tol_sq:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[max(ri, rj)] = min(ri, rj)

    roots = np.fromiter((find(i) for i in range(len(points))), dtype=np.int64, count=len(points))
    if np.array_equal(roots, np.arange(len(points))):
        return triangles
    return roots[triangles]


def _compact_unused_vertices(
    points: np.ndarray,
    triangles: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Drop unreferenced vertices and renumber triangles to the survivors."""
    if len(triangles) == 0:
        return points, triangles
    used = np.unique(triangles)
    if len(used) == len(points):
        return points, triangles
    remap = np.full(len(points), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    return points[used], remap[triangles]


def _edge_direction_stats(triangles: np.ndarray) -> dict[str, object]:
    edge_dirs: dict[tuple[int, int], list[int]] = defaultdict(list)
    for tri in np.asarray(triangles, dtype=np.int64):
        for start, end in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            a = int(start)
            b = int(end)
            if a == b:
                continue
            if a < b:
                edge_dirs[(a, b)].append(1)
            else:
                edge_dirs[(b, a)].append(-1)

    boundary_edges = 0
    nonmanifold_edges = 0
    inconsistent_edges = 0
    for dirs in edge_dirs.values():
        if len(dirs) == 1:
            boundary_edges += 1
        elif len(dirs) != 2:
            nonmanifold_edges += 1
        elif dirs[0] == dirs[1]:
            inconsistent_edges += 1

    return {
        "n_edges": int(len(edge_dirs)),
        "boundary_edges": int(boundary_edges),
        "free_edges": int(boundary_edges),
        "nonmanifold_edges": int(nonmanifold_edges),
        "inconsistent_edges": int(inconsistent_edges),
    }


def _signed_volume(points: np.ndarray, triangles: np.ndarray) -> float:
    if len(triangles) == 0:
        return 0.0
    p0 = points[triangles[:, 0]]
    p1 = points[triangles[:, 1]]
    p2 = points[triangles[:, 2]]
    return float(np.sum(p0 * np.cross(p1, p2)) / 6.0)


def _source_normal_projections(
    points: np.ndarray,
    triangles: np.ndarray,
    tags: np.ndarray,
    source_specs: list[SourceSpec],
) -> dict[str, dict[str, object]]:
    projections: dict[str, dict[str, object]] = {}
    for spec in source_specs:
        mask = tags == spec.tag
        if not np.any(mask):
            continue
        tri = triangles[mask]
        p0 = points[tri[:, 0]]
        p1 = points[tri[:, 1]]
        p2 = points[tri[:, 2]]
        vector = np.sum(np.cross(p1 - p0, p2 - p0), axis=0)
        projections[spec.name] = {
            "tag": int(spec.tag),
            "triangle_count": int(len(tri)),
            "vector_step_units2": [float(v) for v in vector],
            "projection_x_step_units2": float(vector[0]),
            "projection_y_step_units2": float(vector[1]),
            "projection_z_step_units2": float(vector[2]),
        }
    return projections


def _repair_triangle_winding(
    points: np.ndarray,
    triangles: np.ndarray,
) -> tuple[np.ndarray, dict[str, int]]:
    """Repair manifold edge winding; only flip globally when the mesh is closed."""
    repaired = triangles.copy()
    stats = {
        "flipped_consistency": 0,
        "flipped_global": 0,
    }
    if len(repaired) == 0:
        return repaired, stats

    edge_to_triangles: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for tri_idx, tri in enumerate(repaired):
        for start, end in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            a = int(start)
            b = int(end)
            if a == b:
                continue
            if a < b:
                edge_to_triangles[(a, b)].append((tri_idx, 1))
            else:
                edge_to_triangles[(b, a)].append((tri_idx, -1))

    neighbours: list[list[tuple[int, bool]]] = [[] for _ in range(len(repaired))]
    for uses in edge_to_triangles.values():
        if len(uses) != 2:
            continue
        (tri_a, dir_a), (tri_b, dir_b) = uses
        must_differ = dir_a == dir_b
        neighbours[tri_a].append((tri_b, must_differ))
        neighbours[tri_b].append((tri_a, must_differ))

    flip = np.zeros(len(repaired), dtype=bool)
    seen = np.zeros(len(repaired), dtype=bool)
    for seed in range(len(repaired)):
        if seen[seed]:
            continue
        seen[seed] = True
        queue: deque[int] = deque([seed])
        while queue:
            tri_idx = queue.popleft()
            for other, must_differ in neighbours[tri_idx]:
                required = bool(flip[tri_idx]) ^ bool(must_differ)
                if seen[other]:
                    continue
                flip[other] = required
                seen[other] = True
                queue.append(other)

    if np.any(flip):
        repaired[flip] = repaired[flip][:, [0, 2, 1]]
        stats["flipped_consistency"] = int(np.count_nonzero(flip))

    edge_stats = _edge_direction_stats(repaired)
    if (
        edge_stats["boundary_edges"] == 0
        and edge_stats["nonmanifold_edges"] == 0
        and _signed_volume(points, repaired) < 0.0
    ):
        repaired[:, [1, 2]] = repaired[:, [2, 1]]
        stats["flipped_global"] = int(len(repaired))

    return repaired, stats


def _free_edges_on_origin_planes(
    points: np.ndarray,
    triangles: np.ndarray,
    *,
    tolerance: float,
) -> bool:
    """True when every free edge lies on a coordinate plane through the origin."""
    edge_count: dict[tuple[int, int], int] = {}
    for tri in triangles:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge = tuple(sorted((int(a), int(b))))
            edge_count[edge] = edge_count.get(edge, 0) + 1
    for edge, count in edge_count.items():
        if count != 1:
            continue
        midpoint = 0.5 * (points[edge[0]] + points[edge[1]])
        if not any(abs(float(midpoint[axis])) <= tolerance for axis in range(3)):
            return False
    return True


def _detect_symmetry_planes(
    points: np.ndarray,
    triangles: np.ndarray,
    *,
    tolerance: float,
    min_edges_per_plane: int = 3,
) -> tuple[tuple[str, ...], dict[str, object]]:
    """Detect symmetry cut planes from free edges lying on x=0/y=0/z=0.

    Only free edges lying exclusively on a single coordinate plane count
    toward that plane. A cut rim in the x=0 wall crossing height z=0
    contributes edges that sit on both planes at once; counting those toward
    z0 would misread an internal level as a cut plane. A true cut outline
    always has edges away from the other coordinate planes, so the exclusive
    count stays robust. ``min_edges_per_plane`` additionally keeps an
    isolated leak vertex near the origin from masquerading as a plane.
    """
    edge_count: dict[tuple[int, int], int] = {}
    for tri in triangles:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge = tuple(sorted((int(a), int(b))))
            edge_count[edge] = edge_count.get(edge, 0) + 1
    free_edges = [edge for edge, count in edge_count.items() if count == 1]

    plane_counts = {"x0": 0, "y0": 0, "z0": 0}
    shared_plane_edges = 0
    for edge in free_edges:
        midpoint = 0.5 * (points[edge[0]] + points[edge[1]])
        on_planes = [
            plane
            for axis, plane in enumerate(("x0", "y0", "z0"))
            if abs(float(midpoint[axis])) <= tolerance
        ]
        if len(on_planes) == 1:
            plane_counts[on_planes[0]] += 1
        elif len(on_planes) > 1:
            shared_plane_edges += 1

    detected = tuple(
        plane for plane in ("x0", "y0", "z0")
        if plane_counts[plane] >= min_edges_per_plane
    )
    detection = {
        "mode": "auto",
        "free_edges": int(len(free_edges)),
        "plane_free_edge_counts": {k: int(v) for k, v in plane_counts.items()},
        "shared_plane_free_edges": int(shared_plane_edges),
        "min_edges_per_plane": int(min_edges_per_plane),
        "tolerance": float(tolerance),
        "detected_planes": list(detected),
    }
    return detected, detection


def _normalize_to_positive_side(
    points: np.ndarray,
    triangles: np.ndarray,
    *,
    symmetry_planes: tuple[str, ...],
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    """Reflect the reduced domain onto the positive side of each cut plane.

    The Metal native symmetry solve requires the reduced mesh on the positive
    side of its symmetry planes. A model cut to a negative quadrant in CAD is
    the mirror image of the equivalent positive-quadrant model, so reflecting
    it (and flipping winding once per reflection to keep normals outward)
    changes nothing about the represented full-domain geometry.
    """
    axis_for_plane = {"x0": 0, "y0": 1, "z0": 2}
    reflected_axes: list[str] = []
    points = points.copy()
    triangles = triangles.copy()
    for plane in symmetry_planes:
        axis = axis_for_plane[plane]
        coords = points[:, axis]
        if -float(coords.min()) > float(coords.max()):
            points[:, axis] = -points[:, axis]
            reflected_axes.append("xyz"[axis])
    if len(reflected_axes) % 2 == 1:
        triangles = triangles[:, [0, 2, 1]]
    normalization = {
        "symmetry_planes": list(symmetry_planes),
        "reflected_axes": reflected_axes,
    }
    return points, triangles, normalization


def _postprocess_mesh(
    mesh: meshio.Mesh,
    source_specs: list[SourceSpec],
    *,
    symmetry_planes: tuple[str, ...] | str,
    tolerance: float,
) -> tuple[meshio.Mesh, dict[str, object], dict[str, object]]:
    points, triangles, tags = _mesh_triangle_data(mesh)
    before_edge_stats = _edge_direction_stats(triangles)
    before_signed_volume = _signed_volume(points, triangles)
    distinct_before = int(len(np.unique(triangles))) if len(triangles) else 0
    triangles = _weld_near_duplicate_vertices(points, triangles)
    welded_vertices = max(0, distinct_before - (int(len(np.unique(triangles))) if len(triangles) else 0))
    triangles, tags, degenerate_removed = _remove_degenerate_triangles(
        points, triangles, tags, min_quality=DEGENERATE_MIN_QUALITY
    )
    repaired_triangles, repair_stats = _repair_triangle_winding(points, triangles)
    repair_stats["welded_vertices"] = int(welded_vertices)
    after_edge_stats = _edge_direction_stats(repaired_triangles)
    after_signed_volume = _signed_volume(points, repaired_triangles)

    symmetry_detection: dict[str, object] | None = None
    if symmetry_planes == "auto":
        symmetry_planes, symmetry_detection = _detect_symmetry_planes(
            points,
            repaired_triangles,
            tolerance=tolerance,
        )

    points, repaired_triangles, axis_normalization = _normalize_to_positive_side(
        points,
        repaired_triangles,
        symmetry_planes=symmetry_planes,
    )

    # _repair_triangle_winding only enforces global outward orientation for
    # closed meshes. A reduced mesh whose free edges all lie on origin cut
    # planes has an equally well-defined signed volume (the missing cap faces
    # contribute nothing to it), so enforce outward orientation for those too;
    # the solver and the orientation diagnostic both rely on it.
    if (
        after_edge_stats["boundary_edges"] > 0
        and after_signed_volume < 0.0
        and _free_edges_on_origin_planes(points, repaired_triangles, tolerance=tolerance)
    ):
        repaired_triangles = repaired_triangles[:, [0, 2, 1]]
        repair_stats["flipped_global"] = int(len(repaired_triangles))
        after_signed_volume = -after_signed_volume

    # Drop vertices orphaned by welding/degenerate removal so the written node
    # count matches the live mesh the solver assembles.
    points, repaired_triangles = _compact_unused_vertices(points, repaired_triangles)

    repaired_mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", repaired_triangles)],
        cell_data={
            "gmsh:physical": [tags.astype(np.int32, copy=False)],
            "gmsh:geometrical": [tags.astype(np.int32, copy=False)],
        },
        field_data=mesh.field_data,
    )
    topology = _topology_stats(
        points,
        repaired_triangles,
        symmetry_planes=symmetry_planes,
        tolerance=tolerance,
    )
    if symmetry_detection is not None:
        topology["symmetry_plane_detection"] = symmetry_detection
    topology["axis_normalization"] = axis_normalization
    topology["signed_volume_step_units3"] = after_signed_volume
    topology["source_normal_projections"] = _source_normal_projections(
        points,
        repaired_triangles,
        tags,
        source_specs,
    )
    repair = {
        "degenerate_triangles_removed": int(degenerate_removed),
        **repair_stats,
        "before": {
            **before_edge_stats,
            "signed_volume_step_units3": before_signed_volume,
        },
        "after": {
            **after_edge_stats,
            "signed_volume_step_units3": after_signed_volume,
        },
    }
    return repaired_mesh, repair, topology


def _triangle_edge_lengths(points: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    lengths: list[float] = []
    seen: set[tuple[int, int]] = set()
    for tri in triangles:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge = tuple(sorted((int(a), int(b))))
            if edge in seen:
                continue
            seen.add(edge)
            lengths.append(float(np.linalg.norm(points[edge[0]] - points[edge[1]])))
    return np.asarray(lengths, dtype=np.float64)


def _edge_frequency_stats(
    points: np.ndarray,
    triangles: np.ndarray,
    *,
    unit_scale_to_m: float,
    elements_per_wavelength: float,
    speed_of_sound_m_s: float,
) -> dict[str, object]:
    lengths = _triangle_edge_lengths(points, triangles)
    max_edge_step_units = float(np.max(lengths)) if len(lengths) else 0.0
    p95_edge_step_units = float(np.percentile(lengths, 95.0)) if len(lengths) else 0.0
    max_edge_m = max_edge_step_units * unit_scale_to_m
    max_valid_frequency_hz = (
        speed_of_sound_m_s / (elements_per_wavelength * max_edge_m)
        if max_edge_m > 0.0
        else 0.0
    )
    return {
        "max_edge_step_units": max_edge_step_units,
        "max_edge_m": float(max_edge_m),
        "p95_edge_step_units": p95_edge_step_units,
        "p95_edge_m": float(p95_edge_step_units * unit_scale_to_m),
        "max_valid_frequency_hz": float(max_valid_frequency_hz),
    }


def _source_wall_stats(
    points: np.ndarray,
    triangles: np.ndarray,
    tags: np.ndarray,
    spec: SourceSpec,
    *,
    transition_mm: float,
    unit_scale_to_m: float,
    elements_per_wavelength: float,
    speed_of_sound_m_s: float,
) -> dict[str, object] | None:
    """Edge statistics for rigid triangles near a source patch.

    The wave launched by a source travels along the surrounding rigid
    surfaces, so the usable solve band of that source is limited by the
    rigid mesh it traverses, not only by the source patch itself. Rigid
    triangles whose centroid lies within the source refinement transition
    distance are taken as the local wall region.
    """
    source_mask = tags == spec.tag
    rigid_mask = tags == RIGID_TAG
    if not np.any(source_mask) or not np.any(rigid_mask):
        return None
    patch_vertices = points[np.unique(triangles[source_mask])]
    rigid_triangles = triangles[rigid_mask]
    centroids = points[rigid_triangles].mean(axis=1)
    min_distance = np.full(len(centroids), np.inf)
    for start in range(0, len(patch_vertices), 512):
        chunk = patch_vertices[start:start + 512]
        distances = np.linalg.norm(
            centroids[:, None, :] - chunk[None, :, :],
            axis=2,
        ).min(axis=1)
        min_distance = np.minimum(min_distance, distances)
    near_triangles = rigid_triangles[min_distance <= transition_mm]
    if len(near_triangles) == 0:
        return None
    stats = _edge_frequency_stats(
        points,
        near_triangles,
        unit_scale_to_m=unit_scale_to_m,
        elements_per_wavelength=elements_per_wavelength,
        speed_of_sound_m_s=speed_of_sound_m_s,
    )
    return {
        "wall_triangle_count": int(len(near_triangles)),
        "wall_distance_mm": float(transition_mm),
        "wall_max_edge_step_units": float(stats["max_edge_step_units"]),
        "wall_max_edge_m": float(stats["max_edge_m"]),
        "wall_p95_edge_step_units": float(stats["p95_edge_step_units"]),
        "wall_p95_edge_m": float(stats["p95_edge_m"]),
        "wall_max_valid_frequency_hz": float(stats["max_valid_frequency_hz"]),
    }


def _mesh_frequency_validation(
    points: np.ndarray,
    triangles: np.ndarray,
    tags: np.ndarray,
    source_specs: list[SourceSpec],
    *,
    unit_scale_to_m: float,
    requested_max_frequency_hz: float | None,
    transition_mm: float = 200.0,
    elements_per_wavelength: float = FREQUENCY_ELEMENTS_PER_WAVELENGTH,
    speed_of_sound_m_s: float = SPEED_OF_SOUND_M_S,
) -> dict[str, object]:
    global_stats = _edge_frequency_stats(
        points,
        triangles,
        unit_scale_to_m=unit_scale_to_m,
        elements_per_wavelength=elements_per_wavelength,
        speed_of_sound_m_s=speed_of_sound_m_s,
    )
    edge_limit_m = (
        speed_of_sound_m_s / (elements_per_wavelength * requested_max_frequency_hz)
        if requested_max_frequency_hz is not None
        else None
    )
    warnings: list[str] = []
    global_status = "unknown"
    if requested_max_frequency_hz is not None:
        global_status = "valid"
        if requested_max_frequency_hz > float(global_stats["max_valid_frequency_hz"]):
            global_status = "invalid"
            warnings.append(
                "requested max frequency exceeds conservative global mesh limit "
                f"({requested_max_frequency_hz:.6g} Hz > "
                f"{float(global_stats['max_valid_frequency_hz']):.6g} Hz); "
                "global coarse regions are reported but only active source patches hard-fail"
            )

    per_source: dict[str, dict[str, object]] = {}
    invalid_sources: list[str] = []
    for spec in source_specs:
        mask = tags == spec.tag
        source_triangles = triangles[mask]
        stats = _edge_frequency_stats(
            points,
            source_triangles,
            unit_scale_to_m=unit_scale_to_m,
            elements_per_wavelength=elements_per_wavelength,
            speed_of_sound_m_s=speed_of_sound_m_s,
        )
        wall_stats = _source_wall_stats(
            points,
            triangles,
            tags,
            spec,
            transition_mm=transition_mm,
            unit_scale_to_m=unit_scale_to_m,
            elements_per_wavelength=elements_per_wavelength,
            speed_of_sound_m_s=speed_of_sound_m_s,
        )
        patch_limit = float(stats["max_valid_frequency_hz"])
        effective_limit = patch_limit
        if wall_stats is not None:
            wall_limit = float(wall_stats["wall_max_valid_frequency_hz"])
            if wall_limit > 0.0:
                effective_limit = (
                    min(patch_limit, wall_limit) if patch_limit > 0.0 else wall_limit
                )
        source_status = "unknown"
        if requested_max_frequency_hz is not None:
            source_status = "valid"
            if requested_max_frequency_hz > effective_limit:
                source_status = "invalid"
                invalid_sources.append(spec.name)
                if effective_limit < patch_limit:
                    warnings.append(
                        f"{spec.name} rigid walls within the transition distance are "
                        f"underresolved for {requested_max_frequency_hz:.6g} Hz "
                        f"(wall valid {effective_limit:.6g} Hz, patch valid "
                        f"{patch_limit:.6g} Hz)"
                    )
                else:
                    warnings.append(
                        f"{spec.name} source patch is underresolved for "
                        f"{requested_max_frequency_hz:.6g} Hz "
                        f"(max valid {patch_limit:.6g} Hz)"
                    )
        per_source[spec.name] = {
            "name": spec.name,
            "tag": int(spec.tag),
            "requested_resolution_mm": float(spec.resolution_mm),
            "triangle_count": int(len(source_triangles)),
            "status": source_status,
            "effective_max_valid_frequency_hz": float(effective_limit),
            **stats,
            **(wall_stats or {}),
        }

    status = "unknown"
    if requested_max_frequency_hz is not None:
        status = "invalid" if invalid_sources else "valid"

    return {
        "status": status,
        "scope": "global_warn_source_hard",
        "frequency_policy": "global_warn_source_hard",
        "global_status": global_status,
        "global_max_edge_step_units": float(global_stats["max_edge_step_units"]),
        "global_max_edge_m": float(global_stats["max_edge_m"]),
        "global_p95_edge_step_units": float(global_stats["p95_edge_step_units"]),
        "global_p95_edge_m": float(global_stats["p95_edge_m"]),
        "elements_per_wavelength": float(elements_per_wavelength),
        "speed_of_sound_m_s": float(speed_of_sound_m_s),
        "edge_limit_step_units": (
            None if edge_limit_m is None else float(edge_limit_m / unit_scale_to_m)
        ),
        "edge_limit_m": None if edge_limit_m is None else float(edge_limit_m),
        "max_valid_frequency_hz": float(global_stats["max_valid_frequency_hz"]),
        "global_max_valid_frequency_hz": float(global_stats["max_valid_frequency_hz"]),
        "requested_max_frequency_hz": (
            None if requested_max_frequency_hz is None else float(requested_max_frequency_hz)
        ),
        "invalid_sources": invalid_sources,
        "per_source": per_source,
        "warnings": warnings,
    }


def _remove_duplicate_nodes_for_current_gmsh_model() -> dict[str, object]:
    stats: dict[str, object] = {
        "attempted": True,
        "supported": hasattr(gmsh.model.mesh, "removeDuplicateNodes"),
        "node_count_before": None,
        "node_count_after": None,
        "removed": None,
        "error": None,
    }
    if not stats["supported"]:
        return stats
    try:
        before = len(gmsh.model.mesh.getNodes()[0])
        gmsh.model.mesh.removeDuplicateNodes()
        after = len(gmsh.model.mesh.getNodes()[0])
        stats.update(
            {
                "node_count_before": int(before),
                "node_count_after": int(after),
                "removed": int(before - after),
            }
        )
    except Exception as exc:  # pragma: no cover - depends on gmsh build/input geometry
        stats["error"] = str(exc)
    return stats


def _expected_symmetry_planes_from_quadrants(quadrants: int) -> tuple[str, ...]:
    if quadrants == 1:
        return ("x0", "y0")
    if quadrants == 14:
        return ("x0",)
    if quadrants == 12:
        return ("y0",)
    if quadrants == 1234:
        return ()
    raise ValueError("--quadrants must be one of 1, 12, 14, 1234")


def _parse_symmetry_planes(raw: str | None, *, quadrants: int) -> tuple[str, ...]:
    if raw is None:
        return _expected_symmetry_planes_from_quadrants(quadrants)
    aliases = {
        "": (),
        "none": (),
        "full": (),
        "full-model": (),
        "full model": (),
        "x": ("x0",),
        "x0": ("x0",),
        "left-right": ("x0",),
        "left/right": ("x0",),
        "leftright": ("x0",),
        "yz": ("x0",),
        "y": ("y0",),
        "y0": ("y0",),
        "front-back": ("y0",),
        "front/back": ("y0",),
        "frontback": ("y0",),
        "xz": ("y0",),
        "z": ("z0",),
        "z0": ("z0",),
        "top-bottom": ("z0",),
        "top/bottom": ("z0",),
        "topbottom": ("z0",),
        "xy": ("z0",),
    }
    planes: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if key in aliases:
            planes.extend(aliases[key])
            continue
        raise ValueError(
            "--symmetry-planes expects comma-separated x0/y0/z0 or "
            "left-right/front-back/top-bottom"
        )
    ordered = []
    for plane in ("x0", "y0", "z0"):
        if plane in planes:
            ordered.append(plane)
    if len(ordered) != len(set(planes)):
        raise ValueError("--symmetry-planes contains duplicate planes")
    return tuple(ordered)


def _edge_on_expected_plane(
    midpoint: np.ndarray,
    planes: Iterable[str],
    tol: float,
) -> bool:
    for plane in planes:
        if plane == "x0" and abs(float(midpoint[0])) <= tol:
            return True
        if plane == "y0" and abs(float(midpoint[1])) <= tol:
            return True
        if plane == "z0" and abs(float(midpoint[2])) <= tol:
            return True
    return False


def _topology_stats(
    points: np.ndarray,
    triangles: np.ndarray,
    *,
    symmetry_planes: tuple[str, ...],
    tolerance: float,
) -> dict:
    edge_count: dict[tuple[int, int], int] = {}
    for tri in triangles:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge = tuple(sorted((int(a), int(b))))
            edge_count[edge] = edge_count.get(edge, 0) + 1

    free_edges = [edge for edge, count in edge_count.items() if count == 1]
    nonmanifold_edges = [edge for edge, count in edge_count.items() if count > 2]
    edge_direction_stats = _edge_direction_stats(triangles)
    unexpected = []
    samples = []
    for edge in free_edges:
        midpoint = 0.5 * (points[edge[0]] + points[edge[1]])
        if not _edge_on_expected_plane(midpoint, symmetry_planes, tolerance):
            unexpected.append(edge)
            if len(samples) < 20:
                samples.append([float(v) for v in midpoint])

    return {
        "triangles": int(len(triangles)),
        "vertices": int(len(points)),
        "free_edges": int(len(free_edges)),
        "boundary_edges": int(len(free_edges)),
        "nonmanifold_edges": int(len(nonmanifold_edges)),
        "inconsistent_edges": int(edge_direction_stats["inconsistent_edges"]),
        "expected_symmetry_planes": list(symmetry_planes),
        "unexpected_free_edges": int(len(unexpected)),
        "unexpected_free_edge_midpoint_samples": samples,
    }


def _write_wg_source_meshes(
    tagged_mesh_path: Path,
    out_dir: Path,
    source_specs: list[SourceSpec],
    *,
    unit_scale_to_m: float,
) -> dict[str, str]:
    mesh = meshio.read(tagged_mesh_path)
    points, triangles, tags = _mesh_triangle_data(mesh)
    outputs: dict[str, str] = {}
    for spec in source_specs:
        remapped = np.full(tags.shape, RIGID_TAG, dtype=np.int32)
        remapped[tags == spec.tag] = SOURCE_TAG_BASE
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", spec.name).strip("_") or f"source_{spec.tag}"
        out_path = out_dir / f"{safe_name}_source_tag2_m.msh"
        out_mesh = meshio.Mesh(
            points=points * unit_scale_to_m,
            cells=[("triangle", triangles)],
            cell_data={
                "gmsh:physical": [remapped],
                "gmsh:geometrical": [remapped],
            },
            field_data={
                "rigid": np.array([RIGID_TAG, 2], dtype=np.int32),
                safe_name: np.array([SOURCE_TAG_BASE, 2], dtype=np.int32),
            },
        )
        meshio.write(out_path, out_mesh, file_format="gmsh22", binary=False)
        outputs[spec.name] = str(out_path)
    return outputs


def _surface_diagnostics(surface_tags: list[int]) -> list[dict]:
    rows = []
    for tag in surface_tags:
        area = gmsh.model.occ.getMass(2, tag)
        com = gmsh.model.occ.getCenterOfMass(2, tag)
        bbox = gmsh.model.getBoundingBox(2, tag)
        rows.append({
            "surface": int(tag),
            "area_step_units2": float(area),
            "center_step_units": [float(v) for v in com],
            "bbox_step_units": [float(v) for v in bbox],
        })
    return rows


def _source_size_min_mm(spec: SourceSpec, *, f_max_hz: float | None, radiating_epw: float) -> float:
    """Radiating size of a source patch and the wall grading start around it.

    ``min(mm_knob, c/(radiating_epw*f_max))``: the source patch is a radiating
    surface, so it is refined to the band top, and the near-field/flare grades
    out from this same size. Sizing the patch at the surrounding wall
    resolution avoids a size discontinuity at the patch boundary; a coarse
    woofer patch cannot survive a finer baffle anyway, because the field is the
    minimum of all size fields.
    """
    return sizing.role_size_mm(
        sizing.ROLE_RADIATING,
        f_max_hz=f_max_hz,
        mm_knob_mm=spec.resolution_mm,
        radiating_epw=radiating_epw,
    )


def _shadow_size_mm(*, rigid_res_mm: float, f_max_hz: float | None, shadow_epw: float) -> float:
    """Coarse background size for far/shadow surfaces: ``min(rigid_res, ceiling)``."""
    return sizing.role_size_mm(
        sizing.ROLE_SHADOW,
        f_max_hz=f_max_hz,
        mm_knob_mm=rigid_res_mm,
        shadow_epw=shadow_epw,
    )


def _density_configuration(
    source_specs: list[SourceSpec],
    *,
    mesh_sizing_mode: str = "manual-mm",
    rigid_res_mm: float,
    transition_mm: float,
    f_max_hz: float | None = None,
    radiating_epw: float = DEFAULT_RADIATING_EPW,
    shadow_epw: float = DEFAULT_SHADOW_EPW,
    throat_epw: float = DEFAULT_THROAT_EPW,
    refine_specs: list[RefineSpec] | None = None,
    refine_surfaces: dict[str, list[int]] | None = None,
    curvature_segments: int = 0,
) -> dict[str, object]:
    """Describe the planned size field by acoustic role.

    The near-field/baffle is graded by distance from each source patch, from
    the source's (band-refined) radiating size up to the shadow background
    size, so the baffle stays medium rather than being coarsened to shadow.
    Far surfaces relax to the shadow background (``Mesh.MeshSizeMax``).
    Painted refine groups pin a constant size on named faces while keeping
    them rigid.
    """
    refine_specs = refine_specs or []
    refine_surfaces = refine_surfaces or {}
    radiating_res = sizing.frequency_ceiling_mm(radiating_epw, f_max_hz)
    throat_res = sizing.frequency_ceiling_mm(throat_epw, f_max_hz)
    shadow_res = _shadow_size_mm(rigid_res_mm=rigid_res_mm, f_max_hz=f_max_hz, shadow_epw=shadow_epw)
    config: dict[str, object] = {
        "groups": ["rigid", *[spec.name for spec in source_specs]],
        "mesh_size_extend_from_boundary": 0,
        "mesh_size_from_curvature": int(curvature_segments),
        "mesh_size_from_points": 0,
        "mesh_algorithm": 6,
        "mesh_sizing_mode": mesh_sizing_mode,
        "rigid_res_mm": float(rigid_res_mm),
        "transition_mm": float(transition_mm),
        "f_max_hz": None if not f_max_hz else float(f_max_hz),
        "radiating_epw": float(radiating_epw),
        "shadow_epw": float(shadow_epw),
        "throat_epw": float(throat_epw),
        "radiating_res_mm": None if radiating_res is None else float(radiating_res),
        "throat_res_mm": None if throat_res is None else float(throat_res),
        "shadow_res_mm": float(shadow_res),
        "source_fields": {
            spec.name: {
                "tag": int(spec.tag),
                "resolution_mm": float(spec.resolution_mm),
                "field": "Distance/Threshold",
                "role": sizing.ROLE_RADIATING,
                "dist_min_mm": 0.0,
                "dist_max_mm": float(transition_mm),
                # The patch is radiating: refined to the band top, and the
                # near-field/flare grades out from this same size.
                "patch_size_mm": _source_size_min_mm(
                    spec, f_max_hz=f_max_hz, radiating_epw=radiating_epw
                ),
                "size_min_mm": _source_size_min_mm(
                    spec, f_max_hz=f_max_hz, radiating_epw=radiating_epw
                ),
                "size_max_mm": float(shadow_res),
            }
            for spec in source_specs
        },
        "refine_fields": {
            spec.name: {
                "field": "Restrict",
                "role": spec.role,
                "size_mm": spec.size_for_band(f_max_hz, fallback_mm=rigid_res_mm),
                "epw": spec.epw,
                "surfaces": refine_surfaces.get(spec.name, []),
            }
            for spec in refine_specs
            if spec.name in refine_surfaces
        },
    }
    return config


def _predict_mesh_size_cost(
    *,
    source_surfaces: dict[str, list[int]],
    rigid_surfaces: list[int],
    auto_radiating: set[int],
    refine_surfaces: dict[str, list[int]],
    refine_specs: list[RefineSpec],
    active_source_specs: list[SourceSpec],
    density: dict[str, object],
    f_max_hz: float | None,
    transition_mm: float,
    radiating_ceiling: float | None,
    shadow_res: float,
    radiating_epw: float,
    symmetry_planes: tuple[str, ...] | str,
) -> dict[str, object]:
    """Predict triangles/RAM/solve cost from OCC face areas before meshing.

    Each surface is assigned its planned element size by acoustic role (the
    same field the mesher applies): radiating surfaces at the radiating
    ceiling, painted refine groups at their size, and the near-field/baffle at
    the distance-graded size evaluated at the face centroid. ``N ~= 2.3 *
    sum(area / size^2)`` over the quarter model (the symmetry-reduced solve
    matrix dimension).
    """
    refine_by_name = {spec.name: spec for spec in refine_specs}
    refine_surface_to_size: dict[int, tuple[str, float]] = {}
    for name, surfaces in refine_surfaces.items():
        spec = refine_by_name.get(name)
        if spec is None:
            continue
        size_mm = spec.size_for_band(f_max_hz, fallback_mm=float(density["rigid_res_mm"]))
        role = spec.role if spec.role in (sizing.ROLE_RADIATING, sizing.ROLE_SHADOW, sizing.ROLE_THROAT) else f"refine:{name}"
        for surface in surfaces:
            refine_surface_to_size[surface] = (role, size_mm)

    def _sample_face_points(surface: int, n: int) -> list[tuple[float, float, float]]:
        try:
            bounds_min, bounds_max = gmsh.model.getParametrizationBounds(2, surface)
            umin, vmin = float(bounds_min[0]), float(bounds_min[1])
            umax, vmax = float(bounds_max[0]), float(bounds_max[1])
        except Exception:
            com = gmsh.model.occ.getCenterOfMass(2, surface)
            return [(float(com[0]), float(com[1]), float(com[2]))]
        params: list[float] = []
        for i in range(n):
            u = umin + (i + 0.5) * (umax - umin) / n
            for j in range(n):
                params.extend((u, vmin + (j + 0.5) * (vmax - vmin) / n))
        coords = gmsh.model.getValue(2, surface, params)
        return [
            (coords[3 * k], coords[3 * k + 1], coords[3 * k + 2])
            for k in range(len(params) // 2)
        ]

    # Per-source point clouds: gmsh's Distance field measures distance to the
    # source faces, so a face centroid badly overestimates distance for large
    # source patches and underpredicts the near-field. Sample the patches.
    # Each source grades from its own patch size (the mesher combines the
    # per-source Threshold fields with Min), so the clouds stay per-source: a
    # wall next to a coarse woofer must not be counted at the tweeter's size.
    source_clouds: list[tuple[list[tuple[float, float, float]], float]] = []
    for name, surfaces in source_surfaces.items():
        field = density["source_fields"].get(name)
        size_min = float(field["size_min_mm"]) if field else shadow_res
        points: list[tuple[float, float, float]] = []
        for surface in surfaces:
            points.extend(_sample_face_points(surface, 6))
        if points:
            source_clouds.append((points, size_min))
    finest_source_size = min(
        float(field["size_min_mm"]) for field in density["source_fields"].values()
    ) if density["source_fields"] else shadow_res

    def _graded_size_at(xyz: tuple[float, float, float]) -> float:
        """Planned size at a point: Min over the per-source graded fields."""
        size = float(shadow_res)
        for points, size_min in source_clouds:
            best = float("inf")
            for sx, sy, sz in points:
                d = ((xyz[0] - sx) ** 2 + (xyz[1] - sy) ** 2 + (xyz[2] - sz) ** 2) ** 0.5
                if d < best:
                    best = d
            size = min(
                size,
                sizing.graded_size_mm(
                    best,
                    size_min_mm=size_min,
                    size_max_mm=shadow_res,
                    dist_max_mm=float(transition_mm),
                ),
            )
        return size

    def _graded_triangles(surface: int, area: float, n: int = 8) -> float:
        """Sum 2.3*dA/h(x)^2 across a face whose size grades within the face.

        Samples a parametric grid, weights each sample by the local area
        Jacobian, and normalises the sampled area to the true (trimmed) face
        area so trimmed/periodic param domains stay accurate. This is the
        per-sample integral the spec calls for; a single centroid size
        underpredicts a graded face by ~20 %.
        """
        try:
            bounds_min, bounds_max = gmsh.model.getParametrizationBounds(2, surface)
            umin, vmin = float(bounds_min[0]), float(bounds_min[1])
            umax, vmax = float(bounds_max[0]), float(bounds_max[1])
        except Exception:
            size_mm = _graded_size_at(
                tuple(float(v) for v in gmsh.model.occ.getCenterOfMass(2, surface))
            )
            return sizing.TRIANGLES_PER_AREA_OVER_H2 * area / (size_mm * size_mm) if size_mm > 0 else 0.0
        du = (umax - umin) / n
        dv = (vmax - vmin) / n
        params: list[float] = []
        for i in range(n):
            u = umin + (i + 0.5) * du
            for j in range(n):
                params.extend((u, vmin + (j + 0.5) * dv))
        coords = gmsh.model.getValue(2, surface, params)
        derivs = gmsh.model.getDerivative(2, surface, params)
        sampled_area = 0.0
        weighted = 0.0
        for k in range(len(params) // 2):
            xyz = (coords[3 * k], coords[3 * k + 1], coords[3 * k + 2])
            ru = derivs[6 * k : 6 * k + 3]
            rv = derivs[6 * k + 3 : 6 * k + 6]
            cross = (
                ru[1] * rv[2] - ru[2] * rv[1],
                ru[2] * rv[0] - ru[0] * rv[2],
                ru[0] * rv[1] - ru[1] * rv[0],
            )
            jac = (cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) ** 0.5
            d_area = jac * du * dv
            if d_area <= 0.0:
                continue
            size_mm = _graded_size_at(xyz)
            sampled_area += d_area
            if size_mm > 0.0:
                weighted += d_area / (size_mm * size_mm)
        if sampled_area <= 0.0:
            return 0.0
        scale = area / sampled_area  # normalise to the true trimmed area
        return sizing.TRIANGLES_PER_AREA_OVER_H2 * scale * weighted

    regions: list[sizing.Region] = []
    # Source patches: radiating, at their explicit per-source dial.
    for spec in active_source_specs:
        field = density["source_fields"][spec.name]
        size_mm = float(field["patch_size_mm"])
        area = sum(float(gmsh.model.occ.getMass(2, s)) for s in source_surfaces[spec.name])
        regions.append(sizing.Region(area_mm2=area, size_mm=size_mm, label=sizing.ROLE_RADIATING, role=sizing.ROLE_RADIATING))

    radiating_size = float(radiating_ceiling) if radiating_ceiling is not None else finest_source_size
    # Near-field triangle counts are accumulated from per-face graded sampling,
    # so they are added as a pre-summed pseudo-region with the coarsest planned
    # size (shadow) driving the reported near-field valid band.
    near_field_triangles = 0.0
    for surface in rigid_surfaces:
        area = float(gmsh.model.occ.getMass(2, surface))
        if area <= 0.0:
            continue
        if surface in refine_surface_to_size:
            role, size_mm = refine_surface_to_size[surface]
            regions.append(sizing.Region(area_mm2=area, size_mm=size_mm, label=role, role=role))
        elif surface in auto_radiating:
            regions.append(
                sizing.Region(area_mm2=area, size_mm=radiating_size, label=sizing.ROLE_RADIATING, role=sizing.ROLE_RADIATING)
            )
        else:
            near_field_triangles += _graded_triangles(surface, area)
    if near_field_triangles > 0.0:
        # Encode the graded near-field count as an equivalent uniform region so
        # the estimator's per-role bucketing and valid-band reporting hold.
        equiv_size = max(shadow_res, finest_source_size)
        equiv_area = near_field_triangles * equiv_size * equiv_size / sizing.TRIANGLES_PER_AREA_OVER_H2
        regions.append(
            sizing.Region(area_mm2=equiv_area, size_mm=equiv_size, label=sizing.ROLE_NEAR_FIELD, role=sizing.ROLE_NEAR_FIELD)
        )

    freq_count = 1  # per-frequency cost; the solve sweeps many, reported separately
    estimate = sizing.estimate_mesh_cost(regions, freq_count=freq_count)
    payload = estimate.to_dict()
    payload["formula"] = "N ~= 2.3 * sum(area_mm2 / size_mm^2) over the quarter model"
    payload["quarter_model"] = symmetry_planes not in ((), "auto") or bool(symmetry_planes)
    payload["region_count"] = len(regions)
    payload["planned_radiating_size_mm"] = radiating_size
    payload["planned_shadow_size_mm"] = float(shadow_res)
    payload["matrix_ram_gb"] = round(estimate.ram_gb, 3)
    payload["note"] = (
        "matrix RAM = N^2 * 16 bytes (dense complex128); solve time per "
        "frequency is calibrated from the 260612 study, with an O(N^3) upper "
        "bound. Multiply by the solve frequency count for the full sweep."
    )
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Named source and mesh size, e.g. LF:20 or HF:5:4. Repeat per source.",
    )
    parser.add_argument("--transition-mm", type=float, default=200.0)
    parser.add_argument(
        "--rigid-res-mm",
        "--global-res-mm",
        dest="rigid_res_mm",
        type=float,
        default=None,
        help=(
            "Mesh size for rigid body surfaces away from source refinement. "
            "Defaults to the coarsest declared source resolution."
        ),
    )
    parser.add_argument("--quadrants", type=int, default=1234, choices=(1, 12, 14, 1234))
    parser.add_argument(
        "--symmetry-planes",
        default=None,
        help=(
            "Comma-separated symmetry cut planes: x0, y0, z0. Aliases: "
            "left-right, front-back, top-bottom, none. 'auto' detects the cut "
            "planes from free edges on the coordinate planes. Overrides "
            "--quadrants."
        ),
    )
    parser.add_argument(
        "--unit-scale-to-m",
        type=float,
        default=0.001,
        help="Scale from STEP units to metres for WG output meshes. Fusion STEP is usually mm -> 0.001.",
    )
    parser.add_argument("--topology-tol", type=float, default=DEFAULT_TOPOLOGY_TOL)
    parser.add_argument(
        "--requested-max-frequency-hz",
        "--f-max-hz",
        dest="requested_max_frequency_hz",
        type=float,
        default=None,
        help=(
            "Band top in Hz. Drives both the frequency-derived element sizing "
            "(size = min(mm_knob, c/(epw_role * f_max))) and the conservative "
            "mesh frequency validation. When omitted, sizing falls back to the "
            "mm knobs only."
        ),
    )
    parser.add_argument(
        "--mesh-sizing-mode",
        choices=("frequency-role", "manual-mm"),
        default="manual-mm",
        help=(
            "manual-mm (default) uses explicit millimetre caps for sizing "
            "while still validating the finished mesh against the requested "
            "max frequency; frequency-role refines source/radiating/shadow "
            "roles from the requested band top."
        ),
    )
    parser.add_argument(
        "--radiating-epw",
        type=float,
        default=DEFAULT_RADIATING_EPW,
        help=(
            "Elements per wavelength on radiating surfaces (waveguide flare and "
            "source patches). The main accuracy/size lever; default 6."
        ),
    )
    parser.add_argument(
        "--shadow-epw",
        type=float,
        default=DEFAULT_SHADOW_EPW,
        help=(
            "Elements per wavelength on shadowed rear/outer/far surfaces; rides "
            "near the 2 e/w Nyquist floor (default 2.5) to minimise elements "
            "where the field is weak."
        ),
    )
    parser.add_argument(
        "--throat-epw",
        type=float,
        default=DEFAULT_THROAT_EPW,
        help="Elements per wavelength at the throat (tiny area, cheap to refine; default 8).",
    )
    parser.add_argument(
        "--refine",
        action="append",
        default=[],
        help=(
            "Per-face mesh-size override on a painted appearance/shell name, "
            "kept physically rigid. NAME:EPW (elements/wavelength), NAME:<num>mm "
            "(explicit size), or NAME:ROLE (radiating/shadow/throat/near). "
            "Repeat per group."
        ),
    )
    parser.add_argument(
        "--curvature-segments",
        type=float,
        default=0.0,
        help=(
            "gmsh Mesh.MeshSizeFromCurvature segments per 2*pi (0 disables). "
            "CAUTION: high values on OCC bspline shells can stall gmsh; keep "
            "small and time-boxed."
        ),
    )
    parser.add_argument(
        "--mesh-frequency-elements-per-wavelength",
        type=float,
        default=FREQUENCY_ELEMENTS_PER_WAVELENGTH,
        help="Elements per wavelength used to validate the global max edge length.",
    )
    parser.add_argument(
        "--speed-of-sound-m-s",
        type=float,
        default=SPEED_OF_SOUND_M_S,
        help="Speed of sound used for mesh frequency validation.",
    )
    parser.add_argument("--allow-leaks", action="store_true")
    parser.add_argument(
        "--skip-missing-sources",
        action="store_true",
        help=(
            "Ignore requested sources whose STEP shell/style name is absent. "
            "At least one requested source must still be found."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.source:
        raise SystemExit("at least one --source NAME:RES_MM is required")
    if args.transition_mm <= 0.0:
        raise SystemExit("--transition-mm must be positive")
    if args.unit_scale_to_m <= 0.0:
        raise SystemExit("--unit-scale-to-m must be positive")
    if args.requested_max_frequency_hz is not None and args.requested_max_frequency_hz <= 0.0:
        raise SystemExit("--requested-max-frequency-hz must be positive")
    if args.mesh_frequency_elements_per_wavelength <= 0.0:
        raise SystemExit("--mesh-frequency-elements-per-wavelength must be positive")
    if args.speed_of_sound_m_s <= 0.0:
        raise SystemExit("--speed-of-sound-m-s must be positive")
    for epw_name, epw_value in (
        ("--radiating-epw", args.radiating_epw),
        ("--shadow-epw", args.shadow_epw),
        ("--throat-epw", args.throat_epw),
    ):
        if epw_value <= 0.0:
            raise SystemExit(f"{epw_name} must be positive")
    if args.curvature_segments < 0.0:
        raise SystemExit("--curvature-segments must be >= 0")
    f_max_hz = (
        args.requested_max_frequency_hz
        if args.mesh_sizing_mode == "frequency-role"
        else None
    )
    try:
        refine_specs = [
            _parse_refine_spec(
                raw,
                radiating_epw=args.radiating_epw,
                shadow_epw=args.shadow_epw,
                throat_epw=args.throat_epw,
            )
            for raw in args.refine
        ]
    except argparse.ArgumentTypeError as exc:
        raise SystemExit(str(exc)) from exc
    symmetry_auto = (
        args.symmetry_planes is not None
        and args.symmetry_planes.strip().lower() == "auto"
    )
    if symmetry_auto:
        symmetry_planes: tuple[str, ...] | str = "auto"
    else:
        try:
            symmetry_planes = _parse_symmetry_planes(args.symmetry_planes, quadrants=args.quadrants)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    source_specs = [_parse_source_spec(raw, i) for i, raw in enumerate(args.source)]
    if len({spec.name for spec in source_specs}) != len(source_specs):
        raise SystemExit("source names must be unique")
    if len({spec.tag for spec in source_specs}) != len(source_specs):
        raise SystemExit("source tags must be unique")

    step_path = args.step.expanduser().resolve()
    out_dir = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tagged_mesh_path = out_dir / "tagged_sources.msh"

    rigid_res = args.rigid_res_mm
    if rigid_res is None:
        rigid_res = max(spec.resolution_mm for spec in source_specs)
    if rigid_res <= 0.0:
        raise SystemExit("--rigid-res-mm must be positive")

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 1)
        gmsh.option.setNumber("Geometry.OCCMakeSolids", 0)
        gmsh.open(str(step_path))
        gmsh.model.occ.synchronize()

        source_surfaces = _map_step_faces_to_gmsh_surfaces(
            step_path,
            source_specs,
            skip_missing_sources=args.skip_missing_sources,
        )
        active_source_specs = [
            spec for spec in source_specs
            if spec.name in source_surfaces
        ]
        all_surfaces = [tag for dim, tag in sorted(gmsh.model.getEntities(2))]
        source_surface_set = {
            tag for tags_for_source in source_surfaces.values() for tag in tags_for_source
        }
        rigid_surfaces = [tag for tag in all_surfaces if tag not in source_surface_set]
        if not rigid_surfaces:
            raise RuntimeError("no rigid surfaces remain after source classification")

        gmsh.model.addPhysicalGroup(2, rigid_surfaces, tag=RIGID_TAG, name="rigid")
        for spec in active_source_specs:
            gmsh.model.addPhysicalGroup(
                2,
                source_surfaces[spec.name],
                tag=spec.tag,
                name=spec.name,
            )

        # Resolve painted refine groups and auto-classify radiating (flare)
        # surfaces from the STEP body/shell structure.
        refine_surfaces, refine_origins = _map_refine_groups_to_gmsh_surfaces(
            step_path, refine_specs, all_surfaces
        )
        shell_surfaces = _named_shell_gmsh_surfaces(step_path, all_surfaces)
        auto_radiating = _auto_radiating_surfaces(shell_surfaces, source_surface_set)
        refined_surface_set = {
            tag for tags_for_group in refine_surfaces.values() for tag in tags_for_group
        }
        # Don't auto-grade surfaces the user explicitly painted.
        auto_radiating -= refined_surface_set

        density = _density_configuration(
            active_source_specs,
            mesh_sizing_mode=args.mesh_sizing_mode,
            rigid_res_mm=rigid_res,
            transition_mm=args.transition_mm,
            f_max_hz=f_max_hz,
            radiating_epw=args.radiating_epw,
            shadow_epw=args.shadow_epw,
            throat_epw=args.throat_epw,
            refine_specs=refine_specs,
            refine_surfaces=refine_surfaces,
            curvature_segments=args.curvature_segments,
        )

        radiating_ceiling = sizing.frequency_ceiling_mm(args.radiating_epw, f_max_hz)
        shadow_res = float(density["shadow_res_mm"])
        # Finest planned size pins MeshSizeMin; the shadow background caps
        # everything not pulled finer by a field.
        planned_min = min(
            [float(field["patch_size_mm"]) for field in density["source_fields"].values()]
            + [float(field["size_min_mm"]) for field in density["source_fields"].values()]
            + [float(f["size_mm"]) for f in density["refine_fields"].values()]
            + ([float(radiating_ceiling)] if (radiating_ceiling and auto_radiating) else [])
        ) if density["source_fields"] or density["refine_fields"] else rigid_res
        gmsh.option.setNumber("Mesh.MeshSizeMin", max(planned_min, 0.0))
        gmsh.option.setNumber("Mesh.MeshSizeMax", shadow_res)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", density["mesh_size_from_curvature"])
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", density["mesh_size_extend_from_boundary"])
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", density["mesh_size_from_points"])
        gmsh.option.setNumber("Mesh.Algorithm", density["mesh_algorithm"])

        def _add_restrict_field(size_mm: float, surfaces: list[int]) -> int:
            constant = gmsh.model.mesh.field.add("MathEval")
            gmsh.model.mesh.field.setString(constant, "F", repr(float(size_mm)))
            restrict = gmsh.model.mesh.field.add("Restrict")
            gmsh.model.mesh.field.setNumber(restrict, "InField", constant)
            for key in ("SurfacesList", "FacesList"):
                try:
                    gmsh.model.mesh.field.setNumbers(restrict, key, surfaces)
                except Exception:
                    continue
            return restrict

        fields: list[int] = []
        for spec in active_source_specs:
            field_cfg = density["source_fields"][spec.name]
            patch_size = float(field_cfg["patch_size_mm"])
            nearfield_min = float(field_cfg["size_min_mm"])
            boundaries = []
            for surface in source_surfaces[spec.name]:
                boundaries.extend(
                    gmsh.model.getBoundary([(2, surface)], combined=False, recursive=True)
                )
            gmsh.model.mesh.setSize(boundaries, min(patch_size, nearfield_min))

            distance = gmsh.model.mesh.field.add("Distance")
            try:
                gmsh.model.mesh.field.setNumbers(distance, "FacesList", source_surfaces[spec.name])
            except Exception:
                gmsh.model.mesh.field.setNumbers(distance, "SurfacesList", source_surfaces[spec.name])
            gmsh.model.mesh.field.setNumber(distance, "Sampling", 100)

            # Near-field/baffle fallback: grade from the radiating wall size out
            # to the shadow background over the transition distance.
            threshold = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(threshold, "InField", distance)
            gmsh.model.mesh.field.setNumber(threshold, "SizeMin", nearfield_min)
            gmsh.model.mesh.field.setNumber(threshold, "SizeMax", shadow_res)
            gmsh.model.mesh.field.setNumber(threshold, "DistMin", 0.0)
            gmsh.model.mesh.field.setNumber(threshold, "DistMax", args.transition_mm)
            fields.append(threshold)

            # Pin the patch at its explicit per-source dial. gmsh's Distance
            # field is sample-based, so the interior of a large source face
            # would otherwise drift coarser than the dialled size.
            fields.append(_add_restrict_field(patch_size, source_surfaces[spec.name]))

        # Radiating flare auto-classified from the body/shell structure: pin the
        # radiating size regardless of distance so the mouth (far from the
        # throat but the primary radiator) stays fine.
        if auto_radiating and radiating_ceiling is not None:
            fields.append(_add_restrict_field(float(radiating_ceiling), sorted(auto_radiating)))

        # Painted refine overrides.
        for spec in refine_specs:
            surfaces = refine_surfaces.get(spec.name)
            if not surfaces:
                continue
            size_mm = spec.size_for_band(f_max_hz, fallback_mm=rigid_res)
            fields.append(_add_restrict_field(size_mm, surfaces))

        if len(fields) == 1:
            gmsh.model.mesh.field.setAsBackgroundMesh(fields[0])
        elif fields:
            min_field = gmsh.model.mesh.field.add("Min")
            gmsh.model.mesh.field.setNumbers(min_field, "FieldsList", fields)
            gmsh.model.mesh.field.setAsBackgroundMesh(min_field)

        # Pre-mesh size/cost prediction from OCC face areas and the planned
        # size field (no gmsh meshing yet).
        mesh_size_prediction = _predict_mesh_size_cost(
            source_surfaces=source_surfaces,
            rigid_surfaces=rigid_surfaces,
            auto_radiating=auto_radiating,
            refine_surfaces=refine_surfaces,
            refine_specs=refine_specs,
            active_source_specs=active_source_specs,
            density=density,
            f_max_hz=f_max_hz,
            transition_mm=args.transition_mm,
            radiating_ceiling=radiating_ceiling,
            shadow_res=shadow_res,
            radiating_epw=args.radiating_epw,
            symmetry_planes=symmetry_planes,
        )

        gmsh.model.mesh.generate(2)
        duplicate_node_stats = _remove_duplicate_nodes_for_current_gmsh_model()
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        gmsh.option.setNumber("Mesh.Binary", 0)
        gmsh.write(str(tagged_mesh_path))

        source_diag = {
            spec.name: {
                "tag": spec.tag,
                "resolution_mm": spec.resolution_mm,
                "classification_origin": getattr(
                    _map_step_faces_to_gmsh_surfaces,
                    "last_origins",
                    {},
                ).get(spec.name, "unknown"),
                "surfaces": source_surfaces[spec.name],
                "surface_diagnostics": _surface_diagnostics(source_surfaces[spec.name]),
            }
            for spec in active_source_specs
        }
        skipped_sources = getattr(_map_step_faces_to_gmsh_surfaces, "last_missing", {})
        refine_diag = {
            spec.name: {
                "role": spec.role,
                "epw": spec.epw,
                "size_mm": spec.size_for_band(f_max_hz, fallback_mm=rigid_res),
                "classification_origin": refine_origins.get(spec.name, "unmatched"),
                "matched": spec.name in refine_surfaces,
                "surfaces": refine_surfaces.get(spec.name, []),
            }
            for spec in refine_specs
        }
        role_classification = {
            "auto_radiating_surfaces": sorted(int(s) for s in auto_radiating),
            "named_shell_count": len(shell_surfaces),
            "rigid_surface_count": len(rigid_surfaces),
        }
    finally:
        gmsh.finalize()

    mesh = meshio.read(tagged_mesh_path)
    repaired_mesh, repair_stats, topology = _postprocess_mesh(
        mesh,
        active_source_specs,
        symmetry_planes=symmetry_planes,
        tolerance=args.topology_tol,
    )
    resolved_symmetry_planes = tuple(topology["expected_symmetry_planes"])
    meshio.write(tagged_mesh_path, repaired_mesh, file_format="gmsh22", binary=False)
    points, triangles, tags = _mesh_triangle_data(repaired_mesh)
    frequency_validation = _mesh_frequency_validation(
        points,
        triangles,
        tags,
        active_source_specs,
        unit_scale_to_m=args.unit_scale_to_m,
        requested_max_frequency_hz=args.requested_max_frequency_hz,
        transition_mm=args.transition_mm,
        elements_per_wavelength=args.mesh_frequency_elements_per_wavelength,
        speed_of_sound_m_s=args.speed_of_sound_m_s,
    )
    # Radiating-surface band: the patch-only limit of each source (and any
    # radiating refine group), undragged by intentionally coarse shadow walls.
    # This is the trustworthy line deliverable C overlays on the response plots.
    radiating_patch_limits = [
        float(entry["max_valid_frequency_hz"])
        for entry in frequency_validation.get("per_source", {}).values()
        if float(entry.get("max_valid_frequency_hz", 0.0)) > 0.0
    ]
    frequency_validation["radiating_valid_freq_max_hz"] = (
        min(radiating_patch_limits) if radiating_patch_limits else None
    )
    frequency_validation["per_source_radiating_valid_freq_max_hz"] = {
        name: float(entry["max_valid_frequency_hz"])
        for name, entry in frequency_validation.get("per_source", {}).items()
        if float(entry.get("max_valid_frequency_hz", 0.0)) > 0.0
    }

    unique_tags, tag_counts = np.unique(tags, return_counts=True)
    tag_counts_dict = {
        str(int(tag)): int(count)
        for tag, count in zip(unique_tags, tag_counts, strict=True)
    }
    # Close the prediction loop: compare the pre-mesh estimate to the actual
    # triangle count so the predictor's accuracy is recorded for every run.
    actual_triangles = int(sum(tag_counts_dict.values()))
    predicted_triangles = int(mesh_size_prediction.get("n_triangles", 0))
    mesh_size_prediction["actual_n_triangles"] = actual_triangles
    mesh_size_prediction["prediction_error_fraction"] = (
        round((predicted_triangles - actual_triangles) / actual_triangles, 4)
        if actual_triangles > 0
        else None
    )

    wg_meshes = _write_wg_source_meshes(
        tagged_mesh_path,
        out_dir,
        active_source_specs,
        unit_scale_to_m=args.unit_scale_to_m,
    )

    solver_ready = (
        topology["nonmanifold_edges"] == 0
        and topology["inconsistent_edges"] == 0
        and topology["unexpected_free_edges"] == 0
    )
    manifest = {
        "step": str(step_path),
        "tagged_mesh_step_units": str(tagged_mesh_path),
        "wg_source_meshes_m": wg_meshes,
        "quadrants": args.quadrants,
        "symmetry_planes": list(resolved_symmetry_planes),
        "symmetry_planes_mode": "auto" if symmetry_auto else "explicit",
        "unit_scale_to_m": args.unit_scale_to_m,
        "global_res_mm": rigid_res,
        "rigid_res_mm": rigid_res,
        "transition_mm": args.transition_mm,
        "density": density,
        "requested_sources": [
            {
                "name": spec.name,
                "tag": spec.tag,
                "resolution_mm": spec.resolution_mm,
            }
            for spec in source_specs
        ],
        "skipped_sources": skipped_sources,
        "sources": source_diag,
        "refine_groups": refine_diag,
        "role_classification": role_classification,
        "mesh_size_prediction": mesh_size_prediction,
        "physical_tag_triangle_counts": tag_counts_dict,
        "mesh_repair": {
            "gmsh_duplicate_nodes": duplicate_node_stats,
            **repair_stats,
        },
        "topology": topology,
        "mesh_frequency_validation": frequency_validation,
        "solver_ready": bool(solver_ready),
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(manifest, indent=2, sort_keys=True))
    if not solver_ready and not args.allow_leaks:
        print(
            "ERROR: mesh is not solver-ready; unexpected free/non-manifold edges "
            "were found. Re-export stitched/imprinted CAD or rerun with "
            "--allow-leaks for debugging only.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
