from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_fem_chamber.py"
SCRIPTS_DIR = str(SCRIPT.parent)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
SPEC = importlib.util.spec_from_file_location("prepare_fem_chamber", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
fem_prepare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fem_prepare
SPEC.loader.exec_module(fem_prepare)


def test_resolve_boundary_groups_prefers_appearance_and_assigns_stable_tags(
    monkeypatch,
):
    monkeypatch.setattr(fem_prepare, "_advanced_face_order", lambda _path: [10, 20, 30])
    monkeypatch.setattr(
        fem_prepare,
        "_parse_styled_face_groups",
        lambda _path: {"FEM_DRIVER": [10], "MF_ENTRY_1": [20]},
    )
    monkeypatch.setattr(
        fem_prepare,
        "_parse_named_shell_faces",
        lambda _path: {"FEM_DRIVER": [30]},
    )
    groups = fem_prepare._resolve_boundary_groups(
        Path("model.step"),
        ["FEM_DRIVER", "MF_ENTRY_1"],
        [101, 102, 103],
    )
    assert [(group.name, group.tag, group.surfaces) for group in groups] == [
        ("FEM_DRIVER", 100, (101,)),
        ("MF_ENTRY_1", 101, (102,)),
    ]


def test_resolve_boundary_groups_rejects_overlapping_interfaces(monkeypatch):
    monkeypatch.setattr(fem_prepare, "_advanced_face_order", lambda _path: [10])
    monkeypatch.setattr(
        fem_prepare,
        "_parse_styled_face_groups",
        lambda _path: {"A": [10], "B": [10]},
    )
    monkeypatch.setattr(fem_prepare, "_parse_named_shell_faces", lambda _path: {})
    with pytest.raises(RuntimeError, match="overlaps"):
        fem_prepare._resolve_boundary_groups(Path("model.step"), ["A", "B"], [101])
