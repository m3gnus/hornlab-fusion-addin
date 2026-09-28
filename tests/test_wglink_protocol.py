"""Pin the two structural profiles and all canonical JSON byte profiles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import importlib
import json
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADDINS = ROOT / "fusion-addins"
ORACLE = ROOT / "tests/fixtures/wgreturn-endpoint-oracle"
sys.path.insert(0, str(ADDINS / "WGLink"))
import wglink_protocol as protocol  # noqa: E402


@pytest.mark.parametrize("case", json.loads((ORACLE / "cases.json").read_text("utf-8")), ids=lambda row: row["name"])
def test_addin_writer_profile(case):
    try:
        protocol.validate_structure(case["manifest"], protocol.ADDIN_WRITER)
        actual = {"accepted": True, "message": None}
    except protocol.ProtocolValidationError as exc:
        actual = {"accepted": False, "message": str(exc)}
    assert actual == case["expected"]


@pytest.mark.parametrize("case", json.loads((ORACLE / "wg_ingress_cases.json").read_text("utf-8")), ids=lambda row: row["name"])
def test_wg_ingress_profile(case):
    try:
        protocol.validate_structure(case["manifest"], protocol.WG_INGRESS)
        actual = {"accepted": True, "message": None}
    except protocol.ProtocolValidationError as exc:
        actual = {"accepted": False, "message": str(exc)}
    assert actual == case["expected"]


def test_canonical_json_profiles_preserve_bytes_and_nan_policy():
    value = {"é": -0.0, "x": "雪"}
    assert protocol.canonical_json(value, protocol.UTF8_STRICT).encode().hex() == (
        "7b2278223a22e99baa222c22c3a9223a2d302e307d"
    )
    assert protocol.canonical_json(value, protocol.ASCII_STRICT).encode().hex() == (
        "7b2278223a225c7539366561222c225c7530306539223a2d302e307d"
    )
    for profile in (protocol.UTF8_STRICT, protocol.ASCII_STRICT, protocol.ASCII_NORMALIZED):
        with pytest.raises(ValueError, match="Out of range float values"):
            protocol.canonical_json({"x": float("nan")}, profile)
    assert protocol.canonical_json({"x": float("nan")}, protocol.ASCII_NAN_PERMITTED) == '{"x":NaN}'


class Example(Enum):
    ONE = "one"


@dataclass
class Data:
    value: Example


class ArrayLike:
    def tolist(self):
        return [2, 3]


def test_ascii_normalized_matches_geometry_identity_shape():
    value = {1: Data(Example.ONE), "array": ArrayLike()}
    assert protocol.canonical_json(value, protocol.ASCII_NORMALIZED) == (
        '{"1":{"value":"one"},"array":[2,3]}'
    )


def test_both_loose_and_package_import_styles_resolve_same_file(monkeypatch):
    package = types.ModuleType("WGLink")
    package.__path__ = [str(ADDINS / "WGLink")]
    monkeypatch.setitem(sys.modules, "WGLink", package)
    packaged = importlib.import_module("WGLink.wglink_protocol")
    assert Path(packaged.__file__).resolve() == Path(protocol.__file__).resolve()
    assert packaged.source_physical_name(101, "s", None, "HF") == (
        protocol.source_physical_name(101, "s", None, "HF")
    )
