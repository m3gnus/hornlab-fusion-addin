from __future__ import annotations

import builtins
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys

import meshio
import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_step_for_wg_metal.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("prepare_step_for_wg_metal", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_old_mesher_import_has_actionable_compatibility_error(monkeypatch):
    original_import = builtins.__import__

    def reject_step_prepare(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hornlab_mesher.step_prepare":
            raise ImportError(
                "cannot import name 'auto_cut_occ_geometry'",
                name="hornlab_mesher.step_prepare",
            )
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_step_prepare)
    with pytest.raises(RuntimeError, match=r"compatible.*revision pinned.*requirements"):
        _load_script()


def _quarter_tube(angles_deg, z_values):
    """Open quarter-tube surface with cut rims on the x=0 and y=0 planes."""
    angles = np.deg2rad(np.asarray(angles_deg, dtype=np.float64))
    points = np.asarray(
        [
            [np.cos(a), np.sin(a), z]
            for a in angles
            for z in z_values
        ],
        dtype=np.float64,
    )
    n_z = len(z_values)
    triangles = []
    for i in range(len(angles) - 1):
        for j in range(n_z - 1):
            p00 = i * n_z + j
            p01 = i * n_z + j + 1
            p10 = (i + 1) * n_z + j
            p11 = (i + 1) * n_z + j + 1
            triangles.append([p00, p10, p11])
            triangles.append([p00, p11, p01])
    return points, np.asarray(triangles, dtype=np.int64)


def test_preparation_exposes_only_authoritative_tagged_mesh_contract(tmp_path):
    module = _load_script()
    args = module.parse_args(
        [
            "--step",
            str(tmp_path / "model.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
        ]
    )
    assert not hasattr(args, "skip_source_mesh_export")
    assert not hasattr(module, "_write_wg_source_meshes")
    assert not hasattr(module, "_maybe_write_wg_source_meshes")
    source = SCRIPT.read_text(encoding="utf-8")
    assert "wg_source_meshes_m" not in source
    assert "_source_tag2_m.msh" not in source

    with pytest.raises(SystemExit):
        module.parse_args(
            [
                "--step",
                str(tmp_path / "model.step"),
                "--out",
                str(tmp_path / "out"),
                "--source",
                "HF:5",
                "--skip-source-mesh-export",
            ]
        )


def test_parse_solid_brep_faces_excludes_only_closed_solid_shell(tmp_path):
    module = _load_script()
    step = tmp_path / "mixed.step"
    step.write_text(
        "\n".join(
            [
                "#10=ADVANCED_FACE('',(),$,.T.);",
                "#11=ADVANCED_FACE('',(),$,.T.);",
                "#12=ADVANCED_FACE('',(),$,.T.);",
                "#20=CLOSED_SHELL('',(#10,#11));",
                "#21=OPEN_SHELL('',(#12));",
                "#30=MANIFOLD_SOLID_BREP('FEM_AIR',#20);",
                "#31=SHELL_BASED_SURFACE_MODEL('EXTERIOR',(#21));",
            ]
        )
        + "\n",
        encoding="ascii",
    )
    assert module._parse_solid_brep_faces(step) == {10, 11}


def test_anchor_surface_order_recovers_permuted_healed_surfaces():
    module = _load_script()
    reference_geoms = [
        ((0.0, 0.0, 0.0), 253.0),
        ((10.0, 0.0, 0.0), 17_624.0),
        ((0.0, 25.0, 0.0), 320_000.0),
        ((-12.0, 8.0, 5.0), 8_000.0),
    ]
    tags_by_reference = [501, 502, 503, 504]
    healed_by_reference = [
        ((0.001, -0.002, 0.0005), 253.0 * 1.0005),
        ((10.002, -0.001, 0.0), 17_624.0 * 0.9995),
        ((-0.003, 25.001, 0.002), 320_000.0 * 1.0002),
        ((-11.999, 8.003, 4.999), 8_000.0 * 0.9992),
    ]
    permutation = [2, 0, 3, 1]
    healed_tags = [tags_by_reference[i] for i in permutation]
    healed_geoms = [healed_by_reference[i] for i in permutation]

    ordered = module._anchor_surface_order(healed_tags, healed_geoms, reference_geoms)

    assert ordered == tags_by_reference


def test_occ_healing_fallbacks_try_sewing_before_broad_repair():
    module = _load_script()

    assert module.OCC_HEALING_FALLBACKS == (
        ("sew", ("Geometry.OCCSewFaces",)),
        (
            "full",
            (
                "Geometry.OCCFixDegenerated",
                "Geometry.OCCFixSmallEdges",
                "Geometry.OCCFixSmallFaces",
                "Geometry.OCCSewFaces",
            ),
        ),
    )


def test_occ_healing_fallback_retries_after_anchor_rejection(capsys):
    module = _load_script()
    attempts = []
    expected_state = {"source_surfaces": {"HF": [101]}}

    def run_attempt(*, occ_healing_options, surface_order_reference):
        attempts.append((occ_healing_options, surface_order_reference))
        if occ_healing_options == module.OCC_HEALING_FALLBACKS[0][1]:
            raise RuntimeError(
                "cannot anchor healed OCC surfaces: surface count mismatch"
            )
        return expected_state

    state, mode = module._run_occ_healing_fallbacks(
        run_attempt,
        original_mesh_error=RuntimeError("unhealed gmsh meshing failed"),
        original_traceback=None,
        surface_order_reference=[((0.0, 0.0, 0.0), 1.0)],
    )

    assert state is expected_state
    assert mode == "full"
    assert [attempt[0] for attempt in attempts] == [
        mode_options for _mode, mode_options in module.OCC_HEALING_FALLBACKS
    ]
    assert "OCC sew repair rejected before meshing" in capsys.readouterr().err


def test_occ_healing_fallback_reraises_original_with_rejection_reasons(capsys):
    module = _load_script()
    original_error = RuntimeError("unhealed gmsh meshing failed")
    attempts = []

    def run_attempt(*, occ_healing_options, surface_order_reference):
        attempts.append(occ_healing_options)
        if occ_healing_options == module.OCC_HEALING_FALLBACKS[0][1]:
            raise RuntimeError(
                "cannot anchor healed OCC surfaces: surface count mismatch"
            )
        return {"mesh_generation_error": RuntimeError("full repair did not mesh")}

    with pytest.raises(RuntimeError, match="unhealed gmsh meshing failed") as raised:
        module._run_occ_healing_fallbacks(
            run_attempt,
            original_mesh_error=original_error,
            original_traceback=None,
            surface_order_reference=[((0.0, 0.0, 0.0), 1.0)],
        )

    assert raised.value is original_error
    assert attempts == [
        mode_options for _mode, mode_options in module.OCC_HEALING_FALLBACKS
    ]
    stderr = capsys.readouterr().err
    assert "OCC sew repair rejected before meshing" in stderr
    assert "OCC full repair mesh generation failed" in stderr
    assert "Original gmsh error: unhealed gmsh meshing failed" in stderr
    if hasattr(original_error, "add_note"):
        assert any(
            "OCC healing fallback rejection reasons" in note
            for note in original_error.__notes__
        )


def test_anchor_surface_order_fails_loudly_on_bad_input():
    module = _load_script()

    with pytest.raises(RuntimeError, match="surface count mismatch"):
        module._anchor_surface_order(
            [501],
            [((0.0, 0.0, 0.0), 10.0)],
            [((0.0, 0.0, 0.0), 10.0), ((1.0, 0.0, 0.0), 11.0)],
        )

    with pytest.raises(RuntimeError, match="implausible geometry residuals"):
        module._anchor_surface_order(
            [501],
            [((100.0, 0.0, 0.0), 30.0)],
            [((0.0, 0.0, 0.0), 10.0)],
        )


def test_detect_symmetry_planes_finds_quarter_cut_planes():
    module = _load_script()
    points, triangles = _quarter_tube(
        angles_deg=(0.0, 30.0, 60.0, 90.0),
        z_values=(1.0, 1.5, 2.0, 2.5, 3.0),
    )

    planes, detection = module._detect_symmetry_planes(
        points,
        triangles,
        tolerance=1e-9,
    )

    assert planes == ("x0", "y0")
    assert detection["detected_planes"] == ["x0", "y0"]
    assert detection["plane_free_edge_counts"]["x0"] == 4
    assert detection["plane_free_edge_counts"]["y0"] == 4
    assert detection["plane_free_edge_counts"]["z0"] == 0


def test_detect_symmetry_planes_ignores_internal_z_level_crossed_by_cut_rims():
    module = _load_script()
    # Cut rims span z=-2..2, so one rim edge per cut plane crosses z=0 without
    # lying in it. z0 must not be detected as a cut plane.
    points, triangles = _quarter_tube(
        angles_deg=(0.0, 30.0, 60.0, 90.0),
        z_values=(-2.0, -1.5, -0.5, 0.5, 1.5, 2.0),
    )

    planes, detection = module._detect_symmetry_planes(
        points,
        triangles,
        tolerance=1e-9,
    )

    assert planes == ("x0", "y0")
    assert detection["plane_free_edge_counts"]["x0"] == 5
    assert detection["plane_free_edge_counts"]["y0"] == 5
    assert detection["plane_free_edge_counts"]["z0"] == 0
    assert detection["shared_plane_free_edges"] == 0


def test_detect_symmetry_planes_ignores_sparse_origin_edges():
    module = _load_script()
    # Two free edges per cut plane stay below the 3-edge detection threshold.
    points, triangles = _quarter_tube(
        angles_deg=(0.0, 45.0, 90.0),
        z_values=(1.0, 2.0, 3.0),
    )

    planes, detection = module._detect_symmetry_planes(
        points,
        triangles,
        tolerance=1e-9,
    )

    assert planes == ()
    assert detection["plane_free_edge_counts"]["x0"] == 2
    assert detection["plane_free_edge_counts"]["y0"] == 2


def test_detect_symmetry_planes_rejects_candidate_that_mesh_spans():
    module = _load_script()
    # Three free edges on each origin plane meet the edge-count threshold.
    # However, y=0 is internal to the model: vertices exist on both sides, so
    # it must not enable a native quarter-domain solve.
    points = np.asarray(
        [
            [0.0, -1.0, 1.0],
            [0.0, -2.0, 1.0],
            [0.0, -1.0, 2.0],
            [1.0, 0.0, 1.0],
            [2.0, 0.0, 1.0],
            [1.0, 0.0, 2.0],
            [2.0, 1.0, 1.0],
            [3.0, 1.0, 1.0],
            [2.0, 1.0, 2.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2], [3, 4, 5], [6, 7, 8]], dtype=np.int64)

    planes, detection = module._detect_symmetry_planes(
        points,
        triangles,
        tolerance=1e-9,
    )

    assert planes == ("x0",)
    assert detection["plane_free_edge_counts"] == {"x0": 3, "y0": 3, "z0": 0}
    assert detection["plane_vertex_side_counts"]["y0"] == {
        "negative": 3,
        "on_plane": 3,
        "positive": 3,
    }
    assert detection["rejected_spanning_planes"] == ["y0"]


def test_detect_symmetry_planes_returns_empty_for_closed_mesh():
    module = _load_script()
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray(
        [
            [0, 1, 2],
            [0, 3, 1],
            [0, 2, 3],
            [1, 3, 2],
        ],
        dtype=np.int64,
    )

    planes, detection = module._detect_symmetry_planes(points, triangles, tolerance=1e-9)

    assert planes == ()
    assert detection["free_edges"] == 0


def test_postprocess_auto_mode_records_detected_planes():
    module = _load_script()
    points, triangles = _quarter_tube(
        angles_deg=(0.0, 30.0, 60.0, 90.0),
        z_values=(1.0, 1.5, 2.0, 2.5, 3.0),
    )
    tags = np.full(len(triangles), 1, dtype=np.int32)
    tags[0] = 4
    mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )

    _, _, topology = module._postprocess_mesh(
        mesh,
        [module.SourceSpec("HF", 5.0, 4)],
        symmetry_planes="auto",
        tolerance=1e-9,
    )

    assert topology["expected_symmetry_planes"] == ["x0", "y0"]
    assert topology["symmetry_plane_detection"]["detected_planes"] == ["x0", "y0"]


def test_postprocess_healed_snap_zeroes_near_symmetry_plane_only():
    module = _load_script()
    points = np.asarray(
        [
            [0.0, 3.0e-5, 0.0],
            [1.0, 5.0, 0.0],
            [0.0, 5.0, 1.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2]], dtype=np.int64)
    tags = np.asarray([2], dtype=np.int32)

    clean_mesh = meshio.Mesh(
        points=points.copy(),
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )
    clean_repaired, _, _ = module._postprocess_mesh(
        clean_mesh,
        [module.SourceSpec("LF", 30.0, 2)],
        symmetry_planes=("y0",),
        tolerance=1.0e-5,
        symmetry_snap_tolerance=None,
    )
    clean_points, _, _ = module._mesh_triangle_data(clean_repaired)
    assert np.array_equal(clean_points, points)

    healed_mesh = meshio.Mesh(
        points=points.copy(),
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )
    healed_repaired, _, _ = module._postprocess_mesh(
        healed_mesh,
        [module.SourceSpec("LF", 30.0, 2)],
        symmetry_planes=("y0",),
        tolerance=1.0e-5,
        symmetry_snap_tolerance=module.HEALED_SYMMETRY_BAND_MM,
    )
    healed_points, _, _ = module._mesh_triangle_data(healed_repaired)
    assert healed_points[0, 1] == 0.0
    assert healed_points[1, 1] == 5.0
    assert healed_points[2, 1] == 5.0
    assert healed_points[0, 0] == 0.0


def test_topology_stats_tolerates_step_origin_noise():
    module = _load_script()
    points = np.asarray(
        [
            [-4.8e-6, 0.0, 0.0],
            [-4.8e-6, 1.0, 0.0],
            [-4.8e-6, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2]], dtype=np.int64)

    topology = module._topology_stats(
        points,
        triangles,
        symmetry_planes=("x0",),
        tolerance=module.DEFAULT_TOPOLOGY_TOL,
    )

    assert topology["unexpected_free_edges"] == 0


def test_topology_stats_rejects_edges_that_only_cross_symmetry_plane():
    module = _load_script()
    points = np.asarray(
        [
            [1.0, 0.0, 0.0],
            [-1.0, 1.0, 0.0],
            [1.0, 2.0, 0.0],
            [-1.0, 3.0, 0.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2], [0, 2, 3]], dtype=np.int64)

    topology = module._topology_stats(
        points,
        triangles,
        symmetry_planes=("x0",),
        tolerance=1e-9,
    )

    assert topology["free_edges"] == 4
    assert topology["unexpected_free_edges"] == 4


def test_postprocess_auto_mode_reflects_negative_quadrant_to_positive():
    module = _load_script()
    # Quarter modeled in the +x/-y quadrant, like a Fusion export cut to the
    # right/front. The Metal native symmetry solve needs +x/+y.
    points, triangles = _quarter_tube(
        angles_deg=(-90.0, -60.0, -30.0, 0.0),
        z_values=(1.0, 1.5, 2.0, 2.5, 3.0),
    )
    tags = np.full(len(triangles), 1, dtype=np.int32)
    tags[0] = 4
    mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )

    repaired_mesh, _, topology = module._postprocess_mesh(
        mesh,
        [module.SourceSpec("HF", 5.0, 4)],
        symmetry_planes="auto",
        tolerance=1e-9,
    )

    assert topology["expected_symmetry_planes"] == ["x0", "y0"]
    assert topology["axis_normalization"]["reflected_axes"] == ["y"]
    out_points, out_triangles, _ = module._mesh_triangle_data(repaired_mesh)
    assert float(out_points[:, 1].min()) >= -1e-9
    assert float(out_points[:, 1].max()) > 0.5
    assert module._edge_direction_stats(out_triangles)["inconsistent_edges"] == 0


