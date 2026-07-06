from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "solve_fusion_wg_metal.py"
REGEN_SCRIPT = ROOT / "scripts" / "regenerate_fusion_derived_artifacts.py"
SMOKE_MESH = (
    ROOT
    / "runs"
    / "scratch"
    / "260609-fusion-addin-normalized-sources-smoke"
    / "tagged_sources.msh"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("solve_fusion_wg_metal", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_mesh(path: Path, points: np.ndarray, triangles: np.ndarray, tag: int) -> None:
    import meshio

    tags = np.full(len(triangles), int(tag), dtype=np.int32)
    meshio.write(
        path,
        meshio.Mesh(
            points=np.asarray(points, dtype=np.float64),
            cells=[("triangle", np.asarray(triangles, dtype=np.int64))],
            cell_data={
                "gmsh:physical": [tags],
                "gmsh:geometrical": [tags],
            },
        ),
        file_format="gmsh22",
        binary=False,
    )


def _write_spherical_cap_mesh(
    path: Path,
    *,
    throat_radius_m: float,
    sphere_radius_m: float,
    tag: int,
    rings: int = 24,
    segments: int = 192,
) -> None:
    theta_max = np.arcsin(throat_radius_m / sphere_radius_m)
    center_z = -sphere_radius_m * np.cos(theta_max)
    points = [[0.0, 0.0, center_z + sphere_radius_m]]
    for ring in range(1, rings + 1):
        theta = theta_max * ring / rings
        radius = sphere_radius_m * np.sin(theta)
        z = center_z + sphere_radius_m * np.cos(theta)
        for segment in range(segments):
            phi = 2.0 * np.pi * segment / segments
            points.append([radius * np.cos(phi), radius * np.sin(phi), z])

    triangles = []
    first_ring = 1
    for segment in range(segments):
        triangles.append([
            0,
            first_ring + segment,
            first_ring + ((segment + 1) % segments),
        ])
    for ring in range(1, rings):
        inner = 1 + (ring - 1) * segments
        outer = 1 + ring * segments
        for segment in range(segments):
            next_segment = (segment + 1) % segments
            triangles.append([inner + segment, outer + segment, outer + next_segment])
            triangles.append([inner + segment, outer + next_segment, inner + next_segment])

    _write_mesh(path, np.asarray(points), np.asarray(triangles), tag)


def _write_flat_disc_mesh(
    path: Path,
    *,
    radius_m: float,
    tag: int,
    segments: int = 192,
) -> None:
    points = [[0.0, 0.0, 0.0]]
    for segment in range(segments):
        phi = 2.0 * np.pi * segment / segments
        points.append([radius_m * np.cos(phi), radius_m * np.sin(phi), 0.0])
    triangles = [
        [0, 1 + segment, 1 + ((segment + 1) % segments)]
        for segment in range(segments)
    ]
    _write_mesh(path, np.asarray(points), np.asarray(triangles), tag)


def _run_manifest_path(run_dir: Path, name: str) -> Path:
    path = run_dir / "manifests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_fake_solver_imports(workspace_root: Path, *, origin: str) -> None:
    sim_methods = workspace_root / "hornlab-sim" / "hornlab_sim" / "methods"
    sim_methods.mkdir(parents=True)
    (sim_methods.parent / "__init__.py").write_text(
        f"ORIGIN = {origin!r}\n",
        encoding="utf-8",
    )
    sim_methods.joinpath("__init__.py").write_text(
        "\n".join(
            [
                f"bandpass = {origin + '-bandpass'!r}",
                f"driver_coupling = {origin + '-driver-coupling'!r}",
                "class _RadiationImpedance:",
                "    RHO_AIR = 1.2",
                "    C_AIR = 343.0",
                "radiation_impedance = _RadiationImpedance()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plots_pkg = workspace_root / "hornlab-plots" / "hornlab_plots"
    plots_pkg.mkdir(parents=True)
    plots_pkg.joinpath("__init__.py").write_text(
        "\n".join(
            [
                f"ORIGIN = {origin!r}",
                "class FrequencyResponseCurve:",
                "    def __init__(self, **kwargs):",
                "        self.__dict__.update(kwargs)",
                "def save_directivity_plot(*args, **kwargs):",
                "    return None",
                "def save_interference_heatmap(*args, **kwargs):",
                "    return None",
                "def save_directivity_power_plot(*args, **kwargs):",
                "    return None",
                "def save_beamwidth_plot(*args, **kwargs):",
                "    return None",
                "def save_group_delay_plot(*args, **kwargs):",
                "    return None",
                "def save_excursion_plot(*args, **kwargs):",
                "    return None",
                "def save_impedance_plot(*args, **kwargs):",
                "    return None",
                "def save_frequency_response_plot(*args, **kwargs):",
                "    return None",
                "def get_theme(*args, **kwargs):",
                "    return None",
                "def set_theme(*args, **kwargs):",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    bem_pkg = workspace_root / "hornlab-metal-bem" / "hornlab_metal_bem"
    bem_pkg.mkdir(parents=True)
    bem_pkg.joinpath("__init__.py").write_text(
        "\n".join(
            [
                f"ORIGIN = {origin!r}",
                "class ObservationConfig:",
                "    pass",
                "class ObservationFrame:",
                "    pass",
                "class SolveConfig:",
                "    pass",
                "def solve(*args, **kwargs):",
                "    return None",
                "def solve_multi_source(*args, **kwargs):",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_workspace_sibling_path_precedence_prefers_top_level_packages(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "hornlab-fusion-addin"
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    fake_script = scripts_dir / "solve_fusion_wg_metal.py"
    fake_script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    helper_dir = repo / "fusion-addins" / "WGMetalPipeline"
    helper_dir.mkdir(parents=True)
    helper_dir.joinpath("fusion_pipeline_launch.py").write_text(
        "\n".join(
            [
                "class DriverLemParseError(Exception):",
                "    pass",
                "class DriverLemSpec:",
                "    pass",
                "def parse_driver_lem_cli_entries(entries):",
                "    return {}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _write_fake_solver_imports(tmp_path, origin="top")
    legacy_root = tmp_path / "HornLab"
    _write_fake_solver_imports(legacy_root, origin="legacy")

    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in list(sys.modules):
        if name == "fusion_pipeline_launch" or name.startswith(
            ("hornlab_sim", "hornlab_plots", "hornlab_metal_bem")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)

    try:
        spec = importlib.util.spec_from_file_location(
            "solve_fusion_wg_metal_precedence_test",
            fake_script,
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.bandpass == "top-bandpass"
        assert module.HORNLAB_SIM_DIR == (
            tmp_path / "hornlab-sim" / "hornlab_sim"
        ).resolve()
        assert module.HORNLAB_PLOTS_DIR == (
            tmp_path / "hornlab-plots" / "hornlab_plots"
        ).resolve()
        assert module.METAL_BEM_DIR == (
            tmp_path / "hornlab-metal-bem" / "hornlab_metal_bem"
        ).resolve()
    finally:
        for name in list(sys.modules):
            if name == "fusion_pipeline_launch" or name.startswith(
                ("hornlab_sim", "hornlab_plots", "hornlab_metal_bem")
            ):
                sys.modules.pop(name, None)


def test_voltage_drive_pressure_scales_by_cone_acceleration():
    """Bases are unit-normal-ACCELERATION solves: p_V = (j w U / S) * basis.

    Constant volume velocity must give +6 dB/oct with +90 deg phase; a
    mass-controlled velocity U = 1/(j w) must give a flat, real field.
    """
    module = _load_script()
    freqs = np.array([100.0, 200.0])
    basis = np.ones((2, 1, 1), dtype=np.complex128)

    constant_u = module._voltage_drive_pressure(
        np.array([1.0 + 0.0j, 1.0 + 0.0j]),
        frequencies_hz=freqs,
        diaphragm_area_m2=2.0,
        basis_pressure=basis,
    )
    assert np.abs(constant_u[1, 0, 0]) == pytest.approx(2.0 * np.abs(constant_u[0, 0, 0]))
    assert np.angle(constant_u[0, 0, 0]) == pytest.approx(np.pi / 2)
    assert np.abs(constant_u[0, 0, 0]) == pytest.approx(2.0 * np.pi * 100.0 / 2.0)

    omega = 2.0 * np.pi * freqs
    mass_controlled = module._voltage_drive_pressure(
        1.0 / (1j * omega),
        frequencies_hz=freqs,
        diaphragm_area_m2=1.0,
        basis_pressure=basis,
    )
    np.testing.assert_allclose(mass_controlled, np.ones_like(mass_controlled))


def test_solver_surface_avg_to_self_impedance_uses_solver_space_then_conjugates():
    module = _load_script()
    freqs = np.array([100.0, 250.0])
    p_avg_solver = np.array([1.0 + 2.0j, -0.25 + 0.5j])

    z_self = module._solver_surface_avg_to_self_impedance(
        freqs,
        p_avg_solver,
        source_area_m2=0.02,
    )

    omega = 2.0 * np.pi * freqs
    expected = np.conjugate(1j * omega * p_avg_solver) / 0.02
    np.testing.assert_allclose(z_self, expected)


def test_projected_area_helper_spherical_cap_matches_throat_area(tmp_path):
    module = _load_script()
    tag = 7
    throat_radius = 0.10
    mesh_path = tmp_path / "spherical_cap.msh"
    _write_spherical_cap_mesh(
        mesh_path,
        throat_radius_m=throat_radius,
        sphere_radius_m=0.15,
        tag=tag,
    )

    projected, surface, axis, curved = module._mesh_tag_projected_area_m2(
        mesh_path,
        tag,
        mesh_scale=1.0,
    )

    expected_projected = np.pi * throat_radius**2
    assert projected == pytest.approx(expected_projected, rel=2.0e-3)
    assert surface > expected_projected * 1.05
    assert curved is True
    np.testing.assert_allclose(np.abs(axis), [0.0, 0.0, 1.0], atol=1.0e-12)


def test_basis_self_impedance_uses_projected_area_for_axial_curved_cap(tmp_path):
    module = _load_script()
    tag = 7
    mesh_path = tmp_path / "spherical_cap.msh"
    _write_spherical_cap_mesh(
        mesh_path,
        throat_radius_m=0.10,
        sphere_radius_m=0.15,
        tag=tag,
    )
    surface_area = module._mesh_tag_area_m2(mesh_path, tag, mesh_scale=1.0)
    projected_area, _surface, _axis, curved = module._mesh_tag_projected_area_m2(
        mesh_path,
        tag,
        mesh_scale=1.0,
    )
    assert curved is True
    freqs = np.array([100.0, 250.0], dtype=np.float64)
    p_avg_solver = np.array([1.0 + 2.0j, -0.25 + 0.5j], dtype=np.complex128)
    basis = module.PressureBasis(
        source_name="LF",
        source_tag=tag,
        frequencies_hz=freqs,
        observation_angles_deg=np.array([0.0]),
        observation_planes=np.array(["horizontal"]),
        pressure_complex=np.ones((2, 1, 1), dtype=np.complex128),
        surface_pressure_avg_solver=p_avg_solver,
        source_area_m2=surface_area,
        source_motion="axial",
    )

    z_self, payload = module._basis_self_impedance(
        mesh_path,
        SimpleNamespace(mesh_scale=1.0),
        {"name": "LF", "tag": tag, "source_area_m2": surface_area},
        basis,
    )

    omega = 2.0 * np.pi * freqs
    expected = np.conjugate(1j * omega * p_avg_solver) / projected_area
    np.testing.assert_allclose(z_self, expected)
    assert payload["source_area_kind"] == "projected"
    assert payload["source_area_m2"] == pytest.approx(projected_area)
    assert payload["surface_area_m2"] == pytest.approx(surface_area)
    assert payload["formula"].endswith("/A_projected")


def test_basis_self_impedance_keeps_surface_area_for_normal_and_flat_sources(tmp_path):
    module = _load_script()
    tag = 7
    freqs = np.array([100.0, 250.0], dtype=np.float64)
    p_avg_solver = np.array([1.0 + 2.0j, -0.25 + 0.5j], dtype=np.complex128)
    args = SimpleNamespace(mesh_scale=1.0)

    curved_mesh = tmp_path / "spherical_cap.msh"
    _write_spherical_cap_mesh(
        curved_mesh,
        throat_radius_m=0.10,
        sphere_radius_m=0.15,
        tag=tag,
    )
    curved_surface = module._mesh_tag_area_m2(curved_mesh, tag, mesh_scale=1.0)
    normal_basis = module.PressureBasis(
        source_name="LF",
        source_tag=tag,
        frequencies_hz=freqs,
        observation_angles_deg=np.array([0.0]),
        observation_planes=np.array(["horizontal"]),
        pressure_complex=np.ones((2, 1, 1), dtype=np.complex128),
        surface_pressure_avg_solver=p_avg_solver,
        source_area_m2=curved_surface,
    )
    normal_z, normal_payload = module._basis_self_impedance(
        curved_mesh,
        args,
        {"name": "LF", "tag": tag, "source_area_m2": curved_surface},
        normal_basis,
    )
    omega = 2.0 * np.pi * freqs
    np.testing.assert_allclose(
        normal_z,
        np.conjugate(1j * omega * p_avg_solver) / curved_surface,
    )
    assert "source_area_kind" not in normal_payload
    assert normal_payload["formula"].endswith("/S_tag")

    flat_mesh = tmp_path / "flat_disc.msh"
    _write_flat_disc_mesh(flat_mesh, radius_m=0.10, tag=tag)
    flat_surface = module._mesh_tag_area_m2(flat_mesh, tag, mesh_scale=1.0)
    axial_flat_basis = module.PressureBasis(
        source_name="LF",
        source_tag=tag,
        frequencies_hz=freqs,
        observation_angles_deg=np.array([0.0]),
        observation_planes=np.array(["horizontal"]),
        pressure_complex=np.ones((2, 1, 1), dtype=np.complex128),
        surface_pressure_avg_solver=p_avg_solver,
        source_area_m2=flat_surface,
        source_motion="axial",
    )
    flat_z, flat_payload = module._basis_self_impedance(
        flat_mesh,
        args,
        {"name": "LF", "tag": tag, "source_area_m2": flat_surface},
        axial_flat_basis,
    )
    np.testing.assert_allclose(
        flat_z,
        np.conjugate(1j * omega * p_avg_solver) / flat_surface,
    )
    assert flat_payload["source_area_kind"] == "surface"
    assert flat_payload["projected_area_used"] is False
    assert flat_payload["formula"].endswith("/S_tag")


def test_pressure_basis_npz_embeds_surface_avg_area_and_normalization(tmp_path):
    module = _load_script()
    freqs = np.array([100.0, 200.0])
    result = SimpleNamespace(
        frequencies_hz=freqs,
        observation_angles_deg=np.array([0.0, 90.0]),
        observation_planes=np.array(["horizontal"]),
        pressure_complex=np.ones((2, 1, 2), dtype=np.complex128),
        surface_pressure_avg={3: np.array([1.0 + 0.5j, 2.0 + 0.25j])},
    )
    path = tmp_path / "MF_pressure_basis.npz"

    module._write_pressure_basis_npz(
        path,
        result,
        source_name="MF",
        source_tag=3,
        source_area_m2=0.0123,
    )

    with np.load(path, allow_pickle=False) as data:
        assert str(data["source_normalization"].item()) == "unit_normal_acceleration"
        assert str(data["surface_pressure_avg_phase_convention"].item()) == (
            module.SURFACE_PRESSURE_AVG_PHASE_CONVENTION
        )
        assert float(data["source_area_m2"]) == pytest.approx(0.0123)
        np.testing.assert_allclose(
            data["surface_pressure_avg_solver"],
            np.array([1.0 + 0.5j, 2.0 + 0.25j]),
        )


def _load_regen_driver():
    spec = importlib.util.spec_from_file_location(
        "regenerate_fusion_derived_artifacts",
        REGEN_SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_writes_manifest_for_current_smoke_mesh(tmp_path):
    if not SMOKE_MESH.exists():
        pytest.skip(f"smoke mesh not available: {SMOKE_MESH}")
    module = _load_script()
    rc = module.main(
        [
            "--mesh",
            str(SMOKE_MESH),
            "--out",
            str(tmp_path),
            "--source",
            "LF:20:2",
            "--source",
            "HF:5:4",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert not (tmp_path / "direct_solve_manifest.json").exists()
    manifest = _run_manifest_path(tmp_path, "direct_solve_manifest.json").read_text(
        encoding="utf-8"
    )
    assert '"native_symmetry_plane": "yz+xz"' in manifest
    assert '"native_check_open_edges": true' in manifest
    assert '"bem_formulation": "complex_k"' in manifest
    assert '"complex_k_shift": 0.005' in manifest
    payload = json.loads(manifest)
    assert payload["config"]["hornlab_sim_dir"]
    assert payload["config"]["hornlab_plots_dir"]
    assert payload["config"]["hornlab_metal_bem_dir"]
    assert '"tag": 2' in manifest
    assert '"tag": 4' in manifest


def test_dry_run_orders_canonical_sources_hf_mf_lf(tmp_path):
    module = _load_script()
    mesh_path = tmp_path / "dummy.msh"
    mesh_path.write_text("", encoding="utf-8")

    rc = module.main(
        [
            "--mesh",
            str(mesh_path),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:2",
            "--source",
            "PORT_EXIT:10",
            "--source",
            "MF:3",
            "--source",
            "HF:4",
            "--dry-run",
        ]
    )

    assert rc == 0
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "direct_solve_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert [source["name"] for source in manifest["sources"]] == [
        "HF",
        "MF",
        "LF",
        "PORT_EXIT",
    ]


def test_explicit_bigmeh_frame_is_orthonormal():
    if not SMOKE_MESH.exists():
        pytest.skip(f"smoke mesh not available: {SMOKE_MESH}")
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(SMOKE_MESH),
            "--out",
            str(ROOT / "runs" / "scratch" / "unused"),
            "--source",
            "LF:2",
            "--dry-run",
        ]
    )
    frame = module._build_frame(args)
    assert np.allclose(frame.axis, [0.0, 0.0, 1.0])
    assert np.allclose(frame.origin, [0.0, 0.0, 0.31])
    assert np.allclose(frame.u, [1.0, 0.0, 0.0])
    assert np.allclose(frame.v, [0.0, 1.0, 0.0])
    assert np.isclose(np.dot(frame.axis, frame.u), 0.0)
    assert np.isclose(np.dot(frame.axis, frame.v), 0.0)


def test_source_freq_max_overrides_band_for_that_source_only():
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(ROOT / "runs" / "scratch" / "unused.msh"),
            "--out",
            str(ROOT / "runs" / "scratch" / "unused"),
            "--source",
            "LF:2",
            "--source",
            "HF:4",
            "--source-freq-max",
            "LF:1475.5",
            "--freq-max-hz",
            "20000",
            "--dry-run",
        ]
    )
    frame = module._build_frame(args)
    limits = module._parse_source_freq_max(args.source_freq_max)

    assert limits == {"LF": 1475.5}
    lf_cfg = module._build_config(
        args, source_tag=2, frame=frame, freq_max_hz=limits.get("LF")
    )
    hf_cfg = module._build_config(
        args, source_tag=4, frame=frame, freq_max_hz=limits.get("HF")
    )
    assert lf_cfg.freq_max_hz == 1475.5
    assert hf_cfg.freq_max_hz == 20000.0


def test_driver_lem_source_motion_is_axial_except_passive_cardioid_owner(tmp_path):
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "unused.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "LF:2",
            "--driver-lem",
            "LF:Sd=200,Bl=8,Re=5.5,Mmd=18,Cms=6e-4,Rms=2.3",
            "--dry-run",
        ]
    )
    module._normalize_driver_lem_args(args)
    frame = module._build_frame(args)
    source_motion = module._source_motion_for_source(args, "LF")
    cfg = module._build_config(
        args,
        source_tag=2,
        frame=frame,
        source_motion=source_motion,
    )

    assert source_motion == "axial"
    assert cfg.source_motion == "axial"

    cardioid_args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "unused.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "MF:3",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l",
            "10",
            "--passive-cardioid-port-length-mm",
            "20",
            "--passive-cardioid-coupled",
            "--driver-lem",
            "MF:Sd=200,Bl=8,Re=5.5,Mmd=18,Cms=6e-4,Rms=2.3",
        ]
    )
    module._normalize_driver_lem_args(cardioid_args)
    assert module._source_motion_for_source(cardioid_args, "MF") == "normal"


def test_source_mesh_valid_hz_is_overlay_only_not_a_band_clamp(tmp_path):
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(ROOT / "runs" / "scratch" / "unused.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "HF:4",
            "--source-mesh-valid-hz",
            "HF:11352",
            "--freq-max-hz",
            "20000",
            "--dry-run",
        ]
    )
    overlay = module._parse_source_freq_max(args.source_mesh_valid_hz)
    assert overlay == {"HF": 11352.0}
    # The overlay must not narrow the solved band.
    frame = module._build_frame(args)
    cfg = module._build_config(args, source_tag=4, frame=frame, freq_max_hz=None)
    assert cfg.freq_max_hz == 20000.0


def test_source_mesh_valid_and_aperture_overlays_parse_independently(tmp_path):
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(ROOT / "runs" / "scratch" / "unused.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "HF:4",
            "--source-mesh-valid-hz",
            "HF:3642",
            "--source-aperture-valid-hz",
            "HF:11352",
            "--dry-run",
        ]
    )
    assert module._parse_source_freq_max(args.source_mesh_valid_hz) == {"HF": 3642.0}
    assert module._parse_source_freq_max(args.source_aperture_valid_hz) == {"HF": 11352.0}


def test_plot_theme_parser_and_manifest_config(tmp_path):
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "tagged_sources.msh"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:4",
            "--plot-theme",
            "dark",
            "--dry-run",
        ]
    )
    assert args.plot_theme == "dark"

    mesh_path = tmp_path / "tagged_sources.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")
    try:
        rc = module.main(
            [
                "--mesh",
                str(mesh_path),
                "--out",
                str(tmp_path / "out"),
                "--source",
                "HF:4",
                "--plot-theme",
                "dark",
                "--dry-run",
            ]
        )
        assert rc == 0
        manifest = json.loads(
            _run_manifest_path(tmp_path / "out", "direct_solve_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["config"]["plot_theme"] == "dark"
    finally:
        module.set_theme("hornlab")


def test_port_exit_apertures_are_detected_from_sources():
    module = _load_script()

    sources = module._split_sources(
        ["LF:2,PORT_EXIT_L:10", "port_exit_r:20:11", "HF:4"]
    )

    assert module._port_exit_apertures(sources) == [
        ("PORT_EXIT_L", 10),
        ("port_exit_r", 11),
    ]


def test_source_freq_max_for_unknown_source_is_rejected(tmp_path):
    module = _load_script()
    mesh_path = tmp_path / "fake.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="not in --source list"):
        module.main(
            [
                "--mesh",
                str(mesh_path),
                "--out",
                str(tmp_path / "out"),
                "--source",
                "HF:4",
                "--source-freq-max",
                "LF:1000",
                "--dry-run",
            ]
        )


def test_solver_is_canonical_hornlab_metal_bem():
    module = _load_script()

    assert module.solve.__module__.startswith("hornlab_metal_bem")
    assert module.SolveConfig.__module__.startswith("hornlab_metal_bem")

    args = module.parse_args(
        [
            "--mesh",
            str(ROOT / "runs" / "scratch" / "unused.msh"),
            "--out",
            str(ROOT / "runs" / "scratch" / "unused"),
            "--source",
            "HF:4",
            "--native-symmetry-plane",
            "xy",
            "--dry-run",
        ]
    )
    frame = module._build_frame(args)
    cfg = module._build_config(args, source_tag=4, frame=frame)
    assert cfg.native_symmetry_plane == "xy"
    assert cfg.velocity_sources == {4: 1.0}
    assert cfg.formulation == "complex_k"
    assert cfg.complex_k_shift == 0.005
    # Default keeps the strict cut-plane open-edge guard.
    assert cfg.native_check_open_edges is True


def test_standard_bem_formulation_override_is_forwarded():
    module = _load_script()

    args = module.parse_args(
        [
            "--mesh",
            str(ROOT / "runs" / "scratch" / "unused.msh"),
            "--out",
            str(ROOT / "runs" / "scratch" / "unused"),
            "--source",
            "HF:4",
            "--bem-formulation",
            "standard",
            "--complex-k-shift",
            "0.0125",
            "--dry-run",
        ]
    )
    frame = module._build_frame(args)
    cfg = module._build_config(args, source_tag=4, frame=frame)

    assert cfg.formulation == "standard"
    assert cfg.complex_k_shift == 0.0125


def test_complex_k_dash_alias_is_normalized():
    module = _load_script()

    args = module.parse_args(
        [
            "--mesh",
            str(ROOT / "runs" / "scratch" / "unused.msh"),
            "--out",
            str(ROOT / "runs" / "scratch" / "unused"),
            "--source",
            "HF:4",
            "--bem-formulation",
            "complex-k",
            "--dry-run",
        ]
    )
    frame = module._build_frame(args)
    cfg = module._build_config(args, source_tag=4, frame=frame)

    assert args.bem_formulation == "complex_k"
    assert cfg.formulation == "complex_k"


def test_no_native_check_open_edges_relaxes_guard_for_open_mouth_mesh():
    module = _load_script()

    args = module.parse_args(
        [
            "--mesh",
            str(ROOT / "runs" / "scratch" / "unused.msh"),
            "--out",
            str(ROOT / "runs" / "scratch" / "unused"),
            "--source",
            "HF:4",
            "--native-symmetry-plane",
            "yz+xz",
            "--no-native-check-open-edges",
            "--dry-run",
        ]
    )
    frame = module._build_frame(args)
    cfg = module._build_config(args, source_tag=4, frame=frame)
    assert cfg.native_check_open_edges is False


def test_port_exit_radiation_impedance_matrix_writes_conjugated_artifact(
    tmp_path,
    monkeypatch,
):
    module = _load_script()
    solver_matrix = np.array(
        [
            [
                [10.0 - 2.0j, 3.0 - 5.0j],
                [3.0 - 5.0j, 11.0 - 1.0j],
            ]
        ],
        dtype=np.complex128,
    )
    fake_result = module.radiation_impedance.RadiationImpedanceResult(
        frequencies_hz=np.array([250.0]),
        aperture_names=["PORT_EXIT_L", "PORT_EXIT_R"],
        aperture_area_m2={"PORT_EXIT_L": 0.001, "PORT_EXIT_R": 0.002},
        impedance_matrix=solver_matrix,
        solver_logs=[],
    )
    fake_diagnostics = module.radiation_impedance.RadiationMatrixDiagnostics(
        reciprocity_max_abs=np.array([0.0]),
        reciprocity_max_rel=np.array([0.0]),
        passivity_min_eig=np.array([7.0]),
        passivity_ok=np.array([True]),
        low_ka_self_impedance={},
        low_ka_self_impedance_rel_error={},
    )
    captured = {}

    def fake_solve_aperture_matrix(mesh, aperture_tags, frequencies_hz, config):
        captured["mesh"] = mesh
        captured["aperture_tags"] = aperture_tags
        captured["frequencies_hz"] = frequencies_hz
        captured["config"] = config
        return fake_result

    monkeypatch.setattr(
        module.radiation_impedance,
        "solve_aperture_matrix",
        fake_solve_aperture_matrix,
    )
    monkeypatch.setattr(
        module.radiation_impedance,
        "matrix_diagnostics",
        lambda result: fake_diagnostics,
    )

    args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "fake.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "PORT_EXIT_L:10",
            "--source",
            "PORT_EXIT_R:11",
            "--freq-min-hz",
            "250",
            "--freq-max-hz",
            "1000",
            "--freq-count",
            "1",
            "--source-freq-max",
            "PORT_EXIT_R:500",
        ]
    )
    frame = module._build_frame(args)

    payload = module._solve_port_exit_radiation_impedance_matrix(
        tmp_path / "fake.msh",
        tmp_path,
        args,
        apertures=[("PORT_EXIT_L", 10), ("PORT_EXIT_R", 11)],
        frame=frame,
        source_freq_max={"PORT_EXIT_R": 500.0},
    )

    assert captured["aperture_tags"] == {"PORT_EXIT_L": [10], "PORT_EXIT_R": [11]}
    assert captured["config"].velocity_sources == {10: 1.0}
    np.testing.assert_allclose(captured["frequencies_hz"], [250.0])
    assert payload["convention"]["engineering_matrix"].startswith("conj(Z_solver)")
    np.testing.assert_allclose(
        payload["in_phase_termination_load"]["PORT_EXIT_L"],
        [13.0 + 7.0j],
    )
    np.testing.assert_allclose(
        payload["in_phase_termination_load"]["PORT_EXIT_R"],
        [14.0 + 6.0j],
    )

    matrix_npz = np.load(tmp_path / "port_exit_radiation_impedance_matrix.npz")
    np.testing.assert_allclose(
        matrix_npz["engineering_impedance_matrix"],
        np.conjugate(solver_matrix),
    )
    assert (tmp_path / "port_exit_radiation_impedance_matrix.summary.json").exists()


def test_native_symmetry_none_maps_to_solver_none():
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(ROOT / "runs" / "scratch" / "unused.msh"),
            "--out",
            str(ROOT / "runs" / "scratch" / "unused"),
            "--source",
            "HF:4",
            "--native-symmetry-plane",
            "none",
            "--dry-run",
        ]
    )
    frame = module._build_frame(args)
    cfg = module._build_config(args, source_tag=4, frame=frame)
    assert cfg.native_symmetry_plane is None


def test_native_symmetry_xy_maps_to_solver_and_manifest(tmp_path):
    module = _load_script()
    mesh_path = tmp_path / "fake.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")

    rc = module.main(
        [
            "--mesh",
            str(mesh_path),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:4",
            "--native-symmetry-plane",
            "xy",
            "--dry-run",
        ]
    )

    assert rc == 0
    manifest = _run_manifest_path(tmp_path / "out", "direct_solve_manifest.json").read_text(
        encoding="utf-8"
    )
    assert '"native_symmetry_plane": "xy"' in manifest

    args = module.parse_args(
        [
            "--mesh",
            str(mesh_path),
            "--out",
            str(tmp_path / "out2"),
            "--source",
            "HF:4",
            "--native-symmetry-plane",
            "xy",
            "--dry-run",
        ]
    )
    frame = module._build_frame(args)
    cfg = module._build_config(args, source_tag=4, frame=frame)
    assert cfg.native_symmetry_plane == "xy"


def test_solve_one_source_writes_per_source_frequency_response(tmp_path, monkeypatch):
    module = _load_script()

    class FakeResult:
        frequencies_hz = np.geomspace(100.0, 1000.0, 4)
        observation_angles_deg = np.asarray([0.0, 30.0])
        observation_planes = ["horizontal", "vertical"]
        # Solver-convention phasor with a non-trivial phase: the stored basis
        # must be the engineering-convention conjugate.
        pressure_complex = np.full(
            (4, 2, 2), 0.02 * np.exp(1j * 0.7), dtype=np.complex128
        )
        directivity_db = np.full((4, 2, 2), 0.0, dtype=np.float64)
        impedance = {}
        surface_pressure_avg = {}
        timings = {}
        solver_log = []
        mesh_info = {}

    monkeypatch.setattr(module, "solve", lambda _mesh, _config: FakeResult())
    args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "fake.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "HF:4",
        ]
    )
    frame = module._build_frame(args)

    result = module._solve_one_source(
        tmp_path / "fake.msh",
        tmp_path,
        args,
        source_name="HF",
        source_tag=4,
        frame=frame,
    )

    response_png = tmp_path / "HF_frequency_response.png"
    assert result["frequency_response_png"] == str(response_png)
    assert response_png.exists()
    assert response_png.stat().st_size > 500
    basis_npz = tmp_path / "HF_pressure_basis.npz"
    assert result["pressure_basis_npz"] == str(basis_npz)
    assert basis_npz.exists()
    with np.load(basis_npz) as basis:
        assert basis["source_name"].item() == "HF"
        assert int(basis["source_tag"]) == 4
        assert basis["pressure_complex"].shape == (4, 2, 2)
        assert (
            str(basis["phase_convention"].item())
            == module.PRESSURE_NPZ_PHASE_CONVENTION
        )
        np.testing.assert_allclose(
            basis["pressure_complex"],
            np.conjugate(FakeResult.pressure_complex),
        )
    loaded = module._load_pressure_basis(basis_npz)
    np.testing.assert_allclose(
        loaded.pressure_complex,
        np.conjugate(FakeResult.pressure_complex),
    )


