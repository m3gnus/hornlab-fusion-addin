#!/usr/bin/env python3
"""Prepare a named-source STEP surface model for Waveguide Generator Metal BEM.

The expected CAD pattern is:

* one acoustic boundary made from stitched/sewn surfaces,
* named source patches exported as STEP shell/surface model names,
* quarter-domain models aligned to WG Metal's quadrant convention when
  ``--quadrants`` is not ``1234``.

The script writes:

* ``tagged_sources.msh`` in the STEP units, carrying all named sources,
* ``manifest.json`` with topology and source mapping diagnostics.

A full (uncut) model can be reduced automatically with ``--symmetry-planes
auto-cut``: the OCC geometry is tested for mirror symmetry face by face, and
each plane that passes -- geometry *and* source roles -- is cut away, keeping
the positive side and leaving the boundary open on the plane. The mirror test
runs before meshing on purpose; see the block above ``_detect_symmetry_planes``.

It intentionally refuses to report solver-ready output when the mesh has free
edges away from the declared symmetry planes. Use ``--allow-leaks`` only for
debugging bad exports.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
import sys
from pathlib import Path

import gmsh
import meshio
import numpy as np

try:
    from hornlab_mesher.step_prepare import (
        DEFAULT_AUTO_CUT_GRID,
        DEFAULT_AUTO_CUT_TOLERANCE_REL,
        DEFAULT_SYMMETRY_SNAP_BAND_MM,
        OccSurfaceGroup,
        OccSurfaceRole,
        OccSurfaceSelector,
        auto_cut_occ_geometry,
        evaluate_occ_plane_symmetry,
        millimetres_to_step_units,
        remap_surface_tags,
        sample_occ_surface_points,
        snap_symmetry_plane_vertices,
    )
    from hornlab_mesher.step_import import (
        DEGENERATE_MIN_QUALITY,
        FREQUENCY_ELEMENTS_PER_WAVELENGTH,
        OCC_HEALING_FALLBACKS,
        RIGID_TAG,
        SPEED_OF_SOUND_M_S,
        SurfaceGeometry,
        StepFaceGroup,
        StepLabelSelector,
        _compact_unused_vertices,
        _detect_symmetry_planes,
        _edge_direction_stats,
        _mesh_triangle_data,
        _normalize_to_positive_side,
        _remove_degenerate_triangles,
        _repair_triangle_winding,
        _signed_volume,
        _symmetry_source_normal_projection,
        _topology_stats,
        _triangle_edge_lengths,
        _weld_near_duplicate_vertices,
        advanced_face_order as _advanced_face_order,
        anchor_surface_order as _anchor_surface_order,
        gmsh_surface_geometries as _gmsh_surface_geometries,
        gmsh_surface_tags as _gmsh_surface_tags,
        map_optional_step_face_groups,
        map_step_face_groups,
        mesh_frequency_validation as _mesh_frequency_validation,
        named_shell_gmsh_surfaces as _named_shell_gmsh_surfaces,
        parse_named_shell_faces as _parse_named_shell_faces,
        parse_solid_brep_faces as _parse_solid_brep_faces,
        parse_styled_face_groups as _parse_styled_face_groups,
        postprocess_mesh as _postprocess_mesh,
        run_occ_healing_fallbacks as _run_occ_healing_fallbacks,
    )
except (ImportError, ModuleNotFoundError) as exc:
    missing = exc.name or ""
    if missing != "hornlab_mesher" and not missing.startswith("hornlab_mesher."):
        raise
    raise RuntimeError(
        "a compatible hornlab-waveguide-mesher is required to prepare STEP "
        "geometry; install or update the exact revision pinned in this add-in's "
        "requirements.txt"
    ) from exc

from hornlab_mesher import mesh_sizing as sizing


SOURCE_TAG_BASE = 2
DEFAULT_TOPOLOGY_TOL = 1e-5
HEALED_SYMMETRY_BAND_MM = DEFAULT_SYMMETRY_SNAP_BAND_MM

SYMMETRY_AXIS_FOR_PLANE = {"x0": 0, "y0": 1, "z0": 2}
# Mirror-test tolerance as a fraction of the sampled model diagonal. An
# absolute micron threshold is wrong at both ends: a circular horn was once
# rejected over 0.08 mm of CAD discretisation noise, while the asro68 test
# model mirrors to 0.0025 mm. At 5e-4 of a ~720 mm diagonal this is 0.36 mm,
# which passes both by a wide margin and still stays two orders of magnitude
# below the finest mesh size any of these models is meshed at (4 mm), so an
# asymmetry small enough to pass is an asymmetry the BEM cannot resolve.
AUTO_REDUCE_TOL_REL = DEFAULT_AUTO_CUT_TOLERANCE_REL
AUTO_REDUCE_GRID = DEFAULT_AUTO_CUT_GRID


def _millimetres_to_step_units(value_mm: float, unit_scale_to_m: float) -> float:
    """Convert a physical millimetre tolerance to the imported STEP units."""
    return millimetres_to_step_units(value_mm, unit_scale_to_m)


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


@dataclass(frozen=True)
class RefineSpec:
    """Per-face mesh-size override painted via a Fusion appearance/shell name.

    A refine group stays physically rigid (tag 1); it only restricts the local
    mesh size to an explicit millimetre value.
    """

    name: str
    size_mm: float
    role: str = "custom"


def _parse_refine_spec(raw: str) -> RefineSpec:
    """Parse ``--refine NAME:VALUE``.

    ``VALUE`` is ``<num>mm`` for an explicit size ceiling in millimetres.
    """
    parts = [part.strip() for part in raw.split(":")]
    if len(parts) != 2 or not parts[0]:
        raise argparse.ArgumentTypeError("--refine expects NAME:RES_MMmm")
    name, value = parts[0], parts[1].lower()
    if not value.endswith("mm"):
        raise argparse.ArgumentTypeError("--refine expects NAME:RES_MMmm")
    try:
        size_mm = float(value[:-2])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --refine size: {raw!r}") from exc
    if size_mm <= 0.0:
        raise argparse.ArgumentTypeError(f"--refine size must be positive: {raw!r}")
    return RefineSpec(name=name, size_mm=size_mm)


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


def _step_face_groups(source_specs: list[SourceSpec]) -> list[StepFaceGroup]:
    """Apply the add-in's source tag policy to caller-neutral selectors.

    A source is matched by its own painted label and nothing else. There used
    to be a fallback here -- a requested PORT_EXIT_L/_R resolving to the generic
    PORT_EXIT when the generic name was not itself requested -- removed
    2026-08-11 after it was measured never to fire: across 471 recorded prepare
    runs, the only two that asked for PORT_EXIT_L resolved it directly from an
    appearance/style of that exact name.
    """
    return [
        StepFaceGroup(
            name=spec.name,
            selector=StepLabelSelector(spec.name),
            role=OccSurfaceRole("source"),
            tag=spec.tag,
            resolution_mm=spec.resolution_mm,
        )
        for spec in source_specs
    ]


def _map_step_faces_to_gmsh_surfaces(
    step_path: Path,
    source_specs: list[SourceSpec],
    *,
    skip_missing_sources: bool = False,
    gmsh_surfaces: list[int] | None = None,
) -> dict[str, list[int]]:
    """Apply add-in source policy through the caller-neutral STEP mapper."""
    groups = _step_face_groups(source_specs)
    result = map_step_face_groups(
        step_path,
        groups,
        skip_missing_groups=True,
        gmsh_surfaces=gmsh_surfaces,
        named_faces=_parse_named_shell_faces(step_path),
        styled_faces=_parse_styled_face_groups(step_path),
        face_order=_advanced_face_order(step_path),
    )

    if not skip_missing_sources:
        for group in groups:
            if group.name in result.missing_reasons:
                raise RuntimeError(
                    result.missing_reasons[group.name].replace(
                        "group ", "source ", 1
                    )
                )

    missing = {
        group.name: {
            "tag": group.tag,
            "resolution_mm": group.resolution_mm,
            "reason": result.missing_reasons[group.name].replace(
                "group ", "source ", 1
            ),
        }
        for group in groups
        if group.name in result.missing_reasons
    }
    if not result.surfaces:
        requested = ", ".join(spec.name for spec in source_specs)
        first_reason = result.missing_reasons[source_specs[0].name].replace(
            "group ", "source ", 1
        )
        raise RuntimeError(
            f"none of the requested sources were found in the STEP export: {requested}. "
            f"{first_reason}"
        )

    setattr(_map_step_faces_to_gmsh_surfaces, "last_origins", result.origins)
    setattr(_map_step_faces_to_gmsh_surfaces, "last_missing", missing)
    return result.surfaces


def _map_refine_groups_to_gmsh_surfaces(
    step_path: Path,
    refine_specs: list[RefineSpec],
    gmsh_surfaces: list[int],
) -> tuple[dict[str, list[int]], dict[str, str]]:
    """Apply optional add-in refinement labels through the generic mapper."""
    groups = [
        StepFaceGroup(
            name=spec.name,
            selector=StepLabelSelector(spec.name),
            role=OccSurfaceRole(spec.role),
            resolution_mm=spec.size_mm,
        )
        for spec in refine_specs
    ]
    return map_optional_step_face_groups(step_path, groups, gmsh_surfaces)


def _auto_reduce_geometry(
    *,
    surfaces: list[int],
    roles: dict[int, str],
    grid: int,
    tolerance_rel: float,
) -> tuple[tuple[str, ...], dict[int, list[int]], dict[str, object]]:
    """Adapt add-in surface assignments to the caller-neutral mesher API."""
    grouped: dict[str, list[int]] = defaultdict(list)
    for surface in surfaces:
        grouped[roles[surface]].append(surface)
    groups = [
        OccSurfaceGroup(
            name=f"role-{index}",
            selector=OccSurfaceSelector(group_surfaces),
            role=OccSurfaceRole(role),
        )
        for index, (role, group_surfaces) in enumerate(grouped.items())
    ]
    result = auto_cut_occ_geometry(
        groups, grid=grid, tolerance_rel=tolerance_rel
    )
    return result.planes, result.parent_to_children, result.report


_evaluate_plane_symmetry = evaluate_occ_plane_symmetry
_sample_surface_points = sample_occ_surface_points
_remap_after_cut = remap_surface_tags


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


def _source_size_min_mm(spec: SourceSpec) -> float:
    """Radiating size of a source patch and the wall grading start around it.

    The source patch is a radiating surface, but manual-mm sizing uses the
    source's explicit millimetre dial directly.
    """
    return sizing.role_size_mm(
        sizing.ROLE_RADIATING,
        mm_knob_mm=spec.resolution_mm,
    )


def _shadow_size_mm(*, rigid_res_mm: float) -> float:
    """Coarse background size for far/shadow surfaces."""
    return sizing.role_size_mm(
        sizing.ROLE_SHADOW,
        mm_knob_mm=rigid_res_mm,
    )


def _density_configuration(
    source_specs: list[SourceSpec],
    *,
    rigid_res_mm: float,
    transition_mm: float,
    refine_specs: list[RefineSpec] | None = None,
    refine_surfaces: dict[str, list[int]] | None = None,
    curvature_segments: int = 0,
) -> dict[str, object]:
    """Describe the planned size field by acoustic role.

    The near-field/baffle is graded by distance from each source patch, from
    the source's explicit radiating size up to the shadow background size, so
    the baffle stays medium rather than being coarsened to shadow.
    Far surfaces relax to the shadow background (``Mesh.MeshSizeMax``).
    Painted refine groups pin a constant size on named faces while keeping
    them rigid.
    """
    refine_specs = refine_specs or []
    refine_surfaces = refine_surfaces or {}
    shadow_res = _shadow_size_mm(rigid_res_mm=rigid_res_mm)
    config: dict[str, object] = {
        "groups": ["rigid", *[spec.name for spec in source_specs]],
        "mesh_size_extend_from_boundary": 0,
        "mesh_size_from_curvature": int(curvature_segments),
        "mesh_size_from_points": 0,
        "mesh_algorithm": 6,
        "mesh_sizing_mode": "manual-mm",
        "rigid_res_mm": float(rigid_res_mm),
        "transition_mm": float(transition_mm),
        "shadow_res_mm": float(shadow_res),
        "source_fields": {
            spec.name: {
                "tag": int(spec.tag),
                "resolution_mm": float(spec.resolution_mm),
                "field": "Distance/Threshold",
                "role": sizing.ROLE_RADIATING,
                "dist_min_mm": 0.0,
                "dist_max_mm": float(transition_mm),
                "patch_size_mm": _source_size_min_mm(spec),
                "size_min_mm": _source_size_min_mm(spec),
                "size_max_mm": float(shadow_res),
            }
            for spec in source_specs
        },
        "refine_fields": {
            spec.name: {
                "field": "Restrict",
                "role": spec.role,
                "size_mm": spec.size_mm,
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
    refine_surfaces: dict[str, list[int]],
    refine_specs: list[RefineSpec],
    active_source_specs: list[SourceSpec],
    density: dict[str, object],
    transition_mm: float,
    shadow_res: float,
    symmetry_planes: tuple[str, ...] | str,
) -> dict[str, object]:
    """Predict triangles/RAM/solve cost from OCC face areas before meshing.

    Each surface is assigned its planned element size by acoustic role (the
    same field the mesher applies): source patches at their explicit mm size,
    painted refine groups at their explicit mm size, and the near-field/baffle
    at the distance-graded size evaluated at the face centroid. ``N ~= 2.3 *
    sum(area / size^2)`` over the quarter model.
    """
    refine_by_name = {spec.name: spec for spec in refine_specs}
    refine_surface_to_size: dict[int, tuple[str, float]] = {}
    for name, surfaces in refine_surfaces.items():
        spec = refine_by_name.get(name)
        if spec is None:
            continue
        size_mm = spec.size_mm
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
    payload["planned_radiating_size_mm"] = float(finest_source_size)
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
            "planes from free edges on the coordinate planes of an "
            "already-cut model. 'auto-cut' additionally tests a full model's "
            "geometry for mirror symmetry and cuts it to the positive side "
            "before meshing. Overrides --quadrants."
        ),
    )
    parser.add_argument(
        "--auto-reduce-tol-rel",
        type=float,
        default=AUTO_REDUCE_TOL_REL,
        help=(
            "Mirror-test tolerance for --symmetry-planes auto-cut, as a "
            "fraction of the sampled model diagonal."
        ),
    )
    parser.add_argument(
        "--auto-reduce-grid",
        type=int,
        default=AUTO_REDUCE_GRID,
        help=(
            "Per-face parametric sampling grid used by the auto-cut mirror "
            "test (N x N samples per face before trim filtering)."
        ),
    )
    parser.add_argument(
        "--unit-scale-to-m",
        type=float,
        default=0.001,
        help=(
            "Scale from STEP units to metres for mesh-frequency validation. "
            "Fusion STEP is usually mm -> 0.001."
        ),
    )
    parser.add_argument("--topology-tol", type=float, default=DEFAULT_TOPOLOGY_TOL)
    parser.add_argument(
        "--requested-max-frequency-hz",
        "--f-max-hz",
        dest="requested_max_frequency_hz",
        type=float,
        default=None,
        help=(
            "Band top in Hz used for conservative mesh frequency validation. "
            "Mesh sizing itself uses the explicit millimetre values."
        ),
    )
    parser.add_argument(
        "--refine",
        action="append",
        default=[],
        help=(
            "Per-face mesh-size override on a painted appearance/shell name, "
            "kept physically rigid. Use NAME:<num>mm. Repeat per group."
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
    parser.add_argument(
        "--exclude-solid-breps",
        action="store_true",
        help=(
            "Exclude every MANIFOLD_SOLID_BREP/BREP_WITH_VOIDS face from the "
            "exterior BEM mesh. Used when the root Fusion export also contains "
            "a separate FEM air-volume component."
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
    if args.curvature_segments < 0.0:
        raise SystemExit("--curvature-segments must be >= 0")
    try:
        refine_specs = [_parse_refine_spec(raw) for raw in args.refine]
    except argparse.ArgumentTypeError as exc:
        raise SystemExit(str(exc)) from exc
    if args.auto_reduce_tol_rel <= 0.0:
        raise SystemExit("--auto-reduce-tol-rel must be positive")
    if args.auto_reduce_grid < 2:
        raise SystemExit("--auto-reduce-grid must be >= 2")
    symmetry_mode = (
        args.symmetry_planes.strip().lower()
        if args.symmetry_planes is not None
        else ""
    )
    # 'auto-cut' is a third mode of the same knob rather than an independent
    # --auto-reduce flag on purpose: symmetry already has exactly one source of
    # truth here, and a separate boolean would make '--auto-reduce
    # --symmetry-planes x0' a state with two contradictory answers.
    auto_reduce = symmetry_mode == "auto-cut"
    symmetry_auto = symmetry_mode == "auto" or auto_reduce
    if symmetry_auto:
        # After an auto-cut the reduced mesh is re-read by the free-edge
        # detector, exactly as an already-cut CAD model would be. That makes
        # the cut self-checking: the planes it reports back must match the
        # planes that were cut.
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

    def _run_gmsh_attempt(
        *,
        occ_healing_options: tuple[str, ...] = (),
        surface_order_reference: list[SurfaceGeometry] | None = None,
    ) -> dict[str, object]:
        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 1)
            gmsh.option.setNumber("Geometry.OCCMakeSolids", 0)
            for option_name in occ_healing_options:
                gmsh.option.setNumber(option_name, 1)
            gmsh.open(str(step_path))
            gmsh.model.occ.synchronize()
            sorted_surfaces = [tag for dim, tag in sorted(gmsh.model.getEntities(2))]
            surface_geoms = _gmsh_surface_geometries(sorted_surfaces)
            if surface_order_reference is None:
                ordered_surfaces = sorted_surfaces
            else:
                ordered_surfaces = _anchor_surface_order(
                    sorted_surfaces,
                    surface_geoms,
                    surface_order_reference,
                )

            source_surfaces = _map_step_faces_to_gmsh_surfaces(
                step_path,
                source_specs,
                skip_missing_sources=args.skip_missing_sources,
                gmsh_surfaces=ordered_surfaces,
            )
            excluded_surfaces: set[int] = set()
            if args.exclude_solid_breps:
                face_order = _advanced_face_order(step_path)
                face_to_surface = dict(zip(face_order, ordered_surfaces))
                excluded_surfaces = {
                    face_to_surface[face_id]
                    for face_id in _parse_solid_brep_faces(step_path)
                    if face_id in face_to_surface
                }
                source_surfaces = {
                    name: [surface for surface in surfaces if surface not in excluded_surfaces]
                    for name, surfaces in source_surfaces.items()
                }
                emptied = [name for name, surfaces in source_surfaces.items() if not surfaces]
                if emptied:
                    raise RuntimeError(
                        "requested sources exist only on excluded FEM solid BReps: "
                        + ", ".join(emptied)
                    )
            active_source_specs = [
                spec for spec in source_specs
                if spec.name in source_surfaces
            ]
            all_surfaces = [
                surface for surface in ordered_surfaces if surface not in excluded_surfaces
            ]
            source_surface_set = {
                tag for tags_for_source in source_surfaces.values() for tag in tags_for_source
            }

            # Resolve painted refine groups and the STEP body/shell structure
            # before any geometry cut: every one of these lookups is keyed on
            # the STEP face order, which a boolean invalidates.
            refine_surfaces, refine_origins = _map_refine_groups_to_gmsh_surfaces(
                step_path, refine_specs, ordered_surfaces
            )
            refine_surfaces = {
                name: [surface for surface in surfaces if surface not in excluded_surfaces]
                for name, surfaces in refine_surfaces.items()
            }
            refine_surfaces = {
                name: surfaces for name, surfaces in refine_surfaces.items() if surfaces
            }
            shell_surfaces = _named_shell_gmsh_surfaces(step_path, ordered_surfaces)
            shell_surfaces = {
                name: [surface for surface in surfaces if surface not in excluded_surfaces]
                for name, surfaces in shell_surfaces.items()
            }

            auto_reduce_report: dict[str, object] = {"mode": "off"}
            auto_reduce_planes: tuple[str, ...] = ()
            if auto_reduce:
                # Roles are the source name or 'rigid'. Refine groups are mesh
                # paint, not physics, so they do not get a vote; a painted
                # group that only existed on the discarded half simply shows up
                # emptied in the manifest's refine_groups block.
                roles = {surface: "rigid" for surface in all_surfaces}
                for name, surfaces in source_surfaces.items():
                    for surface in surfaces:
                        roles[surface] = name
                auto_reduce_planes, cut_map, auto_reduce_report = _auto_reduce_geometry(
                    surfaces=all_surfaces,
                    roles=roles,
                    grid=args.auto_reduce_grid,
                    tolerance_rel=args.auto_reduce_tol_rel,
                )
                if auto_reduce_planes:
                    source_surfaces = {
                        name: _remap_after_cut(surfaces, cut_map)
                        for name, surfaces in source_surfaces.items()
                    }
                    emptied_sources = [
                        name for name, surfaces in source_surfaces.items() if not surfaces
                    ]
                    if emptied_sources:
                        raise RuntimeError(
                            "auto-cut removed every face of sources: "
                            + ", ".join(emptied_sources)
                        )
                    refine_surfaces = {
                        name: _remap_after_cut(surfaces, cut_map)
                        for name, surfaces in refine_surfaces.items()
                    }
                    refine_surfaces = {
                        name: surfaces for name, surfaces in refine_surfaces.items() if surfaces
                    }
                    shell_surfaces = {
                        name: _remap_after_cut(surfaces, cut_map)
                        for name, surfaces in shell_surfaces.items()
                    }
                    excluded_surfaces = set(_remap_after_cut(excluded_surfaces, cut_map))
                    all_surfaces = _remap_after_cut(all_surfaces, cut_map)
                    active_source_specs = [
                        spec for spec in active_source_specs
                        if source_surfaces.get(spec.name)
                    ]
                    source_surface_set = {
                        tag
                        for tags_for_source in source_surfaces.values()
                        for tag in tags_for_source
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

            auto_radiating = _auto_radiating_surfaces(shell_surfaces, source_surface_set)
            refined_surface_set = {
                tag for tags_for_group in refine_surfaces.values() for tag in tags_for_group
            }
            # Don't auto-grade surfaces the user explicitly painted.
            auto_radiating -= refined_surface_set

            density = _density_configuration(
                active_source_specs,
                rigid_res_mm=rigid_res,
                transition_mm=args.transition_mm,
                refine_specs=refine_specs,
                refine_surfaces=refine_surfaces,
                curvature_segments=args.curvature_segments,
            )

            shadow_res = float(density["shadow_res_mm"])
            # Finest planned size pins MeshSizeMin; the shadow background caps
            # everything not pulled finer by a field.
            planned_min = min(
                [float(field["patch_size_mm"]) for field in density["source_fields"].values()]
                + [float(field["size_min_mm"]) for field in density["source_fields"].values()]
                + [float(f["size_mm"]) for f in density["refine_fields"].values()]
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

            # Painted refine overrides.
            for spec in refine_specs:
                surfaces = refine_surfaces.get(spec.name)
                if not surfaces:
                    continue
                fields.append(_add_restrict_field(spec.size_mm, surfaces))

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
                refine_surfaces=refine_surfaces,
                refine_specs=refine_specs,
                active_source_specs=active_source_specs,
                density=density,
                transition_mm=args.transition_mm,
                shadow_res=shadow_res,
                symmetry_planes=(
                    auto_reduce_planes if auto_reduce_planes else symmetry_planes
                ),
            )

            try:
                gmsh.model.mesh.generate(2)
            except Exception as exc:
                return {
                    "mesh_generation_error": exc,
                    "mesh_generation_traceback": exc.__traceback__,
                    "surface_geoms": surface_geoms,
                }
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
                    "size_mm": spec.size_mm,
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
                "excluded_solid_brep_surface_count": len(excluded_surfaces),
            }
            return {
                "surface_geoms": surface_geoms,
                "auto_reduce": auto_reduce_report,
                "auto_reduce_planes": auto_reduce_planes,
                "source_surfaces": source_surfaces,
                "active_source_specs": active_source_specs,
                "rigid_surfaces": rigid_surfaces,
                "density": density,
                "mesh_size_prediction": mesh_size_prediction,
                "refine_surfaces": refine_surfaces,
                "refine_origins": refine_origins,
                "auto_radiating": auto_radiating,
                "shell_surfaces": shell_surfaces,
                "duplicate_node_stats": duplicate_node_stats,
                "source_diag": source_diag,
                "skipped_sources": skipped_sources,
                "refine_diag": refine_diag,
                "role_classification": role_classification,
            }
        finally:
            gmsh.finalize()

    geometry_healed = False
    geometry_healing_mode = "none"
    gmsh_state = _run_gmsh_attempt()
    mesh_generation_error = gmsh_state.get("mesh_generation_error")
    if mesh_generation_error is not None:
        original_mesh_error = mesh_generation_error
        original_traceback = gmsh_state.get("mesh_generation_traceback")
        surface_order_reference = gmsh_state.get("surface_geoms")
        if not isinstance(surface_order_reference, list):
            raise RuntimeError(
                "unhealed gmsh attempt failed before surface geometry could be captured; "
                "cannot safely run OCC healing fallback"
            )
        assert isinstance(original_mesh_error, Exception)
        # Fusion STEP exports can carry sub-micron sliver edges on trimmed
        # spherical caps that make gmsh's periodic-surface 1D mesh
        # self-intersect; OCC small-edge/degenerate healing removes them, but
        # it is fallback-only because it regresses some otherwise-clean exports.
        gmsh_state, geometry_healing_mode, _rejected_healing_attempts = (
            _run_occ_healing_fallbacks(
                _run_gmsh_attempt,
                original_mesh_error=original_mesh_error,
                original_traceback=original_traceback,
                surface_order_reference=surface_order_reference,
            )
        )
        geometry_healed = True

    auto_reduce_report = gmsh_state["auto_reduce"]
    auto_reduce_planes = tuple(gmsh_state["auto_reduce_planes"])
    active_source_specs = gmsh_state["active_source_specs"]
    density = gmsh_state["density"]
    mesh_size_prediction = gmsh_state["mesh_size_prediction"]
    duplicate_node_stats = gmsh_state["duplicate_node_stats"]
    source_diag = gmsh_state["source_diag"]
    skipped_sources = gmsh_state["skipped_sources"]
    refine_diag = gmsh_state["refine_diag"]
    role_classification = gmsh_state["role_classification"]

    mesh = meshio.read(tagged_mesh_path)
    active_face_groups = _step_face_groups(active_source_specs)
    repaired_mesh, repair_stats, topology = _postprocess_mesh(
        mesh,
        active_face_groups,
        symmetry_planes=symmetry_planes,
        tolerance=args.topology_tol,
        symmetry_snap_tolerance=(
            # The native Metal solve rejects a reduced mesh whose minimum
            # coordinate is below -1e-7 m. An OCC trim lands on the plane to
            # roughly 1e-9 mm, but snapping the cut band to exactly zero
            # removes the question entirely. Auto-cut accepts non-mm STEP
            # units, so its physical-mm band must be converted before it is
            # compared with mesh coordinates.
            _millimetres_to_step_units(
                HEALED_SYMMETRY_BAND_MM,
                args.unit_scale_to_m,
            )
            if auto_reduce_planes
            else (HEALED_SYMMETRY_BAND_MM if geometry_healed else None)
        ),
    )
    resolved_symmetry_planes = tuple(topology["expected_symmetry_planes"])
    # Self-check: the free-edge detector re-reads the cut from the mesh with no
    # knowledge of what was cut. Every plane that was cut must come back as an
    # open rim; if one does not, the reduced boundary was capped there, and a
    # capped plane meshes as a rigid baffle rather than a symmetry plane. The
    # test is containment, not equality: a model supplied already cut on one
    # plane and auto-cut on another legitimately reports both.
    auto_reduce_planes_confirmed = set(auto_reduce_planes) <= set(
        resolved_symmetry_planes
    )
    if auto_reduce_planes:
        auto_reduce_report = dict(auto_reduce_report)
        auto_reduce_report["post_cut_detected_planes"] = list(resolved_symmetry_planes)
        auto_reduce_report["post_cut_planes_confirmed"] = bool(auto_reduce_planes_confirmed)
    meshio.write(tagged_mesh_path, repaired_mesh, file_format="gmsh22", binary=False)
    points, triangles, tags = _mesh_triangle_data(repaired_mesh)
    frequency_validation = _mesh_frequency_validation(
        points,
        triangles,
        tags,
        active_face_groups,
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

    solver_ready = (
        topology["nonmanifold_edges"] == 0
        and topology["inconsistent_edges"] == 0
        and topology["unexpected_free_edges"] == 0
        and auto_reduce_planes_confirmed
    )
    manifest = {
        "step": str(step_path),
        "tagged_mesh_step_units": str(tagged_mesh_path),
        "geometry_healed": bool(geometry_healed),
        "geometry_healing_mode": geometry_healing_mode,
        "quadrants": args.quadrants,
        "auto_reduce": auto_reduce_report,
        "symmetry_planes": list(resolved_symmetry_planes),
        "symmetry_planes_mode": (
            "auto-cut" if auto_reduce else ("auto" if symmetry_auto else "explicit")
        ),
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
    if not auto_reduce_planes_confirmed:
        print(
            "ERROR: auto-cut reduced the model on "
            f"{','.join(auto_reduce_planes)} but the meshed boundary reports "
            f"{','.join(resolved_symmetry_planes) or 'no'} symmetry planes. "
            "The cut plane is not open in the mesh; refusing the reduction.",
            file=sys.stderr,
        )
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