def test_normalize_to_positive_side_is_noop_for_positive_quadrant():
    module = _load_script()
    points, triangles = _quarter_tube(
        angles_deg=(0.0, 30.0, 60.0, 90.0),
        z_values=(1.0, 1.5, 2.0, 2.5, 3.0),
    )

    out_points, out_triangles, normalization = module._normalize_to_positive_side(
        points,
        triangles,
        symmetry_planes=("x0", "y0"),
    )

    assert normalization["reflected_axes"] == []
    assert np.array_equal(out_points, points)
    assert np.array_equal(out_triangles, triangles)


def test_postprocess_removes_degenerate_and_repairs_winding():
    module = _load_script()
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray(
        [
            [0, 1, 2],
            [0, 2, 3],
            [0, 0, 1],
        ],
        dtype=np.int64,
    )
    tags = np.asarray([1, 2, 2], dtype=np.int32)
    mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )

    repaired_mesh, repair, topology = module._postprocess_mesh(
        mesh,
        [module.SourceSpec("LF", 20.0, 2)],
        symmetry_planes=(),
        tolerance=1e-9,
    )

    _, repaired_triangles, repaired_tags = module._mesh_triangle_data(repaired_mesh)
    assert len(repaired_triangles) == 2
    assert repaired_tags.tolist() == [1, 2]
    assert repair["degenerate_triangles_removed"] == 1
    assert topology["inconsistent_edges"] == 0
    assert topology["boundary_edges"] == 4
    assert topology["source_normal_projections"]["LF"]["triangle_count"] == 1


