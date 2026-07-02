from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import meshio
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_wg_metal_orientation.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("diagnose_wg_metal_orientation", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _open_unit_box_with_top_source():
    """Unit box missing its x=0 and y=0 faces, wound outward.

    The top face (z=1, tag 2) models a front-baffle woofer: a source patch
    sitting exactly on the max-projection plane of its own +z normal, firing
    out of the box. All free edges lie on the x=0/y=0 origin planes, so the
    signed volume determines the global orientation.
    """
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 0.0, 1.0],
            [0.0, 1.0, 1.0],
            [1.0, 1.0, 1.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray(
        [
            # x=1 face, outward +x
            [1, 3, 7],
            [1, 7, 5],
            # y=1 face, outward +y
            [2, 6, 7],
            [2, 7, 3],
            # z=0 face, outward -z
            [0, 2, 3],
            [0, 3, 1],
            # z=1 face (source), outward +z
            [4, 5, 7],
            [4, 7, 6],
        ],
        dtype=np.int64,
    )
    tags = np.asarray([1, 1, 1, 1, 1, 1, 2, 2], dtype=np.int32)
    return points, triangles, tags


def test_orientation_sign_positive_for_outward_origin_cut_mesh():
    module = _load_script()
    points, triangles, _ = _open_unit_box_with_top_source()
    free_edges = module._free_edges(triangles)

    sign, volume = module._mesh_orientation_sign(points, triangles, free_edges, tol=1e-9)

    assert sign == 1.0
    assert volume > 0.9


def test_orientation_sign_tolerates_step_origin_noise():
    module = _load_script()
    points, triangles, _ = _open_unit_box_with_top_source()
    points[np.isclose(points[:, 0], 0.0), 0] = -4.8e-6
    free_edges = module._free_edges(triangles)

    sign, volume = module._mesh_orientation_sign(
        points,
        triangles,
        free_edges,
        tol=module.DEFAULT_TOPOLOGY_TOL,
    )

    assert sign == 1.0
    assert volume > 0.9


def test_orientation_sign_negative_for_inward_wound_mesh():
    module = _load_script()
    points, triangles, _ = _open_unit_box_with_top_source()
    inward = triangles[:, [0, 2, 1]]
    free_edges = module._free_edges(inward)

    sign, volume = module._mesh_orientation_sign(points, inward, free_edges, tol=1e-9)

    assert sign == -1.0
    assert volume < -0.9


def test_orientation_sign_unknown_when_free_edges_leave_origin_planes():
    module = _load_script()
    points, triangles, _ = _open_unit_box_with_top_source()
    shifted = points + np.asarray([5.0, 0.0, 0.0])
    free_edges = module._free_edges(triangles)

    sign, _ = module._mesh_orientation_sign(shifted, triangles, free_edges, tol=1e-9)

    assert sign == 0.0


def test_front_baffle_source_keeps_outward_normal_when_oriented():
    """Regression: a woofer on the mouth plane must not get its axis flipped.

    The positional heuristic reads a source at the max projection of its own
    normal as mouth-mounted and flips it, turning a front-baffle LF woofer
    into a -z vote that flips the global observation frame of the whole solve.
    """
    module = _load_script()
    points, triangles, tags = _open_unit_box_with_top_source()
    source = module.Source(name="LF", tag=2)

    frame = module._source_frame(points, triangles, tags, source, orientation_sign=1.0)

    assert frame["inference_reason"] == "oriented source normal (mesh signed volume)"
    np.testing.assert_allclose(frame["inferred_forward_axis"], [0.0, 0.0, 1.0], atol=1e-12)


def test_mirror_domain_snaps_near_plane_vertices_before_welding():
    module = _load_script()
    points, triangles, tags = _open_unit_box_with_top_source()
    points[np.isclose(points[:, 0], 0.0), 0] = -9.6e-6

    expanded_points, expanded_triangles, expanded_tags = module._mirror_domain(
        points,
        triangles,
        tags,
        ("x", "y"),
        tol=module.DEFAULT_TOPOLOGY_TOL,
    )

    assert len(module._free_edges(expanded_triangles)) == 0
    assert len(expanded_triangles) == 4 * len(triangles)
    assert len(expanded_tags) == len(expanded_triangles)
    assert np.min(np.abs(expanded_points[:, 0])) == 0.0


def test_wg_source_mesh_export_uses_expanded_full_domain(tmp_path):
    module = _load_script()
    points, triangles, tags = _open_unit_box_with_top_source()
    expanded_points, expanded_triangles, expanded_tags = module._mirror_domain(
        points,
        triangles,
        tags,
        ("x", "y"),
        tol=module.DEFAULT_TOPOLOGY_TOL,
    )

    outputs = module._write_wg_source_meshes(
        tmp_path,
        expanded_points,
        expanded_triangles,
        expanded_tags,
        [module.Source(name="LF", tag=2)],
        unit_scale_to_m=0.001,
    )

    mesh = meshio.read(outputs["LF"])
    out_tris = mesh.cells_dict["triangle"]
    out_tags = mesh.cell_data_dict["gmsh:physical"]["triangle"]
    assert len(module._free_edges(out_tris)) == 0
    assert set(out_tags.tolist()) == {1, 2}
    assert float(mesh.points.max()) <= 0.001


def test_front_baffle_source_corrected_for_inward_wound_mesh():
    module = _load_script()
    points, triangles, tags = _open_unit_box_with_top_source()
    inward = triangles[:, [0, 2, 1]]
    source = module.Source(name="LF", tag=2)

    frame = module._source_frame(points, inward, tags, source, orientation_sign=-1.0)

    np.testing.assert_allclose(frame["inferred_forward_axis"], [0.0, 0.0, 1.0], atol=1e-12)


def test_unoriented_mesh_falls_back_to_positional_heuristic():
    module = _load_script()
    points, triangles, tags = _open_unit_box_with_top_source()
    source = module.Source(name="LF", tag=2)

    frame = module._source_frame(points, triangles, tags, source, orientation_sign=0.0)

    assert frame["inference_reason"] == "source near max projection; flip normal"
    np.testing.assert_allclose(frame["inferred_forward_axis"], [0.0, 0.0, -1.0], atol=1e-12)
