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


def test_observed_parameters_normalize_lengths_to_mm_and_reject_other_units(core):
    parameter_by_name = {
        "wg_angle": types.SimpleNamespace(expression="30 deg", value=0.5, unit="deg"),
        "wg_horn_length": types.SimpleNamespace(expression="25 mm", value=2.5, unit="mm"),
        "wg_horn_length_cm": types.SimpleNamespace(expression="2.5 cm", value=2.5, unit="cm"),
        "wg_horn_length_in": types.SimpleNamespace(expression="0.984 in", value=2.5, unit="in"),
    }
    parameters = types.SimpleNamespace(
        itemByName=parameter_by_name.get,
    )
    length_scales = {"mm": 1.0, "cm": 10.0, "in": 25.4}

    def convert(value, source_unit, target_unit):
        assert target_unit == "mm"
        if source_unit == "internalUnits":
            return value * 10.0
        return value * length_scales.get(source_unit, -1.0)

    design = types.SimpleNamespace(
        userParameters=parameters,
        unitsManager=types.SimpleNamespace(convert=convert),
    )
    record = {
        "payload": {
            "parameter_expressions": (
                '{"wg_angle": "30 deg", "wg_horn_length": "25 mm", '
                '"wg_horn_length_cm": "2.5 cm", "wg_horn_length_in": "0.984 in"}'
            ),
        },
    }

    assert core._observed_parameters(design, record) == [
        {
            "name": "wg_angle",
            "expected_expression": "30 deg",
            "expression": "30 deg",
            "value": None,
            "unit": None,
        },
        {
            "name": "wg_horn_length",
            "expected_expression": "25 mm",
            "expression": "25 mm",
            "value": 25.0,
            "unit": "mm",
        },
        {
            "name": "wg_horn_length_cm",
            "expected_expression": "2.5 cm",
            "expression": "2.5 cm",
            "value": 25.0,
            "unit": "mm",
        },
        {
            "name": "wg_horn_length_in",
            "expected_expression": "0.984 in",
            "expression": "0.984 in",
            "value": 25.0,
            "unit": "mm",
        },
    ]


def test_body_naming_table_covers_every_insert_body_role(core):
    assert core.BODY_NAMES == {
        "cut_tool": "WGLink waveguide cut tool",
        "enclosure": "WGLink enclosure",
        "throat_patch": "WGLink throat patch body",
        "stitched_waveguide": "WGLink stitched waveguide body",
        "waveguide": "WGLink freestanding waveguide",
        "waveguide_surface": "WGLink waveguide surface",
    }


def test_create_wrapper_clears_ground_to_parent(core):
    occurrence = types.SimpleNamespace(
        component=types.SimpleNamespace(name=""),
        isGroundToParent=True,
    )
    occurrences = types.SimpleNamespace(
        count=0,
        item=lambda _index: None,
        addNewComponent=lambda _transform: occurrence,
    )
    design = types.SimpleNamespace(
        rootComponent=types.SimpleNamespace(occurrences=occurrences)
    )
    core.adsk.core.Matrix3D = types.SimpleNamespace(create=lambda: object())
    warnings = []

    component, created, mode = core._create_wrapper(design, "tiny", {}, warnings)

    assert created is occurrence
    assert component is occurrence.component
    assert component.name == "WGLink_tiny_1"
    assert occurrence.isGroundToParent is False
    assert mode == "occurrence"
    assert warnings == []


def test_create_wrapper_ground_to_parent_refusal_is_a_loud_warning(core):
    class Occurrence:
        component = types.SimpleNamespace(name="")

        @property
        def isGroundToParent(self):
            return True

        @isGroundToParent.setter
        def isGroundToParent(self, _value):
            raise RuntimeError("read only")

    occurrence = Occurrence()
    occurrences = types.SimpleNamespace(
        count=0,
        item=lambda _index: None,
        addNewComponent=lambda _transform: occurrence,
    )
    design = types.SimpleNamespace(
        rootComponent=types.SimpleNamespace(occurrences=occurrences)
    )
    core.adsk.core.Matrix3D = types.SimpleNamespace(create=lambda: object())
    warnings = []

    component, created, mode = core._create_wrapper(design, "tiny", {}, warnings)

    assert (component, created, mode) == (occurrence.component, occurrence, "occurrence")
    assert occurrence.isGroundToParent is True
    assert len(warnings) == 1
    assert warnings[0].startswith("LOUD:")
    assert "Unground From Parent" in warnings[0]


