#!/usr/bin/env python3
"""Regenerate writer byte goldens from the exact source commit in PROVENANCE."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ORACLE = ROOT / "tests" / "fixtures" / "wgreturn-endpoint-oracle"
TEST_PATH = ROOT / "tests" / "test_wgreturn_endpoint_oracle.py"
TEST_MODULE_NAME = "_wgreturn_endpoint_oracle_generator_test"
PROVENANCE_PATH = ORACLE / "PROVENANCE.json"


def main() -> None:
    provenance = json.loads(PROVENANCE_PATH.read_text("utf-8"))
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if source_commit != provenance["base"]:
        raise SystemExit(
            "refusing to generate oracle values outside the recorded base commit"
        )

    spec = importlib.util.spec_from_file_location(TEST_MODULE_NAME, TEST_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit("could not load the endpoint oracle test helpers")
    test_module = importlib.util.module_from_spec(spec)
    sys.modules[TEST_MODULE_NAME] = test_module
    spec.loader.exec_module(test_module)

    monkeypatch = pytest.MonkeyPatch()
    try:
        send_hash, core_json = test_module._addin_functions(monkeypatch)
        fingerprint_hash = test_module._fingerprint_from_production_source()
        goldens = []
        for name, value in test_module._inputs().items():
            goldens.append(
                {
                    "name": name,
                    "send_hash": test_module._capture_json(
                        monkeypatch, send_hash, value
                    ),
                    "core_json": test_module._capture_json(
                        monkeypatch, core_json, value
                    ),
                    "fingerprint_hash": test_module._capture_json(
                        monkeypatch, fingerprint_hash, value
                    ),
                }
            )
    finally:
        monkeypatch.undo()

    goldens_path = ORACLE / "goldens.json"
    goldens_path.write_text(
        json.dumps(goldens, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    provenance["generator"] = "scripts/generate_wgreturn_endpoint_oracle.py"
    provenance["sha256"] = {
        name: hashlib.sha256((ORACLE / name).read_bytes()).hexdigest()
        for name in sorted(provenance["sha256"])
    }
    PROVENANCE_PATH.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"generated {len(goldens)} writer byte rows from {source_commit}")


if __name__ == "__main__":
    main()
