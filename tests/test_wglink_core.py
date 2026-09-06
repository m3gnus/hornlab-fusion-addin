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


def test_a_leftover_helper_is_hidden_by_every_name_fusion_exposes(core):
    """The insert-time half of the STEP export fix.

    Visibility is the only per-body control Fusion's STEP export has, so a
    helper body that survives ``_close_and_thicken`` has to be hidden there or
    it lands in ``assembly.step``. ``BRepBody`` spells the light bulb
    ``isVisible`` on some builds and ``isLightBulbOn`` on others, and the send
    walk reads them in that order, so both are written -- and the read-back, in
    that same order, not the write, decides what is reported.
    """

    both = types.SimpleNamespace(isVisible=True, isLightBulbOn=True)
    assert core._hide_helper_body(both) is True
    assert (both.isVisible, both.isLightBulbOn) == (False, False)

    bulb_only = types.SimpleNamespace(isLightBulbOn=True)
    assert core._hide_helper_body(bulb_only) is True
    assert bulb_only.isLightBulbOn is False


def test_a_helper_fusion_will_not_hide_reports_failure_rather_than_success(core):
    """A refused write must not read as a hidden body.

    If the read-back still says visible, Send is going to refuse this document
    by name, and the insertion report is the only place that can warn while the
    fix is still cheap. Reporting a write that did not take as a success is how
    that warning would go missing.
    """

    class ReadOnlyVisibility:
        name = "WGLink stitched waveguide body"

        @property
        def isVisible(self):
            return True

        @isVisible.setter
        def isVisible(self, _value):
            raise RuntimeError("3 : property is read-only")

    assert core._hide_helper_body(ReadOnlyVisibility()) is False


def test_a_consumed_helper_body_needs_no_hiding(core):
    """A body the stitch consumed is gone, not visible.

    Fusion invalidates the handle rather than deleting the attribute, so the
    question "is it in the file" answers itself, and probing it further is what
    would raise.
    """

    consumed = types.SimpleNamespace(isValid=False)
    assert core._hide_helper_body(consumed) is True