def test_closed_negative_volume_mesh_flips_globally():
    module = _load_script()
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray(
        [
            [0, 1, 2],
            [0, 3, 1],
            [0, 2, 3],
            [1, 3, 2],
        ],
        dtype=np.int64,
    )

    repaired, stats = module._repair_triangle_winding(points, triangles)

    assert stats["flipped_global"] == 4
    assert module._signed_volume(points, repaired) > 0.0
    assert module._edge_direction_stats(repaired)["inconsistent_edges"] == 0


def test_disconnected_closed_components_orient_outward_independently():
    module = _load_script()
    base_points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    inward = np.asarray(
        [
            [0, 1, 2],
            [0, 3, 1],
            [0, 2, 3],
            [1, 3, 2],
        ],
        dtype=np.int64,
    )
    points = np.vstack([base_points, base_points + np.asarray([3.0, 0.0, 0.0])])
    triangles = np.vstack([inward, inward[:, [0, 2, 1]] + 4])

    repaired, stats = module._repair_triangle_winding(points, triangles)

    assert stats["flipped_global"] == 4
    assert module._signed_volume(points, repaired[:4]) > 0.0
    assert module._signed_volume(points, repaired[4:]) > 0.0
    assert module._edge_direction_stats(repaired)["inconsistent_edges"] == 0