def test_impulse_aligned_phase_removes_bulk_delay_for_plots():
    module = _load_script()
    freqs = np.linspace(100.0, 5000.0, 240)
    delay_s = 2.0 / module.SPEED_OF_SOUND_M_S + 0.00035
    pressure = np.exp(-1j * 2.0 * np.pi * freqs * delay_s)

    raw_phase = module._phase_deg_from_pressure(pressure)
    aligned_phase = module._phase_deg_from_pressure(
        pressure,
        frequencies_hz=freqs,
        polar_distance_m=2.0,
        impulse_aligned=True,
    )

    assert np.ptp(np.unwrap(np.radians(raw_phase))) > 5.0
    assert np.max(np.abs(aligned_phase)) < 1.0e-6


def test_phase_alignment_can_fit_only_operating_band_for_plots():
    module = _load_script()
    freqs = np.geomspace(80.0, 16000.0, 320)
    omega = 2.0 * np.pi * freqs
    pressure = np.exp(1j * (-omega * 0.0012 - 0.45 * np.log(freqs / freqs[0])))
    operating_band = (1200.0, 9000.0)

    full_band_phase = module._phase_deg_from_pressure(
        pressure,
        frequencies_hz=freqs,
        polar_distance_m=0.0,
        impulse_aligned=True,
    )
    operating_band_phase = module._phase_deg_from_pressure(
        pressure,
        frequencies_hz=freqs,
        polar_distance_m=0.0,
        impulse_aligned=True,
        fit_frequency_range_hz=operating_band,
    )

    band = (freqs >= operating_band[0]) & (freqs <= operating_band[1])
    full_band_slope = np.polyfit(
        omega[band],
        np.unwrap(np.radians(full_band_phase[band])),
        1,
    )[0]
    operating_band_slope = np.polyfit(
        omega[band],
        np.unwrap(np.radians(operating_band_phase[band])),
        1,
    )[0]

    assert abs(operating_band_slope) < abs(full_band_slope) * 0.05
    assert module._source_phase_fit_band_hz(
        "MF",
        freqs,
        lf_mf_hz=operating_band[0],
        mf_hf_hz=operating_band[1],
    ) == operating_band


