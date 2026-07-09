#!/usr/bin/env python3
"""Create a tagged tetrahedral acoustic-FEM mesh from a Fusion air volume.

The STEP input must contain exactly one watertight solid air body.  Boundary
faces are selected by Fusion appearance/style names (or named STEP shells)
such as ``FEM_DRIVER`` and ``MF_ENTRY_1``.  Every unselected exterior face is
tagged ``rigid``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

import gmsh
import meshio
import numpy as np

from prepare_step_for_wg_metal import (
    SurfaceGeometry,
    _advanced_face_order,
    _anchor_surface_order,
    _gmsh_surface_geometries,
    _parse_named_shell_faces,
    _parse_styled_face_groups,
)


RIGID_TAG = 1
AIR_VOLUME_TAG = 1000
FIRST_INTERFACE_TAG = 100
SPEED_OF_SOUND_M_S = 343.0
DEFAULT_ELEMENTS_PER_WAVELENGTH = 8.0


@dataclass(frozen=True)
class BoundaryGroup:
    name: str
    tag: int
    surfaces: tuple[int, ...]
    origin: str


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _surface_order_reference(step_path: Path) -> list[SurfaceGeometry]:
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCMakeSolids", 0)
        gmsh.open(str(step_path))
        gmsh.model.occ.synchronize()
        surfaces = [tag for dim, tag in sorted(gmsh.model.getEntities(2))]
        return _gmsh_surface_geometries(surfaces)
    finally:
        gmsh.finalize()


def _resolve_boundary_groups(
    step_path: Path,
    boundary_names: list[str],
    ordered_surfaces: list[int],
) -> list[BoundaryGroup]:
    face_order = _advanced_face_order(step_path)
    if len(ordered_surfaces) < len(face_order):
        raise RuntimeError(
            f"STEP has {len(face_order)} ADVANCED_FACE records but gmsh imported "
            f"only {len(ordered_surfaces)} surfaces"
        )
    face_to_surface = dict(zip(face_order, ordered_surfaces))
    named = _parse_named_shell_faces(step_path)
    styled = _parse_styled_face_groups(step_path)

    groups: list[BoundaryGroup] = []
    used: set[int] = set()
    for index, name in enumerate(boundary_names):
        found: tuple[str, list[int]] | None = None
        for origin, candidates in (("appearance/style", styled), ("named shell", named)):
            for label, faces in candidates.items():
                if label.casefold() == name.casefold():
                    found = origin, faces
                    break
            if found is not None:
                break
        if found is None:
            available = sorted(set(named) | set(styled))
            raise RuntimeError(
                f"FEM boundary {name!r} was not found in the STEP export. "
                f"Available names: {', '.join(available) or '(none)'}"
            )
        origin, face_ids = found
        surfaces = tuple(sorted({face_to_surface[face_id] for face_id in face_ids}))
        overlap = used.intersection(surfaces)
        if overlap:
            raise RuntimeError(
                f"FEM boundary {name!r} overlaps another interface on surfaces "
                f"{sorted(overlap)}"
            )
        used.update(surfaces)
        groups.append(
            BoundaryGroup(
                name=name,
                tag=FIRST_INTERFACE_TAG + index,
                surfaces=surfaces,
                origin=origin,
            )
        )
    return groups


def _mesh_counts(path: Path) -> dict[str, int]:
    mesh = meshio.read(path)
    return {
        "points": int(mesh.points.shape[0]),
        "tetrahedra": int(mesh.cells_dict.get("tetra", np.empty((0, 4))).shape[0]),
        "boundary_triangles": int(
            mesh.cells_dict.get("triangle", np.empty((0, 3))).shape[0]
        ),
    }


def prepare(
    step_path: Path,
    out_dir: Path,
    *,
    boundary_names: list[str],
    resolution_mm: float,
    unit_scale_to_m: float,
    elements_per_wavelength: float = DEFAULT_ELEMENTS_PER_WAVELENGTH,
) -> dict[str, Any]:
    if not step_path.is_file():
        raise FileNotFoundError(step_path)
    if not boundary_names:
        raise ValueError("at least one FEM boundary name is required")
    if len({name.casefold() for name in boundary_names}) != len(boundary_names):
        raise ValueError("FEM boundary names must be unique")
    resolution = float(resolution_mm)
    scale = float(unit_scale_to_m)
    epw = float(elements_per_wavelength)
    if not np.isfinite(resolution) or resolution <= 0.0:
        raise ValueError("resolution_mm must be positive and finite")
    if not np.isfinite(scale) or scale <= 0.0:
        raise ValueError("unit_scale_to_m must be positive and finite")
    if not np.isfinite(epw) or epw <= 0.0:
        raise ValueError("elements_per_wavelength must be positive and finite")

    out_dir.mkdir(parents=True, exist_ok=True)
    mesh_path = out_dir / "fem_chamber.msh"
    manifest_path = out_dir / "fem_chamber_mesh_manifest.json"
    reference_geometries = _surface_order_reference(step_path)

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 1)
        gmsh.option.setNumber("Geometry.OCCMakeSolids", 1)
        gmsh.open(str(step_path))
        gmsh.model.occ.synchronize()
        volumes = [tag for dim, tag in gmsh.model.getEntities(3)]
        if len(volumes) != 1:
            raise RuntimeError(
                "FEM STEP must contain exactly one watertight solid air volume; "
                f"gmsh imported {len(volumes)} volumes"
            )
        all_surfaces = [tag for dim, tag in sorted(gmsh.model.getEntities(2))]
        ordered_surfaces = _anchor_surface_order(
            all_surfaces,
            _gmsh_surface_geometries(all_surfaces),
            reference_geometries,
        )
        groups = _resolve_boundary_groups(step_path, boundary_names, ordered_surfaces)
        interface_surfaces = {surface for group in groups for surface in group.surfaces}
        rigid_surfaces = sorted(set(all_surfaces) - interface_surfaces)
        if not rigid_surfaces:
            raise RuntimeError("FEM air volume has no rigid-wall surfaces")

        gmsh.model.addPhysicalGroup(3, volumes, tag=AIR_VOLUME_TAG, name="air")
        gmsh.model.addPhysicalGroup(2, rigid_surfaces, tag=RIGID_TAG, name="rigid")
        for group in groups:
            gmsh.model.addPhysicalGroup(
                2,
                list(group.surfaces),
                tag=group.tag,
                name=group.name,
            )
        boundary_points = gmsh.model.getBoundary(
            [(3, volumes[0])], combined=True, oriented=False, recursive=True
        )
        gmsh.model.mesh.setSize(
            [(dim, tag) for dim, tag in boundary_points if dim == 0],
            resolution,
        )
        gmsh.option.setNumber("Mesh.MeshSizeMin", resolution)
        gmsh.option.setNumber("Mesh.MeshSizeMax", resolution)
        gmsh.model.mesh.generate(3)
        gmsh.option.setNumber("Mesh.MshFileVersion", 4.1)
        gmsh.option.setNumber("Mesh.Binary", 0)
        gmsh.write(str(mesh_path))

        volume_step_units3 = float(gmsh.model.occ.getMass(3, volumes[0]))
        area_by_name_step_units2 = {
            group.name: float(
                sum(gmsh.model.occ.getMass(2, surface) for surface in group.surfaces)
            )
            for group in groups
        }
    finally:
        gmsh.finalize()

    counts = _mesh_counts(mesh_path)
    if counts["tetrahedra"] <= 0:
        raise RuntimeError("gmsh wrote no tetrahedra for the FEM air volume")
    max_valid_frequency_hz = SPEED_OF_SOUND_M_S / (
        epw * resolution * unit_scale_to_m
    )
    payload = {
        "step": str(step_path),
        "mesh": str(mesh_path),
        "unit_scale_to_m": scale,
        "resolution_mm": resolution,
        "elements_per_wavelength": epw,
        "max_valid_frequency_hz": float(max_valid_frequency_hz),
        "volume_m3": volume_step_units3 * scale**3,
        "counts": counts,
        "physical_groups": {
            "volume": {"name": "air", "tag": AIR_VOLUME_TAG},
            "rigid": {"name": "rigid", "tag": RIGID_TAG},
            "interfaces": [
                {
                    **asdict(group),
                    "area_m2": area_by_name_step_units2[group.name] * scale**2,
                }
                for group in groups
            ],
        },
        "status": "complete",
    }
    _write_json(manifest_path, payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--boundary",
        action="append",
        default=[],
        help="Named Fusion appearance/style used on a FEM interface face. Repeatable.",
    )
    parser.add_argument("--resolution-mm", type=float, required=True)
    parser.add_argument("--unit-scale-to-m", type=float, default=0.001)
    parser.add_argument(
        "--elements-per-wavelength",
        type=float,
        default=DEFAULT_ELEMENTS_PER_WAVELENGTH,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = prepare(
        args.step.expanduser().resolve(),
        args.out.expanduser().resolve(),
        boundary_names=[str(name).strip() for name in args.boundary if str(name).strip()],
        resolution_mm=args.resolution_mm,
        unit_scale_to_m=args.unit_scale_to_m,
        elements_per_wavelength=args.elements_per_wavelength,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
