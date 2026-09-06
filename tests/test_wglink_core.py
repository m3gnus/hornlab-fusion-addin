"""Unit coverage for Fusion-side WGLink topology and mouth feature policy."""

from __future__ import annotations

import importlib.util
import json
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


class _UserParameters:
    """Fusion's document-global parameter table, as Insert and Update see it."""

    def __init__(self, existing: dict[str, str] | None = None):
        self.values = {
            name: types.SimpleNamespace(name=name, expression=expression, unit="mm")
            for name, expression in (existing or {}).items()
        }
        self.added: list[str] = []

    @property
    def count(self) -> int:
        return len(self.values)

    def item(self, index):
        return list(self.values.values())[index]

    def itemByName(self, name):
        return self.values.get(name)

    def add(self, name, value, unit, description):
        parameter = types.SimpleNamespace(name=name, expression=value, unit=unit)
        self.values[name] = parameter
        self.added.append(name)
        return parameter


def _horn_bundle(depth_mm: float) -> object:
    """A bundle whose real interface table owns the wg_horn_ namespace."""

    return types.SimpleNamespace(
        manifest={
            "design": {"name": "Horn", "build_mode": "freestanding"},
            "parameters": [
                {
                    "name": "wg_horn_throat_dia",
                    "unit": "mm",
                    "value": 25.4,
                    "role": "interface",
                },
                {
                    "name": "wg_horn_depth",
                    "unit": "mm",
                    "value": depth_mm,
                    "role": "interface",
                },
            ],
        }
    )


def _insert_prefix(core, design, records) -> str:
    """Exactly the namespace decision insert() makes, with real functions."""

    return core.instance_parameter_prefix(
        "horn",
        core._stored_parameter_prefixes(records),
        core._document_parameter_names(design),
    )


def _document(core, existing: dict[str, str]):
    core.adsk.core.ValueInput = types.SimpleNamespace(createByString=lambda value: value)
    parameters = _UserParameters(existing)
    return types.SimpleNamespace(userParameters=parameters), parameters


# Measured against the real allocator and the real parameter push: with no link
# records left, allocation returned "wg_horn_" and the push reassigned the
# surviving wg_horn_depth from "25 mm" to "50.0 mm".
def test_reinsert_after_detach_leaves_the_surviving_parameters_alone(core):
    # Detach deletes link ATTRIBUTES only, so the document keeps wg_horn_* and
    # every feature they drive while no link record mentions them.
    design, parameters = _document(
        core, {"wg_horn_throat_dia": "25.4 mm", "wg_horn_depth": "25 mm"}
    )
    survivor = parameters.values["wg_horn_depth"]

    prefix = _insert_prefix(core, design, {})
    report = core._push_parameters(design, _horn_bundle(50.0), prefix, create_only=True)

    assert prefix == "wg_horn2_"
    assert survivor.expression == "25 mm"
    assert report["updated"] == []
    assert sorted(report["created"]) == ["wg_horn2_depth", "wg_horn2_throat_dia"]


def test_insert_does_not_rewrite_a_user_authored_parameter_in_its_namespace(core):
    # wg_horn_depth here was typed by the user, not minted by any link.
    design, parameters = _document(core, {"wg_horn_depth": "25 mm"})
    authored = parameters.values["wg_horn_depth"]

    prefix = _insert_prefix(core, design, {})
    report = core._push_parameters(design, _horn_bundle(50.0), prefix, create_only=True)

    assert prefix == "wg_horn2_"
    assert authored.expression == "25 mm"
    assert report["updated"] == []


