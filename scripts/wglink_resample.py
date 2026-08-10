#!/usr/bin/env python3
"""Resample a new WGLink grid onto topology already materialized in Fusion.

Fusion's embedded Python is intentionally kept out of the interpolation path.
The document stores the original normalized arc positions, and this command
uses the mesher's canonical cubic resampler at exactly those positions so an
update only assigns fit-point coordinates.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import math
from pathlib import Path
import sys
from typing import Any

import numpy as np

try:
    from hornlab_mesher.grid_resample import (
        normalized_arc_positions,
        resample_point_grid,
    )
except (ImportError, ModuleNotFoundError) as exc:
    missing = exc.name or ""
    if missing != "hornlab_mesher" and not missing.startswith("hornlab_mesher."):
        raise
    _MESHER_IMPORT_ERROR: RuntimeError | None = RuntimeError(
        "a compatible hornlab-waveguide-mesher is required to resample WGLink "
        "geometry; install or update the exact revision pinned in this add-in's "
        "requirements.txt"
    )
else:
    _MESHER_IMPORT_ERROR = None


WG_LINK_MODULE_DIR = Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"
sys.path.insert(0, str(WG_LINK_MODULE_DIR))
from wglink_bundle import WgLinkError, read_bundle  # noqa: E402


MAX_CHECK_POINTS = 400


class ResampleError(ValueError):
    """A deterministic refusal emitted as one-line JSON for the add-in."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Make command-line shape errors follow the same JSON failure contract."""

    def error(self, message: str) -> None:
        raise ResampleError(message)


def _reject_json_constant(value: str) -> None:
    raise ResampleError(f"JSON contains non-finite value {value!r}")


