#!/usr/bin/env python3
"""Run and evaluate a controlled STEP-to-solver A/B regression experiment.

Pipeline arguments follow ``--`` so this command can pass them through without
having to duplicate the pipeline's argument parser.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = REPO_ROOT / "scripts" / "fusion_step_to_wg_pipeline.py"
POLAR_GATE_DB = -20.0


@dataclass(frozen=True)
class RunSpec:
    name: str
    step: Path
    out: Path
    command: tuple[str, ...]


@dataclass(frozen=True)
class RefinePerturbation:
    group: str
    size_mm: float
    delta_pct: float

    @classmethod
    def parse(cls, raw: str) -> RefinePerturbation:
        try:
            group, size_text, delta_text = raw.split(":")
            size_mm = float(size_text.removesuffix("mm"))
            delta_pct = float(delta_text.removesuffix("%"))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "expected GROUP:SIZE_MM:DELTA_PCT, for example WGWALL:8mm:1.25"
            ) from exc
        if not group or size_mm <= 0.0 or delta_pct <= 0.0:
            raise argparse.ArgumentTypeError(
                "refine group, size, and percentage must all be positive"
            )
        if delta_pct >= 100.0:
            raise argparse.ArgumentTypeError(
                "refine perturbation percentage must be below 100 so the "
                "minus-floor mesh size stays positive"
            )
        return cls(group=group, size_mm=size_mm, delta_pct=delta_pct)


def _parse_band(raw: str) -> tuple[float, float]:
    try:
        lo, hi = (float(value) for value in raw.split(":"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected LO:HI") from exc
    if lo < 0.0 or hi < lo:
        raise argparse.ArgumentTypeError("band must satisfy 0 <= LO <= HI")
    return lo, hi


def _option_present(arguments: Sequence[str], option: str) -> bool:
    return any(value == option or value.startswith(f"{option}=") for value in arguments)


def _replace_refine(
    arguments: Sequence[str], group: str, size_mm: float
) -> list[str]:
    """Replace one refine group while preserving every unrelated flag and order."""
    replacement = f"{group}:{size_mm:.12g}mm"
    output: list[str] = []
    found = False
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--refine":
            if index + 1 >= len(arguments):
                raise ValueError("--refine requires a value")
            value = arguments[index + 1]
            if value.partition(":")[0].casefold() == group.casefold():
                output.extend((token, replacement))
                found = True
            else:
                output.extend((token, value))
            index += 2
            continue
        if token.startswith("--refine="):
            value = token.split("=", 1)[1]
            if value.partition(":")[0].casefold() == group.casefold():
                output.append(f"--refine={replacement}")
                found = True
            else:
                output.append(token)
            index += 1
            continue
        output.append(token)
        index += 1
    if not found:
        output.extend(("--refine", replacement))
    return output


def _pipeline_command(
    step: Path,
    out: Path,
    shared_flags: Sequence[str],
    *,
    python: str,
) -> tuple[str, ...]:
    return (
        python,
        str(PIPELINE),
        "--step",
        str(step),
        "--out",
        str(out),
        *shared_flags,
    )


def build_commands(
    step_a: Path,
    step_b: Path,
    out: Path,
    shared_flags: Sequence[str],
    *,
    floor_refine: RefinePerturbation | None = None,
    determinism: bool = False,
    python: str = sys.executable,
) -> list[RunSpec]:
    """Build every solve through one path, changing only declared variables."""
    forbidden = ("--step", "--out", "--mesh-only", "--preflight-only")
    found = [option for option in forbidden if _option_present(shared_flags, option)]
    if found:
        raise ValueError(f"gate owns these pipeline options: {', '.join(found)}")
    flags = list(shared_flags)
    if not _option_present(flags, "--run-solves"):
        flags.append("--run-solves")
    if floor_refine is not None:
        flags = _replace_refine(flags, floor_refine.group, floor_refine.size_mm)

    entries: list[tuple[str, Path, list[str]]] = [
        ("arm_a", step_a, flags),
        ("arm_b", step_b, flags),
    ]
    if floor_refine is not None:
        fraction = floor_refine.delta_pct / 100.0
        entries.extend(
            (
                name,
                step_a,
                _replace_refine(flags, floor_refine.group, size),
            )
            for name, size in (
                ("floor_minus", floor_refine.size_mm * (1.0 - fraction)),
                ("floor_plus", floor_refine.size_mm * (1.0 + fraction)),
            )
        )
    if determinism:
        entries.append(("determinism", step_a, flags))
    return [
        RunSpec(
            name=name,
            step=step,
            out=out / name,
            command=_pipeline_command(step, out / name, run_flags, python=python),
        )
        for name, step, run_flags in entries
    ]


def _run_command(spec: RunSpec) -> None:
    print(f"Running {spec.name}: {' '.join(spec.command)}", flush=True)
    subprocess.run(spec.command, cwd=REPO_ROOT, check=True)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return payload


def _load_source(run: Path, source: str) -> dict[str, Any]:
    result_path = run / "sources" / f"{source}_results.json"
    beamwidth_path = run / "derived" / f"{source}_beamwidth.json"
    result = _read_json(result_path)
    beamwidth = _read_json(beamwidth_path)
    frequencies = np.asarray(result["frequencies_hz"], dtype=float)
    beamwidth_frequencies = np.asarray(
        beamwidth.get("frequencies_hz", result["frequencies_hz"]), dtype=float
    )
    if not np.array_equal(frequencies, beamwidth_frequencies):
        raise ValueError(f"frequency grid mismatch within {run} for {source}")
    polar = np.asarray(result["normalized_spl_db"], dtype=float)
    angles = np.asarray(result["observation_angles_deg"], dtype=float)
    planes = [str(value) for value in result.get("observation_planes", [])]
    if not planes:
        if polar.ndim != 3:
            raise ValueError(f"invalid polar array in {result_path}")
        planes = [f"plane_{index}" for index in range(polar.shape[1])]
    if polar.shape != (frequencies.size, len(planes), angles.size):
        raise ValueError(f"polar grid shape mismatch in {result_path}")
    on_axis = np.asarray(result["on_axis_spl_db"], dtype=float)
    if on_axis.shape != frequencies.shape:
        raise ValueError(f"on-axis grid shape mismatch in {result_path}")
    beamwidth_values = {
        str(plane): np.asarray(values, dtype=float)
        for plane, values in beamwidth["beamwidth_deg"].items()
    }
    if set(beamwidth_values) != set(planes):
        raise ValueError(
            f"beamwidth planes do not match polar planes in {beamwidth_path}: "
            f"{sorted(beamwidth_values)} != {sorted(planes)}"
        )
    return {
        "frequencies": frequencies,
        "on_axis": on_axis,
        "polar": polar,
        "angles": angles,
        "planes": planes,
        "beamwidth": beamwidth_values,
        "limited": {
            str(plane): np.asarray(values, dtype=bool)
            for plane, values in beamwidth.get("limited_by_grid", {}).items()
        },
    }


def _same_grid(a: dict[str, Any], b: dict[str, Any], label: str) -> None:
    checks = (
        np.array_equal(a["frequencies"], b["frequencies"]),
        np.array_equal(a["angles"], b["angles"]),
        a["planes"] == b["planes"],
    )
    if not all(checks):
        raise ValueError(f"result grid mismatch for {label}")
    beamwidth_a = set(a["beamwidth"])
    beamwidth_b = set(b["beamwidth"])
    if beamwidth_a != beamwidth_b:
        raise ValueError(f"beamwidth planes differ for {label}")
    if set(a["limited"]) != beamwidth_a or set(b["limited"]) != beamwidth_b:
        raise ValueError(f"beamwidth limited-by-grid planes differ for {label}")


def compute_metrics(
    a: dict[str, Any],
    b: dict[str, Any],
    band_hz: tuple[float, float],
) -> dict[str, float | None]:
    """Return band maxima with polar and beamwidth validity gates applied."""
    _same_grid(a, b, "comparison")
    frequencies = a["frequencies"]
    band = (frequencies >= band_hz[0]) & (frequencies <= band_hz[1])
    if not np.any(band):
        raise ValueError(f"no frequencies fall inside {band_hz[0]}:{band_hz[1]} Hz")

    metrics: dict[str, float | None] = {
        "on_axis_db": float(np.max(np.abs(a["on_axis"][band] - b["on_axis"][band])))
    }
    for plane_index, plane in enumerate(a["planes"]):
        values_a = a["polar"][band, plane_index, :]
        values_b = b["polar"][band, plane_index, :]
        live = (values_a >= POLAR_GATE_DB) | (values_b >= POLAR_GATE_DB)
        delta = np.where(live, np.abs(values_a - values_b), np.nan)
        metrics[f"polar_{plane}_db"] = (
            float(np.nanmax(delta)) if np.any(np.isfinite(delta)) else None
        )

    beamwidth_planes_a = set(a["beamwidth"])
    beamwidth_planes_b = set(b["beamwidth"])
    if beamwidth_planes_a != beamwidth_planes_b:
        raise ValueError(
            "beamwidth planes differ for comparison: "
            f"{sorted(beamwidth_planes_a)} != {sorted(beamwidth_planes_b)}"
        )
    for plane in sorted(beamwidth_planes_a):
        values_a = a["beamwidth"][plane]
        values_b = b["beamwidth"][plane]
        if values_a.shape != frequencies.shape or values_b.shape != frequencies.shape:
            raise ValueError(f"beamwidth grid shape mismatch for {plane}")
        limited_a = a["limited"][plane]
        limited_b = b["limited"][plane]
        if limited_a.shape != frequencies.shape or limited_b.shape != frequencies.shape:
            raise ValueError(f"beamwidth limited-by-grid shape mismatch for {plane}")
        usable = band & ~limited_a & ~limited_b & np.isfinite(values_a) & np.isfinite(values_b)
        delta = np.abs(values_a[usable] - values_b[usable])
        metrics[f"beamwidth_{plane}_deg"] = float(np.max(delta)) if delta.size else None
    return metrics


def _sources_in(run: Path) -> set[str]:
    sources_dir = run / "sources"
    return {
        path.name.removesuffix("_results.json")
        for path in sources_dir.glob("*_results.json")
    }


def _maximum_metrics(items: Sequence[dict[str, float | None]]) -> dict[str, float | None]:
    keys = set().union(*(item.keys() for item in items))
    return {
        key: max(values) if (values := [item[key] for item in items if item.get(key) is not None]) else None
        for key in sorted(keys)
    }


def _metric_ratio(signal: float | None, floor: float | None) -> float | None:
    if signal is None or floor is None:
        return None
    if floor == 0.0:
        return 0.0 if signal == 0.0 else None
    return signal / floor


def analyse_runs(
    run_dirs: dict[str, Path],
    band_hz: tuple[float, float],
    *,
    max_ratio: float | None = None,
) -> tuple[dict[str, Any], bool]:
    required = ["arm_a", "arm_b"]
    floor_names = [name for name in ("floor_minus", "floor_plus") if name in run_dirs]
    determinism = "determinism" in run_dirs
    all_names = required + floor_names + (["determinism"] if determinism else [])
    source_sets = {name: _sources_in(run_dirs[name]) for name in all_names}
    sources = source_sets["arm_a"]
    if not sources:
        raise ValueError("arm_a contains no source result JSONs")
    mismatched = [name for name, found in source_sets.items() if found != sources]
    if mismatched:
        raise ValueError(
            "source sets differ from arm_a in: " + ", ".join(sorted(mismatched))
        )

    verdict: dict[str, Any] = {
        "band_hz": list(band_hz),
        "polar_gate_db": POLAR_GATE_DB,
        "runs": {name: str(path) for name, path in run_dirs.items()},
        "sources": {},
        "max_ratio": max_ratio,
        "exceeded": [],
    }
    failed = False
    for source in sorted(sources):
        loaded = {name: _load_source(run_dirs[name], source) for name in all_names}
        signal = compute_metrics(loaded["arm_a"], loaded["arm_b"], band_hz)
        floor = None
        if floor_names:
            floor = _maximum_metrics(
                [
                    compute_metrics(loaded["arm_a"], loaded[name], band_hz)
                    for name in floor_names
                ]
            )
        ratio = {
            key: _metric_ratio(value, floor.get(key) if floor else None)
            for key, value in signal.items()
        }
        record: dict[str, Any] = {"signal": signal, "floor": floor, "ratio": ratio}
        if determinism:
            record["determinism"] = compute_metrics(
                loaded["arm_a"], loaded["determinism"], band_hz
            )
        verdict["sources"][source] = record
        if max_ratio is not None and floor is not None:
            for metric, signal_value in signal.items():
                floor_value = floor.get(metric)
                if signal_value is None:
                    continue
                if floor_value is None:
                    failed = True
                    verdict["exceeded"].append(
                        {
                            "source": source,
                            "metric": metric,
                            "ratio": None,
                            "reason": "floor metric is unavailable",
                        }
                    )
                    continue
                if signal_value > max_ratio * floor_value:
                    failed = True
                    verdict["exceeded"].append(
                        {"source": source, "metric": metric, "ratio": ratio[metric]}
                    )
    verdict["passed"] = not failed
    return verdict, failed


def _format_value(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def print_verdict(verdict: dict[str, Any]) -> None:
    lo, hi = verdict["band_hz"]
    print(f"\nA/B regression gate ({lo:g}-{hi:g} Hz)")
    print(f"{'source':10} {'metric':30} {'signal':>12} {'floor':>12} {'ratio':>10}")
    for source, record in verdict["sources"].items():
        for metric, signal in record["signal"].items():
            floor = record["floor"].get(metric) if record["floor"] else None
            ratio = record["ratio"][metric]
            ratio_text = "inf" if ratio is None and floor == 0.0 and signal else _format_value(ratio)
            print(
                f"{source:10} {metric:30} {_format_value(signal):>12} "
                f"{_format_value(floor):>12} {ratio_text:>10}"
            )
        if "determinism" in record:
            values = ", ".join(
                f"{metric}={_format_value(value)}"
                for metric, value in record["determinism"].items()
            )
            print(f"  determinism: {values}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("step_a", type=Path, help="STEP file for arm A")
    parser.add_argument("step_b", type=Path, help="STEP file for arm B")
    parser.add_argument("--out", type=Path, required=True, help="gate output directory")
    parser.add_argument("--band-hz", type=_parse_band, required=True, metavar="LO:HI")
    parser.add_argument("--floor-refine", type=RefinePerturbation.parse)
    parser.add_argument("--determinism", action="store_true")
    parser.add_argument("--skip-solves", action="store_true")
    parser.add_argument("--max-ratio", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw_argv:
        separator = raw_argv.index("--")
        gate_argv = raw_argv[:separator]
        pipeline_flags = raw_argv[separator + 1 :]
        args = _parser().parse_args(gate_argv)
    else:
        args, pipeline_flags = _parser().parse_known_args(raw_argv)
    if args.max_ratio is not None and args.max_ratio < 0.0:
        raise SystemExit("--max-ratio must be non-negative")
    if args.max_ratio is not None and args.floor_refine is None:
        raise SystemExit("--max-ratio requires --floor-refine")
    try:
        specs = build_commands(
            args.step_a,
            args.step_b,
            args.out,
            pipeline_flags,
            floor_refine=args.floor_refine,
            determinism=args.determinism,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.skip_solves:
        for spec in specs:
            _run_command(spec)
    run_dirs = {spec.name: spec.out for spec in specs}
    try:
        verdict, failed = analyse_runs(run_dirs, args.band_hz, max_ratio=args.max_ratio)
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not analyse A/B runs: {exc}") from exc
    print_verdict(verdict)
    verdict_path = args.out / "verdict.json"
    verdict_path.write_text(
        json.dumps(verdict, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {verdict_path}")
    return 1 if args.max_ratio is not None and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
