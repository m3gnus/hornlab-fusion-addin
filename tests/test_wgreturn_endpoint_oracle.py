"""Old-code add-in contract: exact writer verdicts and canonical bytes."""

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGLink"
ORACLE = ROOT / "tests" / "fixtures" / "wgreturn-endpoint-oracle"
sys.path.insert(0, str(ADDIN))
from wglink_return import WgReturnError, validate_return_manifest  # noqa: E402


def _inputs():
    return {
        "unicode_negative_zero": {"é": -0.0, "x": "雪"},
        "large_integer": {"n": 2**100 + 12345, "z": [0, -0.0]},
        "nested": {"b": [True, None, {"a": "λ"}], "a": 1.25},
        "nan": {"x": float("nan")},
        "infinity": {"x": float("inf")},
    }


def _capture_json(monkeypatch, function, value):
    original = json.dumps
    captured = []

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(json, "dumps", capture)
        try:
            result = function(value)
        except Exception as exc:  # oracle pins the current exception contract
            return {"error_type": type(exc).__name__, "message": str(exc)}
    encoded = result.encode("utf-8")
    return {
        "value": result,
        "utf8_hex": encoded.hex(),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "canonical_bytes_hex": captured[0].encode("utf-8").hex(),
    }


def _fingerprint_from_production_source():
    """Execute only the unchanged function body; WGLink's module needs adsk."""
    path = ADDIN / "WGLink.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_fingerprint_hash"
    )
    namespace = {"json": json, "hashlib": hashlib}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_fingerprint_hash"]


def _addin_functions(monkeypatch):
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []
    adsk.core = types.ModuleType("adsk.core")
    adsk.fusion = types.ModuleType("adsk.fusion")
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", adsk.core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", adsk.fusion)
    monkeypatch.syspath_prepend(str(ADDIN))
    core = importlib.import_module("wglink_core")
    send = importlib.import_module("wglink_send")
    return send._canonical_hash, core._json


@pytest.mark.parametrize("case", json.loads((ORACLE / "cases.json").read_text("utf-8")), ids=lambda row: row["name"])
def test_writer_verdict_and_first_error(case):
    manifest = deepcopy(case["manifest"])
    try:
        validate_return_manifest(manifest)
        actual = {"accepted": True, "message": None}
    except WgReturnError as exc:
        actual = {"accepted": False, "message": str(exc)}
    assert actual == case["expected"]


@pytest.mark.parametrize("golden", json.loads((ORACLE / "goldens.json").read_text("utf-8")), ids=lambda row: row["name"])
def test_canonical_sites_call_production_functions(golden, monkeypatch):
    send_hash, core_json = _addin_functions(monkeypatch)
    fingerprint_hash = _fingerprint_from_production_source()
    value = _inputs()[golden["name"]]
    assert _capture_json(monkeypatch, send_hash, value) == golden["send_hash"]
    assert _capture_json(monkeypatch, core_json, value) == golden["core_json"]
    assert _capture_json(monkeypatch, fingerprint_hash, value) == golden["fingerprint_hash"]