def test_main_writes_running_manifest_before_native_solve(tmp_path, monkeypatch):
    module = _load_script()
    mesh_path = tmp_path / "fake.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    captured = {}

    class FakeResult:
        frequencies_hz = np.geomspace(100.0, 1000.0, 4)
        observation_angles_deg = np.asarray([0.0, 30.0])
        observation_planes = ["horizontal", "vertical"]
        pressure_complex = np.full((4, 2, 2), 0.02 + 0.0j, dtype=np.complex128)
        directivity_db = np.full((4, 2, 2), 0.0, dtype=np.float64)
        impedance = {}
        surface_pressure_avg = {}
        timings = {}
        solver_log = []
        mesh_info = {}

    def fake_solve(_mesh, _config):
        manifest = json.loads(
            _run_manifest_path(out_dir, "direct_solve_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        captured["status"] = manifest["status"]
        captured["current_phase"] = manifest["current_phase"]
        captured["current_source"] = manifest["current_source"]
        captured["sources"] = manifest["sources"]
        return FakeResult()

    monkeypatch.setenv(
        "HORNLAB_FUSION_DIRECT_SOLVE_LOCK",
        str(tmp_path / "direct-solve.lock"),
    )
    monkeypatch.setattr(module, "solve", fake_solve)

    rc = module.main(
        [
            "--mesh",
            str(mesh_path),
            "--out",
            str(out_dir),
            "--source",
            "HF:4",
        ]
    )

    assert rc == 0
    assert captured["status"] == "running"
    assert captured["current_phase"] == "solving_source"
    assert captured["current_source"] == {"name": "HF", "tag": 4}
    assert captured["sources"][0]["status"] == "running"
    final_manifest = json.loads(
        _run_manifest_path(out_dir, "direct_solve_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert final_manifest["status"] == "complete"
    assert final_manifest["layout_version"] == 2
    assert final_manifest["layout"]["sources_dir"].endswith("out/sources")


def test_solver_parser_accepts_output_skip_flags(tmp_path):
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "fake.msh"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:4",
            "--skip-per-driver-plots",
            "--skip-combined-set",
            "--skip-passive-cardioid-set",
            "--skip-driver-lem-artifacts",
            "--skip-derived-acoustics",
            "--skip-radiation-impedance",
            "--skip-pressure-bases",
            "--no-run-report",
        ]
    )

    assert args.skip_per_driver_plots is True
    assert args.skip_combined_set is True
    assert args.skip_passive_cardioid_set is True
    assert args.skip_driver_lem_artifacts is True
    assert args.skip_derived_acoustics is True
    assert args.skip_radiation_impedance is True
    assert args.skip_pressure_bases is True
    assert args.no_run_report is True


def test_skip_pressure_bases_keeps_internal_basis_private_for_derived_outputs(
    tmp_path,
):
    module = _load_script()
    mesh_path = tmp_path / "fake.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    layout = module.SolverOutputLayout(out_dir, layout_version=2)
    layout.ensure_dirs()

    class FakeResult:
        frequencies_hz = np.geomspace(100.0, 1000.0, 4)
        observation_angles_deg = np.asarray([0.0, 45.0, 90.0])
        observation_planes = np.asarray(["horizontal", "vertical"])
        pressure_complex = np.full((4, 2, 3), 0.02 + 0.01j, dtype=np.complex128)
        directivity_db = np.zeros((4, 2, 3), dtype=np.float64)
        impedance = {}
        surface_pressure_avg = {}
        timings = {}
        solver_log = []
        mesh_info = {}

    args = module.parse_args(
        [
            "--mesh",
            str(mesh_path),
            "--out",
            str(out_dir),
            "--source",
            "HF:4",
            "--skip-pressure-bases",
            "--skip-radiation-impedance",
            "--no-run-report",
        ]
    )
    frame = module._build_frame(args)
    source_result = module._write_one_source_outputs(
        FakeResult(),
        mesh_path,
        layout.sources_dir,
        args,
        source_name="HF",
        source_tag=4,
    )

    assert source_result["pressure_basis_npz"] is None
    assert not (layout.sources_dir / "HF_pressure_basis.npz").exists()
    assert isinstance(source_result["_pressure_basis"], module.PressureBasis)

    manifest = {"outputs": {}}
    module._apply_post_solve_derived_outputs(
        mesh_path,
        out_dir,
        layout,
        args,
        manifest=manifest,
        manifest_path=_run_manifest_path(out_dir, "direct_solve_manifest.json"),
        source_results=[source_result],
        sources=[("HF", 4)],
        port_exit_apertures=[],
        source_freq_max={},
        source_mesh_valid={},
        source_aperture_valid={},
        frame=frame,
    )

    assert (layout.derived_dir / "HF_group_delay.png").exists()
    assert (layout.combined_dir / "combined_frequency_response.png").exists()
    assert manifest["outputs"]["source_pressure_basis_npzs"] == {}
    assert manifest["outputs"]["source_results_jsons"]["HF"].endswith(
        "HF_results.json"
    )
    assert manifest["outputs"]["source_directivity_heatmap_pngs"]["HF"].endswith(
        "HF_directivity_heatmap.png"
    )
    assert all(
        not str(key).startswith("_")
        for source in manifest["sources"]
        for key in source
    )


def test_post_solve_manifest_registers_driver_lem_impedance_png(
    tmp_path,
    monkeypatch,
):
    module = _load_script()
    mesh_path = tmp_path / "fake.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    layout = module.SolverOutputLayout(out_dir, layout_version=2)
    layout.ensure_dirs()
    args = module.parse_args(
        [
            "--mesh",
            str(mesh_path),
            "--out",
            str(out_dir),
            "--source",
            "LF:2",
            "--skip-derived-acoustics",
            "--skip-combined-set",
            "--skip-radiation-impedance",
            "--skip-passive-cardioid-set",
            "--no-run-report",
        ]
    )
    frame = module._build_frame(args)
    source_result = {
        "name": "LF",
        "tag": 2,
        "frequencies_hz": np.asarray([100.0, 200.0]),
        "on_axis_spl_db": np.asarray([80.0, 81.0]),
    }

    def fake_apply_driver_lem_coupling(_mesh_path, _out_dir, _args, *, source_results):
        outputs = {
            "results_npz": str(layout.driver_lem_dir / "LF_driver_lem_results.npz"),
            "impedance_zma": str(layout.driver_lem_dir / "LF_impedance.zma"),
            "impedance_png": str(layout.driver_lem_dir / "LF_impedance.png"),
            "excursion_png": str(layout.driver_lem_dir / "LF_excursion.png"),
        }
        source_results[0]["driver_lem"] = {
            "status": "complete",
            "outputs": outputs,
        }
        return {
            "status": "complete",
            "type": "per_driver_lem_coupling",
            "sources": {"LF": source_results[0]["driver_lem"]},
        }

    monkeypatch.setattr(
        module,
        "_apply_driver_lem_coupling",
        fake_apply_driver_lem_coupling,
    )
    manifest = {"outputs": {}}
    module._apply_post_solve_derived_outputs(
        mesh_path,
        out_dir,
        layout,
        args,
        manifest=manifest,
        manifest_path=_run_manifest_path(out_dir, "direct_solve_manifest.json"),
        source_results=[source_result],
        sources=[("LF", 2)],
        port_exit_apertures=[],
        source_freq_max={},
        source_mesh_valid={},
        source_aperture_valid={},
        frame=frame,
    )

    assert manifest["outputs"]["driver_lem_impedance_zmas"]["LF"].endswith(
        "LF_impedance.zma"
    )
    assert manifest["outputs"]["driver_lem_impedance_pngs"]["LF"].endswith(
        "LF_impedance.png"
    )


def test_postprocess_only_regenerates_derived_artifacts_without_rewriting_npzs(
    tmp_path,
    monkeypatch,
):
    module = _load_script()
    mesh_path = tmp_path / "fake.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    freqs = np.geomspace(100.0, 1000.0, 4)
    angles = np.array([0.0, 45.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal", "vertical"], dtype=str)

    class FakeResult:
        def __init__(self, tag):
            self.frequencies_hz = freqs
            self.observation_angles_deg = angles
            self.observation_planes = planes
            amplitude = {3: 0.018, 4: 0.02, 10: 0.006}[tag]
            phase = {3: 0.1, 4: 0.4, 10: -0.2}[tag]
            self.pressure_complex = np.full(
                (freqs.size, planes.size, angles.size),
                amplitude * np.exp(1j * phase),
                dtype=np.complex128,
            )
            self.directivity_db = np.zeros(
                (freqs.size, planes.size, angles.size),
                dtype=np.float64,
            )
            self.impedance = {}
            self.surface_pressure_avg = {}
            self.timings = {}
            self.solver_log = []
            self.mesh_info = {}

    def fake_solve_multi_source(_mesh, velocity_sources, _config):
        return [
            FakeResult(next(iter(velocity_source)))
            for velocity_source in velocity_sources
        ]

    def fake_solve_aperture_matrix(_mesh, aperture_tags, frequencies_hz, _config):
        names = list(aperture_tags)
        matrix = np.zeros((len(frequencies_hz), len(names), len(names)), dtype=np.complex128)
        matrix[:, 0, 0] = 80.0 - 8.0j
        return module.radiation_impedance.RadiationImpedanceResult(
            frequencies_hz=np.asarray(frequencies_hz, dtype=np.float64),
            aperture_names=names,
            aperture_area_m2={name: 0.01 for name in names},
            impedance_matrix=matrix,
            solver_logs=[],
        )

    def fake_matrix_diagnostics(result):
        nfreq = result.frequencies_hz.size
        return module.radiation_impedance.RadiationMatrixDiagnostics(
            reciprocity_max_abs=np.zeros(nfreq),
            reciprocity_max_rel=np.zeros(nfreq),
            passivity_min_eig=np.ones(nfreq),
            passivity_ok=np.ones(nfreq, dtype=bool),
            low_ka_self_impedance={},
            low_ka_self_impedance_rel_error={},
        )

    monkeypatch.setenv(
        "HORNLAB_FUSION_DIRECT_SOLVE_LOCK",
        str(tmp_path / "direct-solve.lock"),
    )
    monkeypatch.setattr(module, "solve_multi_source", fake_solve_multi_source)
    monkeypatch.setattr(
        module.radiation_impedance,
        "solve_aperture_matrix",
        fake_solve_aperture_matrix,
    )
    monkeypatch.setattr(
        module.radiation_impedance,
        "matrix_diagnostics",
        fake_matrix_diagnostics,
    )

    argv = [
        "--mesh", str(mesh_path),
        "--out", str(out_dir),
        "--source", "MF:3",
        "--source", "HF:4",
        "--source", "PORT_EXIT:10",
        "--freq-min-hz", "100",
        "--freq-max-hz", "1000",
        "--freq-count", "4",
        "--crossover-mf-hf-hz", "500",
        "--export-vituixcad",
    ]
    assert module.main(argv) == 0
    npz_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in out_dir.rglob("*.npz")
    }
    assert set(npz_hashes) == {
        "HF_pressure_basis.npz",
        "MF_pressure_basis.npz",
        "PORT_EXIT_pressure_basis.npz",
        "port_exit_radiation_impedance_matrix.npz",
    }

    for path in (
        out_dir / "sources" / "MF_frequency_response.png",
        out_dir / "derived" / "MF_group_delay.png",
        out_dir / "derived" / "MF_beamwidth.csv",
        out_dir / "derived" / "MF_directivity_index_power_response.json",
        out_dir / "combined" / "combined_frequency_response_time_aligned.png",
        out_dir / "derived" / "combined_time_aligned_group_delay.csv",
        out_dir / "derived" / "combined_time_aligned_directivity_index_power_response.json",
        out_dir / "combined" / "driver_time_alignment.txt",
        out_dir / "vituixcad" / "hor" / "MF 0.frd",
    ):
        path.unlink()

    monkeypatch.setattr(
        module,
        "solve_multi_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("postprocess-only must not solve sources")
        ),
    )
    monkeypatch.setattr(
        module.radiation_impedance,
        "solve_aperture_matrix",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("postprocess-only must not solve radiation matrix")
        ),
    )

    assert module.main([*argv, "--postprocess-only"]) == 0
    for path in (
        out_dir / "sources" / "MF_frequency_response.png",
        out_dir / "derived" / "MF_group_delay.png",
        out_dir / "derived" / "MF_beamwidth.csv",
        out_dir / "derived" / "MF_directivity_index_power_response.json",
        out_dir / "combined" / "combined_frequency_response_time_aligned.png",
        out_dir / "derived" / "combined_time_aligned_group_delay.csv",
        out_dir / "derived" / "combined_time_aligned_directivity_index_power_response.json",
        out_dir / "combined" / "driver_time_alignment.txt",
        out_dir / "vituixcad" / "hor" / "MF 0.frd",
    ):
        assert path.exists()
        assert path.stat().st_size > 0
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in out_dir.rglob("*.npz")
        if path.name in npz_hashes
    } == npz_hashes
    manifest = json.loads(
        _run_manifest_path(out_dir, "direct_solve_manifest.json").read_text()
    )
    assert manifest["layout_version"] == 2
    assert manifest["postprocess_only"] is True
    assert manifest["status"] == "complete"
    assert manifest["outputs"]["source_group_delay_pngs"]["MF"].endswith(
        "MF_group_delay.png"
    )
    assert manifest["outputs"]["combined_time_aligned_group_delay_csv"].endswith(
        "combined_time_aligned_group_delay.csv"
    )


def test_postprocess_only_without_port_exit_mirrors_original_cardioid_skip(
    tmp_path,
    monkeypatch,
):
    """Cardioid flags without a PORT_EXIT source never produced a radiation
    matrix — the combine skipped. postprocess-only must reproduce that skip
    instead of demanding a matrix file the original run never wrote."""
    module = _load_script()
    mesh_path = tmp_path / "fake.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    freqs = np.geomspace(100.0, 1000.0, 4)
    angles = np.array([0.0, 45.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal", "vertical"], dtype=str)

    class FakeResult:
        def __init__(self, tag):
            self.frequencies_hz = freqs
            self.observation_angles_deg = angles
            self.observation_planes = planes
            amplitude = {3: 0.018, 4: 0.02}[tag]
            self.pressure_complex = np.full(
                (freqs.size, planes.size, angles.size),
                amplitude + 0.0j,
                dtype=np.complex128,
            )
            self.directivity_db = np.zeros(
                (freqs.size, planes.size, angles.size),
                dtype=np.float64,
            )
            self.impedance = {}
            self.surface_pressure_avg = {}
            self.timings = {}
            self.solver_log = []
            self.mesh_info = {}

    monkeypatch.setenv(
        "HORNLAB_FUSION_DIRECT_SOLVE_LOCK",
        str(tmp_path / "direct-solve.lock"),
    )
    monkeypatch.setattr(
        module,
        "solve_multi_source",
        lambda _mesh, velocity_sources, _config: [
            FakeResult(next(iter(velocity_source)))
            for velocity_source in velocity_sources
        ],
    )

    argv = [
        "--mesh", str(mesh_path),
        "--out", str(out_dir),
        "--source", "MF:3",
        "--source", "HF:4",
        "--freq-min-hz", "100",
        "--freq-max-hz", "1000",
        "--freq-count", "4",
        "--passive-cardioid-mf",
        "--passive-cardioid-rear-volume-l", "10",
        "--passive-cardioid-port-length-mm", "20",
    ]
    assert module.main(argv) == 0
    assert not (out_dir / "port_exit_radiation_impedance_matrix.npz").exists()
    assert not (out_dir / "sources" / "port_exit_radiation_impedance_matrix.npz").exists()

    monkeypatch.setattr(
        module,
        "solve_multi_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("postprocess-only must not solve sources")
        ),
    )
    monkeypatch.setattr(
        module.radiation_impedance,
        "solve_aperture_matrix",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("postprocess-only must not solve radiation matrix")
        ),
    )
    assert module.main([*argv, "--postprocess-only"]) == 0
    manifest = json.loads(
        _run_manifest_path(out_dir, "direct_solve_manifest.json").read_text()
    )
    assert manifest["status"] == "complete"
    combine = manifest["passive_cardioid"]
    assert combine["status"] == "skipped"
    assert "PORT_EXIT" in combine["reason"]


