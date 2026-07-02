from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wg_mesh_sizing.py"


def _load():
    spec = importlib.util.spec_from_file_location("wg_mesh_sizing", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


M = _load()


def test_role_size_uses_explicit_mm_knob():
    assert M.role_size_mm(M.ROLE_RADIATING, mm_knob_mm=30.0) == 30.0
    assert M.role_size_mm(M.ROLE_SHADOW, mm_knob_mm=12.0) == 12.0
    assert M.role_size_mm(M.ROLE_THROAT, mm_knob_mm=2.0) == 2.0


def test_valid_f_max_inverts_the_size_formula():
    assert M.valid_f_max_hz(5.7166, epw=6.0) == pytest.approx(10_000.0, rel=1e-3)


def test_graded_size_is_linear_between_dist_bounds():
    assert M.graded_size_mm(0.0, size_min_mm=4.0, size_max_mm=30.0, dist_max_mm=200.0) == 4.0
    assert M.graded_size_mm(200.0, size_min_mm=4.0, size_max_mm=30.0, dist_max_mm=200.0) == 30.0
    assert M.graded_size_mm(100.0, size_min_mm=4.0, size_max_mm=30.0, dist_max_mm=200.0) == pytest.approx(17.0)
    # clamps beyond the transition
    assert M.graded_size_mm(500.0, size_min_mm=4.0, size_max_mm=30.0, dist_max_mm=200.0) == 30.0


def test_triangle_count_uniform_plate():
    # 100x100 mm plate at 5 mm: 2.3 * 10000 / 25 = 920
    n = M.estimate_triangle_count([(10_000.0, 5.0)])
    assert n == 920


def test_triangle_count_sums_per_region_not_global_h():
    # A coarse half and a fine half: per-region sum must beat a single global h
    fine = M.Region(area_mm2=10_000.0, size_mm=5.0, label="radiating")
    coarse = M.Region(area_mm2=10_000.0, size_mm=20.0, label="shadow")
    per_region = M.estimate_triangle_count([fine, coarse])
    # global mean size sqrt would underpredict; per-region = 920 + 57 = 977
    assert per_region == pytest.approx(977, abs=2)


def test_matrix_ram_matches_measured_study_points():
    # 8k tris -> ~1.0 GB, 28178 -> 12.7 GB, 37665 -> 22.7 GB (complex128)
    assert M.matrix_ram_bytes(8000) / 1e9 == pytest.approx(1.024, rel=1e-3)
    assert M.matrix_ram_bytes(28178) / 1e9 == pytest.approx(12.7, rel=2e-2)
    assert M.matrix_ram_bytes(37665) / 1e9 == pytest.approx(22.7, rel=2e-2)


def test_solve_time_calibration_reproduces_anchor_points():
    # The power-law fit must pass through its own calibration anchors.
    assert M.solve_seconds_per_freq(8000) == pytest.approx(1.0, rel=1e-6)
    assert M.solve_seconds_per_freq(28178) == pytest.approx(21.0, rel=1e-6)
    # monotonic and super-linear
    assert M.solve_seconds_per_freq(16000) > 2.0 * M.solve_seconds_per_freq(8000)


def test_feasibility_gate_bands():
    assert M.feasibility_from_ram_gb(0.5) == "ok"
    assert M.feasibility_from_ram_gb(12.0) == "caution"
    assert M.feasibility_from_ram_gb(30.0) == "warn"
    assert M.feasibility_from_ram_gb(45.0) == "infeasible"


def test_estimate_mesh_cost_buckets_by_role():
    regions = [
        M.Region(area_mm2=20_000.0, size_mm=5.72, label="radiating", role="radiating"),
        M.Region(area_mm2=60_000.0, size_mm=13.72, label="shadow", role="shadow"),
    ]
    est = M.estimate_mesh_cost(regions, freq_count=60)
    assert est.n_triangles > 0
    assert set(est.per_role_triangles) == {"radiating", "shadow"}
    # radiating band reaches ~10 kHz from its 5.72 mm size
    assert est.per_role_valid_f_max_hz["radiating"] == pytest.approx(10_000.0, rel=2e-2)
    # shadow band is much lower
    assert est.per_role_valid_f_max_hz["shadow"] < est.per_role_valid_f_max_hz["radiating"]
    assert est.solve_seconds_total == pytest.approx(est.solve_seconds_per_freq * 60, rel=1e-6)
    assert est.ram_bytes == M.matrix_ram_bytes(est.n_triangles)


def test_estimate_mesh_cost_serializes():
    est = M.estimate_mesh_cost([M.Region(10_000.0, 5.0, label="radiating")], freq_count=10)
    d = est.to_dict()
    assert d["n_triangles"] == 920
    assert d["per_role_triangles"] == {"radiating": 920}
    assert "feasibility" in d