def test_insert_refuses_by_name_when_a_collision_cannot_be_allocated_away(core):
    # A parameter table that cannot be walked hides the collision from
    # allocation, so the create-only push is what keeps the promise. Refusing
    # is the correct outcome; rewriting the user's expression is the defect.
    design, parameters = _document(core, {"wg_horn_depth": "25 mm"})
    design.userParameters = types.SimpleNamespace(
        itemByName=parameters.itemByName, add=parameters.add
    )

    with pytest.raises(core.WgLinkError) as refusal:
        core._push_parameters(design, _horn_bundle(50.0), "wg_horn_", create_only=True)

    message = str(refusal.value)
    assert "wg_horn_depth" in message
    assert "never takes over ones it did not create" in message
    assert parameters.values["wg_horn_depth"].expression == "25 mm"
    assert parameters.added == []


def test_insert_refuses_a_mouth_overshoot_parameter_it_did_not_create(core):
    design, parameters = _document(core, {"wg_horn_mouth_overshoot": "3 mm"})
    design.userParameters = types.SimpleNamespace(
        itemByName=parameters.itemByName, add=parameters.add
    )

    with pytest.raises(core.WgLinkError, match="wg_horn_mouth_overshoot"):
        core._push_mouth_overshoot_parameter(
            design, "wg_horn_", 5.0, create_only=True
        )

    assert parameters.values["wg_horn_mouth_overshoot"].expression == "3 mm"


def test_an_existing_link_keeps_its_namespace_when_a_second_copy_is_inserted(core):
    # One live link owns wg_horn_. Inserting a second copy of the same design
    # takes the next namespace, and the first link's own Update still writes
    # the namespace its record names -- Update never reallocates, because
    # Fusion cannot retarget the datums and user features naming it.
    design, parameters = _document(
        core, {"wg_horn_throat_dia": "25.4 mm", "wg_horn_depth": "25 mm"}
    )
    records = {
        "wgi_one": {"payload": {"slug": "horn", "parameter_prefix": "wg_horn_"}}
    }
    owned = parameters.values["wg_horn_depth"]

    second = _insert_prefix(core, design, records)
    core._push_parameters(design, _horn_bundle(50.0), second, create_only=True)
    first = core._record_parameter_prefix(records["wgi_one"]["payload"])
    replaced = core._push_parameters(design, _horn_bundle(30.0), first)

    assert (first, second) == ("wg_horn_", "wg_horn2_")
    assert owned.expression == "30.0 mm"
    assert parameters.values["wg_horn2_depth"].expression == "50.0 mm"
    assert replaced["created"] == []
    assert sorted(replaced["updated"]) == ["wg_horn_depth", "wg_horn_throat_dia"]


def test_a_link_minted_before_the_stamp_keeps_its_slug_namespace(core):
    assert core._record_parameter_prefix({"slug": "horn"}) == "wg_horn_"
    assert (
        core._record_parameter_prefix(
            {"slug": "horn", "parameter_prefix": "wg_horn2_"}
        )
        == "wg_horn2_"
    )


def test_document_parameter_names_prefer_the_table_that_holds_model_parameters(core):
    # A model parameter occupies a name too: userParameters.add refuses a name
    # a feature already carries, so allocation must see it.
    user = _UserParameters({"wg_horn_depth": "25 mm"})
    every = _UserParameters({"wg_horn_depth": "25 mm", "d17": "4 mm"})
    design = types.SimpleNamespace(allParameters=every, userParameters=user)

    assert sorted(core._document_parameter_names(design)) == ["d17", "wg_horn_depth"]
    assert core._document_parameter_names(
        types.SimpleNamespace(userParameters=user)
    ) == ["wg_horn_depth"]
    assert core._document_parameter_names(types.SimpleNamespace()) == []


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


def test_enclosure_refusal_explains_vertical_offset_recovery(core, monkeypatch):
    monkeypatch.setattr(
        core,
        "enclosure_plan",
        lambda _bundle: types.SimpleNamespace(rectangle_mm=(-172.0, 12.0, 172.0, 359.0)),
    )
    bundle = types.SimpleNamespace(grid={"vertical_offset_mm": 80.0})

    with pytest.raises(core.WgLinkError) as caught:
        core._validate_enclosure_placement(bundle)

    message = str(caught.value)
    assert "enc_y0=12 mm" in message
    assert "80 mm vertical offset" in message
    assert "reduce that offset or increase the bottom enclosure margin" in message


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