def test_postprocess_only_keyless_bases_use_corrected_phase_for_alignment_and_cardioid(
    tmp_path,
    monkeypatch,
):
    module = _load_script()
    freqs = np.geomspace(200.0, 2000.0, 30)
    planes = np.array(["horizontal"], dtype=str)

    align_dir = tmp_path / "align"
    align_dir.mkdir()
    angles = np.array([0.0, 45.0], dtype=np.float64)
    for name, tag, arrival_s in (("MF", 3, 0.0), ("HF", 4, 0.3e-3)):
        _write_synthetic_basis(
            align_dir / f"{name}_pressure_basis.npz",
            name=name,
            tag=tag,
            freqs=freqs,
            angles=angles,
            planes=planes,
            pressure=_delayed_pressure(freqs, planes, angles, arrival_s=arrival_s),
        )
    (align_dir / "direct_solve_manifest.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "MF",
                        "tag": 3,
                        "pressure_basis_npz": str(align_dir / "MF_pressure_basis.npz"),
                    },
                    {
                        "name": "HF",
                        "tag": 4,
                        "pressure_basis_npz": str(align_dir / "HF_pressure_basis.npz"),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    assert module.main(
        [
            "--mesh", str(align_dir / "unused.msh"),
            "--out", str(align_dir),
            "--source", "MF:3",
            "--source", "HF:4",
            "--freq-min-hz", "200",
            "--freq-max-hz", "2000",
            "--crossover-mf-hf-hz", "1000",
            "--postprocess-only",
        ]
    ) == 0
    manifest = json.loads((align_dir / "direct_solve_manifest.json").read_text())
    assert manifest["layout_version"] == 1
    assert (align_dir / "combined_frequency_response_time_aligned.png").exists()
    assert not (align_dir / "combined" / "combined_frequency_response_time_aligned.png").exists()
    assert manifest["crossover_alignment"]["delays_ms"]["MF"] == pytest.approx(
        0.3,
        abs=1.0e-6,
    )
    assert manifest["crossover_alignment"]["delays_ms"]["HF"] == pytest.approx(
        0.0,
        abs=1.0e-9,
    )

    cardioid_dir = tmp_path / "cardioid"
    cardioid_dir.mkdir()
    mesh_path = cardioid_dir / "tagged_sources.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")
    c = module.SPEED_OF_SOUND_M_S
    d = 0.1
    tau = d / c
    cardioid_freqs = np.array([200.0, 400.0, 800.0], dtype=np.float64)
    cardioid_angles = np.array([0.0, 90.0, 180.0], dtype=np.float64)
    k = 2.0 * np.pi * cardioid_freqs / c
    mf_pressure = np.ones((cardioid_freqs.size, 1, cardioid_angles.size), dtype=np.complex128)
    port_pressure = np.exp(
        1j
        * k[:, None, None]
        * d
        * np.cos(np.radians(cardioid_angles))[None, None, :]
    )
    _write_synthetic_basis(
        cardioid_dir / "MF_pressure_basis.npz",
        name="MF",
        tag=3,
        freqs=cardioid_freqs,
        angles=cardioid_angles,
        planes=planes,
        pressure=mf_pressure,
    )
    _write_synthetic_basis(
        cardioid_dir / "PORT_EXIT_pressure_basis.npz",
        name="PORT_EXIT",
        tag=10,
        freqs=cardioid_freqs,
        angles=cardioid_angles,
        planes=planes,
        pressure=port_pressure,
    )
    matrix_npz = cardioid_dir / "port_exit_radiation_impedance_matrix.npz"
    np.savez_compressed(
        matrix_npz,
        frequencies_hz=cardioid_freqs,
        aperture_names=np.asarray(["PORT_EXIT"]),
        aperture_area_m2=np.asarray([0.01], dtype=np.float64),
        solver_impedance_matrix=np.full((cardioid_freqs.size, 1, 1), 100.0 - 10.0j),
    )
    (cardioid_dir / "direct_solve_manifest.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "MF",
                        "tag": 3,
                        "pressure_basis_npz": str(cardioid_dir / "MF_pressure_basis.npz"),
                    },
                    {
                        "name": "PORT_EXIT",
                        "tag": 10,
                        "pressure_basis_npz": str(
                            cardioid_dir / "PORT_EXIT_pressure_basis.npz"
                        ),
                    },
                ],
                "radiation_impedance": {
                    "status": "complete",
                    "outputs": {"npz": str(matrix_npz)},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_mesh_tag_area_m2", lambda _m, _t, mesh_scale: 0.01)
    monkeypatch.setattr(
        module.radiation_impedance,
        "terminated_chamber_port_branch",
        lambda frequencies_hz, termination_load, **_kwargs: SimpleNamespace(
            frequencies_hz=frequencies_hz,
            termination_load=termination_load,
            input_impedance=np.ones(cardioid_freqs.size, dtype=np.complex128),
            exit_to_input_volume_velocity_ratio=np.exp(
                -1j * 2.0 * np.pi * frequencies_hz * tau
            ),
        ),
    )
    assert module.main(
        [
            "--mesh", str(mesh_path),
            "--out", str(cardioid_dir),
            "--source", "MF:3",
            "--source", "PORT_EXIT:10",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l", "10",
            "--passive-cardioid-port-length-mm", "20",
            "--postprocess-only",
        ]
    ) == 0
    with np.load(cardioid_dir / "MF_passive_cardioid_results.npz") as result:
        total = result["pressure_complex"]
        rear = int(np.argmin(np.abs(cardioid_angles - 180.0)))
        front = int(np.argmin(np.abs(cardioid_angles)))
        assert np.max(np.abs(total[:, :, rear])) < 1.0e-9
        assert np.min(np.abs(total[:, :, front])) > 0.5


def test_passive_cardioid_mf_combine_writes_complex_sum(tmp_path, monkeypatch):
    module = _load_script()
    freqs = np.array([100.0, 200.0], dtype=np.float64)
    angles = np.array([-90.0, 0.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal", "vertical"], dtype=str)
    mf_pressure = np.full((2, 2, 3), 0.010 + 0.0j, dtype=np.complex128)
    port_pressure = np.full((2, 2, 3), 0.002 + 0.0j, dtype=np.complex128)

    mf_basis = tmp_path / "MF_pressure_basis.npz"
    port_basis = tmp_path / "PORT_EXIT_pressure_basis.npz"
    np.savez_compressed(
        mf_basis,
        source_name=np.asarray("MF"),
        source_tag=np.asarray(3, dtype=np.int32),
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=mf_pressure,
    )
    np.savez_compressed(
        port_basis,
        source_name=np.asarray("PORT_EXIT"),
        source_tag=np.asarray(10, dtype=np.int32),
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=port_pressure,
    )

    matrix_npz = tmp_path / "port_exit_radiation_impedance_matrix.npz"
    solver_matrix = np.array([[[100.0 - 10.0j]], [[120.0 - 12.0j]]], dtype=np.complex128)
    np.savez_compressed(
        matrix_npz,
        frequencies_hz=freqs,
        aperture_names=np.asarray(["PORT_EXIT"]),
        aperture_area_m2=np.asarray([0.01], dtype=np.float64),
        solver_impedance_matrix=solver_matrix,
    )
    monkeypatch.setattr(
        module,
        "_mesh_tag_area_m2",
        lambda _mesh, _tag, mesh_scale: 0.02,
    )
    args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "fake.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "MF:3",
            "--source",
            "PORT_EXIT:10",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l",
            "10",
            "--passive-cardioid-port-length-mm",
            "20",
            "--passive-cardioid-foam-resistance-pa-s-m3",
            "50",
        ]
    )

    payload = module._solve_passive_cardioid_mf(
        tmp_path / "fake.msh",
        tmp_path,
        args,
        source_results=[
            {
                "name": "MF",
                "tag": 3,
                "pressure_basis_npz": str(mf_basis),
                "mesh_valid_freq_max_hz": None,
                "aperture_valid_freq_max_hz": None,
            },
            {
                "name": "PORT_EXIT",
                "tag": 10,
                "pressure_basis_npz": str(port_basis),
                "mesh_valid_freq_max_hz": None,
                "aperture_valid_freq_max_hz": None,
            },
        ],
        radiation_payload={"outputs": {"npz": str(matrix_npz)}},
    )

    assert payload["status"] == "complete"
    assert (tmp_path / "MF_passive_cardioid_results.npz").exists()
    assert (tmp_path / "MF_passive_cardioid_summary.json").exists()
    assert (tmp_path / "MF_passive_cardioid_frequency_response.png").stat().st_size > 500
    assert (tmp_path / "MF_passive_cardioid_directivity_heatmap.png").stat().st_size > 500

    termination_load = module.radiation_impedance.termination_load_from_solver_matrix(
        solver_matrix,
        receiver_index=0,
    )
    branch = module.radiation_impedance.terminated_chamber_port_branch(
        freqs,
        termination_load,
        chamber_volume_m3=0.010,
        port_area_m2=0.01,
        port_length_m=0.020,
        series_resistance_pa_s_m3=50.0,
    )
    expected_weight = -1.0 * (0.02 / 0.01) * branch.exit_to_input_volume_velocity_ratio
    with np.load(tmp_path / "MF_passive_cardioid_results.npz") as result:
        np.testing.assert_allclose(result["port_velocity_weight"], expected_weight)
        np.testing.assert_allclose(
            result["pressure_complex"],
            mf_pressure + expected_weight[:, None, None] * port_pressure,
        )


def test_passive_cardioid_coupled_writes_additive_artifacts(tmp_path, monkeypatch):
    module = _load_script()
    fixture = _write_passive_cardioid_fixture(tmp_path)
    monkeypatch.setattr(module, "_mesh_tag_area_m2", lambda _m, _t, mesh_scale: 0.02)
    impedance_plot_calls = []
    original_save_impedance_plot = module.save_impedance_plot

    def spy_save_impedance_plot(*args, **kwargs):
        impedance_plot_calls.append(kwargs)
        return original_save_impedance_plot(*args, **kwargs)

    monkeypatch.setattr(module, "save_impedance_plot", spy_save_impedance_plot)
    off_dir = tmp_path / "off"
    on_dir = tmp_path / "on"
    off_dir.mkdir()
    on_dir.mkdir()

    off_payload = module._solve_passive_cardioid_mf(
        tmp_path / "fake.msh",
        off_dir,
        _passive_cardioid_args(module, tmp_path, off_dir, coupled=False),
        source_results=fixture["source_results"],
        radiation_payload={"outputs": {"npz": str(fixture["matrix_npz"])}},
    )
    on_payload = module._solve_passive_cardioid_mf(
        tmp_path / "fake.msh",
        on_dir,
        _passive_cardioid_args(module, tmp_path, on_dir, coupled=True),
        source_results=fixture["source_results"],
        radiation_payload={"outputs": {"npz": str(fixture["matrix_npz"])}},
    )

    assert off_payload["status"] == "complete"
    assert on_payload["status"] == "complete"
    assert on_payload["coupled"]["status"] == "complete"
    assert on_payload["coupled"]["coupled_ratio_check"] == "passed"
    assert (on_dir / "MF_passive_cardioid_coupled_results.npz").exists()
    assert (on_dir / "MF_passive_cardioid_coupled_frequency_response.png").stat().st_size > 500
    assert (on_dir / "MF_passive_cardioid_impedance.zma").exists()
    assert (on_dir / "MF_passive_cardioid_impedance.png").stat().st_size > 500
    assert impedance_plot_calls[-1]["title"] == "Electrical Input Impedance"
    assert (
        impedance_plot_calls[-1]["ylabel"]
        == "|Z| [ohm] / phase-split real+imag [ohm]"
    )
    assert not (on_dir / "MF_passive_cardioid_coupled_directivity_heatmap.png").exists()

    with np.load(off_dir / "MF_passive_cardioid_results.npz") as off:
        with np.load(on_dir / "MF_passive_cardioid_results.npz") as on:
            assert set(on.files) == set(off.files)
            for key in off.files:
                if off[key].dtype.kind in {"U", "S"}:
                    np.testing.assert_array_equal(on[key], off[key])
                else:
                    np.testing.assert_allclose(on[key], off[key])
            fixed_directivity = off["directivity_db"]
            fixed_pressure = off["pressure_complex"]

    with np.load(on_dir / "MF_passive_cardioid_coupled_results.npz") as coupled:
        assert "pressure_complex" in coupled.files
        assert "electrical_input_impedance" in coupled.files
        np.testing.assert_allclose(
            coupled["directivity_db"],
            fixed_directivity,
            atol=1.0e-9,
        )
        normalized = module._directivity_from_pressure_array(
            coupled["pressure_complex"],
            coupled["observation_angles_deg"],
        )
        np.testing.assert_allclose(normalized, fixed_directivity, atol=1.0e-9)
        assert not np.allclose(coupled["pressure_complex"], fixed_pressure)

    summary = json.loads(
        (on_dir / "MF_passive_cardioid_summary.json").read_text(encoding="utf-8")
    )
    assert summary["coupled"]["driver"]["mmd_eff_g"] == pytest.approx(18.0)
    assert summary["coupled"]["drive_voltage_v"] == pytest.approx(2.83)
    assert summary["outputs"]["coupled_results_npz"].endswith(
        "MF_passive_cardioid_coupled_results.npz"
    )
    assert summary["outputs"]["impedance_png"].endswith(
        "MF_passive_cardioid_impedance.png"
    )
    assert summary["coupled"]["outputs"]["impedance_png"].endswith(
        "MF_passive_cardioid_impedance.png"
    )

    rows = [
        line.split()
        for line in (on_dir / "MF_passive_cardioid_impedance.zma")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("*")
    ]
    zma = np.asarray(rows, dtype=np.float64)
    assert zma.shape == (fixture["freqs"].size, 3)
    assert np.all(zma[:, 1] > 0.0)
    impedance = zma[:, 1] * np.exp(1j * np.radians(zma[:, 2]))
    assert np.all(impedance.real >= 5.5 - 1.0e-9)


def test_passive_cardioid_mf_uses_stored_source_area_when_available(
    tmp_path,
    monkeypatch,
):
    module = _load_script()
    fixture = _write_passive_cardioid_fixture(tmp_path)
    monkeypatch.setattr(
        module,
        "_mesh_tag_area_m2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored pressure-basis area should avoid mesh lookup")
        ),
    )

    payload = module._solve_passive_cardioid_mf(
        tmp_path / "fake.msh",
        tmp_path,
        _passive_cardioid_args(module, tmp_path, tmp_path),
        source_results=fixture["source_results"],
        radiation_payload={"outputs": {"npz": str(fixture["matrix_npz"])}},
    )

    assert payload["status"] == "complete"
    assert (tmp_path / "MF_passive_cardioid_results.npz").exists()


def test_passive_cardioid_coupled_args_validate_driver_requirements(tmp_path):
    module = _load_script()
    base = [
        "--mesh", str(tmp_path / "fake.msh"),
        "--out", str(tmp_path),
        "--source", "MF:3",
        "--source", "PORT_EXIT:10",
        "--passive-cardioid-mf",
        "--passive-cardioid-rear-volume-l", "10",
        "--passive-cardioid-port-length-mm", "20",
        "--passive-cardioid-coupled",
    ]
    with pytest.raises(SystemExit):
        module._validate_passive_cardioid_args(module.parse_args(base))

    both_mmd_and_mms = module.parse_args(
        base
        + _passive_cardioid_driver_cli()
        + ["--passive-cardioid-driver-mms-g", "20"]
    )
    module._validate_passive_cardioid_args(both_mmd_and_mms)
    assert both_mmd_and_mms.driver_lem_specs["MF"].params["mmd_kg"] == pytest.approx(
        0.018
    )
    assert any(
        "both Mmd and Mms supplied" in warning
        for warning in both_mmd_and_mms.driver_lem_specs["MF"].warnings
    )

    no_compliance = [
        item
        for item in _passive_cardioid_driver_cli()
        if item not in {"--passive-cardioid-driver-cms-mm-per-n", "0.6"}
    ]
    with pytest.raises(SystemExit):
        module._validate_passive_cardioid_args(
            module.parse_args(base + no_compliance)
        )


def test_deprecated_passive_cardioid_driver_alias_conflict_warns_deterministically(
    tmp_path,
    capsys,
):
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh", str(tmp_path / "fake.msh"),
            "--out", str(tmp_path),
            "--source", "MF:3",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l", "10",
            "--passive-cardioid-port-length-mm", "20",
            "--passive-cardioid-coupled",
            "--driver-lem",
            "MF:Sd=210,Bl=9,Re=6,Mmd=19,Cms=5e-4,Rms=2.5",
        ]
        + _passive_cardioid_driver_cli()
    )

    module._validate_passive_cardioid_args(args)

    assert args.driver_lem_specs["MF"].params["sd_m2"] == pytest.approx(0.021)
    assert "ignored because --driver-lem MF" in capsys.readouterr().out


def test_passive_cardioid_coupled_skips_matrix_without_mf_aperture(tmp_path, monkeypatch):
    module = _load_script()
    fixture = _write_passive_cardioid_fixture(tmp_path, include_mf_matrix=False)
    monkeypatch.setattr(module, "_mesh_tag_area_m2", lambda _m, _t, mesh_scale: 0.02)

    payload = module._solve_passive_cardioid_mf(
        tmp_path / "fake.msh",
        tmp_path,
        _passive_cardioid_args(module, tmp_path, tmp_path, coupled=True),
        source_results=fixture["source_results"],
        radiation_payload={"outputs": {"npz": str(fixture["matrix_npz"])}},
    )

    assert payload["status"] == "complete"
    assert payload["coupled"]["status"] == "skipped"
    assert "predates the MF-aperture extension" in payload["coupled"]["reason"]
    assert (tmp_path / "MF_passive_cardioid_results.npz").exists()
    assert not (tmp_path / "MF_passive_cardioid_coupled_results.npz").exists()


def test_per_driver_lem_skips_mf_when_passive_cardioid_coupled_owns_it(tmp_path):
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh", str(tmp_path / "fake.msh"),
            "--out", str(tmp_path),
            "--source", "MF:3",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l", "10",
            "--passive-cardioid-port-length-mm", "20",
            "--passive-cardioid-coupled",
            "--driver-lem",
            "MF:Sd=200,Bl=8,Re=5.5,Mmd=18,Cms=6e-4,Rms=2.3",
        ]
    )
    module._normalize_driver_lem_args(args)

    payload = module._apply_driver_lem_coupling(
        tmp_path / "fake.msh",
        tmp_path,
        args,
        source_results=[
            {
                "name": "MF",
                "tag": 3,
                "pressure_basis_npz": str(tmp_path / "missing.npz"),
                "results_json": str(tmp_path / "missing.json"),
            }
        ],
    )

    assert payload["sources"]["MF"]["status"] == "skipped"
    assert "passive-cardioid coupled mode owns" in payload["sources"]["MF"]["reason"]


