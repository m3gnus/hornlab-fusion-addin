from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "regenerate_fusion_derived_artifacts.py"
SOLVER_SCRIPT = ROOT / "scripts" / "solve_fusion_wg_metal.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "regenerate_fusion_derived_artifacts",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_strip_runtime_flags_keeps_normal_source_options():
    driver = _load_driver()
    argv = [
        "--mesh",
        "/old/tagged_sources.msh",
        "--out",
        "/old/run",
        "--source",
        "HF:4",
        "--source-freq-max",
        "HF:20000",
        "--source-mesh-valid-hz",
        "HF:16000",
        "--source-aperture-valid-hz",
        "HF:18000",
        "--dry-run",
        "--postprocess-only",
    ]

    assert driver._strip_runtime_flags(argv) == argv[:-2]


def test_recover_hybrid_postprocess_command_uses_completed_manifest_sources(tmp_path):
    driver = _load_driver()
    run_dir = tmp_path / "hybrid-run"
    run_dir.mkdir()
    (run_dir / "tagged_sources.msh").write_text("$MeshFormat\n", encoding="utf-8")
    solve_cmd = [
        "/venv/bin/python",
        str(SOLVER_SCRIPT),
        "--mesh",
        "/old/tagged_sources.msh",
        "--out",
        "/old/run",
        "--fem-chamber-mesh",
        "/old/fem_chamber.msh",
        "--fem-driver-boundary",
        "FEM_DRIVER",
        "--fem-entry",
        "MF_ENTRY_A",
        "--fem-entry",
        "MF_ENTRY_B",
        "--fem-output-source",
        "MF",
        "--fem-output-tag",
        "3",
        "--fem-loss-factor",
        "0.002",
        "--fem-area-tolerance",
        "0.05",
        "--source",
        "MF_ENTRY_A:2",
        "--source",
        "MF_ENTRY_B:4",
        "--source-freq-max",
        "MF_ENTRY_A:12000",
        "--source-mesh-valid-hz",
        "MF_ENTRY_A:10000",
        "--source-aperture-valid-hz",
        "MF_ENTRY_A:11000",
        "--dry-run",
    ]
    manifests_dir = run_dir / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "final_summary_manifest.json").write_text(
        json.dumps({"commands": {"solve": solve_cmd}}),
        encoding="utf-8",
    )

    command, reason = driver._recover_postprocess_command(run_dir)

    assert reason is None
    assert command is not None
    assert command[:2] == [sys.executable, str(SOLVER_SCRIPT)]
    assert "--postprocess-only" in command
    assert not any(item.startswith("--fem-") for item in command)
    assert not any(
        item
        in {
            "--source",
            "--source-freq-max",
            "--source-mesh-valid-hz",
            "--source-aperture-valid-hz",
        }
        for item in command
    )
    assert command[command.index("--mesh") + 1] == str(run_dir / "tagged_sources.msh")
    assert command[command.index("--out") + 1] == str(run_dir)