def _packaged_runtime_fixture(core, monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    addin = tmp_path / "Fusion AddIns" / "WGLink"
    runtime = tmp_path / "Waveguide Generator" / "wglink-runtime"
    python = tmp_path / "Waveguide Generator" / ".venv" / "bin" / "python"
    addin.mkdir(parents=True)
    (runtime / "scripts").mkdir(parents=True)
    (runtime / "scripts" / "wglink_resample.py").touch()
    python.parent.mkdir(parents=True)
    python.touch()
    (addin / core.PACKAGED_RUNTIME_FILE).write_text(
        json.dumps({"schema": 1, "root": str(runtime), "python": str(python)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "ADDIN_DIR", addin)
    return runtime.resolve(), python.resolve()


def test_packaged_copy_uses_wgs_existing_runtime(core, monkeypatch, tmp_path: Path):
    runtime, python = _packaged_runtime_fixture(core, monkeypatch, tmp_path)

    resolved_root = core._repo_root({})

    assert resolved_root == runtime
    assert core._python_for_resampler(resolved_root, {}) == python


def test_headless_runtime_options_override_packaged_defaults(
    core, monkeypatch, tmp_path: Path
):
    _packaged_runtime_fixture(core, monkeypatch, tmp_path)
    developer = tmp_path / "developer checkout"
    developer_python = developer / "custom-python"
    (developer / "scripts").mkdir(parents=True)
    (developer / "scripts" / "wglink_resample.py").touch()
    developer_python.touch()

    resolved_root = core._repo_root({"repo_root": developer})

    assert resolved_root == developer.resolve()
    assert core._python_for_resampler(
        resolved_root, {"python_path": developer_python}
    ) == developer_python


def test_packaged_copy_does_not_fall_back_to_an_unmanaged_python(
    core, monkeypatch, tmp_path: Path
):
    runtime, python = _packaged_runtime_fixture(core, monkeypatch, tmp_path)
    python.unlink()
    monkeypatch.setattr(core.shutil, "which", lambda _name: "/unmanaged/python3")

    with pytest.raises(core.WgLinkError, match="managed Python interpreter is missing"):
        core._python_for_resampler(runtime, {})


def test_invalid_packaged_runtime_names_the_reinstall_remedy(
    core, monkeypatch, tmp_path: Path
):
    addin = tmp_path / "WGLink"
    addin.mkdir()
    (addin / core.PACKAGED_RUNTIME_FILE).write_text(
        '{"schema": 99, "root": "/tmp/nope", "python": "/tmp/nope"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "ADDIN_DIR", addin)

    with pytest.raises(core.WgLinkError, match="re-run Waveguide Generator"):
        core._repo_root({})


def _stub_no_op_update(core, monkeypatch, record, observed_paths):
    design = types.SimpleNamespace()
    monkeypatch.setattr(core, "_design", lambda _app: design)
    monkeypatch.setattr(core, "_resolve_link", lambda *_args, **_kwargs: record)
    monkeypatch.setattr(
        core, "_link_frame_report", lambda *_args: {"verdict": "in_frame"}
    )
    monkeypatch.setattr(core, "_refuse_bad_link_frame", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        core, "link_state", lambda *_args: types.SimpleNamespace(verdict="same_export")
    )
    monkeypatch.setattr(core, "parameter_slug", lambda _bundle: "horn")
    monkeypatch.setattr(core, "_validate_enclosure_placement", lambda _bundle: None)
    monkeypatch.setattr(core, "_validate_mouth_outline", lambda _bundle: None)
    monkeypatch.setattr(
        core,
        "_resample_payload",
        lambda path, _topology, _options: (observed_paths.append(path), {})[1],
    )
    monkeypatch.setattr(core, "_ring_sketches", lambda *_args: [])
    monkeypatch.setattr(core, "_interface_sketches", lambda *_args: {})
    monkeypatch.setattr(core, "_validate_rebuild_topology", lambda *_args: (0, 0))
    monkeypatch.setattr(
        core,
        "_no_op_update_report",
        lambda _app, _design, _record, bundle, _options, _frame: {
            "updated_export_sequence": bundle.identity.export_sequence,
        },
    )
    return design


def _update_bundle(design_id: str, sequence: int) -> object:
    return types.SimpleNamespace(
        identity=types.SimpleNamespace(
            design_id=design_id,
            export_sequence=sequence,
        ),
        manifest={"design": {"build_mode": "freestanding"}},
        grid={},
    )


def test_update_self_heals_a_missing_bundle_path_by_design_identity(
    core, monkeypatch, tmp_path: Path
) -> None:
    missing = tmp_path / "moved-away.wglink"
    old_match = tmp_path / "old-match.wglink"
    newest_match = tmp_path / "newest-match.wglink"
    other_design = tmp_path / "other-design.wglink"
    for path in (old_match, newest_match, other_design):
        path.mkdir()
    record = {
        "instance_id": "wgi_one",
        "payload": {
            "bundle_path": str(missing),
            "build_mode": "freestanding",
            "design_id": "wgd_target",
            "slug": "horn",
            "topology": "{}",
        },
    }
    observed_paths: list[Path] = []
    design = _stub_no_op_update(core, monkeypatch, record, observed_paths)
    discovered = [
        types.SimpleNamespace(path=old_match, modified_at=300.0),
        types.SimpleNamespace(path=newest_match, modified_at=100.0),
        types.SimpleNamespace(path=other_design, modified_at=500.0),
    ]
    bundles = {
        old_match: _update_bundle("wgd_target", 3),
        newest_match: _update_bundle("wgd_target", 7),
        other_design: _update_bundle("wgd_other", 99),
    }
    monkeypatch.setattr(core.wglink_workspace, "discover_bundles", lambda: discovered)
    monkeypatch.setattr(core, "_read_owned_bundle", lambda path: bundles[Path(path)])
    rewritten: list[tuple[object, str, dict[str, str]]] = []
    monkeypatch.setattr(
        core,
        "_update_payload_attributes",
        lambda target, instance_id, updates: rewritten.append(
            (target, instance_id, updates)
        ),
    )

    report = core.update(object(), None)

    assert report == {"updated_export_sequence": 7}
    assert observed_paths == [newest_match]
    assert rewritten == [
        (design, "wgi_one", {"bundle_path": str(newest_match.resolve())})
    ]


def test_update_refuses_when_design_is_not_discoverable_in_the_wg_workspace(
    core, monkeypatch, tmp_path: Path
) -> None:
    missing = tmp_path / "moved-away.wglink"
    other_design = tmp_path / "other-design.wglink"
    other_design.mkdir()
    record = {
        "instance_id": "wgi_one",
        "payload": {
            "bundle_path": str(missing),
            "build_mode": "freestanding",
            "design_id": "wgd_target",
            "slug": "horn",
            "topology": "{}",
        },
    }
    _stub_no_op_update(core, monkeypatch, record, [])
    monkeypatch.setattr(
        core.wglink_workspace,
        "discover_bundles",
        lambda: [types.SimpleNamespace(path=other_design, modified_at=1.0)],
    )
    monkeypatch.setattr(
        core,
        "_read_owned_bundle",
        lambda _path: _update_bundle("wgd_other", 10),
    )

    with pytest.raises(core.WgLinkError) as refusal:
        core.update(object(), None)

    message = str(refusal.value)
    assert "could not be found in the current WG workspace" in message
    assert "headless wglink_core.relink API" in message


# Measured on 2026-09-06 from design "260308Tritonia-M". The user had changed
# nothing in Fusion, yet WGLink published
# driftedParameters == ["wg_260308tritonia_m_throat_dia"]: the throat is 25.4 mm
# (one inch, with no finite binary representation) and Fusion re-emitted the
# expression it had parsed one unit in the last place away.
TRITONIA_STORED_EXPRESSIONS = {
    "wg_260308tritonia_m_depth": "124.5923644327676 mm",
    "wg_260308tritonia_m_mouth_h": "320.0 mm",
    "wg_260308tritonia_m_mouth_w": "320.0 mm",
    "wg_260308tritonia_m_throat_dia": "25.400000000000006 mm",
    "wg_260308tritonia_m_vertical_offset": "0.0 mm",
    "wg_260308tritonia_m_wall_t": "6.0 mm",
}
TRITONIA_LIVE_EXPRESSIONS = {
    **TRITONIA_STORED_EXPRESSIONS,
    "wg_260308tritonia_m_throat_dia": "25.400000000000009 mm",
}


def _drift_record(stored):
    return {"payload": {"parameter_expressions": json.dumps(stored)}}


def _drift_design(live):
    parameters = {
        name: types.SimpleNamespace(expression=expression)
        for name, expression in live.items()
    }
    return types.SimpleNamespace(
        userParameters=types.SimpleNamespace(itemByName=parameters.get)
    )


def test_untouched_tritonia_throat_is_not_reported_as_drifted(core):
    drift = core._parameter_drift(
        _drift_design(TRITONIA_LIVE_EXPRESSIONS),
        _drift_record(TRITONIA_STORED_EXPRESSIONS),
    )

    assert drift == []


def test_parameter_drift_still_names_an_edited_throat(core):
    live = {
        **TRITONIA_LIVE_EXPRESSIONS,
        # A tenth of a micron on the throat: far finer than any waveguide can
        # be machined, and still reported.
        "wg_260308tritonia_m_throat_dia": "25.4001 mm",
    }

    drift = core._parameter_drift(
        _drift_design(live), _drift_record(TRITONIA_STORED_EXPRESSIONS)
    )

    assert drift == [
        {
            "name": "wg_260308tritonia_m_throat_dia",
            "expected": "25.400000000000006 mm",
            "actual": "25.4001 mm",
        }
    ]


def test_parameter_drift_names_a_deleted_and_a_retyped_parameter(core):
    live = {
        **TRITONIA_LIVE_EXPRESSIONS,
        # Same number, different unit: a hundredfold change of the mouth.
        "wg_260308tritonia_m_mouth_w": "320.0 cm",
    }
    del live["wg_260308tritonia_m_wall_t"]

    drift = core._parameter_drift(
        _drift_design(live), _drift_record(TRITONIA_STORED_EXPRESSIONS)
    )

    assert [row["name"] for row in drift] == [
        "wg_260308tritonia_m_mouth_w",
        "wg_260308tritonia_m_wall_t",
    ]
    assert drift[1]["actual"] is None


def test_parameter_drift_names_a_parameter_it_cannot_read(core):
    class Unreadable:
        @property
        def expression(self):
            raise RuntimeError("Fusion refused")

    design = types.SimpleNamespace(
        userParameters=types.SimpleNamespace(
            itemByName={"wg_x_throat_dia": Unreadable()}.get
        )
    )

    drift = core._parameter_drift(design, _drift_record({"wg_x_throat_dia": "25.4 mm"}))

    assert [row["name"] for row in drift] == ["wg_x_throat_dia"]
    assert "unreadable" in str(drift[0]["actual"])


def test_a_link_name_is_cleaned_but_its_spelling_is_the_users_own(core):
    assert core.normalize_link_name("  Left waveguide  ") == "Left waveguide"
    assert core.normalize_link_name("") == ""
    assert core.normalize_link_name(None) == ""
    # Punctuation, spaces and case all survive: this is a label, not a slug,
    # and nothing downstream turns it into an identifier.
    assert core.normalize_link_name("Tritonia-M (left) · v2") == "Tritonia-M (left) · v2"


def test_a_link_name_refuses_control_characters_and_absurd_length(core):
    with pytest.raises(core.WgLinkError) as newline:
        core.normalize_link_name("left\nright")
    assert "single line" in str(newline.value)

    with pytest.raises(core.WgLinkError) as long:
        core.normalize_link_name("x" * (core.LINK_NAME_LIMIT + 1))
    assert str(core.LINK_NAME_LIMIT) in str(long.value)


def test_display_name_prefers_the_users_label_and_falls_back_to_wgs(core):
    named = {"link_name": "Left waveguide", "design_name": "260308Tritonia-M"}
    assert core.link_display_name(named) == "Left waveguide"

    # A link inserted before the field existed carries no link_name at all,
    # and must read exactly as it always did.
    assert core.link_display_name({"design_name": "260308Tritonia-M"}) == "260308Tritonia-M"
    assert core.link_display_name({"link_name": "", "design_name": "asro68"}) == "asro68"
    assert core.link_display_name({}) == ""
    assert core.link_display_name(None) == ""


def test_setting_a_link_name_touches_the_label_and_nothing_else(core, monkeypatch):
    payload = {
        "design_name": "260308Tritonia-M",
        "design_id": "wgd_one",
        "lineage_id": "wgl_one",
        "parameter_prefix": "wg_260308tritonia_m_",
        "slug": "260308tritonia_m",
        "bundle_path": "/w/260308Tritonia-M.wglink",
    }
    record = {"instance_id": "wgi_one", "payload": payload}
    written: list[dict[str, str]] = []
    monkeypatch.setattr(core, "_design", lambda _app: object())
    monkeypatch.setattr(core, "_resolve_link", lambda *_args, **_kwargs: record)
    monkeypatch.setattr(
        core,
        "_update_payload_attributes",
        lambda _design, _instance, updates: written.append(dict(updates)),
    )

    report = core.set_link_name(object(), "  Left waveguide ")

    # The one attribute a rename may write. The namespace, the ids and the
    # bundle path are what an already-linked document depends on.
    assert written == [{"link_name": "Left waveguide"}]
    assert report["link_name"] == "Left waveguide"
    assert report["display_name"] == "Left waveguide"
    assert report["design_name"] == "260308Tritonia-M"
    assert report["parameter_prefix"] == "wg_260308tritonia_m_"
    assert payload["parameter_prefix"] == "wg_260308tritonia_m_"
    assert payload["design_id"] == "wgd_one"


def test_clearing_a_link_name_returns_the_wg_design_name(core, monkeypatch):
    record = {
        "instance_id": "wgi_one",
        "payload": {"design_name": "asro68", "link_name": "Old label"},
    }
    written: list[dict[str, str]] = []
    monkeypatch.setattr(core, "_design", lambda _app: object())
    monkeypatch.setattr(core, "_resolve_link", lambda *_args, **_kwargs: record)
    monkeypatch.setattr(
        core,
        "_update_payload_attributes",
        lambda _design, _instance, updates: written.append(dict(updates)),
    )

    report = core.set_link_name(object(), "")

    assert written == [{"link_name": ""}]
    assert report["display_name"] == "asro68"


def test_a_link_name_survives_update_because_the_refresh_never_names_it(core):
    # Update merges its refresh into the stored payload, so a key it does not
    # write is preserved. Guard the property rather than the spelling.
    stored = {"link_name": "Left waveguide", "design_name": "old", "slug": "horn"}
    refresh = {"design_name": "renamed", "slug": "horn"}
    assert "link_name" not in refresh
    stored.update(refresh)
    assert stored["link_name"] == "Left waveguide"
    assert core.link_display_name(stored) == "Left waveguide"


def test_the_link_name_is_a_recognised_payload_attribute(core):
    # _link_records only copies whitelisted direct attributes off an entity;
    # a label missing from that set would vanish on every read.
    assert "link_name" in core._PAYLOAD_KEYS