def test_postprocess_driver_lem_uses_results_json_surface_avg_once(
    tmp_path,
    monkeypatch,
):
    module = _load_script()
    freqs = np.array([100.0, 250.0], dtype=np.float64)
    angles = np.array([0.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal"], dtype=str)
    basis_npz = tmp_path / "LF_pressure_basis.npz"
    _write_synthetic_basis(
        basis_npz,
        name="LF",
        tag=2,
        freqs=freqs,
        angles=angles,
        planes=planes,
        pressure=np.ones((freqs.size, planes.size, angles.size), dtype=np.complex128),
    )
    p_avg_solver = np.array([1.0 + 2.0j, -0.25 + 0.5j], dtype=np.complex128)
    previous_result_json = tmp_path / "LF_previous_results.json"
    module._write_json(
        previous_result_json,
        {"surface_pressure_avg": {"2": p_avg_solver}},
    )
    args = module.parse_args(
        [
            "--mesh", str(tmp_path / "missing-postprocess-mesh.msh"),
            "--out", str(tmp_path),
            "--source", "LF:2",
            "--driver-lem",
            "LF:Sd=200,Bl=8,Re=5.5,Mmd=18,Cms=6e-4,Rms=2.3,Xmax=4.5",
            "--drive-voltage", "4",
            "--rg-ohm", "0.2",
        ]
    )
    module._normalize_driver_lem_args(args)
    source_result = module._write_one_source_derived_outputs_from_basis(
        basis_npz,
        tmp_path,
        args,
        source_name="LF",
        source_tag=2,
        previous_result_json=previous_result_json,
    )
    monkeypatch.setattr(module, "_mesh_tag_area_m2", lambda _m, _t, mesh_scale: 0.02)
    captured: dict[str, np.ndarray] = {}
    electrical = np.array([5.0 + 5.0j, 3.0 - 4.0j], dtype=np.complex128)

    def fake_coupled_direct_radiator_response(_frequencies_hz, **kwargs):
        captured["z_self"] = np.asarray(kwargs["z_self"], dtype=np.complex128)
        return SimpleNamespace(
            cone_volume_velocity=np.array([0.01 + 0.0j, 0.02 + 0.0j]),
            acoustic_load=np.asarray(kwargs["z_self"], dtype=np.complex128),
            electrical_input_impedance=electrical,
            cone_excursion_m=np.array([0.001, 0.002]),
            mmd_correction_kg=0.0,
            diagnostics={
                "mmd_source": "Mmd",
                "mmd_eff_kg": 0.018,
                "sd_eff_m2": 0.02,
            },
        )

    monkeypatch.setattr(
        module.driver_coupling,
        "coupled_direct_radiator_response",
        fake_coupled_direct_radiator_response,
    )
    impedance_plot_calls = []
    original_save_impedance_plot = module.save_impedance_plot

    def spy_save_impedance_plot(*args, **kwargs):
        impedance_plot_calls.append(kwargs)
        return original_save_impedance_plot(*args, **kwargs)

    monkeypatch.setattr(module, "save_impedance_plot", spy_save_impedance_plot)
    import matplotlib.axes

    axhline_calls = []
    original_axhline = matplotlib.axes.Axes.axhline

    def spy_axhline(self, y=0, *plot_args, **plot_kwargs):
        axhline_calls.append((y, plot_kwargs.get("label")))
        return original_axhline(self, y, *plot_args, **plot_kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "axhline", spy_axhline)

    payload = module._apply_driver_lem_coupling(
        tmp_path / "missing-postprocess-mesh.msh",
        tmp_path,
        args,
        source_results=[source_result],
    )

    assert payload["sources"]["LF"]["status"] == "complete"
    omega = 2.0 * np.pi * freqs
    expected_z_self = np.conjugate(1j * omega * p_avg_solver) / 0.02
    np.testing.assert_allclose(captured["z_self"], expected_z_self)
    rows = [
        line.split()
        for line in (tmp_path / "LF_impedance.zma")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("*")
    ]
    zma = np.asarray(rows, dtype=np.float64)
    np.testing.assert_allclose(zma[:, 0], freqs)
    np.testing.assert_allclose(zma[:, 1], np.abs(electrical), rtol=1.0e-6)
    np.testing.assert_allclose(
        zma[:, 2],
        np.degrees(np.angle(electrical)),
        atol=1.0e-6,
    )
    assert any(
        label == "Xmax" and y == pytest.approx(4.5)
        for y, label in axhline_calls
    )
    assert (tmp_path / "LF_impedance.png").stat().st_size > 500
    assert impedance_plot_calls[-1]["title"] == "Electrical Input Impedance"
    assert (
        impedance_plot_calls[-1]["ylabel"]
        == "|Z| [ohm] / phase-split real+imag [ohm]"
    )
    assert payload["sources"]["LF"]["outputs"]["impedance_png"].endswith(
        "LF_impedance.png"
    )
    assert source_result["driver_lem_impedance_png"].endswith("LF_impedance.png")
    assert not (tmp_path / "vituixcad" / "LF_impedance.zma").exists()


def test_skip_driver_lem_artifacts_keeps_active_basis_private(
    tmp_path,
    monkeypatch,
):
    module = _load_script()
    freqs = np.array([100.0, 250.0], dtype=np.float64)
    angles = np.array([0.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal"], dtype=str)
    basis = module.PressureBasis(
        source_name="LF",
        source_tag=2,
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=np.ones((freqs.size, planes.size, angles.size), dtype=np.complex128),
        surface_pressure_avg_solver=np.array([1.0 + 0.0j, 1.5 + 0.0j]),
        source_area_m2=0.02,
    )
    args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "fake.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "LF:2",
            "--driver-lem",
            "LF:Sd=200,Bl=8,Re=5.5,Mmd=18,Cms=6e-4,Rms=2.3,Xmax=4.5",
            "--skip-driver-lem-artifacts",
            "--skip-per-driver-plots",
        ]
    )
    module._normalize_driver_lem_args(args)

    def fake_coupled_direct_radiator_response(_frequencies_hz, **kwargs):
        return SimpleNamespace(
            cone_volume_velocity=np.array([0.01 + 0.0j, 0.02 + 0.0j]),
            acoustic_load=np.asarray(kwargs["z_self"], dtype=np.complex128),
            electrical_input_impedance=np.array([5.0 + 0.0j, 6.0 + 0.0j]),
            cone_excursion_m=np.array([0.001, 0.002]),
            mmd_correction_kg=0.0,
            diagnostics={
                "mmd_source": "Mmd",
                "mmd_eff_kg": 0.018,
                "sd_eff_m2": 0.02,
            },
        )

    monkeypatch.setattr(
        module.driver_coupling,
        "coupled_direct_radiator_response",
        fake_coupled_direct_radiator_response,
    )
    source_result = {
        "name": "LF",
        "tag": 2,
        "pressure_basis_npz": None,
        "results_json": str(tmp_path / "LF_results.json"),
        "_pressure_basis": basis,
    }
    payload = module._apply_driver_lem_coupling(
        tmp_path / "fake.msh",
        tmp_path,
        args,
        source_results=[source_result],
    )

    assert payload["sources"]["LF"]["status"] == "complete"
    assert payload["sources"]["LF"]["outputs"] == {}
    assert isinstance(source_result["_active_pressure_basis"], module.PressureBasis)
    assert "active_pressure_basis_npz" not in source_result
    assert not (tmp_path / "LF_driver_lem_pressure.npz").exists()
    assert not (tmp_path / "LF_driver_lem_results.npz").exists()
    assert not (tmp_path / "LF_impedance.zma").exists()
    assert not (tmp_path / "LF_impedance.png").exists()
    assert not (tmp_path / "LF_excursion.png").exists()

    vituixcad_payload = module._write_vituixcad_export(
        tmp_path,
        [source_result],
        polar_distance_m=2.0,
    )
    export_dir = Path(vituixcad_payload["export_dir"])
    assert (export_dir / "LF_impedance.zma").exists()
    assert vituixcad_payload["outputs"]["vituixcad_driver_zmas"]["LF"].endswith(
        "LF_impedance.zma"
    )


def test_driver_lem_missing_surface_avg_skips_instead_of_crashing(
    tmp_path,
    capsys,
):
    module = _load_script()
    freqs = np.array([100.0, 250.0], dtype=np.float64)
    angles = np.array([0.0], dtype=np.float64)
    planes = np.array(["horizontal"], dtype=str)
    basis_npz = tmp_path / "LF_pressure_basis.npz"
    _write_synthetic_basis(
        basis_npz,
        name="LF",
        tag=2,
        freqs=freqs,
        angles=angles,
        planes=planes,
        pressure=np.ones((freqs.size, planes.size, angles.size), dtype=np.complex128),
    )
    result_json = tmp_path / "LF_results.json"
    module._write_json(result_json, {"surface_pressure_avg": {}})
    args = module.parse_args(
        [
            "--mesh", str(tmp_path / "fake.msh"),
            "--out", str(tmp_path),
            "--source", "LF:2",
            "--driver-lem",
            "LF:Sd=200,Bl=8,Re=5.5,Mmd=18,Cms=6e-4,Rms=2.3",
        ]
    )
    module._normalize_driver_lem_args(args)

    payload = module._apply_driver_lem_coupling(
        tmp_path / "fake.msh",
        tmp_path,
        args,
        source_results=[
            {
                "name": "LF",
                "tag": 2,
                "pressure_basis_npz": str(basis_npz),
                "results_json": str(result_json),
            }
        ],
    )

    assert payload["sources"]["LF"]["status"] == "skipped"
    assert "no surface_pressure_avg" in payload["sources"]["LF"]["reason"]
    assert "DRIVER LEM WARNING: LF" in capsys.readouterr().out


def test_passive_cardioid_skips_when_required_sources_were_skipped(tmp_path):
    module = _load_script()
    args = module.parse_args(
        [
            "--mesh",
            str(tmp_path / "fake.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "HF:4",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l",
            "10",
            "--passive-cardioid-port-length-mm",
            "20",
        ]
    )

    payload = module._solve_passive_cardioid_mf(
        tmp_path / "fake.msh",
        tmp_path,
        args,
        source_results=[{"name": "HF", "tag": 4}],
        radiation_payload=None,
    )

    assert payload["status"] == "skipped"
    assert "requires solved source 'MF'" in payload["reason"]
    assert payload["available_sources"] == ["HF"]


def test_passive_cardioid_dry_run_preserves_requested_polar_window(tmp_path):
    module = _load_script()
    mesh_path = tmp_path / "dummy.msh"
    mesh_path.write_text("", encoding="utf-8")

    rc = module.main(
        [
            "--mesh",
            str(mesh_path),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "MF:3",
            "--source",
            "PORT_EXIT:10",
            "--polar-angle-min-deg",
            "0",
            "--polar-angle-max-deg",
            "90",
            "--polar-angle-count",
            "19",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l",
            "10",
            "--passive-cardioid-port-length-mm",
            "25",
            "--dry-run",
        ]
    )

    assert rc == 0
    manifest = _run_manifest_path(tmp_path / "out", "direct_solve_manifest.json").read_text(
        encoding="utf-8"
    )

    assert '"polar_angle_min_deg": 0.0' in manifest
    assert '"polar_angle_max_deg": 90.0' in manifest
    assert '"polar_angle_count": 19' in manifest
    assert '"enabled": true' in manifest


def _write_synthetic_basis(
    path,
    *,
    name,
    tag,
    freqs,
    angles,
    planes,
    pressure,
    source_area_m2=None,
):
    arrays = dict(
        source_name=np.asarray(name),
        source_tag=np.asarray(tag, dtype=np.int32),
        frequencies_hz=np.asarray(freqs, dtype=np.float64),
        observation_angles_deg=np.asarray(angles, dtype=np.float64),
        observation_planes=np.asarray(planes, dtype=str),
        pressure_complex=np.asarray(pressure, dtype=np.complex128),
    )
    if source_area_m2 is not None:
        arrays["source_area_m2"] = np.asarray(float(source_area_m2), dtype=np.float64)
    np.savez_compressed(path, **arrays)


def _synthetic_source_result(path, name, tag):
    return {
        "name": name,
        "tag": tag,
        "pressure_basis_npz": str(path),
        "mesh_valid_freq_max_hz": None,
        "aperture_valid_freq_max_hz": None,
    }


def _passive_cardioid_driver_cli():
    return [
        "--passive-cardioid-driver-sd-cm2", "200",
        "--passive-cardioid-driver-bl-tm", "8",
        "--passive-cardioid-driver-re-ohm", "5.5",
        "--passive-cardioid-driver-le-mh", "0.1",
        "--passive-cardioid-driver-mmd-g", "18",
        "--passive-cardioid-driver-cms-mm-per-n", "0.6",
        "--passive-cardioid-driver-qms", "5",
        "--passive-cardioid-drive-voltage", "2.83",
    ]


def _write_passive_cardioid_fixture(tmp_path, *, include_mf_matrix=True):
    freqs = np.array([100.0, 200.0, 400.0], dtype=np.float64)
    angles = np.array([-90.0, 0.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal", "vertical"], dtype=str)
    angle_shape = np.array([0.7, 1.0, 0.8], dtype=np.float64)
    mf_pressure = (
        0.010
        * angle_shape[None, None, :]
        * np.ones((freqs.size, planes.size, angles.size), dtype=np.complex128)
    )
    port_pressure = (
        (0.002 + 0.0004j)
        * np.array([1.0, 0.9, 0.6], dtype=np.float64)[None, None, :]
        * np.ones((freqs.size, planes.size, angles.size), dtype=np.complex128)
    )
    mf_basis = tmp_path / "MF_pressure_basis.npz"
    port_basis = tmp_path / "PORT_EXIT_pressure_basis.npz"
    _write_synthetic_basis(
        mf_basis,
        name="MF",
        tag=3,
        freqs=freqs,
        angles=angles,
        planes=planes,
        pressure=mf_pressure,
        source_area_m2=0.02,
    )
    _write_synthetic_basis(
        port_basis,
        name="PORT_EXIT",
        tag=10,
        freqs=freqs,
        angles=angles,
        planes=planes,
        pressure=port_pressure,
        source_area_m2=0.01,
    )
    matrix_npz = tmp_path / "port_exit_radiation_impedance_matrix.npz"
    if include_mf_matrix:
        solver_matrix = np.zeros((freqs.size, 2, 2), dtype=np.complex128)
        solver_matrix[:, 0, 0] = np.array([100.0 - 10.0j, 120.0 - 12.0j, 140.0 - 14.0j])
        solver_matrix[:, 0, 1] = np.array([6.0 - 0.6j, 7.0 - 0.7j, 8.0 - 0.8j])
        solver_matrix[:, 1, 0] = np.array([6.0 - 0.6j, 7.0 - 0.7j, 8.0 - 0.8j])
        solver_matrix[:, 1, 1] = np.array([320.0 - 20.0j, 340.0 - 22.0j, 360.0 - 24.0j])
        np.savez_compressed(
            matrix_npz,
            frequencies_hz=freqs,
            aperture_names=np.asarray(["PORT_EXIT", "MF"]),
            aperture_area_m2=np.asarray([0.01, 0.02], dtype=np.float64),
            solver_impedance_matrix=solver_matrix,
            in_phase_aperture_names=np.asarray(["PORT_EXIT"]),
        )
    else:
        solver_matrix = np.array(
            [[[100.0 - 10.0j]], [[120.0 - 12.0j]], [[140.0 - 14.0j]]],
            dtype=np.complex128,
        )
        np.savez_compressed(
            matrix_npz,
            frequencies_hz=freqs,
            aperture_names=np.asarray(["PORT_EXIT"]),
            aperture_area_m2=np.asarray([0.01], dtype=np.float64),
            solver_impedance_matrix=solver_matrix,
        )
    source_results = [
        _synthetic_source_result(mf_basis, "MF", 3),
        _synthetic_source_result(port_basis, "PORT_EXIT", 10),
    ]
    return {
        "freqs": freqs,
        "angles": angles,
        "planes": planes,
        "mf_pressure": mf_pressure,
        "port_pressure": port_pressure,
        "matrix_npz": matrix_npz,
        "source_results": source_results,
    }


def _passive_cardioid_args(module, tmp_path, out_dir, *, coupled=False):
    argv = [
        "--mesh", str(tmp_path / "fake.msh"),
        "--out", str(out_dir),
        "--source", "MF:3",
        "--source", "PORT_EXIT:10",
        "--passive-cardioid-mf",
        "--passive-cardioid-rear-volume-l", "10",
        "--passive-cardioid-port-length-mm", "20",
        "--passive-cardioid-foam-resistance-pa-s-m3", "50",
    ]
    if coupled:
        argv.append("--passive-cardioid-coupled")
        argv.extend(_passive_cardioid_driver_cli())
    return module.parse_args(argv)


def test_crossover_chain_three_way_needs_both_fields():
    module = _load_script()
    chain, reason = module._crossover_chain(
        ["LF", "MF", "HF"], lf_mf_hz=130.0, mf_hf_hz=None
    )
    assert chain is None
    assert "both" in str(reason)
    chain, xos = module._crossover_chain(
        ["LF", "MF", "HF"], lf_mf_hz=130.0, mf_hf_hz=1000.0
    )
    assert chain == ["LF", "MF", "HF"]
    assert xos == [130.0, 1000.0]


def test_crossover_chain_two_way_uses_natural_or_single_field():
    module = _load_script()
    # Natural field for the pair wins.
    chain, xos = module._crossover_chain(
        ["MF", "HF"], lf_mf_hz=130.0, mf_hf_hz=1000.0
    )
    assert chain == ["MF", "HF"]
    assert xos == [1000.0]
    # A single filled field is used even if it is not the pair's natural one.
    chain, xos = module._crossover_chain(
        ["MF", "HF"], lf_mf_hz=800.0, mf_hf_hz=None
    )
    assert chain == ["MF", "HF"]
    assert xos == [800.0]
    # LF+HF with both 3-way fields filled and no LF/HF field is ambiguous.
    chain, reason = module._crossover_chain(
        ["LF", "HF"], lf_mf_hz=130.0, mf_hf_hz=1000.0
    )
    assert chain is None
    assert "ambiguous" in str(reason)
    # The dedicated LF/HF field makes the two-way unambiguous...
    chain, xos = module._crossover_chain(
        ["LF", "HF"], lf_mf_hz=None, mf_hf_hz=None, lf_hf_hz=500.0
    )
    assert chain == ["LF", "HF"]
    assert xos == [500.0]
    # ...and overrides leftover LF/MF and MF/HF values for the LF+HF pair.
    chain, xos = module._crossover_chain(
        ["LF", "HF"], lf_mf_hz=130.0, mf_hf_hz=1000.0, lf_hf_hz=500.0
    )
    assert chain == ["LF", "HF"]
    assert xos == [500.0]
    # Fewer than two drivers cannot form a crossover sum.
    chain, reason = module._crossover_chain(
        ["HF"], lf_mf_hz=130.0, mf_hf_hz=1000.0
    )
    assert chain is None


def test_crossover_weights_lr4_pair_is_allpass():
    module = _load_script()
    freqs = np.geomspace(20.0, 20000.0, 200)
    weights = module._crossover_weights(freqs, ["MF", "HF"], [1000.0])
    total = weights["MF"] + weights["HF"]
    np.testing.assert_allclose(np.abs(total), 1.0, atol=1.0e-9)


def test_two_way_crossover_combine_writes_outputs(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 25)
    angles = np.arange(0.0, 181.0, 15.0)
    planes = np.array(["horizontal", "vertical"], dtype=str)
    pressure = np.full(
        (freqs.size, planes.size, angles.size), 0.02 + 0.0j, dtype=np.complex128
    )
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=freqs, angles=angles, planes=planes,
        pressure=pressure,
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=freqs, angles=angles, planes=planes,
        pressure=pressure,
    )

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(mf_npz, "MF", 3),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        lf_mf_hz=None,
        mf_hf_hz=1000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    assert payload["members"] == ["MF", "HF"]
    assert payload["crossovers_hz"] == [1000.0]
    # Identical coincident sources need no alignment delay, and the LR4
    # LP+HP pair is allpass, so the aligned sum equals one source's SPL.
    for delay_ms in payload["delays_ms"].values():
        assert abs(delay_ms) < 1.0e-6
    outputs = payload["outputs"]
    for key in (
        "combined_time_aligned_frequency_response_png",
        "combined_time_aligned_directivity_heatmap_png",
        "combined_interference_heatmap_png",
        "driver_time_alignment_txt",
    ):
        assert Path(outputs[key]).exists(), key
        assert Path(outputs[key]).stat().st_size > 500
    off_axis = outputs["combined_off_axis_frequency_response_pngs"]
    assert set(off_axis) == {"horizontal", "vertical"}
    for png in off_axis.values():
        assert Path(png).exists()
    report = Path(outputs["driver_time_alignment_txt"]).read_text(encoding="utf-8")
    assert "MF -> HF (2-way)" in report
    assert "MF/HF: LR4 at 1000.000 Hz" in report


def test_two_way_lf_hf_crossover_uses_explicit_field(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 25)
    angles = np.arange(0.0, 181.0, 15.0)
    planes = np.array(["horizontal", "vertical"], dtype=str)
    pressure = np.full(
        (freqs.size, planes.size, angles.size), 0.02 + 0.0j, dtype=np.complex128
    )
    lf_npz = tmp_path / "LF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        lf_npz, name="LF", tag=2, freqs=freqs, angles=angles, planes=planes,
        pressure=pressure,
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=freqs, angles=angles, planes=planes,
        pressure=pressure,
    )

    # Leftover 3-way fields (130/1000) are present, but the dedicated LF/HF
    # field resolves the LF+HF two-way unambiguously and takes precedence.
    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(lf_npz, "LF", 2),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        lf_mf_hz=130.0,
        mf_hf_hz=1000.0,
        lf_hf_hz=500.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    assert payload["members"] == ["LF", "HF"]
    assert payload["crossovers_hz"] == [500.0]
    report = Path(payload["outputs"]["driver_time_alignment_txt"]).read_text(
        encoding="utf-8"
    )
    assert "LF -> HF (2-way)" in report
    assert "LF/HF: LR4 at 500.000 Hz" in report


def test_two_way_lf_hf_ambiguous_crossover_is_skipped(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 12)
    angles = np.arange(0.0, 181.0, 30.0)
    planes = np.array(["horizontal"], dtype=str)
    pressure = np.full(
        (freqs.size, planes.size, angles.size), 0.02 + 0.0j, dtype=np.complex128
    )
    lf_npz = tmp_path / "LF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        lf_npz, name="LF", tag=2, freqs=freqs, angles=angles, planes=planes,
        pressure=pressure,
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=freqs, angles=angles, planes=planes,
        pressure=pressure,
    )

    # Both 3-way fields filled, no LF/HF field: refuse to guess, skip loudly.
    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(lf_npz, "LF", 2),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        lf_mf_hz=130.0,
        mf_hf_hz=1000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "skipped"
    assert "ambiguous" in payload["reason"]
    assert "LF/HF" in payload["reason"]


def test_crossover_combine_uses_active_driver_lem_basis_and_reports_trim(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 25)
    angles = np.array([0.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal"], dtype=str)
    unit_lf_npz = tmp_path / "LF_pressure_basis.npz"
    active_lf_npz = tmp_path / "LF_driver_lem_pressure.npz"
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    _write_synthetic_basis(
        unit_lf_npz,
        name="LF",
        tag=2,
        freqs=freqs,
        angles=angles,
        planes=planes,
        pressure=np.full((freqs.size, 1, angles.size), 0.01, dtype=np.complex128),
    )
    np.savez_compressed(
        active_lf_npz,
        source_name=np.asarray("LF"),
        source_tag=np.asarray(2, dtype=np.int32),
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=np.full(
            (freqs.size, 1, angles.size),
            0.04,
            dtype=np.complex128,
        ),
        phase_convention=np.asarray(module.PRESSURE_NPZ_PHASE_CONVENTION),
        source_normalization=np.asarray("voltage_driven_driver_lem"),
    )
    _write_synthetic_basis(
        mf_npz,
        name="MF",
        tag=3,
        freqs=freqs,
        angles=angles,
        planes=planes,
        pressure=np.full((freqs.size, 1, angles.size), 0.02, dtype=np.complex128),
    )

    lf_result = _synthetic_source_result(unit_lf_npz, "LF", 2)
    lf_result["active_pressure_basis_npz"] = str(active_lf_npz)
    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            lf_result,
            _synthetic_source_result(mf_npz, "MF", 3),
        ],
        lf_mf_hz=500.0,
        mf_hf_hz=None,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    assert payload["level_match"]["gains_db"]["LF"] < 0.0
    assert payload["level_match"]["gains_db"]["MF"] > 0.0
    weights = module._crossover_weights(freqs, ["LF", "MF"], [500.0])
    lf_band = freqs <= 500.0
    mf_band = freqs >= 500.0
    lf_median = float(
        np.median(module._spl_db_from_pressure(0.04 * weights["LF"])[lf_band])
    )
    mf_median = float(
        np.median(module._spl_db_from_pressure(0.02 * weights["MF"])[mf_band])
    )
    expected_lf_gain = float(np.median([lf_median, mf_median]) - lf_median)
    unit_lf_median = float(
        np.median(module._spl_db_from_pressure(0.01 * weights["LF"])[lf_band])
    )
    unit_lf_gain = float(np.median([unit_lf_median, mf_median]) - unit_lf_median)
    assert payload["level_match"]["gains_db"]["LF"] == pytest.approx(
        expected_lf_gain,
        abs=1.0e-9,
    )
    assert unit_lf_gain > 0.0
    report = Path(payload["outputs"]["driver_time_alignment_txt"]).read_text(
        encoding="utf-8"
    )
    assert "Applied gain (dB)" in report
    assert "LF" in report and "MF" in report


def test_three_way_combine_interpolates_clamped_lf_grid(tmp_path):
    module = _load_script()
    full = np.geomspace(100.0, 10000.0, 25)
    clamped = np.geomspace(100.0, 800.0, 25)
    angles = np.arange(0.0, 181.0, 15.0)
    planes = np.array(["horizontal", "vertical"], dtype=str)

    def _grid(freqs):
        return np.full(
            (freqs.size, planes.size, angles.size), 0.02 + 0.0j, dtype=np.complex128
        )

    lf_npz = tmp_path / "LF_pressure_basis.npz"
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        lf_npz, name="LF", tag=2, freqs=clamped, angles=angles, planes=planes,
        pressure=_grid(clamped),
    )
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=full, angles=angles, planes=planes,
        pressure=_grid(full),
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=full, angles=angles, planes=planes,
        pressure=_grid(full),
    )

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(lf_npz, "LF", 2),
            _synthetic_source_result(mf_npz, "MF", 3),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        lf_mf_hz=300.0,
        mf_hf_hz=2000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    assert payload["members"] == ["LF", "MF", "HF"]
    assert payload["source_solved_freq_max_hz"]["LF"] == pytest.approx(800.0)
    assert payload["source_solved_freq_max_hz"]["HF"] == pytest.approx(10000.0)
    report = Path(payload["outputs"]["driver_time_alignment_txt"]).read_text(
        encoding="utf-8"
    )
    assert "Clamped solve bands" in report
    assert "LF 800 Hz" in report


def test_harmonize_bases_zeroes_clamped_source_above_its_band():
    module = _load_script()
    full = np.geomspace(100.0, 10000.0, 25)
    clamped = np.geomspace(100.0, 800.0, 25)
    angles = np.array([0.0, 90.0])
    planes = np.array(["horizontal"], dtype=str)
    make = lambda freqs: module.PressureBasis(
        source_name="X",
        source_tag=1,
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=np.full(
            (freqs.size, 1, angles.size), 0.02 + 0.0j, dtype=np.complex128
        ),
    )
    freqs, grids, solved_top = module._harmonize_bases(
        {"LF": make(clamped), "HF": make(full)}
    )
    np.testing.assert_allclose(freqs, full)
    assert solved_top == {"LF": pytest.approx(800.0), "HF": pytest.approx(10000.0)}
    above = freqs > 800.0 * (1.0 + 1.0e-6)
    assert np.all(grids["LF"][above] == 0.0)
    assert np.all(np.abs(grids["LF"][~above]) > 0.0)
    np.testing.assert_allclose(np.abs(grids["HF"]), 0.02)


def test_directivity_power_integration_monopole_and_dipole():
    module = _load_script()
    freqs = np.array([100.0, 500.0], dtype=np.float64)
    angles = np.linspace(0.0, 180.0, 721)
    planes = np.array(["horizontal", "vertical"], dtype=str)
    monopole = np.full(
        (freqs.size, planes.size, angles.size),
        0.02 + 0.0j,
        dtype=np.complex128,
    )

    mono = module._directivity_power_metrics_from_pressure(
        monopole,
        angles,
        polar_distance_m=2.0,
    )

    np.testing.assert_allclose(mono["directivity_index_db"], 0.0, atol=1.0e-10)
    np.testing.assert_allclose(mono["power_response_db"], 60.0, atol=1.0e-10)
    expected_power = (
        4.0
        * np.pi
        * 2.0**2
        * (0.02**2)
        / (module.radiation_impedance.RHO_AIR * module.radiation_impedance.C_AIR)
    )
    np.testing.assert_allclose(mono["acoustic_power_w"], expected_power, rtol=1.0e-10)

    dipole_pattern = np.cos(np.radians(angles))
    dipole = dipole_pattern[None, None, :] * np.ones(
        (freqs.size, planes.size, 1),
        dtype=np.complex128,
    )
    dip = module._directivity_power_metrics_from_pressure(
        dipole,
        angles,
        polar_distance_m=1.0,
    )

    np.testing.assert_allclose(
        dip["directivity_index_db"],
        10.0 * np.log10(3.0),
        atol=2.0e-4,
    )


def test_beamwidth_minus6_db_synthetic_beam_per_plane():
    module = _load_script()
    freqs = np.array([500.0, 1000.0, 2000.0], dtype=np.float64)
    angles = np.linspace(-90.0, 90.0, 361)
    planes = np.array(["horizontal", "vertical"], dtype=str)
    h_pressure = 10.0 ** ((-6.0 * (angles / 25.0) ** 2) / 20.0)
    v_pressure = 10.0 ** ((-6.0 * (angles / 40.0) ** 2) / 20.0)
    pressure = np.repeat(
        np.stack([h_pressure, v_pressure], axis=0)[None, :, :],
        freqs.size,
        axis=0,
    ).astype(np.complex128)

    widths, limited, assumed_symmetric = module._beamwidth_minus6_db_by_plane(
        pressure,
        angles,
        planes,
    )

    np.testing.assert_allclose(widths["horizontal"], 50.0, atol=1.0e-9)
    np.testing.assert_allclose(widths["vertical"], 80.0, atol=1.0e-9)
    assert not np.any(limited["horizontal"])
    assert not np.any(limited["vertical"])
    assert not np.any(assumed_symmetric["horizontal"])
    assert not np.any(assumed_symmetric["vertical"])


def test_beamwidth_flags_one_sided_symmetry_assumption_in_artifacts(tmp_path):
    module = _load_script()
    freqs = np.array([500.0, 1000.0], dtype=np.float64)
    one_sided_angles = np.linspace(0.0, 90.0, 91)
    planes = np.array(["horizontal"], dtype=str)
    polar_distance_m = 2.0
    pattern = 10.0 ** ((-6.0 * (one_sided_angles / 30.0) ** 2) / 20.0)
    common_delay = np.exp(
        -1j * 2.0 * np.pi * freqs * polar_distance_m / module.SPEED_OF_SOUND_M_S
    )
    pressure = common_delay[:, None, None] * pattern[None, None, :]

    outputs = module._write_pressure_grid_derived_artifacts(
        tmp_path,
        "one-sided",
        label="One sided",
        frequencies_hz=freqs,
        angles_deg=one_sided_angles,
        planes=planes,
        pressure_complex=pressure,
        polar_distance_m=polar_distance_m,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    payload = json.loads(Path(outputs["beamwidth_json"]).read_text(encoding="utf-8"))
    assert payload["assumed_symmetric_from_one_sided_grid"]["horizontal"] == [
        True,
        True,
    ]
    with Path(outputs["beamwidth_csv"]).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [
        row["horizontal_assumed_symmetric_from_one_sided_grid"] for row in rows
    ] == ["true", "true"]

    full_angles = np.linspace(-90.0, 90.0, 181)
    full_pattern = 10.0 ** ((-6.0 * (full_angles / 30.0) ** 2) / 20.0)
    full_pressure = common_delay[:, None, None] * full_pattern[None, None, :]
    _widths, _limited, full_assumed = module._beamwidth_minus6_db_by_plane(
        full_pressure,
        full_angles,
        planes,
    )
    assert not np.any(full_assumed["horizontal"])


def test_group_delay_engineering_pure_delay_is_positive():
    module = _load_script()
    freqs = np.linspace(100.0, 2000.0, 200)
    tau_s = 0.0007
    pressure = np.exp(-1j * 2.0 * np.pi * freqs * tau_s)

    group_delay_s, phase_rad = module._group_delay_from_pressure(freqs, pressure)

    np.testing.assert_allclose(group_delay_s, tau_s, atol=1.0e-12)
    np.testing.assert_allclose(
        phase_rad,
        -2.0 * np.pi * freqs * tau_s,
        atol=1.0e-12,
    )


def test_group_delay_sparse_log_grid_removes_common_propagation_delay(caplog):
    module = _load_script()
    freqs = np.geomspace(50.0, 20000.0, 60)
    polar_distance_m = 2.0
    tau_s = polar_distance_m / module.SPEED_OF_SOUND_M_S + 0.2e-3
    pressure = np.exp(-1j * 2.0 * np.pi * freqs * tau_s)

    with caplog.at_level(logging.WARNING):
        group_delay_s, _phase_rad = module._group_delay_from_pressure(
            freqs,
            pressure,
            polar_distance_m=polar_distance_m,
            warning_label="synthetic sparse band",
        )

    assert not [record for record in caplog.records if record.name == module.LOGGER.name]
    assert np.all(np.isfinite(group_delay_s))
    assert np.min(group_delay_s) > 0.0
    np.testing.assert_allclose(group_delay_s, tau_s, rtol=0.01)


def test_crossover_combine_skips_single_source_gracefully(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 10)
    angles = np.array([0.0, 90.0])
    planes = np.array(["horizontal"], dtype=str)
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=freqs, angles=angles, planes=planes,
        pressure=np.full((freqs.size, 1, angles.size), 0.02, dtype=np.complex128),
    )
    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [_synthetic_source_result(hf_npz, "HF", 4)],
        lf_mf_hz=None,
        mf_hf_hz=1000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )
    assert payload["status"] == "skipped"
    assert "two" in payload["reason"]