@pytest.mark.parametrize("grounded", [False, True])
def test_audit_reports_ground_to_parent_without_mutating_it(core, monkeypatch, grounded):
    class Occurrence:
        objectType = "adsk::fusion::Occurrence"

        def __init__(self, value):
            self._value = value
            self.writes = 0

        @property
        def isGroundToParent(self):
            return self._value

        @isGroundToParent.setter
        def isGroundToParent(self, value):
            self.writes += 1
            self._value = value

    occurrence = Occurrence(grounded)
    design = types.SimpleNamespace(findEntityByToken=lambda _token: [occurrence])
    record = {
        "instance_id": "instance-1",
        "payload": {
            "bundle_path": "",
            "occurrence_token": "occurrence-1",
            "wrapper": "occurrence",
        },
        "body": None,
    }
    monkeypatch.setattr(core, "_design", lambda _app: design)
    monkeypatch.setattr(core, "_resolve_link", lambda *_args, **_kwargs: record)
    monkeypatch.setattr(core, "_feature_health", lambda _design: ([], []))
    monkeypatch.setattr(
        core, "_link_frame_report", lambda *_args: {"verdict": "in_frame"}
    )
    monkeypatch.setattr(core, "_local_body_state", lambda _record: "unchanged")
    monkeypatch.setattr(core, "_parameter_drift", lambda *_args: [])

    report = core.audit(object())

    assert report["ground_to_parent"] is grounded
    assert occurrence.writes == 0
    remedy = [
        warning
        for warning in report["warnings"]
        if "Unground From Parent" in warning
    ]
    assert bool(remedy) is grounded


def test_link_frame_verdict_accepts_component_local_throat_within_tolerance(core):
    verdict = core.link_frame_verdict(
        [0.004, 79.994],
        0.003,
        80.0,
        0.0,
    )

    assert verdict["verdict"] == "in_frame"
    assert verdict["offset_mm"] == pytest.approx([0.004, -0.006, 0.003])


def test_link_frame_verdict_reports_measured_body_move(core):
    verdict = core.link_frame_verdict(
        [23.26, 258.79],
        0.0,
        80.0,
        0.0,
    )

    assert verdict["verdict"] == "moved"
    assert verdict["expected_center_mm"] == [0.0, 80.0]
    assert verdict["offset_mm"] == pytest.approx([23.26, 178.79, 0.0])
    assert verdict["distance_mm"] == pytest.approx(180.2966, rel=1.0e-5)


def test_link_frame_verdict_checks_stored_throat_plane_independently(core):
    verdict = core.link_frame_verdict([0.0, 80.0], 2.5, 80.0, 0.0)

    assert verdict["verdict"] == "moved"
    assert verdict["offset_mm"] == [0.0, 0.0, 2.5]


def test_link_frame_refusal_names_offset_and_both_remedies_but_force_allows(core):
    frame = core.link_frame_verdict([23.26, 258.79], 0.0, 80.0, 0.0)

    with pytest.raises(core.WgLinkError) as caught:
        core._refuse_bad_link_frame(frame, force=False)

    message = str(caught.value)
    assert "+23.260, +178.790, +0.000" in message
    assert "Undo the body Move" in message
    assert "Detach" in message
    core._refuse_bad_link_frame(frame, force=True)


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
    result_body = types.SimpleNamespace(name="")
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
    assert result_body.name == core.BODY_NAMES["cut_tool"]
    assert captured == {
        "profile": face,
        "operation": "join",
        "extent": ("distance", ("expression", "wg_tiny_mouth_overshoot")),
        "direction": "positive",
    }