def _open_unit_box(*, inward: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unit box missing its x=0 and y=0 faces; free edges on origin planes."""
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
            [1, 3, 7],
            [1, 7, 5],
            [2, 6, 7],
            [2, 7, 3],
            [0, 2, 3],
            [0, 3, 1],
            [4, 5, 7],
            [4, 7, 6],
        ],
        dtype=np.int64,
    )
    if inward:
        triangles = triangles[:, [0, 2, 1]]
    tags = np.asarray([1, 1, 1, 1, 1, 1, 2, 2], dtype=np.int32)
    return points, triangles, tags


def test_open_origin_cut_inward_mesh_flips_globally():
    module = _load_script()
    points, triangles, tags = _open_unit_box(inward=True)
    mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )

    _, repair, topology = module._postprocess_mesh(
        mesh,
        [module.SourceSpec("LF", 20.0, 2)],
        symmetry_planes=("x0", "y0"),
        tolerance=1e-9,
    )

    assert repair["flipped_global"] == 8
    assert topology["signed_volume_step_units3"] > 0.0
    # The source on the z=1 plane fires +z once normals point outward.
    assert topology["source_normal_projections"]["LF"]["projection_z_step_units2"] > 0.0


def test_symmetry_reduced_source_anchor_wins_when_signed_volume_disagrees():
    module = _load_script()
    points, triangles, _ = _open_unit_box(inward=True)
    # This is the rollback-like acoustic-domain winding: the wall normals face
    # into the bore and the source at z=0 fires +z. Its open-shell signed
    # volume is negative, so the former volume oracle inverted every normal.
    tags = np.asarray([1, 1, 1, 1, 2, 2, 1, 1], dtype=np.int32)
    mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )

    _, repair, topology = module._postprocess_mesh(
        mesh,
        [module.SourceSpec("LF", 20.0, 2)],
        symmetry_planes=("x0", "y0"),
        tolerance=1e-9,
    )

    assert repair["before"]["signed_volume_step_units3"] < 0.0
    assert repair["flipped_global"] == 0
    assert topology["signed_volume_step_units3"] < 0.0
    assert topology["source_normal_projections"]["LF"]["projection_z_step_units2"] > 0.0


def test_symmetry_source_anchor_is_translation_and_rotation_invariant():
    module = _load_script()
    points, triangles, _ = _open_unit_box(inward=True)
    tags = np.asarray([1, 1, 1, 1, 2, 2, 1, 1], dtype=np.int32)
    baseline = module._symmetry_source_normal_projection(
        points,
        triangles,
        tags,
        source_tags={2},
        symmetry_planes=("x0", "y0"),
    )

    translated = points + np.asarray([17.0, -31.0, 43.0])
    rotated = points[:, [2, 0, 1]]
    translated_projection = module._symmetry_source_normal_projection(
        translated,
        triangles,
        tags,
        source_tags={2},
        symmetry_planes=("x0", "y0"),
    )
    rotated_projection = module._symmetry_source_normal_projection(
        rotated,
        triangles,
        tags,
        source_tags={2},
        symmetry_planes=("y0", "z0"),
    )

    assert baseline is not None and baseline > 0.0
    assert translated_projection == pytest.approx(baseline)
    assert rotated_projection == pytest.approx(baseline)


def test_symmetry_reduced_component_without_source_is_left_unjudged():
    module = _load_script()
    points, triangles, _ = _open_unit_box(inward=True)
    tags = np.full(len(triangles), 1, dtype=np.int32)

    repaired, stats = module._repair_triangle_winding(
        points,
        triangles,
        tags=tags,
        source_tags={2},
        symmetry_planes=("x0", "y0"),
        tolerance=1e-9,
    )

    np.testing.assert_array_equal(repaired, triangles)
    assert stats["flipped_global"] == 0
    assert stats["unjudged_symmetry_components"] == 1
    assert stats["unjudged_symmetry_no_source"] == 1


def test_open_mesh_with_off_plane_free_edges_keeps_winding():
    module = _load_script()
    points, triangles, tags = _open_unit_box(inward=True)
    points = points + np.asarray([5.0, 0.0, 0.0])
    mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )

    _, repair, _ = module._postprocess_mesh(
        mesh,
        [module.SourceSpec("LF", 20.0, 2)],
        symmetry_planes=(),
        tolerance=1e-9,
    )

    assert repair["flipped_global"] == 0
    assert repair["after"]["inconsistent_edges"] == 0


def test_open_origin_edges_do_not_flip_without_declared_symmetry():
    module = _load_script()
    points, triangles, tags = _open_unit_box(inward=True)
    mesh = meshio.Mesh(
        points=points,
        cells=[("triangle", triangles)],
        cell_data={"gmsh:physical": [tags]},
    )

    _, repair, topology = module._postprocess_mesh(
        mesh,
        [module.SourceSpec("LF", 20.0, 2)],
        symmetry_planes=(),
        tolerance=1e-9,
    )

    assert repair["flipped_global"] == 0
    assert topology["signed_volume_step_units3"] < 0.0
    assert topology["unexpected_free_edges"] > 0


def test_mesh_frequency_validation_warns_for_coarse_global_edge_far_from_source():
    module = _load_script()
    points = np.asarray(
        [
            # coarse rigid triangle far outside the source transition distance
            [1000.0, 0.0, 0.0],
            [1100.0, 0.0, 0.0],
            [1000.0, 100.0, 0.0],
            # fine source patch at the origin
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
    tags = np.asarray([1, 4], dtype=np.int32)

    validation = module._mesh_frequency_validation(
        points,
        triangles,
        tags,
        [module.SourceSpec("HF", 4.0, 4)],
        unit_scale_to_m=0.001,
        requested_max_frequency_hz=20_000.0,
        transition_mm=200.0,
    )

    assert validation["status"] == "valid"
    assert validation["global_status"] == "invalid"
    assert validation["per_source"]["HF"]["status"] == "valid"
    assert validation["warnings"]


def test_triangle_edge_lengths_deduplicates_reversed_shared_edges():
    module = _load_script()
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [2.0, 1.0, 0.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2], [2, 1, 3]], dtype=np.int64)

    lengths = module._triangle_edge_lengths(points, triangles)

    np.testing.assert_array_equal(
        lengths,
        np.asarray([2.0, np.sqrt(5.0), 1.0, 1.0, 2.0]),
    )


def test_mesh_frequency_validation_limits_source_by_nearby_coarse_walls():
    """BIGMEH_v4 regression: a fine HF patch next to coarse rigid horn walls
    must not validate to the patch limit, because the wave the patch launches
    travels along those walls."""
    module = _load_script()
    points = np.asarray(
        [
            # fine 4mm HF source patch at the origin
            [0.0, 0.0, 0.0],
            [4.0, 0.0, 0.0],
            [0.0, 4.0, 0.0],
            # coarse rigid wall triangle ~40mm edges, centroid ~30mm away
            [10.0, 10.0, 0.0],
            [50.0, 10.0, 0.0],
            [10.0, 50.0, 0.0],
            # far rigid triangle outside the transition distance
            [1000.0, 0.0, 0.0],
            [1100.0, 0.0, 0.0],
            [1000.0, 100.0, 0.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2], [3, 4, 5], [6, 7, 8]], dtype=np.int64)
    tags = np.asarray([4, 1, 1], dtype=np.int32)

    validation = module._mesh_frequency_validation(
        points,
        triangles,
        tags,
        [module.SourceSpec("HF", 4.0, 4)],
        unit_scale_to_m=0.001,
        requested_max_frequency_hz=10_000.0,
        transition_mm=200.0,
    )

    hf = validation["per_source"]["HF"]
    # patch alone supports ~10.1 kHz (4mm * sqrt(2) hypotenuse)
    assert hf["max_valid_frequency_hz"] > 10_000.0
    # nearby 40mm wall caps the effective band near 1 kHz
    assert hf["wall_triangle_count"] == 1
    assert hf["effective_max_valid_frequency_hz"] < 1_100.0
    assert hf["status"] == "invalid"
    assert validation["status"] == "invalid"
    assert any("rigid walls" in warning for warning in validation["warnings"])


def test_mesh_frequency_validation_hard_fails_underresolved_source():
    module = _load_script()
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [100.0, 0.0, 0.0],
            [0.0, 100.0, 0.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2]], dtype=np.int64)
    tags = np.asarray([4], dtype=np.int32)

    validation = module._mesh_frequency_validation(
        points,
        triangles,
        tags,
        [module.SourceSpec("HF", 4.0, 4)],
        unit_scale_to_m=0.001,
        requested_max_frequency_hz=20_000.0,
    )

    assert validation["status"] == "invalid"
    assert validation["global_status"] == "invalid"
    assert validation["invalid_sources"] == ["HF"]
    assert validation["per_source"]["HF"]["status"] == "invalid"


def test_density_configuration_uses_canonical_boundary_extension_and_gradual_fields():
    module = _load_script()
    specs = [
        module.SourceSpec("LF", 20.0, 2),
        module.SourceSpec("HF", 5.0, 4),
    ]

    density = module._density_configuration(
        specs,
        rigid_res_mm=40.0,
        transition_mm=200.0,
    )

    assert density["groups"] == ["rigid", "LF", "HF"]
    assert density["mesh_size_extend_from_boundary"] == 0
    assert density["source_fields"]["HF"]["field"] == "Distance/Threshold"
    assert density["source_fields"]["HF"]["size_min_mm"] == 5.0
    assert density["source_fields"]["HF"]["size_max_mm"] == 40.0
    assert density["source_fields"]["HF"]["dist_max_mm"] == 200.0


def test_density_configuration_records_explicit_manual_mm_sizes():
    module = _load_script()
    specs = [module.SourceSpec("LF", 20.0, 2), module.SourceSpec("HF", 5.0, 4)]

    density = module._density_configuration(
        specs,
        rigid_res_mm=30.0,
        transition_mm=200.0,
    )

    assert density["mesh_sizing_mode"] == "manual-mm"
    assert density["shadow_res_mm"] == pytest.approx(30.0)
    assert density["source_fields"]["LF"]["patch_size_mm"] == pytest.approx(20.0)
    assert density["source_fields"]["HF"]["patch_size_mm"] == pytest.approx(5.0)


def test_parse_refine_spec_accepts_explicit_mm_only():
    module = _load_script()
    mm_spec = module._parse_refine_spec("Rim:8mm")
    assert mm_spec.size_mm == 8.0
    assert mm_spec.role == "custom"

    with pytest.raises(Exception):
        module._parse_refine_spec("Baffle:3")
    with pytest.raises(Exception):
        module._parse_refine_spec("Rear:shadow")
    with pytest.raises(Exception):
        module._parse_refine_spec("bad")


def test_auto_radiating_promotes_source_bearing_shell_only_when_multibody():
    module = _load_script()
    # single shell -> no body signal -> nothing auto-promoted
    assert module._auto_radiating_surfaces({"Body": [1, 2, 3]}, {1}) == set()
    # waveguide shell carries the source -> its other faces become radiating;
    # the separate cabinet shell stays unclassified
    shells = {"Waveguide": [1, 2, 3], "Cabinet": [4, 5, 6]}
    assert module._auto_radiating_surfaces(shells, {1}) == {2, 3}


def test_weld_merges_near_duplicate_vertices():
    module = _load_script()
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0 + 1e-6, 0.0, 0.0],  # 1 micrometre from vertex 1
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2], [0, 3, 2]], dtype=np.int64)
    welded = module._weld_near_duplicate_vertices(points, triangles, tol_mm=5.0e-3)
    # vertex 3 collapses onto vertex 1
    assert 3 not in set(welded.flatten().tolist())
    assert 1 in set(welded.flatten().tolist())


def test_remove_degenerate_drops_needle_slivers():
    module = _load_script()
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [10.0, 0.0, 0.0],
            [10.0, 1.0e-4, 0.0],  # needle: ~1e-4 mm tall over 10 mm base
            [0.0, 5.0, 0.0],
        ],
        dtype=np.float64,
    )
    triangles = np.asarray([[0, 1, 2], [0, 1, 3]], dtype=np.int64)
    tags = np.asarray([1, 1], dtype=np.int32)
    kept, kept_tags, removed = module._remove_degenerate_triangles(
        points, triangles, tags, min_quality=module.DEGENERATE_MIN_QUALITY
    )
    assert removed == 1
    assert len(kept) == 1
    assert kept.tolist() == [[0, 1, 3]]


def test_compact_unused_vertices_drops_orphans():
    module = _load_script()
    points = np.arange(15, dtype=np.float64).reshape(5, 3)
    triangles = np.asarray([[0, 2, 4]], dtype=np.int64)
    out_points, out_triangles = module._compact_unused_vertices(points, triangles)
    assert len(out_points) == 3
    assert out_triangles.tolist() == [[0, 1, 2]]


def test_missing_requested_source_is_hard_failure_by_default(monkeypatch, tmp_path):
    module = _load_script()
    step_path = tmp_path / "model.step"
    step_path.write_text("", encoding="ascii")
    specs = [
        module.SourceSpec("LF", 20.0, 2),
        module.SourceSpec("HF", 5.0, 4),
    ]
    monkeypatch.setattr(module, "_parse_named_shell_faces", lambda _path: {"LF": [10]})
    monkeypatch.setattr(module, "_parse_styled_face_groups", lambda _path: {})
    monkeypatch.setattr(module, "_advanced_face_order", lambda _path: [10])
    monkeypatch.setattr(module.gmsh.model, "getEntities", lambda dim: [(2, 101)])

    with pytest.raises(RuntimeError, match="source 'HF' not found"):
        module._map_step_faces_to_gmsh_surfaces(step_path, specs)


def test_skip_missing_sources_is_explicit_escape_hatch(monkeypatch, tmp_path):
    module = _load_script()
    step_path = tmp_path / "model.step"
    step_path.write_text("", encoding="ascii")
    specs = [
        module.SourceSpec("LF", 20.0, 2),
        module.SourceSpec("HF", 5.0, 4),
    ]
    monkeypatch.setattr(module, "_parse_named_shell_faces", lambda _path: {"LF": [10]})
    monkeypatch.setattr(module, "_parse_styled_face_groups", lambda _path: {})
    monkeypatch.setattr(module, "_advanced_face_order", lambda _path: [10])
    monkeypatch.setattr(module.gmsh.model, "getEntities", lambda dim: [(2, 101)])

    mapping = module._map_step_faces_to_gmsh_surfaces(
        step_path,
        specs,
        skip_missing_sources=True,
    )

    assert mapping == {"LF": [101]}
    assert module._map_step_faces_to_gmsh_surfaces.last_missing["HF"]["tag"] == 4


def test_a_source_is_matched_only_by_its_own_painted_label(monkeypatch, tmp_path):
    """No label ever stands in for another, in either direction.

    Replaces two tests for a PORT_EXIT_L/_R -> PORT_EXIT fallback, deleted
    2026-08-11: across 471 recorded prepare runs it never fired once, and the
    only two runs requesting PORT_EXIT_L resolved it directly from an
    appearance/style of that exact name. The second of those tests had also
    become a false pass -- it asserted the fallback was correctly SUPPRESSED
    when both legacy names were requested, which now holds trivially because
    there is no fallback at all.
    """
    module = _load_script()
    step_path = tmp_path / "model.step"
    step_path.write_text("", encoding="ascii")
    specs = [
        module.SourceSpec("LF", 20.0, 2),
        module.SourceSpec("PORT_EXIT_L", 25.0, 10),
    ]
    monkeypatch.setattr(module, "_parse_named_shell_faces", lambda _path: {})
    monkeypatch.setattr(
        module,
        "_parse_styled_face_groups",
        lambda _path: {"LF": [10], "PORT_EXIT": [11, 12]},
    )
    monkeypatch.setattr(module, "_advanced_face_order", lambda _path: [10, 11, 12])
    monkeypatch.setattr(
        module.gmsh.model, "getEntities", lambda dim: [(2, 101), (2, 102), (2, 103)]
    )

    mapping = module._map_step_faces_to_gmsh_surfaces(
        step_path,
        specs,
        skip_missing_sources=True,
    )

    # PORT_EXIT is painted, PORT_EXIT_L is not: the request is MISSING, not
    # quietly satisfied by the neighbouring label.
    assert mapping == {"LF": [101]}
    assert set(module._map_step_faces_to_gmsh_surfaces.last_missing) == {"PORT_EXIT_L"}
    assert (
        module._map_step_faces_to_gmsh_surfaces.last_origins["LF"]
        == "appearance/style"
    )


# ---------------------------------------------------------------------------
# Geometry-level auto reduction (--symmetry-planes auto-cut)
# ---------------------------------------------------------------------------


@contextmanager
def _gmsh_session(module):
    """Own a gmsh session for a geometry test, whatever the body does."""
    module.gmsh.initialize()
    try:
        module.gmsh.option.setNumber("General.Terminal", 0)
        module.gmsh.model.add("auto_reduce_fixture")
        yield module.gmsh
    finally:
        module.gmsh.finalize()


def _box_surfaces(gmsh_module, x0, y0, z0, dx, dy, dz):
    """Add an OCC box and return its face tags."""
    volume = gmsh_module.model.occ.addBox(x0, y0, z0, dx, dy, dz)
    gmsh_module.model.occ.synchronize()
    return [
        tag
        for dim, tag in gmsh_module.model.getBoundary(
            [(3, volume)], combined=False, oriented=False
        )
        if dim == 2
    ]


def _face_with_center(gmsh_module, surfaces, axis, value, tolerance=1e-9):
    """The face whose centre of mass sits at ``value`` along ``axis``."""
    for surface in surfaces:
        com = gmsh_module.model.occ.getCenterOfMass(2, surface)
        if abs(float(com[axis]) - value) <= tolerance:
            return surface
    raise AssertionError(f"no face centred at axis {axis} = {value}")


def test_auto_reduce_cuts_only_the_plane_the_full_model_mirrors_about():
    module = _load_script()
    with _gmsh_session(module) as gmsh_module:
        # Symmetric about x=0; deliberately offset in y (it straddles y=0 but
        # does not mirror about it) and entirely above z=0.
        surfaces = _box_surfaces(gmsh_module, -1.0, -0.5, 0.0, 2.0, 2.0, 2.0)
        roles = {surface: "rigid" for surface in surfaces}
        # A source face that straddles the plane maps onto itself, which is a
        # valid mirror pair and has to pass the role test.
        straddling = _face_with_center(gmsh_module, surfaces, 1, -0.5)
        roles[straddling] = "HF"

        planes, mapping, report = module._auto_reduce_geometry(
            surfaces=surfaces,
            roles=roles,
            grid=5,
            tolerance_rel=module.AUTO_REDUCE_TOL_REL,
        )

        assert planes == ("x0",)
        assert report["cut_planes"] == ["x0"]
        assert report["planes"]["x0"]["accepted"] is True
        assert report["planes"]["x0"]["points_off_model"] == 0
        assert report["planes"]["x0"]["role_mismatches"] == 0
        assert report["planes"]["x0"]["max_residual_step_units"] < (
            report["planes"]["x0"]["tolerance_step_units"]
        )
        # y=0 is straddled but not mirrored; z=0 is not straddled at all.
        assert report["planes"]["y0"]["accepted"] is False
        assert report["planes"]["y0"]["points_off_model"] > 0
        assert report["planes"]["z0"]["accepted"] is False
        assert "straddle" in report["planes"]["z0"]["reason"]

        # The source face survives the cut and is still one face.
        assert mapping[straddling]
        remaining = [tag for dim, tag in gmsh_module.model.getEntities(2) if dim == 2]
        # The x=-1 face is gone, no face was created on the cut plane.
        assert len(remaining) == 5
        assert gmsh_module.model.getEntities(3) == []
        for surface in remaining:
            bbox = gmsh_module.model.getBoundingBox(2, surface)
            assert bbox[0] > -1.0e-6
            assert bbox[3] > 1.0e-6, "a face left lying on x=0 would mesh as a baffle"


def test_auto_reduce_cut_leaves_the_meshed_boundary_open_on_the_plane():
    module = _load_script()
    with _gmsh_session(module) as gmsh_module:
        surfaces = _box_surfaces(gmsh_module, -1.0, -0.5, 0.0, 2.0, 2.0, 2.0)
        planes, _mapping, _report = module._auto_reduce_geometry(
            surfaces=surfaces,
            roles={surface: "rigid" for surface in surfaces},
            grid=5,
            tolerance_rel=module.AUTO_REDUCE_TOL_REL,
        )
        assert planes == ("x0",)

        gmsh_module.option.setNumber("Mesh.MeshSizeMin", 0.25)
        gmsh_module.option.setNumber("Mesh.MeshSizeMax", 0.5)
        gmsh_module.model.mesh.generate(2)
        node_tags, coords, _ = gmsh_module.model.mesh.getNodes()
        index = {int(tag): i for i, tag in enumerate(node_tags)}
        points = np.asarray(coords, dtype=np.float64).reshape(-1, 3)
        triangles = []
        for dim, surface in gmsh_module.model.getEntities(2):
            if dim != 2:
                continue
            _types, _tags, node_lists = gmsh_module.model.mesh.getElements(2, surface)
            for nodes in node_lists:
                flat = [index[int(node)] for node in nodes]
                triangles.extend(
                    flat[i : i + 3] for i in range(0, len(flat), 3)
                )
        triangles = np.asarray(triangles, dtype=np.int64)

    assert len(triangles) > 0
    # Nothing may lie flat on the cut plane: the solver rejects such a triangle
    # because a symmetry plane is an image plane, not a physical boundary.
    on_plane = np.all(np.abs(points[triangles][:, :, 0]) <= 1e-9, axis=1)
    assert not np.any(on_plane)
    # And the free-edge detector must recover exactly the plane that was cut,
    # with no knowledge that a cut happened.
    detected, detection = module._detect_symmetry_planes(
        points, triangles, tolerance=1e-6
    )
    assert detected == ("x0",)
    assert detection["plane_free_edge_counts"]["x0"] > 0


def test_auto_reduce_refuses_a_full_model_that_does_not_mirror():
    module = _load_script()
    with _gmsh_session(module) as gmsh_module:
        # Straddles x=0 but is one unit longer on the positive side.
        surfaces = _box_surfaces(gmsh_module, -1.0, -0.5, 0.0, 3.0, 2.0, 2.0)
        planes, mapping, report = module._auto_reduce_geometry(
            surfaces=surfaces,
            roles={surface: "rigid" for surface in surfaces},
            grid=5,
            tolerance_rel=module.AUTO_REDUCE_TOL_REL,
        )

        assert planes == ()
        assert mapping == {}
        assert report["cut"] is None
        verdict = report["planes"]["x0"]
        assert verdict["accepted"] is False
        assert verdict["points_off_model"] > 0
        assert verdict["worst_off_model_distance_step_units"] > (
            verdict["tolerance_step_units"]
        )
        assert verdict["failure_samples"]
        # Nothing was touched: the geometry is still the full model.
        assert len(gmsh_module.model.getEntities(2)) == len(surfaces)


def test_auto_reduce_refuses_sources_that_mirror_geometrically_but_not_by_role():
    module = _load_script()
    with _gmsh_session(module) as gmsh_module:
        # Two boxes that are each other's mirror image about x=0, so the
        # geometry test alone passes. Only their source painting differs.
        right = _box_surfaces(gmsh_module, 0.2, -0.5, 0.0, 0.8, 1.0, 1.0)
        left = _box_surfaces(gmsh_module, -1.0, -0.5, 0.0, 0.8, 1.0, 1.0)
        surfaces = right + left
        right_front = _face_with_center(gmsh_module, right, 1, -0.5)
        left_front = _face_with_center(gmsh_module, left, 1, -0.5)

        roles = {surface: "rigid" for surface in surfaces}
        roles[right_front] = "HF"
        mismatched = module._evaluate_plane_symmetry(
            "x0",
            samples={
                surface: module._sample_surface_points(surface, grid=5)
                for surface in surfaces
            },
            roles=roles,
            bboxes={
                surface: tuple(
                    float(v) for v in gmsh_module.model.getBoundingBox(2, surface)
                )
                for surface in surfaces
            },
            tolerance=0.01,
            span_tolerance=1e-3,
        )

        assert mismatched.accepted is False
        assert mismatched.points_off_model == 0, "the geometry itself does mirror"
        assert mismatched.role_mismatches > 0
        assert any(
            sample["kind"] == "role_mismatch" for sample in mismatched.failure_samples
        )

        # Painting the mirror partner with the same role is the only difference,
        # and it flips the verdict.
        roles[left_front] = "HF"
        matched = module._evaluate_plane_symmetry(
            "x0",
            samples={
                surface: module._sample_surface_points(surface, grid=5)
                for surface in surfaces
            },
            roles=roles,
            bboxes={
                surface: tuple(
                    float(v) for v in gmsh_module.model.getBoundingBox(2, surface)
                )
                for surface in surfaces
            },
            tolerance=0.01,
            span_tolerance=1e-3,
        )
        assert matched.accepted is True
        assert matched.role_mismatches == 0


def test_auto_reduce_ignores_untrimmed_surface_extensions():
    module = _load_script()
    with _gmsh_session(module) as gmsh_module:
        # A bore punched through the +x half only. Both halves of the top face
        # lie on the same underlying plane, so a closest-point test that
        # ignored trimming would happily mirror the bore onto solid material
        # and call x=0 symmetric. y=0 stays a genuine mirror plane, which keeps
        # the test honest: the trim filter has to reject one and keep the other.
        box = gmsh_module.model.occ.addBox(-1.0, -0.5, 0.0, 2.0, 1.0, 1.0)
        bore = gmsh_module.model.occ.addCylinder(0.5, 0.0, -0.1, 0.0, 0.0, 1.2, 0.2)
        gmsh_module.model.occ.cut([(3, box)], [(3, bore)])
        gmsh_module.model.occ.synchronize()
        surfaces = [tag for dim, tag in gmsh_module.model.getEntities(2) if dim == 2]

        planes, _mapping, report = module._auto_reduce_geometry(
            surfaces=surfaces,
            roles={surface: "rigid" for surface in surfaces},
            grid=9,
            tolerance_rel=module.AUTO_REDUCE_TOL_REL,
        )

        assert planes == ("y0",)
        assert report["planes"]["x0"]["accepted"] is False
        assert report["planes"]["x0"]["points_off_model"] > 0


def test_symmetry_planes_auto_cut_is_opt_in_and_off_by_default(tmp_path):
    module = _load_script()
    base = [
        "--step", str(tmp_path / "model.step"),
        "--out", str(tmp_path / "out"),
        "--source", "HF:5",
    ]

    default_args = module.parse_args(base)
    assert default_args.symmetry_planes is None
    assert default_args.auto_reduce_tol_rel == module.AUTO_REDUCE_TOL_REL
    assert default_args.auto_reduce_grid == module.AUTO_REDUCE_GRID

    auto_cut_args = module.parse_args(base + ["--symmetry-planes", "auto-cut"])
    assert auto_cut_args.symmetry_planes == "auto-cut"
    # 'auto-cut' is handled in main() before the plane parser, which still only
    # understands the explicit plane vocabulary.
    with pytest.raises(ValueError):
        module._parse_symmetry_planes("auto-cut", quadrants=1234)


def test_auto_cut_snap_band_is_converted_from_mm_to_step_units():
    module = _load_script()

    assert module._millimetres_to_step_units(1.0e-4, 1.0e-3) == pytest.approx(1.0e-4)
    assert module._millimetres_to_step_units(1.0e-4, 1.0) == pytest.approx(1.0e-7)
    assert module._millimetres_to_step_units(1.0e-4, 0.0254) == pytest.approx(
        1.0e-4 / 25.4
    )


@pytest.mark.skipif(
    not os.environ.get("HORNLAB_AUTO_REDUCE_STEP"),
    reason="set HORNLAB_AUTO_REDUCE_STEP to a full mirror-symmetric STEP to run",
)
def test_auto_reduce_matches_expected_planes_for_a_real_step(tmp_path):
    """End-to-end auto-cut against a real CAD export, when one is provided.

    ``HORNLAB_AUTO_REDUCE_STEP`` points at the STEP; ``HORNLAB_AUTO_REDUCE_ARGS``
    carries the whitespace-separated ``--source`` flags it needs. The reference
    models are licensed CC BY-NC-SA and are never vendored into this repo.
    """
    module = _load_script()
    step = Path(os.environ["HORNLAB_AUTO_REDUCE_STEP"]).expanduser()
    extra = os.environ.get("HORNLAB_AUTO_REDUCE_ARGS", "--source HF:4 --source LF:20").split()
    expected = os.environ.get("HORNLAB_AUTO_REDUCE_EXPECT_PLANES", "x0").split(",")
    out = tmp_path / "out"
    assert module.main(
        ["--step", str(step), "--out", str(out), *extra, "--symmetry-planes", "auto-cut"]
    ) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["auto_reduce"]["cut_planes"] == expected
    assert manifest["symmetry_planes"] == expected
    assert manifest["auto_reduce"]["post_cut_planes_confirmed"] is True
    assert manifest["solver_ready"] is True