def _delayed_pressure(freqs, planes, angles, *, arrival_s, amplitude=0.02):
    """Uniform radiator whose wave arrives ``arrival_s`` after t=0.

    Built in the SOLVER ``e^{-i omega t}`` convention (a delay of tau
    multiplies the phasor by ``e^{+i omega tau}``), exactly as
    ``hornlab_metal_bem`` produces ``pressure_complex``. Written to a legacy
    (keyless) NPZ, the loader conjugates this into the engineering
    convention that the combine/export math assumes.
    """
    phase = np.exp(1j * 2.0 * np.pi * np.asarray(freqs) * arrival_s)
    return amplitude * phase[:, None, None] * np.ones(
        (len(freqs), len(planes), len(angles)), dtype=np.complex128
    )


def test_two_way_combine_recovers_known_arrival_offset(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 60)
    angles = np.arange(0.0, 181.0, 15.0)
    planes = np.array(["horizontal"], dtype=str)
    # HF arrives 0.3 ms after MF (e.g. deeper horn throat): MF must be
    # delayed by 0.3 ms to align.
    offset_s = 0.3e-3
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=freqs, angles=angles, planes=planes,
        pressure=_delayed_pressure(freqs, planes, angles, arrival_s=0.0),
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=freqs, angles=angles, planes=planes,
        pressure=_delayed_pressure(freqs, planes, angles, arrival_s=offset_s),
    )

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(mf_npz, "MF", 3),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        lf_mf_hz=None,
        mf_hf_hz=1000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    assert payload["delays_ms"]["MF"] == pytest.approx(0.3, abs=1.0e-6)
    assert payload["delays_ms"]["HF"] == pytest.approx(0.0, abs=1.0e-9)
    assert payload["arrival_offsets_ms"]["HF"] == pytest.approx(0.3, abs=1.0e-6)


def test_two_way_combine_is_coherent_across_band_not_only_at_fc(tmp_path):
    """Solver-convention delayed bases must align coherently through fc±20%.

    Two identical radiators offset by a pure arrival delay sum to an allpass
    (LR4 LP+HP) once aligned — |sum| equals the driver amplitude at EVERY
    frequency, not just the crossover. Getting the delay complement-wrong
    (T-dt instead of dt) is coherent exactly at fc but rips through the rest
    of the band, which is what this pins down.
    """
    module = _load_script()
    freqs = np.geomspace(200.0, 5000.0, 400)
    angles = np.array([0.0, 30.0])
    planes = np.array(["horizontal"], dtype=str)
    fc = 1000.0
    offset_s = 0.3e-3
    amplitude = 0.02
    arrivals = {"MF": 0.0, "HF": offset_s}
    paths = {}
    for name, tag in (("MF", 3), ("HF", 4)):
        paths[name] = tmp_path / f"{name}_pressure_basis.npz"
        _write_synthetic_basis(
            paths[name], name=name, tag=tag, freqs=freqs, angles=angles,
            planes=planes,
            pressure=_delayed_pressure(
                freqs, planes, angles, arrival_s=arrivals[name],
                amplitude=amplitude,
            ),
        )

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(paths["MF"], "MF", 3),
            _synthetic_source_result(paths["HF"], "HF", 4),
        ],
        lf_mf_hz=None,
        mf_hf_hz=fc,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    assert payload["delays_ms"]["MF"] == pytest.approx(0.3, abs=1.0e-6)
    # Rebuild the aligned sum in the engineering convention from the REPORTED
    # delays and gains: if they are physical, the sum is allpass-flat.
    weights = module._crossover_weights(freqs, ["MF", "HF"], [fc])
    total = np.zeros(freqs.size, dtype=np.complex128)
    for name in ("MF", "HF"):
        engineering = amplitude * np.exp(
            -1j * 2.0 * np.pi * freqs * arrivals[name]
        )
        gain = 10.0 ** (payload["level_match"]["gains_db"][name] / 20.0)
        delay = np.exp(
            -1j * 2.0 * np.pi * freqs * payload["delays_ms"][name] * 1.0e-3
        )
        total += engineering * weights[name] * gain * delay
    band = (freqs >= 0.8 * fc) & (freqs <= 1.2 * fc)
    np.testing.assert_allclose(np.abs(total[band]), amplitude, rtol=1.0e-6)
    # The pure-delay pair stays coherent over the full solved band too.
    np.testing.assert_allclose(np.abs(total), amplitude, rtol=1.0e-6)


def test_near_inphase_pair_gets_small_delay_not_full_period(tmp_path):
    """A pair only a few degrees out at fc must get a near-zero delay.

    The passive-cardioid port pulls the MF on-axis phase a little past the LF,
    making the LF/MF phase difference slightly NEGATIVE (~-10 deg) at the
    crossover. The old minimum-non-negative wrap turned that into a
    near-full-period (~4.86 ms at 200 Hz) delay on LF -- coherent exactly at fc
    but a deep cancellation notch just above it. Here LF arrives a hair after
    MF to reproduce that negative phase; the aligned pair must stay coherent
    across the band, i.e. the delay must be a small fraction of a period.
    """
    module = _load_script()
    # Log-symmetric band around fc so the two channels' level-match gains match
    # exactly (LP over [fc/5, fc] mirrors HP over [fc, fc*5]); this isolates the
    # alignment, matching test_two_way_combine_is_coherent_across_band.
    fc = 200.0
    freqs = np.geomspace(fc / 5.0, fc * 5.0, 400)
    angles = np.array([0.0, 30.0])
    planes = np.array(["horizontal"], dtype=str)
    amplitude = 0.02
    # arg(LF/MF)|fc = -2*pi*fc*offset; 0.1389 ms -> about -10 deg, matching the
    # PartyMEH passive-cardioid run's LF-MF raw phase difference.
    arrivals = {"LF": 0.1389e-3, "MF": 0.0}
    paths = {}
    for name, tag in (("LF", 2), ("MF", 3)):
        paths[name] = tmp_path / f"{name}_pressure_basis.npz"
        _write_synthetic_basis(
            paths[name], name=name, tag=tag, freqs=freqs, angles=angles,
            planes=planes,
            pressure=_delayed_pressure(
                freqs, planes, angles, arrival_s=arrivals[name],
                amplitude=amplitude,
            ),
        )

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(paths["LF"], "LF", 2),
            _synthetic_source_result(paths["MF"], "MF", 3),
        ],
        lf_mf_hz=fc,
        mf_hf_hz=None,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    # The trigger: LF-MF is only a few degrees out at fc.
    raw = payload["phase_checks"][0]["raw_phase_deg"]
    assert -15.0 < raw < -5.0
    # Regression: the relative delay must be a small fraction of the 5 ms
    # period, NOT the old ~4.86 ms full-period wrap.
    rel_ms = abs(payload["delays_ms"]["LF"] - payload["delays_ms"]["MF"])
    assert rel_ms < 1.0
    # And the aligned pure-delay pair sums allpass-flat across the whole band.
    weights = module._crossover_weights(freqs, ["LF", "MF"], [fc])
    total = np.zeros(freqs.size, dtype=np.complex128)
    for name in ("LF", "MF"):
        engineering = amplitude * np.exp(
            -1j * 2.0 * np.pi * freqs * arrivals[name]
        )
        gain = 10.0 ** (payload["level_match"]["gains_db"][name] / 20.0)
        delay = np.exp(
            -1j * 2.0 * np.pi * freqs * payload["delays_ms"][name] * 1.0e-3
        )
        total += engineering * weights[name] * gain * delay
    np.testing.assert_allclose(np.abs(total), amplitude, rtol=1.0e-6)


