from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ab_gate.py"


def _load_ab_gate():
    spec = importlib.util.spec_from_file_location("ab_gate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(on_axis: list[float], polar: list[list[list[float]]]) -> dict:
    return {
        "frequencies_hz": [100.0, 1000.0, 10000.0],
        "on_axis_spl_db": on_axis,
        "normalized_spl_db": polar,
        "observation_angles_deg": [0.0, 30.0, 90.0],
        "observation_planes": ["horizontal", "vertical"],
    }


BASE_POLAR = [
    [[0.0, -10.0, -40.0], [0.0, -10.0, -40.0]],
    [[0.0, -22.0, -40.0], [0.0, -15.0, -40.0]],
    [[0.0, -10.0, -40.0], [0.0, -10.0, -40.0]],
]


def _shifted_polar(scale: float) -> list[list[list[float]]]:
    polar = np.asarray(BASE_POLAR, dtype=float)
    polar[0, :, :] += 50.0 * scale
    polar[1, 0, 0] += 1.0 * scale
    polar[1, 0, 1] += 6.0 * scale
    polar[1, 0, 2] -= 20.0 * scale
    polar[1, 1, 1] += 3.0 * scale
    polar[2, :, :] += 40.0 * scale
    return polar.tolist()


def _write_run(root: Path, name: str, scale: float) -> None:
    run = root / name
    sources = run / "sources"
    derived = run / "derived"
    sources.mkdir(parents=True)
    derived.mkdir()
    result = _payload(
        [50.0 * scale, 2.0 * scale, 40.0 * scale],
        _shifted_polar(scale),
    )
    (sources / "HF_results.json").write_text(json.dumps(result), encoding="utf-8")
    beamwidth = {
        "frequencies_hz": result["frequencies_hz"],
        "beamwidth_deg": {
            "horizontal": [100.0 - 50.0 * scale, 90.0 - 5.0 * scale, 80.0],
            "vertical": [100.0, 90.0 - 20.0 * scale, 80.0],
        },
        "limited_by_grid": {
            "horizontal": [False, False, False],
            "vertical": [False, bool(scale), False],
        },
    }
    (derived / "HF_beamwidth.json").write_text(
        json.dumps(beamwidth), encoding="utf-8"
    )


def test_metrics_apply_band_polar_and_beamwidth_gates(tmp_path):
    ab_gate = _load_ab_gate()
    _write_run(tmp_path, "arm_a", 0.0)
    _write_run(tmp_path, "arm_b", 1.0)

    a = ab_gate._load_source(tmp_path / "arm_a", "HF")
    b = ab_gate._load_source(tmp_path / "arm_b", "HF")
    metrics = ab_gate.compute_metrics(a, b, (500.0, 2000.0))

    assert metrics["on_axis_db"] == 2.0
    # The 20 dB change at 90 degrees is excluded because both arms are below -20 dB.
    # The 30-degree point remains live because arm B moved to -16 dB.
    assert metrics["polar_horizontal_db"] == 6.0
    assert metrics["polar_vertical_db"] == 3.0
    assert metrics["beamwidth_horizontal_deg"] == 5.0
    assert metrics["beamwidth_vertical_deg"] is None


def test_metrics_refuse_to_silently_drop_a_missing_beamwidth_plane(tmp_path):
    ab_gate = _load_ab_gate()
    _write_run(tmp_path, "arm_a", 0.0)
    _write_run(tmp_path, "arm_b", 1.0)
    beamwidth_path = tmp_path / "arm_b" / "derived" / "HF_beamwidth.json"
    beamwidth = json.loads(beamwidth_path.read_text(encoding="utf-8"))
    del beamwidth["beamwidth_deg"]["vertical"]
    del beamwidth["limited_by_grid"]["vertical"]
    beamwidth_path.write_text(json.dumps(beamwidth), encoding="utf-8")

    a = ab_gate._load_source(tmp_path / "arm_a", "HF")
    b = ab_gate._load_source(tmp_path / "arm_b", "HF")
    with pytest.raises(ValueError, match="beamwidth planes differ"):
        ab_gate.compute_metrics(a, b, (500.0, 2000.0))


def test_floor_uses_largest_perturbation_and_ratios_band_maxima(tmp_path):
    ab_gate = _load_ab_gate()
    for name, scale in (
        ("arm_a", 0.0),
        ("arm_b", 1.0),
        ("floor_minus", 0.25),
        ("floor_plus", 0.5),
    ):
        _write_run(tmp_path, name, scale)

    verdict, failed = ab_gate.analyse_runs(
        {name: tmp_path / name for name in ("arm_a", "arm_b", "floor_minus", "floor_plus")},
        (500.0, 2000.0),
        max_ratio=2.1,
    )

    hf = verdict["sources"]["HF"]
    assert hf["signal"]["on_axis_db"] == 2.0
    assert hf["floor"]["on_axis_db"] == 1.0
    assert hf["ratio"]["on_axis_db"] == 2.0
    assert hf["ratio"]["polar_horizontal_db"] == 2.0
    assert failed is False


def test_ratio_gate_fails_closed_when_a_signal_metric_has_no_floor(tmp_path):
    ab_gate = _load_ab_gate()
    for name, scale in (
        ("arm_a", 0.0),
        ("arm_b", 1.0),
        ("floor_minus", 0.25),
        ("floor_plus", 0.5),
    ):
        _write_run(tmp_path, name, scale)
    for name in ("floor_minus", "floor_plus"):
        path = tmp_path / name / "derived" / "HF_beamwidth.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["limited_by_grid"]["horizontal"][1] = True
        path.write_text(json.dumps(payload), encoding="utf-8")

    verdict, failed = ab_gate.analyse_runs(
        {
            name: tmp_path / name
            for name in ("arm_a", "arm_b", "floor_minus", "floor_plus")
        },
        (500.0, 2000.0),
        max_ratio=1.0e9,
    )

    assert failed is True
    assert {
        "source": "HF",
        "metric": "beamwidth_horizontal_deg",
        "ratio": None,
        "reason": "floor metric is unavailable",
    } in verdict["exceeded"]


def test_max_ratio_controls_exit_and_skip_solves_uses_existing_runs(tmp_path, monkeypatch):
    ab_gate = _load_ab_gate()
    for name, scale in (
        ("arm_a", 0.0),
        ("arm_b", 1.0),
        ("floor_minus", 0.25),
        ("floor_plus", 0.5),
    ):
        _write_run(tmp_path, name, scale)

    calls = []
    monkeypatch.setattr(ab_gate, "_run_command", calls.append)
    common = [
        "a.step",
        "b.step",
        "--out",
        str(tmp_path),
        "--band-hz",
        "500:2000",
        "--floor-refine",
        "WGWALL:8mm:1.25",
        "--skip-solves",
    ]
    assert ab_gate.main([*common, "--max-ratio", "2.1"]) == 0
    assert ab_gate.main([*common, "--max-ratio", "1.9"]) == 1
    assert calls == []
    verdict = json.loads((tmp_path / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["passed"] is False


def test_max_ratio_requires_a_floor_perturbation(tmp_path):
    ab_gate = _load_ab_gate()

    with pytest.raises(SystemExit, match="--max-ratio requires --floor-refine"):
        ab_gate.main(
            [
                "a.step",
                "b.step",
                "--out",
                str(tmp_path),
                "--band-hz",
                "500:2000",
                "--max-ratio",
                "2",
                "--skip-solves",
            ]
        )


def test_floor_refine_rejects_a_non_positive_minus_perturbation():
    ab_gate = _load_ab_gate()

    with pytest.raises(
        argparse.ArgumentTypeError,
        match="below 100",
    ):
        ab_gate.RefinePerturbation.parse("WGWALL:8mm:100")


def test_command_builder_gives_a_and_b_identical_pipeline_flags(tmp_path, monkeypatch):
    ab_gate = _load_ab_gate()
    observed = []
    original = ab_gate._pipeline_command

    def spy(step, out, shared_flags, *, python):
        observed.append(tuple(shared_flags))
        return original(step, out, shared_flags, python=python)

    monkeypatch.setattr(ab_gate, "_pipeline_command", spy)
    refine = ab_gate.RefinePerturbation.parse("WGWALL:8mm:1.25")
    specs = ab_gate.build_commands(
        Path("a.step"),
        Path("b.step"),
        tmp_path,
        ["--sources", "HF:3:4", "--refine", "CABINET:20mm"],
        floor_refine=refine,
        determinism=True,
        python="python",
    )

    assert [spec.name for spec in specs] == [
        "arm_a",
        "arm_b",
        "floor_minus",
        "floor_plus",
        "determinism",
    ]
    assert observed[0] == observed[1] == observed[4]
    assert "--run-solves" in observed[0]
    assert "WGWALL:7.9mm" in observed[2]
    assert "WGWALL:8.1mm" in observed[3]
    for left, right in zip(specs[0].command, specs[1].command):
        if left not in {"a.step", str(tmp_path / "arm_a")}:
            assert left == right
