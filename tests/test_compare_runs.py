from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compare_runs.py"


def _load_compare():
    spec = importlib.util.spec_from_file_location("compare_runs", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _write_manifest(run: Path, name: str, payload: dict) -> str:
    return _write_json(run / "manifests" / name, payload)


def _write_basis(path: Path, name: str, gain: float) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    freqs = np.asarray([100.0, 200.0, 400.0])
    angles = np.asarray([0.0, 30.0, 60.0])
    planes = np.asarray(["horizontal", "vertical"])
    pressure = np.ones((freqs.size, planes.size, angles.size), dtype=np.complex128)
    pressure *= gain * 2.0e-5 * np.asarray([10.0, 12.0, 11.0])[:, None, None]
    np.savez_compressed(
        path,
        source_name=np.asarray(name),
        source_tag=np.asarray(4),
        frequencies_hz=freqs,
        observation_angles_deg=angles,
        observation_planes=planes,
        pressure_complex=pressure,
        phase_convention=np.asarray("engineering_exp_plus_jwt"),
    )
    return str(path)


def _derived_payload(offset: float = 0.0) -> dict:
    return {
        "frequencies_hz": [100.0, 200.0, 400.0],
        "directivity_index_db": [3.0 + offset, 4.0 + offset, 5.0 + offset],
        "beamwidth_deg": {
            "horizontal": [120.0 - offset, 100.0 - offset, 80.0 - offset],
            "vertical": [110.0 - offset, 95.0 - offset, 75.0 - offset],
        },
        "group_delay_ms": [1.2 + offset, 1.1 + offset, 1.0 + offset],
    }


def _make_layout1_run(root: Path) -> Path:
    run = root / "260701-flat"
    run.mkdir()
    freqs = [100.0, 200.0, 400.0]
    result = _write_json(
        run / "MF_results.json",
        {"frequencies_hz": freqs, "on_axis_spl_db": [84.0, 86.0, 85.0]},
    )
    directivity = _write_json(
        run / "MF_directivity_index_power_response.json",
        {
            "frequencies_hz": freqs,
            "directivity_index_db": _derived_payload()["directivity_index_db"],
        },
    )
    beamwidth = _write_json(
        run / "MF_beamwidth.json",
        {
            "frequencies_hz": freqs,
            "beamwidth_deg": _derived_payload()["beamwidth_deg"],
        },
    )
    group_delay = _write_json(
        run / "MF_group_delay.json",
        {
            "frequencies_hz": freqs,
            "group_delay_ms": _derived_payload()["group_delay_ms"],
        },
    )
    manifest = {
        "status": "complete",
        "layout_version": 1,
        "config": {
            "freq_count": 3,
            "freq_max_hz": 400,
            "crossover": {"mf_hf_hz": None},
        },
        "sources": [
            {
                "name": "MF",
                "results_json": result,
                "directivity_power_json": directivity,
                "beamwidth_json": beamwidth,
                "group_delay_json": group_delay,
            }
        ],
    }
    _write_json(run / "direct_solve_manifest.json", manifest)
    return run


def _make_layout2_run(root: Path) -> Path:
    run = root / "260702-structured"
    lf_basis = _write_basis(run / "sources" / "LF_pressure_basis.npz", "LF", 1.0)
    hf_basis = _write_basis(run / "sources" / "HF_pressure_basis.npz", "HF", 0.8)
    directivity = _write_json(
        run / "derived" / "combined_time_aligned_directivity_index_power_response.json",
        {
            "frequencies_hz": [100.0, 200.0, 400.0],
            "directivity_index_db": _derived_payload(0.5)["directivity_index_db"],
        },
    )
    beamwidth = _write_json(
        run / "derived" / "combined_time_aligned_beamwidth.json",
        {
            "frequencies_hz": [100.0, 200.0, 400.0],
            "beamwidth_deg": _derived_payload(0.5)["beamwidth_deg"],
        },
    )
    group_delay = _write_json(
        run / "derived" / "combined_time_aligned_group_delay.json",
        {
            "frequencies_hz": [100.0, 200.0, 400.0],
            "group_delay_ms": _derived_payload(0.5)["group_delay_ms"],
        },
    )
    manifest = {
        "status": "complete",
        "layout_version": 2,
        "config": {
            "freq_count": 5,
            "freq_max_hz": 400,
            "crossover": {"mf_hf_hz": 250},
        },
        "sources": [
            {"name": "LF", "pressure_basis_npz": lf_basis},
            {"name": "HF", "pressure_basis_npz": hf_basis},
        ],
        "outputs": {
            "source_pressure_basis_npzs": {"LF": lf_basis, "HF": hf_basis},
            "combined_time_aligned_directivity_power_json": directivity,
            "combined_time_aligned_beamwidth_json": beamwidth,
            "combined_time_aligned_group_delay_json": group_delay,
        },
        "crossover_alignment": {
            "status": "complete",
            "members": ["LF", "HF"],
            "crossovers_hz": [250.0],
            "level_match": {"gains_db": {"LF": 0.0, "HF": -1.5}},
            "delays_ms": {"LF": 0.2, "HF": 0.0},
        },
    }
    _write_manifest(run, "direct_solve_manifest.json", manifest)
    _write_manifest(run, "final_summary_manifest.json", {"direct_solve": manifest})
    return run


def test_compare_runs_writes_html_and_pngs_for_layout1_and_layout2(tmp_path):
    compare = _load_compare()
    run_a = _make_layout1_run(tmp_path)
    run_b = _make_layout2_run(tmp_path)
    out = tmp_path / "compare"

    rc = compare.main(
        [
            str(run_a),
            str(run_b),
            "--out",
            str(out),
            "--plot-theme",
            "hornlab",
            "--name-a",
            "flat",
            "--name-b",
            "structured",
        ]
    )

    assert rc == 0
    html = (out / "ab_compare.html").read_text(encoding="utf-8")
    assert "flat vs structured" in html
    assert "freq_count" in html
    assert "aligned sum" in html
    for name in (
        "on_axis_frequency_response.png",
        "directivity_index.png",
        "beamwidth.png",
        "group_delay.png",
    ):
        data = (out / name).read_bytes()
        assert data.startswith(b"\x89PNG")