def test_three_way_combine_recovers_chained_arrival_offsets(tmp_path):
    module = _load_script()
    freqs = np.geomspace(50.0, 10000.0, 80)
    angles = np.arange(0.0, 181.0, 15.0)
    planes = np.array(["horizontal"], dtype=str)
    # Arrivals: LF first, MF +0.2 ms, HF +0.5 ms. The alignment zeroes the
    # TOTAL phase difference (driver arrival + filter phase) at each
    # crossover — a band-pass branch carries extra phase from its other
    # filter tail, so the added delays exceed the raw arrival offsets. The
    # invariant to pin is coherence: aligned phase ~0 at both crossovers,
    # delays ordered LF > MF > HF = 0.
    arrivals = {"LF": 0.0, "MF": 0.2e-3, "HF": 0.5e-3}
    paths = {}
    for name, tag in (("LF", 2), ("MF", 3), ("HF", 4)):
        paths[name] = tmp_path / f"{name}_pressure_basis.npz"
        _write_synthetic_basis(
            paths[name], name=name, tag=tag, freqs=freqs, angles=angles,
            planes=planes,
            pressure=_delayed_pressure(freqs, planes, angles, arrival_s=arrivals[name]),
        )

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(paths["LF"], "LF", 2),
            _synthetic_source_result(paths["MF"], "MF", 3),
            _synthetic_source_result(paths["HF"], "HF", 4),
        ],
        lf_mf_hz=300.0,
        mf_hf_hz=2000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    assert payload["delays_ms"]["HF"] == pytest.approx(0.0, abs=1.0e-9)
    assert payload["delays_ms"]["MF"] > 0.2
    assert payload["delays_ms"]["LF"] > payload["delays_ms"]["MF"]
    for row in payload["phase_checks"]:
        assert abs(row["raw_phase_deg"]) > 1.0  # non-trivial input
        assert abs(row["aligned_phase_deg"]) < 1.0e-3  # coherent at the XO


def test_crossover_above_clamped_band_measures_alignment_at_band_top(tmp_path):
    module = _load_script()
    full = np.geomspace(100.0, 10000.0, 60)
    clamped = np.geomspace(100.0, 1500.0, 60)
    angles = np.arange(0.0, 181.0, 15.0)
    planes = np.array(["horizontal"], dtype=str)
    offset_s = 0.3e-3
    # MF clamped to 1.5 kHz but the MF/HF crossover requested at 2 kHz: the
    # pair phase must be measured at the clamped top, not on the zero-filled
    # region (which would silently produce a 0 ms delay).
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=clamped, angles=angles, planes=planes,
        pressure=_delayed_pressure(clamped, planes, angles, arrival_s=0.0),
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=full, angles=angles, planes=planes,
        pressure=_delayed_pressure(full, planes, angles, arrival_s=offset_s),
    )

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(mf_npz, "MF", 3),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        lf_mf_hz=None,
        mf_hf_hz=2000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
    )

    assert payload["status"] == "complete"
    # Evaluation snaps to the highest master-grid sample inside both solved
    # bands (just below the 1.5 kHz clamp), never onto the zero-filled region.
    eval_hz = payload["pair_alignment_eval_hz"]["MF-HF"]
    assert 1300.0 < eval_hz <= 1500.0
    assert payload["delays_ms"]["MF"] == pytest.approx(0.3, abs=1.0e-4)
    assert payload["alignment_warnings"]
    assert "measured at" in payload["alignment_warnings"][0]
    report = Path(payload["outputs"]["driver_time_alignment_txt"]).read_text(
        encoding="utf-8"
    )
    assert "WARNING" in report


def test_crossover_combine_uses_passive_cardioid_mf_override(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 30)
    angles = np.arange(0.0, 181.0, 15.0)
    planes = np.array(["horizontal"], dtype=str)
    direct = _delayed_pressure(freqs, planes, angles, arrival_s=0.0, amplitude=0.02)
    combined = _delayed_pressure(freqs, planes, angles, arrival_s=0.0, amplitude=0.05)

    mf_npz = tmp_path / "MF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=freqs, angles=angles, planes=planes,
        pressure=direct,
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=freqs, angles=angles, planes=planes,
        pressure=direct,
    )
    override_npz = tmp_path / "MF_passive_cardioid_results.npz"
    np.savez_compressed(
        override_npz,
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=combined,
    )

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(mf_npz, "MF", 3),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        lf_mf_hz=None,
        mf_hf_hz=1000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
        mf_override_npz=override_npz,
        mf_override_kind="passive_cardioid_combined",
    )

    assert payload["status"] == "complete"
    assert payload["mf_basis"] == "passive_cardioid_combined"
    # The MF channel is ~8 dB hotter than the direct basis, so the level
    # match must trim MF relative to HF — proof the override grid was used.
    assert payload["level_match"]["gains_db"]["MF"] == pytest.approx(
        -0.5 * 20.0 * np.log10(0.05 / 0.02), abs=0.2
    )
    report = Path(payload["outputs"]["driver_time_alignment_txt"]).read_text(
        encoding="utf-8"
    )
    assert "passive_cardioid_combined" in report


