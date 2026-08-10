"""Unit coverage for Fusion-side WGLink topology and mouth feature policy."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGLink"
CORE = ADDIN / "wglink_core.py"


@pytest.fixture
def core(monkeypatch):
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []
    adsk_core = types.ModuleType("adsk.core")
    adsk_fusion = types.ModuleType("adsk.fusion")
    adsk.core = adsk_core
    adsk.fusion = adsk_fusion
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", adsk_core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", adsk_fusion)
    monkeypatch.syspath_prepend(str(ADDIN))

    name = "wglink_core_unit_test"
    spec = importlib.util.spec_from_file_location(name, CORE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_selected_sections_contains_exactly_the_planned_real_rings(core):
    grid = {
        "n_phi": 4,
        "inner_points": [
            [[ray, station, station * 10.0] for station in range(4)]
            for ray in range(4)
        ],
    }
    plan = types.SimpleNamespace(phi_stride=2, ring_indices=(0, 2, 3))

    sections = core._selected_sections(grid, plan)

    assert len(sections) == len(plan.ring_indices)
    assert [section[0][2] for section in sections] == [0.0, 20.0, 30.0]
    assert all(len(section) == 2 for section in sections)


def test_mouth_overshoot_parameter_is_created_then_updated_in_place(core):
    class Parameters:
        def __init__(self):
            self.values = {}
            self.added = []

        def itemByName(self, name):
            return self.values.get(name)

        def add(self, name, value, unit, description):
            parameter = types.SimpleNamespace(expression=value)
            self.values[name] = parameter
            self.added.append((name, value, unit, description))
            return parameter

    parameters = Parameters()
    design = types.SimpleNamespace(userParameters=parameters)
    core.adsk.core.ValueInput = types.SimpleNamespace(createByString=lambda value: value)

    name, first = core._push_mouth_overshoot_parameter(design, "wg_tiny_", 5.0)
    original = parameters.values[name]
    _, second = core._push_mouth_overshoot_parameter(design, "wg_tiny_", 7.5)

    assert name == "wg_tiny_mouth_overshoot"
    assert first == {"created": [name], "updated": []}
    assert second == {"created": [], "updated": [name]}
    assert parameters.values[name] is original
    assert original.expression == "7.5 mm"
    assert len(parameters.added) == 1


def _spline(point_count: int):
    return types.SimpleNamespace(fitPoints=types.SimpleNamespace(count=point_count))


def test_rebuild_refuses_pre_face_extrude_synthetic_ring_with_recreate_message(core):
    rings = [
        ("inner", section, object(), _spline(8))
        for section in range(4)
    ]
    interfaces = {
        "throat": (object(), _spline(8)),
        "mouth": (object(), _spline(8)),
    }
    payload = {"sections": 3, "points_per_ring": 8}
    topology = {"walls": 1, "overshoot_mm": 5.0}

    with pytest.raises(core.WgLinkError, match=r"predates.*face-extrude.*Recreate"):
        core._validate_rebuild_topology(rings, interfaces, payload, topology)


def test_rebuild_accepts_one_ring_per_planned_section(core):
    rings = [
        ("inner", section, object(), _spline(8))
        for section in range(3)
    ]
    interfaces = {
        "throat": (object(), _spline(8)),
        "mouth": (object(), _spline(8)),
    }

    assert core._validate_rebuild_topology(
        rings,
        interfaces,
        {"sections": 3, "points_per_ring": 8},
        {"walls": 1, "overshoot_mm": 5.0},
    ) == (3, 8)


def test_rebuild_interfaces_use_the_last_real_section_for_the_mouth(core):
    payload = {
        "sections": 3,
        "points": [
            [[0.0, 0.0, 0.0], [0.0, 0.0, 10.0], [0.0, 0.0, 20.0]],
            [[1.0, 0.0, 0.0], [1.0, 0.0, 10.0], [1.0, 0.0, 20.0]],
        ],
    }

    interfaces = core._rebuild_interface_points(payload)

    assert interfaces["throat"] == [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    assert interfaces["mouth"] == [[0.0, 0.0, 20.0], [1.0, 0.0, 20.0]]


def _face(*, z_internal: float, normal_z: float, reversed_: bool = False):
    plane = types.SimpleNamespace(
        is_plane=True,
        normal=types.SimpleNamespace(x=0.0, y=0.0, z=normal_z),
    )
    point = types.SimpleNamespace(z=z_internal)
    return types.SimpleNamespace(
        geometry=plane,
        boundingBox=types.SimpleNamespace(minPoint=point, maxPoint=point),
        isParamReversed=reversed_,
    )


def test_mouth_cap_face_requires_unique_planar_face_at_mouth_with_plus_z_normal(core):
    core.adsk.core.Plane = types.SimpleNamespace(
        cast=lambda geometry: geometry if getattr(geometry, "is_plane", False) else None
    )
    mouth = _face(z_internal=9.477, normal_z=1.0)
    wrong_normal = _face(z_internal=9.477, normal_z=-1.0)
    wrong_plane = _face(z_internal=8.0, normal_z=1.0)

    assert core._mouth_cap_face(
        types.SimpleNamespace(faces=[wrong_normal, mouth, wrong_plane]), 94.77
    ) is mouth

    with pytest.raises(core.WgLinkError, match=r"exactly one.*found 2.*ambiguous"):
        core._mouth_cap_face(
            types.SimpleNamespace(faces=[mouth, _face(z_internal=9.477, normal_z=1.0)]),
            94.77,
        )


def test_mouth_overshoot_extrude_joins_face_using_parameter_expression(core, monkeypatch):
    face = object()
    body = object()
    result_body = object()
    captured = {}

    class ExtrudeInput:
        def setOneSideExtent(self, extent, direction):
            captured["extent"] = extent
            captured["direction"] = direction

    class Extrudes:
        def createInput(self, profile, operation):
            captured["profile"] = profile
            captured["operation"] = operation
            return ExtrudeInput()

        def add(self, _extrude_input):
            return types.SimpleNamespace(
                name="",
                bodies=types.SimpleNamespace(count=1, item=lambda _index: result_body),
            )

    core.adsk.core.ValueInput = types.SimpleNamespace(
        createByString=lambda expression: ("expression", expression)
    )
    core.adsk.fusion.FeatureOperations = types.SimpleNamespace(
        JoinFeatureOperation="join"
    )
    core.adsk.fusion.DistanceExtentDefinition = types.SimpleNamespace(
        create=lambda value: ("distance", value)
    )
    core.adsk.fusion.ExtentDirections = types.SimpleNamespace(
        PositiveExtentDirection="positive"
    )
    monkeypatch.setattr(core, "_mouth_cap_face", lambda loft_body, z: face)
    monkeypatch.setattr(core, "_stamp_managed", lambda *args: None)
    component = types.SimpleNamespace(
        features=types.SimpleNamespace(extrudeFeatures=Extrudes())
    )

    extended, _feature = core._extrude_mouth_overshoot(
        component,
        body,
        mouth_z_mm=94.77,
        overshoot_parameter="wg_tiny_mouth_overshoot",
        instance_id="instance-1",
    )

    assert extended is result_body
    assert captured == {
        "profile": face,
        "operation": "join",
        "extent": ("distance", ("expression", "wg_tiny_mouth_overshoot")),
        "direction": "positive",
    }
