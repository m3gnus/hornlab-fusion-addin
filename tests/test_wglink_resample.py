"""Verify the external resample handoff preserves Fusion's stored topology."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

import numpy as np
import pytest

from hornlab_mesher.grid_resample import (
    normalized_arc_positions,
    resample_point_grid,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wglink_resample.py"


def _load_script(name: str = "wglink_resample"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _points(n_phi: int = 8, n_length: int = 6) -> list:
    grid = np.empty((n_phi, n_length, 3), dtype=float)
    angles = 2.0 * np.pi * np.arange(n_phi) / n_phi
    stations = np.linspace(0.0, 1.0, n_length) ** 1.7
    for section, station in enumerate(stations):
        radius = 10.0 + 30.0 * station**1.2
        grid[:, section, 0] = radius * np.cos(angles)
        grid[:, section, 1] = 5.0 + 0.8 * radius * np.sin(angles)
        grid[:, section, 2] = 100.0 * station
    return grid.tolist()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_bundle(
    path: Path,
    *,
    n_phi: int = 8,
    n_length: int = 6,
    outer: bool = False,
    nonfinite: bool = False,
) -> tuple[Path, dict]:
    path.mkdir()
    points = _points(n_phi, n_length)
    if nonfinite:
        points[0][1][0] = math.nan
    outer_points = None
    if outer:
        outer_points = np.asarray(points, dtype=float)
        outer_points[..., :2] *= 1.2
        outer_points = outer_points.tolist()
    ring_z = [float(np.mean(np.asarray(points)[:, index, 2])) for index in range(n_length)]
    grid = {
        "build_mode": "freestanding" if outer else "enclosure",
        "closed": True,
        "frame": "link-local",
        "has_outer_points": outer,
        "inner_points": points,
        "n_length": n_length,
        "n_phi": n_phi,
        "outer_points": outer_points,
        "ring_planar": [True] * n_length,
        "ring_z_mm": ring_z,
        "units": "mm",
    }
    grid_bytes = json.dumps(grid, allow_nan=True, sort_keys=True).encode() + b"\n"
    step_bytes = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
    (path / "point-grid.json").write_bytes(grid_bytes)
    (path / "waveguide.step").write_bytes(step_bytes)
    manifest = {
        "coordinate_system": {
            "handedness": "right",
            "length_unit": "mm",
            "matrix_convention": "row-major-local-to-parent",
            "step_from_design": [
                [1, 0, 0, 0],
                [0, 1, 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 1],
            ],
        },
        "design": {
            "build_mode": grid["build_mode"],
            "id": "wgd_resample",
            "lineage_id": "wgl_resample",
        },
        "export": {"id": "wge_resample", "sequence": 4},
        "files": {
            "point-grid.json": {
                "sha256": _digest(grid_bytes),
                "size_bytes": len(grid_bytes),
            },
            "waveguide.step": {
                "sha256": _digest(step_bytes),
                "size_bytes": len(step_bytes),
            },
        },
        "required_features": ["checksummed-files-v1", "link-local-frame-v1"],
        "wglink_version": "1.0",
    }
    (path / "wglink.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path, grid


def _write_topology(
    path: Path,
    *,
    point_count: int = 5,
    positions: list[float] | None = None,
    overshoot: float = 0.0,
    walls: int = 1,
    has_outer: bool = False,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "has_outer": has_outer,
                "overshoot_mm": overshoot,
                "point_count": point_count,
                "section_arc_positions": positions or [0.0, 0.35, 1.0],
                "sections": len(positions or [0.0, 0.35, 1.0]),
                "walls": walls,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def test_payload_shape_counts_and_stored_arc_positions_are_honoured(tmp_path):
    module = _load_script()
    bundle, grid = _write_bundle(tmp_path / "new.wglink")
    topology = _write_topology(tmp_path / "topology.json")

    payload = module.build_payload(bundle, topology)

    points = np.asarray(payload["points"])
    expected = resample_point_grid(
        np.asarray(grid["inner_points"]),
        point_count=5,
        section_arc_positions=np.asarray([0.0, 0.35, 1.0]),
    )
    assert points.shape == (5, 3, 3)
    assert points == pytest.approx(expected)
    assert payload["sections"] == 3
    assert payload["points_per_ring"] == 5
    assert payload["source_grid"] == [8, 6]
    assert payload["ring_z_mm"] == pytest.approx(np.mean(expected[:, :, 2], axis=0))
    assert payload["throat_z_mm"] == pytest.approx(0.0)
    assert payload["outer_points"] is None


def test_topology_section_count_must_match_stored_arc_positions(tmp_path):
    module = _load_script()
    bundle, _grid = _write_bundle(tmp_path / "new.wglink")
    topology = _write_topology(tmp_path / "topology.json")
    stored = json.loads(topology.read_text(encoding="utf-8"))
    stored["sections"] = 4
    topology.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(module.ResampleError, match="sections must equal"):
        module.build_payload(bundle, topology)


def test_positive_overshoot_travels_without_adding_a_section(tmp_path):
    module = _load_script()
    bundle, grid = _write_bundle(tmp_path / "new.wglink")
    topology = _write_topology(tmp_path / "topology.json", overshoot=5.0)

    payload = module.build_payload(bundle, topology)
    points = np.asarray(payload["points"])
    expected = resample_point_grid(
        np.asarray(grid["inner_points"]),
        point_count=5,
        section_arc_positions=np.asarray([0.0, 0.35, 1.0]),
    )

    assert payload["overshoot_mm"] == 5.0
    assert payload["sections"] == 3
    assert points.shape == (5, 3, 3)
    assert points == pytest.approx(expected)


def test_zero_overshoot_does_not_append_section(tmp_path):
    module = _load_script()
    bundle, _grid = _write_bundle(tmp_path / "new.wglink")
    topology = _write_topology(tmp_path / "topology.json", overshoot=0.0)

    payload = module.build_payload(bundle, topology)

    assert payload["sections"] == 3
    assert np.asarray(payload["points"]).shape == (5, 3, 3)


def test_two_wall_payload_resamples_outer_without_adding_overshoot_section(tmp_path):
    module = _load_script()
    bundle, _grid = _write_bundle(tmp_path / "new.wglink", outer=True)
    topology = _write_topology(
        tmp_path / "topology.json",
        overshoot=2.5,
        walls=2,
        has_outer=True,
    )

    payload = module.build_payload(bundle, topology)
    outer = np.asarray(payload["outer_points"])

    assert payload["overshoot_mm"] == 2.5
    assert payload["sections"] == 3
    assert outer.shape == (5, 3, 3)


def test_check_points_are_raw_new_grid_points_strictly_between_sections(tmp_path):
    module = _load_script()
    bundle, grid = _write_bundle(tmp_path / "new.wglink", n_phi=12, n_length=20)
    targets = np.asarray([0.0, 0.28, 0.67, 1.0])
    topology = _write_topology(
        tmp_path / "topology.json", positions=targets.tolist()
    )

    payload = module.build_payload(bundle, topology)
    raw = np.asarray(grid["inner_points"])
    raw_positions = normalized_arc_positions(raw)
    eligible = {
        tuple(point)
        for station, position in enumerate(raw_positions)
        if any(left < position < right for left, right in zip(targets, targets[1:]))
        for point in raw[:, station, :]
    }

    assert 0 < len(payload["check_points"]) <= module.MAX_CHECK_POINTS
    assert all(tuple(point) in eligible for point in payload["check_points"])


def test_nonfinite_new_grid_is_refused_as_one_line_json(tmp_path, capsys):
    module = _load_script()
    bundle, _grid = _write_bundle(tmp_path / "new.wglink", nonfinite=True)
    topology = _write_topology(tmp_path / "topology.json")
    output = tmp_path / "payload.json"

    code = module.main(
        ["--bundle", str(bundle), "--topology", str(topology), "--out", str(output)]
    )

    lines = capsys.readouterr().out.splitlines()
    assert code != 0
    assert len(lines) == 1
    assert "non-finite" in json.loads(lines[0])["error"]
    assert not output.exists()


@pytest.mark.parametrize(("n_phi", "n_length"), [(2, 6), (8, 1)])
def test_too_small_new_grid_is_refused(tmp_path, capsys, n_phi, n_length):
    module = _load_script()
    bundle, _grid = _write_bundle(
        tmp_path / "new.wglink", n_phi=n_phi, n_length=n_length
    )
    topology = _write_topology(tmp_path / "topology.json")

    code = module.main(
        [
            "--bundle",
            str(bundle),
            "--topology",
            str(topology),
            "--out",
            str(tmp_path / "payload.json"),
        ]
    )

    error = json.loads(capsys.readouterr().out)["error"]
    assert code != 0
    assert "at least" in error


def test_missing_mesher_has_actionable_json_error(monkeypatch, tmp_path, capsys):
    original_import = builtins.__import__

    def reject_grid_resample(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hornlab_mesher.grid_resample":
            raise ModuleNotFoundError(
                "No module named 'hornlab_mesher'", name="hornlab_mesher"
            )
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_grid_resample)
    module = _load_script("wglink_resample_missing_mesher")

    code = module.main(
        [
            "--bundle",
            str(tmp_path / "missing.wglink"),
            "--topology",
            str(tmp_path / "missing.json"),
            "--out",
            str(tmp_path / "payload.json"),
        ]
    )

    error = json.loads(capsys.readouterr().out)["error"]
    assert code != 0
    assert "compatible hornlab-waveguide-mesher" in error
    assert "revision pinned" in error
    assert "requirements.txt" in error


def test_main_writes_deterministic_sorted_json_without_timestamp(tmp_path):
    module = _load_script()
    bundle, _grid = _write_bundle(tmp_path / "new.wglink")
    topology = _write_topology(tmp_path / "topology.json")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    common = ["--bundle", str(bundle), "--topology", str(topology), "--out"]

    assert module.main([*common, str(first)]) == 0
    assert module.main([*common, str(second)]) == 0

    text = first.read_text(encoding="utf-8")
    assert text == second.read_text(encoding="utf-8")
    assert "timestamp" not in text
    assert list(json.loads(text)) == sorted(json.loads(text))


def test_invalid_topology_is_a_one_line_json_failure(tmp_path, capsys):
    module = _load_script()
    bundle, _grid = _write_bundle(tmp_path / "new.wglink")
    topology = _write_topology(
        tmp_path / "topology.json", positions=[0.0, 0.5, 0.5, 1.0]
    )

    code = module.main(
        [
            "--bundle",
            str(bundle),
            "--topology",
            str(topology),
            "--out",
            str(tmp_path / "payload.json"),
        ]
    )

    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert len(lines) == 1
    assert "strictly increasing" in json.loads(lines[0])["error"]