def test_crossover_combine_uses_coupled_passive_cardioid_override(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 30)
    angles = np.arange(0.0, 181.0, 15.0)
    planes = np.array(["horizontal"], dtype=str)
    direct = _delayed_pressure(freqs, planes, angles, arrival_s=0.0, amplitude=0.02)
    fixed = _delayed_pressure(freqs, planes, angles, arrival_s=0.0, amplitude=0.03)
    coupled = _delayed_pressure(freqs, planes, angles, arrival_s=0.0, amplitude=0.06)

    mf_npz = tmp_path / "MF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=freqs, angles=angles, planes=planes,
        pressure=direct,
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=freqs, angles=angles, planes=planes,
        pressure=direct,
    )
    fixed_npz = tmp_path / "MF_passive_cardioid_results.npz"
    coupled_npz = tmp_path / "MF_passive_cardioid_coupled_results.npz"
    for path, pressure in ((fixed_npz, fixed), (coupled_npz, coupled)):
        np.savez_compressed(
            path,
            frequencies_hz=freqs,
            observation_angles_deg=angles,
            observation_planes=planes,
            pressure_complex=pressure,
            phase_convention=np.asarray(module.PRESSURE_NPZ_PHASE_CONVENTION),
        )

    override, kind = module._preferred_passive_cardioid_results(
        {
            "status": "complete",
            "outputs": {"results_npz": str(fixed_npz)},
            "coupled": {
                "status": "complete",
                "outputs": {"results_npz": str(coupled_npz)},
            },
        }
    )
    assert override == coupled_npz
    assert kind == "passive_cardioid_coupled"

    payload = module._write_crossover_alignment_outputs(
        tmp_path,
        [
            _synthetic_source_result(mf_npz, "MF", 3),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        lf_mf_hz=None,
        mf_hf_hz=1000.0,
        polar_distance_m=2.0,
        mesh_valid_hz=None,
        mesh_valid_radiating_hz=None,
        mf_override_npz=override,
        mf_override_kind=kind,
    )

    assert payload["status"] == "complete"
    assert payload["mf_basis"] == "passive_cardioid_coupled"
    assert payload["level_match"]["gains_db"]["MF"] == pytest.approx(
        -0.5 * 20.0 * np.log10(0.06 / 0.02), abs=0.2
    )


def test_passive_cardioid_exterior_drive_uses_port_mf_mutual_term(tmp_path, monkeypatch):
    module = _load_script()
    freqs = np.array([100.0, 200.0], dtype=np.float64)
    angles = np.array([-90.0, 0.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal", "vertical"], dtype=str)
    mf_pressure = np.full((2, 2, 3), 0.010 + 0.0j, dtype=np.complex128)
    port_pressure = np.full((2, 2, 3), 0.002 + 0.0j, dtype=np.complex128)

    mf_basis = tmp_path / "MF_pressure_basis.npz"
    port_basis = tmp_path / "PORT_EXIT_pressure_basis.npz"
    for path, name, tag, pressure in (
        (mf_basis, "MF", 3, mf_pressure),
        (port_basis, "PORT_EXIT", 10, port_pressure),
    ):
        np.savez_compressed(
            path,
            source_name=np.asarray(name),
            source_tag=np.asarray(tag, dtype=np.int32),
            frequencies_hz=freqs,
            observation_angles_deg=angles,
            observation_planes=planes,
            pressure_complex=pressure,
        )

    # 2x2 solver matrix over (PORT_EXIT, MF): the mutual column drives the
    # exterior term; the engineering convention is conj(solver).
    z_pp = np.array([100.0 - 10.0j, 120.0 - 12.0j], dtype=np.complex128)
    z_pm = np.array([40.0 - 4.0j, 50.0 - 5.0j], dtype=np.complex128)
    solver_matrix = np.zeros((2, 2, 2), dtype=np.complex128)
    solver_matrix[:, 0, 0] = z_pp
    solver_matrix[:, 0, 1] = z_pm
    solver_matrix[:, 1, 0] = z_pm
    solver_matrix[:, 1, 1] = np.array([500.0 - 50.0j, 520.0 - 52.0j])
    matrix_npz = tmp_path / "port_exit_radiation_impedance_matrix.npz"
    np.savez_compressed(
        matrix_npz,
        frequencies_hz=freqs,
        aperture_names=np.asarray(["PORT_EXIT", "MF"]),
        aperture_area_m2=np.asarray([0.01, 0.02], dtype=np.float64),
        solver_impedance_matrix=solver_matrix,
    )
    monkeypatch.setattr(module, "_mesh_tag_area_m2", lambda _m, _t, mesh_scale: 0.02)
    args = module.parse_args(
        [
            "--mesh", str(tmp_path / "fake.msh"),
            "--out", str(tmp_path),
            "--source", "MF:3",
            "--source", "PORT_EXIT:10",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l", "10",
            "--passive-cardioid-port-length-mm", "20",
            "--passive-cardioid-foam-resistance-pa-s-m3", "50",
        ]
    )
    source_results = [
        {
            "name": "MF", "tag": 3, "pressure_basis_npz": str(mf_basis),
            "mesh_valid_freq_max_hz": None, "aperture_valid_freq_max_hz": None,
        },
        {
            "name": "PORT_EXIT", "tag": 10, "pressure_basis_npz": str(port_basis),
            "mesh_valid_freq_max_hz": None, "aperture_valid_freq_max_hz": None,
        },
    ]

    payload = module._solve_passive_cardioid_mf(
        tmp_path / "fake.msh", tmp_path, args,
        source_results=source_results,
        radiation_payload={"outputs": {"npz": str(matrix_npz)}},
    )

    assert payload["status"] == "complete"
    diag = payload["diagnostics"]
    assert diag["exterior_drive_included"] is True
    rho_c2 = module.radiation_impedance.RHO_AIR * module.radiation_impedance.C_AIR**2
    compliance = 0.010 / rho_c2
    assert diag["chamber_compliance_m3_per_pa"] == pytest.approx(compliance)
    assert diag["rc_corner_hz"] == pytest.approx(1.0 / (2.0 * np.pi * 50.0 * compliance))

    # Hand-compute the expected weight: conj() converts the solver mutual to
    # the engineering convention, and the exterior term multiplies the same
    # branch ratio as the interior rear drive.
    termination = module.radiation_impedance.termination_load_from_solver_matrix(
        solver_matrix, receiver_index=0,
    )
    branch = module.radiation_impedance.terminated_chamber_port_branch(
        freqs, termination,
        chamber_volume_m3=0.010, port_area_m2=0.01, port_length_m=0.020,
        series_resistance_pa_s_m3=50.0,
    )
    omega = 2.0 * np.pi * freqs
    exterior = -1j * omega * compliance * np.conjugate(z_pm)
    expected_weight = (
        (0.02 / 0.01)
        * branch.exit_to_input_volume_velocity_ratio
        * (-1.0 + exterior)
    )
    with np.load(tmp_path / "MF_passive_cardioid_results.npz") as result:
        np.testing.assert_allclose(result["port_velocity_weight"], expected_weight)
        np.testing.assert_allclose(result["exterior_drive"], exterior)
    # The mutual term must actually change the result vs interior-only.
    interior_only = (0.02 / 0.01) * branch.exit_to_input_volume_velocity_ratio * -1.0
    assert not np.allclose(expected_weight, interior_only)


def test_passive_cardioid_monopole_pair_nulls_at_rear(tmp_path, monkeypatch):
    """Textbook cardioid: rear source inverted and delayed by its own spacing.

    MF sits at the origin, the port exit a distance d behind it. With the
    port driven at ``-e^{-j w d/c} * Q_mf`` the pair nulls at 180 deg at
    every frequency: path lead and drive delay cancel exactly. Bases are
    written through ``_write_pressure_basis_npz`` from solver-convention
    fields, so this pins the write-conjugate + branch-multiply chain end to
    end; conjugate-wrong port phase flips the null to the FRONT.
    """
    module = _load_script()
    c = module.SPEED_OF_SOUND_M_S
    d = 0.1
    tau = d / c
    freqs = np.array([200.0, 400.0, 800.0], dtype=np.float64)
    angles = np.array([0.0, 90.0, 180.0], dtype=np.float64)
    planes = np.array(["horizontal"], dtype=str)
    k = 2.0 * np.pi * freqs / c
    # Solver-convention (e^{+ikr}) far fields with the common radius dropped:
    # the port sits d*cos(theta) farther from the observer at angle theta.
    mf_pressure = np.ones((freqs.size, 1, angles.size), dtype=np.complex128)
    port_pressure = np.exp(
        1j * k[:, None, None] * d * np.cos(np.radians(angles))[None, None, :]
    )

    mf_npz = tmp_path / "MF_pressure_basis.npz"
    port_npz = tmp_path / "PORT_EXIT_pressure_basis.npz"
    for path, name, tag, pressure in (
        (mf_npz, "MF", 3, mf_pressure),
        (port_npz, "PORT_EXIT", 10, port_pressure),
    ):
        module._write_pressure_basis_npz(
            path,
            SimpleNamespace(
                frequencies_hz=freqs,
                observation_angles_deg=angles,
                observation_planes=planes,
                pressure_complex=pressure,
            ),
            source_name=name,
            source_tag=tag,
        )

    matrix_npz = tmp_path / "port_exit_radiation_impedance_matrix.npz"
    np.savez_compressed(
        matrix_npz,
        frequencies_hz=freqs,
        aperture_names=np.asarray(["PORT_EXIT"]),
        aperture_area_m2=np.asarray([0.01], dtype=np.float64),
        solver_impedance_matrix=np.full((3, 1, 1), 100.0 - 10.0j),
    )
    monkeypatch.setattr(module, "_mesh_tag_area_m2", lambda _m, _t, mesh_scale: 0.01)
    # Idealize the branch to a pure engineering-convention delay of d/c so
    # the combine's weight is exactly the textbook -e^{-j w tau}.
    monkeypatch.setattr(
        module.radiation_impedance,
        "terminated_chamber_port_branch",
        lambda frequencies_hz, termination_load, **_kwargs: SimpleNamespace(
            frequencies_hz=frequencies_hz,
            termination_load=termination_load,
            input_impedance=np.ones(freqs.size, dtype=np.complex128),
            exit_to_input_volume_velocity_ratio=np.exp(
                -1j * 2.0 * np.pi * frequencies_hz * tau
            ),
        ),
    )
    args = module.parse_args(
        [
            "--mesh", str(tmp_path / "fake.msh"),
            "--out", str(tmp_path),
            "--source", "MF:3",
            "--source", "PORT_EXIT:10",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l", "10",
            "--passive-cardioid-port-length-mm", "20",
        ]
    )

    payload = module._solve_passive_cardioid_mf(
        tmp_path / "fake.msh", tmp_path, args,
        source_results=[
            {
                "name": "MF", "tag": 3, "pressure_basis_npz": str(mf_npz),
                "mesh_valid_freq_max_hz": None, "aperture_valid_freq_max_hz": None,
            },
            {
                "name": "PORT_EXIT", "tag": 10, "pressure_basis_npz": str(port_npz),
                "mesh_valid_freq_max_hz": None, "aperture_valid_freq_max_hz": None,
            },
        ],
        radiation_payload={"outputs": {"npz": str(matrix_npz)}},
    )

    assert payload["status"] == "complete"
    with np.load(tmp_path / "MF_passive_cardioid_results.npz") as result:
        np.testing.assert_allclose(
            result["port_velocity_weight"],
            -np.exp(-1j * 2.0 * np.pi * freqs * tau),
        )
        total = result["pressure_complex"]
        expected = 1.0 - np.exp(
            -1j
            * (
                2.0 * np.pi * freqs[:, None, None] * tau
                + k[:, None, None] * d * np.cos(np.radians(angles))[None, None, :]
            )
        )
        np.testing.assert_allclose(total, expected, atol=1.0e-12)
        rear = int(np.argmin(np.abs(angles - 180.0)))
        front = int(np.argmin(np.abs(angles)))
        # Perfect rear null at every frequency; the front must stay live
        # (a conjugate-wrong port basis would null the front instead).
        assert np.max(np.abs(total[:, :, rear])) < 1.0e-9
        assert np.min(np.abs(total[:, :, front])) > 0.5


def test_export_vituixcad_crossover_requires_combined_set(tmp_path):
    module = _load_script()
    mesh_path = tmp_path / "fake.msh"
    mesh_path.write_text("$MeshFormat\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="combined/crossover set"):
        module.main(
            [
                "--mesh",
                str(mesh_path),
                "--out",
                str(tmp_path / "out"),
                "--source",
                "MF:3",
                "--source",
                "HF:4",
                "--freq-min-hz",
                "100",
                "--freq-max-hz",
                "2000",
                "--crossover-mf-hf-hz",
                "1000",
                "--export-vituixcad",
                "--skip-combined-set",
                "--dry-run",
            ]
        )


def test_postprocess_only_vituixcad_skip_combined_regenerates_frd_without_vxp(
    tmp_path,
):
    module = _load_script()
    freqs = np.geomspace(100.0, 2000.0, 8)
    angles = np.array([0.0, 45.0, 90.0], dtype=np.float64)
    planes = np.array(["horizontal"], dtype=str)
    for name, tag in (("MF", 3), ("HF", 4)):
        _write_synthetic_basis(
            tmp_path / f"{name}_pressure_basis.npz",
            name=name,
            tag=tag,
            freqs=freqs,
            angles=angles,
            planes=planes,
            pressure=_delayed_pressure(freqs, planes, angles, arrival_s=0.0),
        )
    _run_manifest_path(tmp_path, "direct_solve_manifest.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "MF",
                        "tag": 3,
                        "pressure_basis_npz": str(tmp_path / "MF_pressure_basis.npz"),
                    },
                    {
                        "name": "HF",
                        "tag": 4,
                        "pressure_basis_npz": str(tmp_path / "HF_pressure_basis.npz"),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    assert module.main(
        [
            "--mesh",
            str(tmp_path / "old-run-mesh.msh"),
            "--out",
            str(tmp_path),
            "--source",
            "MF:3",
            "--source",
            "HF:4",
            "--freq-min-hz",
            "100",
            "--freq-max-hz",
            "2000",
            "--crossover-mf-hf-hz",
            "1000",
            "--export-vituixcad",
            "--skip-combined-set",
            "--postprocess-only",
            "--no-run-report",
        ]
    ) == 0

    assert (tmp_path / "vituixcad" / "hor" / "MF 0.frd").exists()
    assert (tmp_path / "vituixcad" / "hor" / "HF 0.frd").exists()
    assert not (tmp_path / "vituixcad" / "HornLab_active_lr4.vxp").exists()


def test_vituixcad_export_writes_frd_sets_with_shared_timing(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 20)
    angles = np.arange(0.0, 181.0, 45.0)  # 0,45,90,135,180
    planes = np.array(["horizontal", "vertical"], dtype=str)
    distance_m = 2.0
    # MF: pure time-of-flight arrival at the observation distance -> after
    # the common ToF removal its exported phase must be ~0 at every
    # frequency. HF: extra 0.2 ms -> exported phase -360*f*0.0002 deg.
    tof_s = distance_m / module.SPEED_OF_SOUND_M_S
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=freqs, angles=angles, planes=planes,
        pressure=_delayed_pressure(freqs, planes, angles, arrival_s=tof_s),
    )
    _write_synthetic_basis(
        hf_npz, name="HF", tag=4, freqs=freqs, angles=angles, planes=planes,
        pressure=_delayed_pressure(freqs, planes, angles, arrival_s=tof_s + 0.2e-3),
    )

    payload = module._write_vituixcad_export(
        tmp_path,
        [
            _synthetic_source_result(mf_npz, "MF", 3),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        polar_distance_m=distance_m,
    )

    assert payload["status"] == "complete"
    assert payload["drivers"] == ["MF", "HF"]
    export_dir = Path(payload["export_dir"])
    assert (export_dir / "README.txt").exists()
    # 5 solved angles + mirrored -45/-90/-135 => 8 files per driver per plane.
    hor_mf = sorted((export_dir / "hor").glob("MF *.frd"))
    assert len(hor_mf) == 8
    assert (export_dir / "hor" / "MF -45.frd").exists()
    assert (export_dir / "ver" / "HF 90.frd").exists()

    def _read_frd(path):
        rows = [
            line.split()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("*")
        ]
        data = np.asarray(rows, dtype=np.float64)
        return data[:, 0], data[:, 1], data[:, 2]

    f_mf, _spl, phase_mf = _read_frd(export_dir / "hor" / "MF 0.frd")
    np.testing.assert_allclose(f_mf, freqs, rtol=1.0e-6)
    # Common ToF removed -> MF phase flat at ~0 deg.
    assert np.max(np.abs(phase_mf)) < 1.0e-3
    _f, _spl, phase_hf = _read_frd(export_dir / "hor" / "HF 0.frd")
    expected = np.angle(np.exp(-1j * 2.0 * np.pi * freqs * 0.2e-3), deg=True)
    np.testing.assert_allclose(phase_hf, expected, atol=1.0e-3)


def test_vituixcad_export_writes_active_lr4_vxp_when_alignment_is_available(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 10000.0, 20)
    angles = np.array([0.0, 45.0, 90.0])
    planes = np.array(["horizontal", "vertical"], dtype=str)
    distance_m = 2.0
    tof_s = distance_m / module.SPEED_OF_SOUND_M_S
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    hf_npz = tmp_path / "HF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz,
        name="MF",
        tag=3,
        freqs=freqs,
        angles=angles,
        planes=planes,
        pressure=_delayed_pressure(freqs, planes, angles, arrival_s=tof_s),
    )
    _write_synthetic_basis(
        hf_npz,
        name="HF",
        tag=4,
        freqs=freqs,
        angles=angles,
        planes=planes,
        pressure=_delayed_pressure(freqs, planes, angles, arrival_s=tof_s),
    )

    payload = module._write_vituixcad_export(
        tmp_path,
        [
            _synthetic_source_result(mf_npz, "MF", 3),
            _synthetic_source_result(hf_npz, "HF", 4),
        ],
        polar_distance_m=distance_m,
        active_crossover_payload={
            "status": "complete",
            "type": "lr4_time_aligned_on_axis_sum",
            "members": ["MF", "HF"],
            "crossovers_hz": [1000.0],
            "mf_basis": "direct",
            "level_match": {"gains_db": {"MF": -2.0, "HF": 1.5}},
            "delays_ms": {"MF": 0.25, "HF": 0.0},
        },
    )

    vxp_path = Path(payload["outputs"]["vituixcad_active_lr4_vxp"])
    assert vxp_path.name == "HornLab_active_lr4.vxp"
    root = ET.parse(vxp_path).getroot()

    assert [node.findtext("Model") for node in root.findall("DRIVER")] == ["MF", "HF"]
    filenames = [node.findtext("FileName") for node in root.findall(".//RESPONSE")]
    assert "hor/MF -45.frd" in filenames
    assert "ver/HF 90.frd" in filenames

    parts = root.findall("./CROSSOVER/PART")
    filters = [part for part in parts if (part.findtext("Type") or "").startswith("Active")]
    assert sorted(part.findtext("Type") for part in filters) == [
        "Active High pass",
        "Active Low pass",
    ]
    assert {part.findtext("PARAM/Value") for part in filters} == {"1000"}

    buffers = [part for part in parts if part.findtext("Type") == "Buffer"]
    buffer_params = {
        part.findtext("PartID"): {
            param.findtext("Name"): param.findtext("Value")
            for param in part.findall("PARAM")
        }
        for part in buffers
    }
    assert {"A": "1.5", "dt": "0"} in buffer_params.values()
    assert {"A": "-2", "dt": "250"} in buffer_params.values()
    readme = (vxp_path.parent / "README.txt").read_text(encoding="utf-8")
    assert "HornLab_active_lr4.vxp contains the computed active LR4" in readme


def test_vituixcad_export_phase_slope_is_negative_for_delayed_arrival(tmp_path):
    """A later arrival must export measurement-convention falling phase.

    Solver-convention input carries e^{+i omega tau} for a delay; exporting
    its angle unconverted would rise with frequency, and the e^{+jkd} ToF
    factor would then DOUBLE the propagation phase instead of removing it.
    """
    module = _load_script()
    # Dense linear grid so the unwrapped slope is unambiguous.
    freqs = np.linspace(500.0, 3000.0, 201)
    angles = np.array([0.0])
    planes = np.array(["horizontal"], dtype=str)
    distance_m = 2.0
    tof_s = distance_m / module.SPEED_OF_SOUND_M_S
    extra_s = 0.3e-3
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=freqs, angles=angles, planes=planes,
        pressure=_delayed_pressure(
            freqs, planes, angles, arrival_s=tof_s + extra_s
        ),
    )

    payload = module._write_vituixcad_export(
        tmp_path,
        [_synthetic_source_result(mf_npz, "MF", 3)],
        polar_distance_m=distance_m,
    )

    assert payload["status"] == "complete"
    rows = [
        line.split()
        for line in (Path(payload["export_dir"]) / "hor" / "MF 0.frd")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("*")
    ]
    data = np.asarray(rows, dtype=np.float64)
    unwrapped_rad = np.unwrap(np.radians(data[:, 2]))
    assert np.all(np.diff(unwrapped_rad) < 0.0)
    np.testing.assert_allclose(
        unwrapped_rad[-1] - unwrapped_rad[0],
        -2.0 * np.pi * extra_s * (freqs[-1] - freqs[0]),
        rtol=1.0e-6,
    )


def test_vituixcad_export_includes_passive_cardioid_combined_mf(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 2000.0, 10)
    angles = np.array([0.0, 90.0, 180.0])
    planes = np.array(["horizontal"], dtype=str)
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=freqs, angles=angles, planes=planes,
        pressure=_delayed_pressure(freqs, planes, angles, arrival_s=0.0),
    )
    combined_npz = tmp_path / "MF_passive_cardioid_results.npz"
    np.savez_compressed(
        combined_npz,
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=_delayed_pressure(freqs, planes, angles, arrival_s=0.0, amplitude=0.05),
    )

    payload = module._write_vituixcad_export(
        tmp_path,
        [_synthetic_source_result(mf_npz, "MF", 3)],
        polar_distance_m=2.0,
        passive_payload={
            "status": "complete",
            "outputs": {"results_npz": str(combined_npz)},
        },
    )

    assert payload["drivers"] == ["MF", "MF_cardioid"]
    assert (Path(payload["export_dir"]) / "hor" / "MF_cardioid 0.frd").exists()


def test_vituixcad_export_includes_coupled_passive_cardioid_zma(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 2000.0, 10)
    angles = np.array([0.0, 90.0, 180.0])
    planes = np.array(["horizontal"], dtype=str)
    mf_npz = tmp_path / "MF_pressure_basis.npz"
    _write_synthetic_basis(
        mf_npz, name="MF", tag=3, freqs=freqs, angles=angles, planes=planes,
        pressure=_delayed_pressure(freqs, planes, angles, arrival_s=0.0),
    )
    fixed_npz = tmp_path / "MF_passive_cardioid_results.npz"
    coupled_npz = tmp_path / "MF_passive_cardioid_coupled_results.npz"
    np.savez_compressed(
        fixed_npz,
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=_delayed_pressure(
            freqs, planes, angles, arrival_s=0.0, amplitude=0.03
        ),
        phase_convention=np.asarray(module.PRESSURE_NPZ_PHASE_CONVENTION),
    )
    np.savez_compressed(
        coupled_npz,
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=_delayed_pressure(
            freqs, planes, angles, arrival_s=0.0, amplitude=0.05
        ),
        phase_convention=np.asarray(module.PRESSURE_NPZ_PHASE_CONVENTION),
    )
    zma = tmp_path / "MF_passive_cardioid_impedance.zma"
    zma.write_text(
        "* synthetic\n100.0\t5.5\t0.0\n200.0\t6.0\t10.0\n",
        encoding="utf-8",
    )

    payload = module._write_vituixcad_export(
        tmp_path,
        [_synthetic_source_result(mf_npz, "MF", 3)],
        polar_distance_m=2.0,
        passive_payload={
            "status": "complete",
            "outputs": {"results_npz": str(fixed_npz)},
            "coupled": {
                "status": "complete",
                "outputs": {
                    "results_npz": str(coupled_npz),
                    "impedance_zma": str(zma),
                },
            },
        },
    )

    export_dir = Path(payload["export_dir"])
    assert payload["drivers"] == ["MF", "MF_cardioid"]
    assert (export_dir / "MF_passive_cardioid_impedance.zma").exists()
    assert payload["outputs"]["vituixcad_mf_cardioid_zma"].endswith(
        "MF_passive_cardioid_impedance.zma"
    )
    rows = [
        line.split()
        for line in (export_dir / "hor" / "MF_cardioid 0.frd")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("*")
    ]
    data = np.asarray(rows, dtype=np.float64)
    assert data[0, 1] == pytest.approx(20.0 * np.log10(0.05 / module.P_REF), abs=1.0e-4)
    readme = (export_dir / "README.txt").read_text(encoding="utf-8")
    assert "MF_passive_cardioid_impedance.zma" in readme
    assert "No ZMA is exported" not in readme


def test_vituixcad_export_copies_direct_driver_lem_zma_only_during_export(tmp_path):
    module = _load_script()
    freqs = np.geomspace(100.0, 2000.0, 10)
    angles = np.array([0.0, 90.0, 180.0])
    planes = np.array(["horizontal"], dtype=str)
    active_npz = tmp_path / "LF_driver_lem_pressure.npz"
    np.savez_compressed(
        active_npz,
        source_name=np.asarray("LF"),
        source_tag=np.asarray(2, dtype=np.int32),
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=_delayed_pressure(
            freqs,
            planes,
            angles,
            arrival_s=0.0,
            amplitude=0.04,
        ),
        phase_convention=np.asarray(module.PRESSURE_NPZ_PHASE_CONVENTION),
        source_normalization=np.asarray("voltage_driven_driver_lem"),
    )
    zma = tmp_path / "LF_impedance.zma"
    zma.write_text(
        "* synthetic\n100.0\t5.5\t0.0\n200.0\t6.0\t10.0\n",
        encoding="utf-8",
    )
    source_result = {
        "name": "LF",
        "tag": 2,
        "pressure_basis_npz": str(tmp_path / "LF_pressure_basis.npz"),
        "active_pressure_basis_npz": str(active_npz),
        "driver_lem": {
            "status": "complete",
            "outputs": {"impedance_zma": str(zma)},
        },
    }

    assert not (tmp_path / "vituixcad" / "LF_impedance.zma").exists()
    payload = module._write_vituixcad_export(
        tmp_path,
        [source_result],
        polar_distance_m=2.0,
    )

    export_dir = Path(payload["export_dir"])
    assert (export_dir / "LF_impedance.zma").exists()
    assert payload["outputs"]["vituixcad_driver_zmas"]["LF"].endswith(
        "LF_impedance.zma"
    )
    assert "LF_impedance.zma" in (export_dir / "README.txt").read_text(
        encoding="utf-8"
    )


def test_regenerate_driver_recovers_reference_style_solve_command(tmp_path):
    driver = _load_regen_driver()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "tagged_sources.msh").write_text("$MeshFormat\n", encoding="utf-8")
    launch = {
        "command": [
            "/venv/bin/python",
            str(ROOT / "scripts" / "fusion_step_to_wg_pipeline.py"),
            "--step",
            str(run_dir / "model.step"),
            "--out",
            str(run_dir),
            "--run-solves",
        ],
    }
    solve_cmd = [
        "/venv/bin/python",
        str(SCRIPT),
        "--mesh",
        "/old/location/tagged_sources.msh",
        "--out",
        "/old/location",
        "--source",
        "HF:4",
        "--freq-count",
        "8",
        "--dry-run",
    ]
    manifests_dir = run_dir / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "fusion_addin_launch.json").write_text(
        json.dumps(launch),
        encoding="utf-8",
    )
    (manifests_dir / "final_summary_manifest.json").write_text(
        json.dumps({"commands": {"solve": solve_cmd}}),
        encoding="utf-8",
    )

    command, reason = driver._recover_postprocess_command(run_dir)

    assert reason is None
    assert command[0] == sys.executable
    assert command[1] == str(SCRIPT)
    assert "--postprocess-only" in command
    assert "--dry-run" not in command
    assert command[command.index("--out") + 1] == str(run_dir)
    assert command[command.index("--mesh") + 1] == str(run_dir / "tagged_sources.msh")
    assert command[command.index("--source") + 1] == "HF:4"


def test_regenerate_driver_recovers_expanded_solve_mesh_by_basename(tmp_path):
    driver = _load_regen_driver()
    run_dir = tmp_path / "expanded-run"
    run_dir.mkdir()
    (run_dir / "tagged_sources.msh").write_text("$MeshFormat\n", encoding="utf-8")
    expanded_mesh = run_dir / "expanded_4quarter.msh"
    expanded_mesh.write_text("$MeshFormat\n", encoding="utf-8")
    solve_cmd = [
        "/venv/bin/python",
        str(SCRIPT),
        "--mesh",
        "/old/location/expanded_4quarter.msh",
        "--out",
        "/old/location",
        "--source",
        "HF:4",
    ]
    (run_dir / "fusion_wg_pipeline_manifest.json").write_text(
        json.dumps(
            {
                "solve_mesh": "/old/location/expanded_4quarter.msh",
                "commands": {"solve": solve_cmd},
            }
        ),
        encoding="utf-8",
    )

    command, reason = driver._recover_postprocess_command(run_dir)

    assert reason is None
    assert command[command.index("--mesh") + 1] == str(expanded_mesh)
    assert command[command.index("--out") + 1] == str(run_dir)


def test_regenerate_driver_reports_skip_without_recoverable_launch(
    tmp_path,
    capsys,
):
    driver = _load_regen_driver()
    recoverable = tmp_path / "missing-launch-with-manifest"
    recoverable.mkdir()
    (recoverable / "final_summary_manifest.json").write_text(
        json.dumps({"commands": {"solve": ["python", str(SCRIPT)]}}),
        encoding="utf-8",
    )
    unrecoverable = tmp_path / "missing-everything"
    unrecoverable.mkdir()

    rc = driver.main([str(recoverable), str(unrecoverable), "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    # Manifest commands are recoverable without the launch json.
    assert f"DRY-RUN {recoverable}" in captured.out
    assert f"SKIPPED {unrecoverable}" in captured.out
    assert "no recoverable solve_fusion_wg_metal.py command" in captured.out
