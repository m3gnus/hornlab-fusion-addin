#!/usr/bin/env python3
"""Regenerate Fusion WG Metal derived artifacts without re-solving."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
SOLVER_SCRIPT = REPO_ROOT / "scripts" / "solve_fusion_wg_metal.py"

DERIVED_MARKERS = (
    "driver_time_alignment.txt",
    "combined_frequency_response_time_aligned.png",
    "combined_directivity_heatmap_time_aligned.png",
    "combined_time_aligned_directivity_index_power_response.png",
    "combined_time_aligned_beamwidth.png",
    "combined_time_aligned_group_delay.png",
    "MF_passive_cardioid_results.npz",
    "MF_passive_cardioid_coupled_results.npz",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_keyless_pressure_basis(path: Path) -> bool:
    try:
        with np.load(path, allow_pickle=False) as data:
            return "pressure_complex" in data.files and "phase_convention" not in data.files
    except Exception:
        return False


def _has_keyless_pressure_basis(run_dir: Path) -> bool:
    return any(_is_keyless_pressure_basis(path) for path in run_dir.rglob("*_pressure_basis.npz"))


def _has_derived_artifacts(run_dir: Path) -> bool:
    if any((run_dir / name).exists() for name in DERIVED_MARKERS):
        return True
    for folder in ("combined", "cardioid", "derived"):
        if any((run_dir / folder / name).exists() for name in DERIVED_MARKERS):
            return True
    if (run_dir / "vituixcad").is_dir():
        return True
    return (
        any(run_dir.rglob("*_frequency_response.png"))
        or any(run_dir.rglob("*_directivity_heatmap.png"))
    )


def _sweep_candidates(root: Path) -> list[Path]:
    if not root.exists():
        raise SystemExit(f"root not found: {root}")
    return [
        path
        for path in sorted(root.iterdir())
        if path.is_dir()
        and _has_keyless_pressure_basis(path)
        and _has_derived_artifacts(path)
    ]


def _script_index(command: list[str], script_name: str) -> int | None:
    for index, value in enumerate(command):
        if Path(value).name == script_name:
            return index
    return None


def _command_candidates(run_dir: Path, launch: dict[str, Any]) -> list[list[str]]:
    candidates: list[list[str]] = []
    launch_commands = launch.get("commands")
    if isinstance(launch_commands, dict) and isinstance(launch_commands.get("solve"), list):
        candidates.append([str(item) for item in launch_commands["solve"]])
    if isinstance(launch.get("command"), list):
        candidates.append([str(item) for item in launch["command"]])
    for manifest_name in (
        "final_summary_manifest.json",
        "fusion_wg_pipeline_manifest.json",
    ):
        manifest_path = run_dir / manifest_name
        if not manifest_path.exists():
            continue
        manifest = _read_json(manifest_path)
        commands = manifest.get("commands")
        if isinstance(commands, dict) and isinstance(commands.get("solve"), list):
            candidates.append([str(item) for item in commands["solve"]])
    return candidates


def _strip_runtime_flags(argv: list[str]) -> list[str]:
    stripped: list[str] = []
    for item in argv:
        if item in {"--postprocess-only", "--dry-run"}:
            continue
        stripped.append(item)
    return stripped


def _replace_option_value(argv: list[str], flag: str, value: str) -> list[str]:
    replaced: list[str] = []
    found = False
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == flag:
            replaced.extend([flag, value])
            found = True
            index += 2
            continue
        replaced.append(item)
        index += 1
    if not found:
        replaced.extend([flag, value])
    return replaced


def _option_value(argv: list[str], flag: str) -> str | None:
    for index, item in enumerate(argv[:-1]):
        if item == flag:
            return argv[index + 1]
    return None


def _append_mesh_candidate(candidates: list[str], value: Any) -> None:
    if isinstance(value, str) and value.strip():
        candidates.append(value.strip())


def _recover_mesh_path(run_dir: Path, argv: list[str]) -> Path | None:
    candidates: list[str] = []
    for manifest_name in (
        "fusion_wg_pipeline_manifest.json",
        "final_summary_manifest.json",
        "direct_solve_manifest.json",
    ):
        manifest_path = run_dir / manifest_name
        if not manifest_path.exists():
            continue
        manifest = _read_json(manifest_path)
        _append_mesh_candidate(candidates, manifest.get("solve_mesh"))
        _append_mesh_candidate(candidates, manifest.get("mesh"))
        direct = manifest.get("direct_solve")
        if isinstance(direct, dict):
            _append_mesh_candidate(candidates, direct.get("mesh"))
    _append_mesh_candidate(candidates, _option_value(argv, "--mesh"))

    for raw in candidates:
        path = Path(raw).expanduser()
        if path.name:
            local = run_dir / path.name
            if local.exists():
                return local
        if path.exists():
            return path

    tagged = run_dir / "tagged_sources.msh"
    return tagged if tagged.exists() else None


def _recover_postprocess_command(run_dir: Path) -> tuple[list[str] | None, str | None]:
    # The launch json is the primary record but not required: the summary
    # manifests carry the same commands.solve argv (some early runs have
    # only those).
    launch_path = run_dir / "fusion_addin_launch.json"
    launch = _read_json(launch_path) if launch_path.exists() else {}
    for candidate in _command_candidates(run_dir, launch):
        script_idx = _script_index(candidate, "solve_fusion_wg_metal.py")
        if script_idx is None:
            continue
        script = Path(candidate[script_idx])
        if not script.exists():
            script = SOLVER_SCRIPT
        argv = _strip_runtime_flags(candidate[script_idx + 1:])
        argv = _replace_option_value(argv, "--out", str(run_dir))
        mesh_path = _recover_mesh_path(run_dir, argv)
        if mesh_path is not None:
            argv = _replace_option_value(argv, "--mesh", str(mesh_path))
        argv.append("--postprocess-only")
        return [sys.executable, str(script), *argv], None
    return None, (
        "no recoverable solve_fusion_wg_metal.py command in "
        "fusion_addin_launch.json or the summary manifests"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folders", nargs="*", type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Sweep child run folders with keyless pressure bases and derived artifacts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print recovered postprocess commands without executing them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_folders = [path.expanduser().resolve() for path in args.run_folders]
    if args.root is not None:
        run_folders.extend(_sweep_candidates(args.root.expanduser().resolve()))
    if not run_folders:
        raise SystemExit("provide one or more run folders, or --root")

    summary: list[tuple[str, Path, str]] = []
    for run_dir in run_folders:
        command, reason = _recover_postprocess_command(run_dir)
        if command is None:
            summary.append(("skipped", run_dir, str(reason)))
            print(f"SKIP {run_dir}: {reason}")
            continue
        if args.dry_run:
            summary.append(("dry-run", run_dir, shlex.join(command)))
            print(f"DRY-RUN {run_dir}: {shlex.join(command)}")
            continue
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            summary.append(("success", run_dir, "regenerated"))
            print(f"OK {run_dir}")
        else:
            detail = _failure_detail(run_dir, result)
            summary.append(("failed", run_dir, detail))
            print(f"FAIL {run_dir}: {detail}")

    print("Summary:")
    for status, run_dir, detail in summary:
        print(f"{status.upper()} {run_dir}: {detail}")
    return 1 if any(status == "failed" for status, _run_dir, _detail in summary) else 0


def _failure_detail(run_dir: Path, result: subprocess.CompletedProcess) -> str:
    """One-line cause for a failed postprocess run.

    The solve script records its exception in the direct-solve manifest;
    prefer that over dumping raw output, and fall back to the last stderr
    line so the sweep summary stays scannable.
    """
    manifest_path = run_dir / "direct_solve_manifest.json"
    if manifest_path.exists():
        try:
            error = _read_json(manifest_path).get("error")
        except (OSError, json.JSONDecodeError):
            error = None
        if error:
            return str(error)
    for stream in (result.stderr, result.stdout):
        lines = [line for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return f"returncode {result.returncode}"


if __name__ == "__main__":
    raise SystemExit(main())