def _reject_nonfinite_tree(value: object, *, location: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ResampleError(f"JSON contains non-finite value at {location}: {value!r}")
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_nonfinite_tree(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonfinite_tree(child, location=f"{location}[{index}]")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream, parse_constant=_reject_json_constant)
    except ResampleError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResampleError(f"could not read {label} {str(path)!r}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResampleError(f"{label} must contain a JSON object: {str(path)!r}")
    _reject_nonfinite_tree(payload, location=path.name)
    return payload


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResampleError(f"{name} must be a finite number, got {value!r}")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ResampleError(f"{name} must be finite, got {value!r}") from exc
    if not math.isfinite(number):
        raise ResampleError(f"{name} must be finite, got {value!r}")
    return number


def _topology(payload: dict[str, Any]) -> tuple[int, np.ndarray, float, int, bool]:
    point_count = payload.get("point_count")
    if isinstance(point_count, bool) or not isinstance(point_count, int) or point_count < 3:
        raise ResampleError(f"topology point_count must be at least 3, got {point_count!r}")
    raw_positions = payload.get("section_arc_positions")
    if not isinstance(raw_positions, list) or len(raw_positions) < 2:
        raise ResampleError(
            "topology section_arc_positions must contain at least two values"
        )
    positions = np.asarray(
        [
            _finite_number(value, f"section_arc_positions[{index}]")
            for index, value in enumerate(raw_positions)
        ],
        dtype=float,
    )
    if np.any(np.diff(positions) <= 0.0):
        raise ResampleError("topology section_arc_positions must be strictly increasing")
    if positions[0] < 0.0 or positions[-1] > 1.0:
        raise ResampleError("topology section_arc_positions must lie in [0, 1]")
    if positions[0] != 0.0 or positions[-1] != 1.0:
        raise ResampleError(
            "topology section_arc_positions must include throat 0.0 and mouth 1.0"
        )
    overshoot = _finite_number(payload.get("overshoot_mm"), "topology overshoot_mm")
    if overshoot < 0.0:
        raise ResampleError(f"topology overshoot_mm must not be negative, got {overshoot!r}")
    walls = payload.get("walls")
    if isinstance(walls, bool) or walls not in (1, 2):
        raise ResampleError(f"topology walls must be 1 or 2, got {walls!r}")
    has_outer = payload.get("has_outer")
    if not isinstance(has_outer, bool):
        raise ResampleError(f"topology has_outer must be boolean, got {has_outer!r}")
    if (walls == 2) != has_outer:
        raise ResampleError(
            f"topology walls={walls} conflicts with has_outer={has_outer}"
        )
    return point_count, positions, overshoot, walls, has_outer


def _append_overshoot(points: np.ndarray, overshoot_mm: float) -> np.ndarray:
    if overshoot_mm <= 0.0:
        return points
    mouth = np.array(points[:, -1:, :], copy=True)
    mouth[:, :, 2] += overshoot_mm
    return np.concatenate([points, mouth], axis=1)


def _even_sample(points: np.ndarray, count: int) -> list[list[float]]:
    if len(points) <= count:
        return points.tolist()
    sample_indices = np.linspace(0, len(points) - 1, num=count, dtype=int)
    return points[sample_indices].tolist()


def _check_points(
    raw_points: np.ndarray,
    targets: np.ndarray,
    *,
    maximum: int = MAX_CHECK_POINTS,
) -> list[list[float]]:
    """Sample raw new-grid stations lying strictly between CAD sections."""

    raw_positions = normalized_arc_positions(raw_points)
    station_indices = [
        station
        for station, position in enumerate(raw_positions)
        if any(
            float(left) < float(position) < float(right)
            for left, right in zip(targets, targets[1:])
        )
    ]
    if not station_indices:
        return []
    # Preserve the raw mesher coordinates as ground truth.  Selecting rays
    # first gives even coverage around phi before the final bounded sampling.
    candidates = raw_points[:, station_indices, :].reshape((-1, 3))
    return _even_sample(candidates, maximum)


def build_payload(bundle_path: Path, topology_path: Path) -> dict[str, Any]:
    """Build the deterministic update payload without writing it."""

    if _MESHER_IMPORT_ERROR is not None:
        raise _MESHER_IMPORT_ERROR
    try:
        bundle = read_bundle(bundle_path)
    except WgLinkError as exc:
        raise ResampleError(str(exc)) from exc
    bundle.close()
    topology = _load_json(topology_path, "topology")
    point_count, positions, overshoot, walls, _has_outer = _topology(topology)
    inner = np.asarray(bundle.grid["inner_points"], dtype=float)
    if inner.ndim != 3 or inner.shape[2] != 3:
        raise ResampleError(
            f"new grid inner_points must have shape (n_phi, n_length, 3), got {inner.shape}"
        )
    if inner.shape[0] < 3 or inner.shape[1] < 2:
        raise ResampleError(
            "new grid must contain at least 3 rays and 2 sections, got "
            f"{inner.shape[0]}x{inner.shape[1]}"
        )
    if not np.all(np.isfinite(inner)):
        raise ResampleError("new grid inner_points contains non-finite coordinates")

    try:
        points = resample_point_grid(
            inner,
            point_count=point_count,
            section_arc_positions=positions,
        )
    except ValueError as exc:
        raise ResampleError(f"could not resample inner_points: {exc}") from exc
    outer_points: np.ndarray | None = None
    if walls == 2:
        raw_outer = bundle.grid.get("outer_points")
        if raw_outer is None:
            raise ResampleError("topology requests two walls but new grid has no outer_points")
        outer = np.asarray(raw_outer, dtype=float)
        if outer.shape != inner.shape or not np.all(np.isfinite(outer)):
            raise ResampleError(
                "new grid outer_points must be finite and match inner_points"
            )
        try:
            outer_points = resample_point_grid(
                outer,
                point_count=point_count,
                section_arc_positions=positions,
            )
        except ValueError as exc:
            raise ResampleError(f"could not resample outer_points: {exc}") from exc

    checks = _check_points(inner, positions)
    points = _append_overshoot(points, overshoot)
    if outer_points is not None:
        outer_points = _append_overshoot(outer_points, overshoot)
    ring_z = [float(np.mean(points[:, section, 2])) for section in range(points.shape[1])]
    return {
        "check_points": checks,
        "outer_points": outer_points.tolist() if outer_points is not None else None,
        "overshoot_mm": overshoot,
        "points": points.tolist(),
        "points_per_ring": int(points.shape[0]),
        "ring_z_mm": ring_z,
        "sections": int(points.shape[1]),
        "source_grid": [int(inner.shape[0]), int(inner.shape[1])],
        "throat_z_mm": ring_z[0],
    }


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--topology", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(
                payload,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        raise ResampleError(f"could not write output {str(path)!r}: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, returning nonzero with one JSON error line on failure."""

    try:
        args = _parser().parse_args(argv)
        payload = build_payload(args.bundle, args.topology)
        _write_payload(args.out, payload)
    except Exception as exc:  # noqa: BLE001 - the CLI has one machine-readable boundary
        sys.stdout.write(
            json.dumps({"error": str(exc)}, separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
