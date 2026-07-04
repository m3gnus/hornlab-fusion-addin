from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_run_report.py"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_run_report", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _touch(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"artifact")
    return str(path)


def _write_manifest(run: Path, name: str, payload: dict) -> None:
    path = run / "manifests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_render_run_report_includes_expected_sections_and_relative_paths(tmp_path):
    renderer = _load_renderer()
    run = tmp_path / "260703-120000-demo"
    manifest = {
        "pipeline": "solve_fusion_wg_metal",
        "status": "complete",
        "started_at": "2026-07-03T12:00:00",
        "finished_at": "2026-07-03T12:05:00",
        "layout_version": 2,
        "mesh": str(run / "tagged_sources.msh"),
        "config": {
            "freq_min_hz": 100,
            "freq_max_hz": 1000,
            "freq_count": 4,
            "freq_spacing": "log",
            "crossover": {"lf_mf_hz": None, "mf_hf_hz": 500, "type": "lr4"},
        },
        "sources": [
            {
                "name": "MF",
                "tag": 3,
                "results_json": _touch(run / "sources" / "MF_results.json"),
                "pressure_basis_npz": _touch(run / "sources" / "MF_pressure_basis.npz"),
                "frequency_response_png": _touch(run / "sources" / "MF_frequency_response.png"),
                "directivity_heatmap_png": _touch(run / "sources" / "MF_directivity_heatmap.png"),
            }
        ],
        "outputs": {
            "source_frequency_response_pngs": {
                "MF": str(run / "sources" / "MF_frequency_response.png")
            },
            "source_pressure_basis_npzs": {
                "MF": str(run / "sources" / "MF_pressure_basis.npz")
            },
            "source_results_jsons": {
                "MF": str(run / "sources" / "MF_results.json")
            },
            "source_directivity_heatmap_pngs": {
                "MF": str(run / "sources" / "MF_directivity_heatmap.png")
            },
            "combined_time_aligned_frequency_response_png": _touch(
                run / "combined" / "combined_frequency_response_time_aligned.png"
            ),
            "driver_time_alignment_txt": _touch(
                run / "combined" / "driver_time_alignment.txt"
            ),
            "source_directivity_power_pngs": {
                "MF": _touch(
                    run / "derived" / "MF_directivity_index_power_response.png"
                )
            },
            "source_beamwidth_pngs": {
                "MF": _touch(run / "derived" / "MF_beamwidth.png")
            },
            "source_group_delay_pngs": {
                "MF": _touch(run / "derived" / "MF_group_delay.png")
            },
            "driver_lem_impedance_zmas": {
                "MF": _touch(run / "driver-lem" / "MF_impedance.zma")
            },
            "driver_lem_impedance_pngs": {
                "MF": _touch(run / "driver-lem" / "MF_impedance.png")
            },
            "driver_lem_excursion_pngs": {
                "MF": _touch(run / "driver-lem" / "MF_excursion.png")
            },
            "passive_cardioid_frequency_response_png": _touch(
                run / "cardioid" / "MF_passive_cardioid_frequency_response.png"
            ),
            "passive_cardioid_impedance_png": _touch(
                run / "cardioid" / "MF_passive_cardioid_impedance.png"
            ),
            "passive_cardioid_summary_json": _touch(
                run / "cardioid" / "MF_passive_cardioid_summary.json"
            ),
            "port_exit_radiation_impedance_npz": _touch(
                run / "sources" / "port_exit_radiation_impedance_matrix.npz"
            ),
            "port_exit_radiation_impedance_summary_json": _touch(
                run / "sources" / "port_exit_radiation_impedance_matrix.summary.json"
            ),
            "vituixcad_export_dir": str(run / "vituixcad"),
            "vituixcad_readme_txt": _touch(run / "vituixcad" / "README.txt"),
            "vituixcad_active_lr4_vxp": _touch(
                run / "vituixcad" / "HornLab_active_lr4.vxp"
            ),
            "vituixcad_driver_zmas": {
                "MF": _touch(run / "vituixcad" / "MF_impedance.zma"),
                "MF_cardioid": _touch(
                    run / "vituixcad" / "MF_cardioid_impedance.zma"
                ),
            },
            "vituixcad_mf_cardioid_zma": str(
                run / "vituixcad" / "MF_cardioid_impedance.zma"
            ),
        },
    }
    (run / "logs").mkdir(parents=True)
    _touch(run / "logs" / "solve_fusion_wg_metal.stdout.log")
    _write_manifest(run, "direct_solve_manifest.json", manifest)
    _write_manifest(
        run,
        "final_summary_manifest.json",
        {"status": "complete", "direct_solve": manifest},
    )

    report = renderer.render_run(run)
    html = report.read_text(encoding="utf-8")

    assert "Run Config" in html
    assert "Per-Driver Plots" in html
    assert "Combined / Crossover" in html
    assert "Derived Acoustics" in html
    assert "Radiation Impedance" in html
    assert "Driver LEM" in html
    assert "Passive Cardioid" in html
    assert "VituixCAD" in html
    assert "sources/MF_frequency_response.png" in html
    assert "sources/MF_results.json" in html
    assert "derived/MF_group_delay.png" in html
    assert "driver-lem/MF_impedance.zma" in html
    assert "driver-lem/MF_impedance.png" in html
    assert "cardioid/MF_passive_cardioid_impedance.png" in html
    assert "cardioid/MF_passive_cardioid_summary.json" in html
    assert "sources/port_exit_radiation_impedance_matrix.npz" in html
    assert "vituixcad/HornLab_active_lr4.vxp" in html
    assert "vituixcad/MF_impedance.zma" in html
    assert html.count("vituixcad/MF_cardioid_impedance.zma") == 1
    assert "logs/solve_fusion_wg_metal.stdout.log" in html


def test_render_index_orders_manifest_runs_newest_first_and_tolerates_v1(tmp_path):
    renderer = _load_renderer()
    old_run = tmp_path / "260701-old"
    new_run = tmp_path / "260703-new"
    old_run.mkdir()
    new_run.mkdir()
    (old_run / "direct_solve_manifest.json").write_text(
        json.dumps({"status": "complete", "started_at": "2026-07-01T09:00:00"}),
        encoding="utf-8",
    )
    _write_manifest(
        new_run,
        "direct_solve_manifest.json",
        {
            "status": "failed",
            "started_at": "2026-07-03T09:00:00",
            "layout_version": 2,
        },
    )

    index = renderer.render_index(tmp_path)
    html = index.read_text(encoding="utf-8")

    assert html.index("260703-new") < html.index("260701-old")
    assert "failed" in html
    assert "complete" in html


def test_render_index_links_folder_when_manifest_cannot_render_report(tmp_path):
    renderer = _load_renderer()
    bad_run = tmp_path / "260704-bad"
    bad_run.mkdir()
    (bad_run / "direct_solve_manifest.json").write_text("{not json", encoding="utf-8")

    index = renderer.render_index(tmp_path)
    html = index.read_text(encoding="utf-8")

    assert 'href="260704-bad/"' in html
    assert 'href="260704-bad/report.html"' not in html
