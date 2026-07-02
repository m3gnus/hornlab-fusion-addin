#!/usr/bin/env python3
"""Diagnose orientation and expand a reduced-domain WG Metal BEM mesh.

This is a companion to ``prepare_step_for_wg_metal.py``. It reads the tagged
mesh written by that tool, reports open-edge symmetry planes, reports the
forward axis that the WG/hornlab-metal-bem frame heuristic would infer for
each source tag, and optionally writes a mirrored full-domain mesh for visual
inspection or full-domain solving.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable

import meshio
import numpy as np


DEFAULT_TOPOLOGY_TOL = 1e-5
RIGID_TAG = 1
SOURCE_TAG_BASE = 2


@dataclass(frozen=True)
class Source:
    name: str
    tag: int


def _parse_source(raw: str) -> Source:
    if ":" not in raw:
        raise argparse.ArgumentTypeError("--source expects NAME:TAG")
    name, tag_text = raw.split(":", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("source name must not be empty")
    return Source(name=name, tag=int(tag_text))


def _triangle_data(mesh_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mesh = meshio.read(mesh_path)
    if "triangle" not in mesh.cells_dict:
        raise RuntimeError(f"{mesh_path} has no triangle cells")
    triangles = np.asarray(mesh.cells_dict["triangle"], dtype=np.int64)
    points = np.asarray(mesh.points, dtype=np.float64)
    tags = np.asarray(mesh.cell_data_dict["gmsh:physical"]["triangle"], dtype=np.int32)
    return points, triangles, tags


def _free_edges(triangles: np.ndarray) -> list[tuple[int, int]]:
    counts: dict[tuple[int, int], int] = {}
    for tri in triangles:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge = tuple(sorted((int(a), int(b))))
            counts[edge] = counts.get(edge, 0) + 1
    return [edge for edge, count in counts.items() if count == 1]


def _signed_volume(points: np.ndarray, triangles: np.ndarray) -> float:
    if len(triangles) == 0:
        return 0.0
    p0 = points[triangles[:, 0]]
    p1 = points[triangles[:, 1]]
    p2 = points[triangles[:, 2]]
    return float(np.sum(p0 * np.cross(p1, p2)) / 6.0)


def _mesh_orientation_sign(
    points: np.ndarray,
    triangles: np.ndarray,
    free_edges: list[tuple[int, int]],
    *,
    tol: float,
) -> tuple[float, float]:
    """Global outward/inward orientation sign from the mesh signed volume.

    The signed volume determines orientation for a closed mesh, and equally
    for a reduced mesh whose only free edges lie on coordinate planes through
    the origin: position vectors on such a plane are perpendicular to its
    normal, so the missing cut faces contribute nothing to the volume
    integral. Returns 0.0 when free edges leave that regime (leaks, arbitrary
    open shells) and the sign is not trustworthy.
    """
    volume = _signed_volume(points, triangles)
    for edge in free_edges:
        midpoint = 0.5 * (points[edge[0]] + points[edge[1]])
        if not any(abs(float(midpoint[axis])) <= tol for axis in range(3)):
            return 0.0, volume
    bbox = points.max(axis=0) - points.min(axis=0)
    bbox_volume = float(np.prod(bbox))
    if bbox_volume <= 0.0 or abs(volume) <= 1.0e-6 * bbox_volume:
        return 0.0, volume
    return (1.0 if volume > 0.0 else -1.0), volume


def _plane_free_edge_counts(points: np.ndarray, free_edges: Iterable[tuple[int, int]], tol: float) -> list[dict]:
    free_edges = list(free_edges)
    mins = points.min(axis=0)
    maxs = points.max(axis=0)
    rows = []
    for axis, axis_name in enumerate(("x", "y", "z")):
        for kind, value in (("min", mins[axis]), ("zero", 0.0), ("max", maxs[axis])):
            matching = []
            for edge in free_edges:
                midpoint = 0.5 * (points[edge[0]] + points[edge[1]])
                if abs(float(midpoint[axis] - value)) <= tol:
                    matching.append(edge)
            if matching:
                rows.append({
                    "axis": axis_name,
                    "kind": kind,
                    "value": float(value),
                    "free_edges": int(len(matching)),
                })
    rows.sort(key=lambda item: (-int(item["free_edges"]), str(item["axis"]), str(item["kind"])))
    return rows


def _principal_axis_mouth_centers(points: np.ndarray) -> dict[str, list[float]]:
    """Mouth centroid candidates along every principal direction.

    For each direction, the mouth region is taken as all points within 2% of
    the projection span from the maximum projection, matching the per-source
    mouth heuristic. The pipeline picks the entry for its snapped frame axis.
    """
    centers: dict[str, list[float]] = {}
    for axis_index, axis_name in enumerate(("x", "y", "z")):
        proj = points[:, axis_index]
        span = float(proj.max() - proj.min())
        window = 0.02 * span if span > 0.0 else 0.0
        for sign, sign_name in ((1.0, "+"), (-1.0, "-")):
            signed = sign * proj
            mouth_points = points[signed >= signed.max() - window]
            centers[f"{sign_name}{axis_name}"] = [
                float(v) for v in mouth_points.mean(axis=0)
            ]
    return centers


def _source_frame(
    points: np.ndarray,
    triangles: np.ndarray,
    tags: np.ndarray,
    source: Source,
    *,
    orientation_sign: float = 0.0,
) -> dict:
    mask = tags == source.tag
    if not np.any(mask):
        return {"name": source.name, "tag": source.tag, "error": "tag not present"}
    source_triangles = triangles[mask]
    p0 = points[source_triangles[:, 0]]
    p1 = points[source_triangles[:, 1]]
    p2 = points[source_triangles[:, 2]]
    normals = np.cross(p1 - p0, p2 - p0)
    twice_area = np.linalg.norm(normals, axis=1)
    valid = twice_area > 1e-15
    normals = normals[valid]
    twice_area = twice_area[valid]
    centroids = ((p0 + p1 + p2) / 3.0)[valid]
    if len(normals) == 0:
        return {"name": source.name, "tag": source.tag, "error": "no nondegenerate source triangles"}

    source_center = np.average(centroids, weights=twice_area, axis=0)

    oriented_sum = np.sum(normals, axis=0)
    if orientation_sign != 0.0 and float(np.linalg.norm(oriented_sum)) > 1e-12:
        # The winding-consistent area-weighted normal, corrected to point out
        # of the solid into the air, is the firing direction for any driver
        # mounting (throat, side port, front baffle). No positional guess.
        avg_normal = orientation_sign * oriented_sum / np.linalg.norm(oriented_sum)
        axis = avg_normal.copy()
        reason = "oriented source normal (mesh signed volume)"
    else:
        # Orientation unknown: align triangle normals to an arbitrary
        # reference and fall back to guessing the sign from where the source
        # sits along its own normal. Wrong for sources mounted at the mouth
        # plane, e.g. a front-baffle woofer.
        ref = normals[0]
        signs = np.sign(normals @ ref)
        signs[signs == 0] = 1.0
        normal_sum = np.sum(normals * signs[:, None], axis=0)
        avg_normal = normal_sum / np.linalg.norm(normal_sum)

        projections = points @ avg_normal
        source_proj = float(source_center @ avg_normal)
        min_proj = float(projections.min())
        max_proj = float(projections.max())
        span = max_proj - min_proj
        if span < 1e-12:
            axis = avg_normal.copy()
            reason = "degenerate span"
        else:
            source_from_min = abs(source_proj - min_proj) / span
            source_from_max = abs(source_proj - max_proj) / span
            if min(source_from_min, source_from_max) > 0.25:
                axis = avg_normal.copy()
                reason = "source near middle; trust source normal"
            elif source_from_min < source_from_max:
                axis = avg_normal.copy()
                reason = "source near min projection"
            else:
                axis = -avg_normal
                reason = "source near max projection; flip normal"

    proj_axis = points @ axis
    mouth_threshold = float(proj_axis.max() - 0.02 * (proj_axis.max() - proj_axis.min()))
    mouth_center = points[proj_axis >= mouth_threshold].mean(axis=0)
    return {
        "name": source.name,
        "tag": int(source.tag),
        "triangles": int(np.count_nonzero(mask)),
        "source_center": [float(v) for v in source_center],
        "average_source_normal": [float(v) for v in avg_normal],
        "inferred_forward_axis": [float(v) for v in axis],
        "inference_reason": reason,
        "mouth_center_for_inferred_axis": [float(v) for v in mouth_center],
        "projection_span": [float(proj_axis.min()), float(proj_axis.max())],
    }


def _mirror_domain(
    points: np.ndarray,
    triangles: np.ndarray,
    tags: np.ndarray,
    axes: tuple[str, ...],
    *,
    tol: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not axes:
        return points.copy(), triangles.copy(), tags.copy()
    axis_index = {"x": 0, "y": 1, "z": 2}
    mirror_indices = [axis_index[a] for a in axes]
    signs = []
    for mask in range(2 ** len(mirror_indices)):
        vec = np.ones(3)
        for bit, axis in enumerate(mirror_indices):
            if mask & (1 << bit):
                vec[axis] = -1.0
        signs.append(vec)

    coord_to_new: dict[tuple[int, int, int], int] = {}
    new_points: list[np.ndarray] = []
    new_triangles: list[list[int]] = []
    new_tags: list[int] = []
    scale = 1.0 / tol

    for sign_vec in signs:
        det = float(np.prod(sign_vec[mirror_indices]))
        remapped_vertices: dict[int, int] = {}
        mirrored = points * sign_vec[None, :]
        for old_idx, coord in enumerate(mirrored):
            coord = coord.copy()
            for axis in mirror_indices:
                if abs(float(coord[axis])) <= tol:
                    coord[axis] = 0.0
            key = tuple(np.round(coord * scale).astype(np.int64).tolist())
            if key not in coord_to_new:
                coord_to_new[key] = len(new_points)
                new_points.append(coord.copy())
            remapped_vertices[old_idx] = coord_to_new[key]
        for tri, tag in zip(triangles, tags, strict=True):
            out_tri = [remapped_vertices[int(i)] for i in tri]
            if det < 0.0:
                out_tri = [out_tri[0], out_tri[2], out_tri[1]]
            new_triangles.append(out_tri)
            new_tags.append(int(tag))

    return (
        np.asarray(new_points, dtype=np.float64),
        np.asarray(new_triangles, dtype=np.int64),
        np.asarray(new_tags, dtype=np.int32),
    )


def _write_mesh(path: Path, points: np.ndarray, triangles: np.ndarray, tags: np.ndarray) -> None:
    used_tags = sorted({int(v) for v in tags.tolist()})
    field_data = {
        ("rigid" if tag == 1 else f"source_{tag}"): np.array([tag, 2], dtype=np.int32)
        for tag in used_tags
    }
    mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", triangles)],
        cell_data={
            "gmsh:physical": [tags],
            "gmsh:geometrical": [tags],
        },
        field_data=field_data,
    )
    meshio.write(path, mesh, file_format="gmsh22", binary=False)


def _safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "source"


def _write_wg_source_meshes(
    out_dir: Path,
    points: np.ndarray,
    triangles: np.ndarray,
    tags: np.ndarray,
    sources: list[Source],
    *,
    unit_scale_to_m: float,
) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for source in sources:
        if not np.any(tags == source.tag):
            continue
        remapped = np.full(tags.shape, RIGID_TAG, dtype=np.int32)
        remapped[tags == source.tag] = SOURCE_TAG_BASE
        safe_name = _safe_stem(source.name)
        out_path = out_dir / f"{safe_name}_source_tag2_m.msh"
        mesh = meshio.Mesh(
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
        meshio.write(out_path, mesh, file_format="gmsh22", binary=False)
        outputs[source.name] = str(out_path)
    return outputs


def _write_preview(path: Path, points: np.ndarray, triangles: np.ndarray, tags: np.ndarray) -> None:
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    colors = {1: "#b7b7b7", 2: "#1f77b4", 4: "#d62728", 5: "#2ca02c"}
    fig = plt.figure(figsize=(9.0, 7.0), dpi=160)
    ax = fig.add_subplot(111, projection="3d")
    for tag in sorted(set(tags.tolist())):
        tris = triangles[tags == tag]
        # Downsample large rigid groups for a readable preview.
        if tag == 1 and len(tris) > 2500:
            step = int(np.ceil(len(tris) / 2500))
            tris = tris[::step]
        poly = Poly3DCollection(points[tris], alpha=0.58 if tag == 1 else 0.85)
        poly.set_facecolor(colors.get(int(tag), "#9467bd"))
        poly.set_edgecolor((0.08, 0.08, 0.08, 0.18))
        poly.set_linewidth(0.15)
        ax.add_collection3d(poly)
    mins = points.min(axis=0)
    maxs = points.max(axis=0)
    centre = 0.5 * (mins + maxs)
    radius = float(np.max(maxs - mins) * 0.55)
    ax.set_xlim(centre[0] - radius, centre[0] + radius)
    ax.set_ylim(centre[1] - radius, centre[1] + radius)
    ax.set_zlim(centre[2] - radius, centre[2] + radius)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")
    ax.view_init(elev=24, azim=-55)
    ax.set_title("Expanded 4-quarter tagged mesh")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mesh", type=Path, required=True, help="Tagged .msh from prepare_step_for_wg_metal.py")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", action="append", type=_parse_source, default=[])
    parser.add_argument(
        "--mirror-axes",
        default="x,y",
        help=(
            "Comma-separated coordinate axes to mirror across for full-domain "
            "expansion, e.g. x,y for a quarter mesh or z for a top/bottom half."
        ),
    )
    parser.add_argument("--tol", type=float, default=DEFAULT_TOPOLOGY_TOL)
    parser.add_argument(
        "--unit-scale-to-m",
        type=float,
        default=0.001,
        help="Scale from mesh units to metres for WG per-source mesh exports.",
    )
    parser.add_argument("--no-preview", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mesh_path = args.mesh.expanduser().resolve()
    out_dir = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    points, triangles, tags = _triangle_data(mesh_path)
    sources = args.source or [
        Source(name=f"tag_{int(tag)}", tag=int(tag))
        for tag in sorted(set(tags.tolist()))
        if int(tag) != 1
    ]
    free_edges = _free_edges(triangles)
    plane_counts = _plane_free_edge_counts(points, free_edges, args.tol)
    orientation_sign, signed_volume = _mesh_orientation_sign(
        points, triangles, free_edges, tol=args.tol
    )
    source_frames = [
        _source_frame(points, triangles, tags, source, orientation_sign=orientation_sign)
        for source in sources
    ]

    raw_mirror_axes = args.mirror_axes.strip().lower()
    mirror_axes = tuple(
        part.strip().lower()
        for part in raw_mirror_axes.split(",")
        if part.strip() and part.strip().lower() not in {"none", "full"}
    )
    if len(set(mirror_axes)) != len(mirror_axes) or any(axis not in {"x", "y", "z"} for axis in mirror_axes):
        raise SystemExit("--mirror-axes must contain unique axes from x,y,z, or none")
    expanded_points, expanded_triangles, expanded_tags = _mirror_domain(
        points, triangles, tags, mirror_axes, tol=args.tol
    )
    expansion_factor = 2 ** len(mirror_axes)
    axis_suffix = "".join(mirror_axes) if mirror_axes else "none"
    expanded_path = out_dir / f"expanded_{expansion_factor}q_{axis_suffix}.msh"
    _write_mesh(expanded_path, expanded_points, expanded_triangles, expanded_tags)
    preview_path = None
    if not args.no_preview:
        preview_path = out_dir / f"expanded_{expansion_factor}q_{axis_suffix}_preview.png"
        _write_preview(preview_path, expanded_points, expanded_triangles, expanded_tags)
    wg_source_meshes = _write_wg_source_meshes(
        out_dir,
        expanded_points,
        expanded_triangles,
        expanded_tags,
        sources,
        unit_scale_to_m=args.unit_scale_to_m,
    )

    unique_tags, tag_counts = np.unique(tags, return_counts=True)
    expanded_unique, expanded_counts = np.unique(expanded_tags, return_counts=True)
    report = {
        "mesh": str(mesh_path),
        "bbox": {
            "min": [float(v) for v in points.min(axis=0)],
            "max": [float(v) for v in points.max(axis=0)],
        },
        "triangles": int(len(triangles)),
        "vertices": int(len(points)),
        "tag_triangle_counts": {
            str(int(tag)): int(count)
            for tag, count in zip(unique_tags, tag_counts, strict=True)
        },
        "free_edges": int(len(free_edges)),
        "free_edge_plane_counts": plane_counts,
        "signed_volume_step_units3": float(signed_volume),
        "orientation_sign": float(orientation_sign),
        "source_frame_inference": source_frames,
        "principal_axis_mouth_centers": _principal_axis_mouth_centers(points),
        "expanded_mesh": {
            "mirror_axes": list(mirror_axes),
            "mesh": str(expanded_path),
            "preview_png": str(preview_path) if preview_path else None,
            "expansion_factor": int(expansion_factor),
            "triangles": int(len(expanded_triangles)),
            "vertices": int(len(expanded_points)),
            "tag_triangle_counts": {
                str(int(tag)): int(count)
                for tag, count in zip(expanded_unique, expanded_counts, strict=True)
            },
        },
        "wg_source_meshes_m": wg_source_meshes,
    }
    if mirror_axes == ("x", "y"):
        report["expanded_4quarter"] = report["expanded_mesh"]
    report_path = out_dir / "orientation_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
