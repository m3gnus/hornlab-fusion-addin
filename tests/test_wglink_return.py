"""Pin the Fusion-free return-leg evidence and scope policy."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
from wglink_return import (  # noqa: E402
    BASE_RETURN_FEATURES,
    WgReturnError,
    validate_domain_record,
    build_return_manifest,
    dumps_return_manifest,
    loads_return_manifest,
    plan_export_scope,
    validate_return_manifest,
)


INSTANCE_ID = "0b6a41c2-a17e-45fb-ae8c-bdf0239e581a"
RETURN_ID = "wgr_01J5A8QK3M9T2XVBH0RD7NWE6C"
IDENTITY_MATRIX = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]


def _fingerprint() -> dict:
    return {
        "is_solid": True,
        "volume_mm3": 53546796.0,
        "bbox_mm": [-172.0, -427.0, -185.23, 172.0, 152.0, 94.77],
    }


def _instance(
    *,
    instance_id: str = INSTANCE_ID,
    parameter_prefix: str = "wg_tritonia_",
    matrix: list[list[float]] | None = None,
) -> dict:
    return {
        "instance_id": instance_id,
        "design_id": "wgd_01J4Y2WZQK8Z3TFD3E7V9XKQ4M",
        "lineage_id": "wgl_01J4Y2WZQK8Z3TFD3E7V9XKQ4M",
        "edit_version": 19,
        "design_hash": "sha256:" + "6b" * 32,
        "formula": "osse",
        "config": {
            "root": {"formula": "OSSE"},
            "dimensions": {"length": {"raw": "130", "value": 130.0}},
            "extra_keys": {"Symmetry": "1234", "Tag": "preserved WG input"},
        },
        "export_id": "wge_01J4Y2ZDK8Z3TFD3E7V9XKQ4M",
        "export_sequence": 7,
        "geometry_hash": "sha256:" + "96" * 32,
        "origin_bundle_id": "wgb_01J4Y2ZFK8Z3TFD3E7V9XKQ4M",
        "build_mode": "enclosure",
        "parameter_prefix": parameter_prefix,
        "occurrence_path": f"Tritonia speaker/{parameter_prefix}",
        "assembly_from_link": matrix or deepcopy(IDENTITY_MATRIX),
        "chirality": "original",
        "body_evidence": {
            "local_body_state": "unmodified",
            "baseline_fingerprint": _fingerprint(),
            "observed_fingerprint": _fingerprint(),
            "observed_at": "2026-08-12T09:14:03Z",
        },
        "source_contract": {
            "role": "HF",
            "throat_z_mm": 0.0,
            "throat_plane_link": {
                "origin_mm": [0.0, 0.0, 0.0],
                "normal": [0.0, 0.0, 1.0],
            },
            "axis_link": {
                "origin_mm": [0.0, 0.0, 0.0],
                "direction": [0.0, 0.0, 1.0],
            },
            "throat_diameter_mm": 25.4,
            "expected_disc_area_mm2": 506.707,
        },
    }


def _source(
    *,
    source_id: str = "source-hf",
    instance_id: str | None = INSTANCE_ID,
    channel_id: str = "drive-hf",
) -> dict:
    selectors = {"appearance_labels": ["HF"]}
    if instance_id is not None:
        selectors["linked_throat"] = {"instance_id": instance_id}
    return {
        "id": source_id,
        "role": "HF",
        "instance_id": instance_id,
        "required": True,
        "default_drive_channel_id": channel_id,
        "patch_policy": "single-connected",
        "expected_connected_components": 1,
        "selectors": selectors,
        "observed": {
            "face_count": 1,
            "total_area_mm2": 506.696,
            "per_face_area_mm2": [506.696],
            "bodies": ["speaker"],
        },
        "suggested_resolution_mm": 4.0,
    }


def _worked_example() -> dict:
    return build_return_manifest(
        return_record={
            "id": RETURN_ID,
            "created_at": "2026-08-12T09:14:03Z",
        },
        generator={
            "adapter": "hornlab-fusion-addin/WGLink",
            "adapter_version": "1.0.0",
            "cad_app": "fusion360",
            "cad_version": "2704.1.53",
        },
        document={"name": "Tritonia speaker", "native_id": None},
        coordinate_system={
            "length_unit": "mm",
            "handedness": "right",
            "matrix_convention": "row-major-local-to-parent",
            "solver_anchor_instance_id": INSTANCE_ID,
        },
        assembly={
            "file": "assembly.step",
            "n_bodies_expected": 1,
            "bbox_mm": [
                [-172.0, -427.0, -185.23],
                [172.0, 152.0, 94.77],
            ],
        },
        files={
            "assembly.step": {
                "sha256": "sha256:" + "98" * 32,
                "size_bytes": 6221931,
                "media_type": "model/step",
                "purpose": "exterior-assembly",
            }
        },
        scope={
            "selection": "root",
            "included": [
                {
                    "object_id": "body-0001",
                    "name": "speaker",
                    "component": "wg_tritonia",
                    "body_kind": "solid",
                    "visible": True,
                    "external_reference": "none",
                    "wglink_instance_id": INSTANCE_ID,
                }
            ],
            "skipped": [
                {
                    "object_id": "body-0002",
                    "name": "jig_left",
                    "kind": "hidden_body",
                    "reason": "hidden bodies are excluded by policy",
                    "severity": "degraded",
                },
                {
                    "object_id": "construction-1",
                    "kind": "construction",
                    "count": 14,
                    "reason": "construction entities have no STEP representation",
                    "severity": "info",
                },
            ],
            "fem_air_volumes": [],
            "status": "degraded",
        },
        instances=[_instance()],
        sources=[
            _source(),
            {
                "id": "source-lf",
                "role": "LF",
                "instance_id": None,
                "required": True,
                "default_drive_channel_id": "drive-lf",
                "patch_policy": "explicit-disconnected",
                "expected_connected_components": 2,
                "selectors": {"appearance_labels": ["LF"]},
                "observed": {
                    "face_count": 2,
                    "total_area_mm2": 39771.88,
                    "per_face_area_mm2": [19885.94, 19885.94],
                    "bodies": ["speaker"],
                },
                "suggested_resolution_mm": 30.0,
            },
        ],
    )


def test_return_module_imports_only_the_standard_library():
    source = ROOT / "fusion-addins" / "WGLink" / "wglink_return.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])

    assert imported <= sys.stdlib_module_names | {"__future__"}


def test_surface_fingerprint_accepts_the_observer_null_volume():
    manifest = _worked_example()
    evidence = manifest["instances"][0]["body_evidence"]
    surface = {
        "is_solid": False,
        "volume_mm3": None,
        "bbox_mm": [-1.0, -2.0, 0.0, 1.0, 2.0, 0.0],
    }
    evidence["baseline_fingerprint"] = surface
    evidence["observed_fingerprint"] = surface

    validate_return_manifest(manifest)


def test_explicit_body_exclusion_is_a_recorded_degradation():
    plan = plan_export_scope(
        "root",
        [
            {
                "kind": "body",
                "body_kind": "solid",
                "object_id": "body-jig",
                "name": "measurement jig",
                "component": "Speaker",
                "visible": False,
                "declaration": "exclude",
            }
        ],
    )

    assert plan.status == "degraded"
    assert plan.skipped[0]["kind"] == "excluded_body"


def test_visible_explicit_body_exclusion_refuses_with_visibility_recovery():
    plan = plan_export_scope(
        "root",
        [
            {
                "kind": "body",
                "body_kind": "solid",
                "object_id": "body-jig",
                "name": "measurement jig",
                "component": "Speaker",
                "visible": True,
                "declaration": "exclude",
            }
        ],
    )

    with pytest.raises(
        WgReturnError,
        match="measurement jig.*still visible.*hide the body.*clear the 'exclude' declaration",
    ) as exc:
        plan.manifest_scope()
    assert exc.value.reasons[0]["decision"] == "refuse"


def test_section_2_6_worked_example_round_trips_exactly():
    manifest = _worked_example()

    encoded = dumps_return_manifest(manifest)

    assert encoded.endswith("\n")
    assert loads_return_manifest(encoded) == manifest


@dataclass
class Candidate:
    object_id: str
    name: str
    body_kind: str
    visible: bool
    component: str = "speaker"
    kind: str = "body"
    suppressed: bool = False
    external_reference: str = "none"
    declaration: str | None = None
    wglink_managed: bool = False
    wglink_role: str | None = None


def test_s1_suppressed_object_skips_degraded_with_reason():
    plan = plan_export_scope(
        "root",
        [Candidate("suppressed", "suppressed", "solid", True, suppressed=True)],
    )

    assert plan.status == "degraded"
    assert plan.skipped[0]["kind"] == "suppressed"
    assert "no evaluated geometry" in plan.skipped[0]["reason"]


def test_s2_unresolved_external_link_refuses_and_names_fix():
    plan = plan_export_scope(
        "root",
        [
            Candidate(
                "external",
                "Supplier cabinet",
                "solid",
                True,
                external_reference="unresolved",
            )
        ],
    )

    with pytest.raises(WgReturnError, match="Supplier cabinet.*resolve or remove") as exc:
        plan.manifest_scope()
    assert exc.value.reasons[0]["decision"] == "refuse"


def test_s3_stale_external_link_includes_degraded_with_reason():
    plan = plan_export_scope(
        "root",
        [
            Candidate(
                "external",
                "Supplier cabinet",
                "solid",
                True,
                external_reference="resolved-stale",
            )
        ],
    )

    assert plan.status == "degraded"
    assert plan.included[0]["external_reference"] == "resolved-stale"
    assert "stale" in plan.included[0]["reason"]


def test_s4_current_external_link_includes_clean_with_reason():
    plan = plan_export_scope(
        "root",
        [
            Candidate(
                "external",
                "Supplier cabinet",
                "solid",
                True,
                external_reference="resolved-current",
            )
        ],
    )

    assert plan.status == "clean"
    assert plan.included[0]["external_reference"] == "resolved-current"
    assert "current and included" in plan.included[0]["reason"]


def test_s5_mesh_body_skips_degraded_with_reason():
    plan = plan_export_scope(
        "root",
        [Candidate("mesh", "scan", "mesh", True)],
    )

    assert plan.skipped[0]["severity"] == "degraded"
    assert "not authoritative B-rep" in plan.skipped[0]["reason"]


def test_s6_construction_entities_aggregate_with_informational_reason():
    plan = plan_export_scope(
        "root",
        [
            {"kind": "construction", "object_id": "axes", "count": 4},
            {"kind": "construction", "object_id": "sketches", "count": 10},
        ],
    )

    assert plan.status == "clean"
    assert len(plan.skipped) == 1
    assert plan.skipped[0]["count"] == 14
    assert "no STEP representation" in plan.skipped[0]["reason"]


def test_s7_fem_air_volume_records_separate_one_solid_member_with_reason():
    plan = plan_export_scope(
        "root",
        [
            {
                "kind": "fem_air_volume",
                "object_id": "fem-mf",
                "name": "mf-air",
                "file": "fem/mf-air.step",
                "solid_count": 1,
            }
        ],
    )

    assert plan.fem_air_volumes[0]["n_bodies_expected"] == 1
    assert "separate one-solid member" in plan.fem_air_volumes[0]["reason"]


def test_s7_fem_air_volume_refuses_more_than_one_solid_and_names_fix():
    plan = plan_export_scope(
        "root",
        [
            {
                "kind": "fem_air_volume",
                "object_id": "fem-mf",
                "name": "mf-air",
                "file": "fem/mf-air.step",
                "solid_count": 2,
            }
        ],
    )

    with pytest.raises(WgReturnError, match="mf-air.*exactly one solid"):
        plan.manifest_scope()


def test_s8_wglink_helper_skips_informational_with_reason():
    plan = plan_export_scope(
        "root",
        [
            Candidate(
                "helper",
                "cut tool",
                "solid",
                True,
                wglink_managed=True,
                wglink_role="cut-tool",
            )
        ],
    )

    assert plan.status == "clean"
    assert plan.skipped[0]["kind"] == "wglink_helper"
    assert "final body carries the solve exterior" in plan.skipped[0]["reason"]


def test_s9_hidden_brep_body_skips_degraded_with_ratified_reason():
    plan = plan_export_scope(
        "root",
        [Candidate("hidden", "jig_left", "solid", False)],
    )

    assert plan.status == "degraded"
    assert plan.skipped[0]["kind"] == "hidden_body"
    assert plan.skipped[0]["reason"] == "hidden bodies are excluded by policy"


def test_s10_visible_brep_solid_includes_with_reason():
    plan = plan_export_scope(
        "root",
        [Candidate("cabinet", "speaker", "solid", True)],
    )

    assert plan.included[0]["body_kind"] == "solid"
    assert "included in the exterior assembly" in plan.included[0]["reason"]


@pytest.mark.parametrize(
    "candidate, phrase",
    [
        (
            Candidate(
                "managed-shell",
                "waveguide",
                "surface",
                True,
                wglink_managed=True,
                wglink_role="waveguide",
            ),
            "WGLink-managed surface",
        ),
        (
            Candidate(
                "declared-shell",
                "cabinet shell",
                "surface",
                True,
                declaration="exterior-shell",
            ),
            "declared as an exterior shell",
        ),
    ],
)
def test_s11_declared_or_managed_surface_includes_with_reason(candidate, phrase):
    plan = plan_export_scope("root", [candidate])

    assert plan.included[0]["body_kind"] == "surface"
    assert phrase in plan.included[0]["reason"]


def test_s12_unclassified_visible_surface_refuses_with_one_declaration_remedy():
    plan = plan_export_scope(
        "root",
        [Candidate("helper-shell", "mystery helper", "surface", True)],
    )

    with pytest.raises(
        WgReturnError,
        match="mystery helper.*mark it 'exterior-shell' or exclude it",
    ) as exc:
        plan.manifest_scope()
    assert exc.value.reasons[0]["decision"] == "refuse"


@pytest.mark.parametrize(
    "dependency, phrase",
    [
        ("contains_solver_anchor", "solver anchor"),
        ("contains_required_source", "required source"),
        ("only_enclosing_exterior", "only enclosing exterior"),
        ("requested_fem_air_volume", "requested FEM air volume"),
    ],
)
def test_required_dependency_converts_nominal_skip_to_recorded_refusal(
    dependency, phrase
):
    candidate = {
        "object_id": "hidden",
        "name": "required hidden body",
        "body_kind": "solid",
        "visible": False,
        dependency: True,
    }

    plan = plan_export_scope("root", [candidate])

    assert phrase in plan.refusals[0]["reason"]
    with pytest.raises(WgReturnError, match=phrase):
        plan.manifest_scope()


@pytest.mark.parametrize("selection", [{"kind": "body"}, {"kind": "face"}])
def test_body_and_face_selection_are_refused_with_remedy(selection):
    with pytest.raises(WgReturnError, match="root or exactly one occurrence"):
        plan_export_scope(selection, [])


def test_multiple_occurrence_selection_is_refused_with_remedy():
    with pytest.raises(WgReturnError, match="exactly one occurrence"):
        plan_export_scope({"kind": "occurrences", "paths": ["a", "b"]}, [])


def test_occurrence_subtree_selection_is_recorded_by_path():
    plan = plan_export_scope(
        {"kind": "occurrence", "path": "Speaker/Cabinet"}, []
    )

    assert plan.manifest_scope()["selection"] == "Speaker/Cabinet"


def test_instances_may_be_empty_and_native_id_may_be_null():
    manifest = _worked_example()
    manifest["instances"] = []
    manifest["coordinate_system"].pop("solver_anchor_instance_id")
    manifest["sources"] = [_source(instance_id=None)]

    validate_return_manifest(manifest)


def test_sources_must_not_be_empty():
    manifest = _worked_example()
    manifest["sources"] = []

    with pytest.raises(WgReturnError, match="at least one drivable patch"):
        validate_return_manifest(manifest)


def test_acoustics_must_be_null_in_version_1_0():
    manifest = _worked_example()
    manifest["acoustics"] = {"driver": "not-yet-owned-here"}

    with pytest.raises(WgReturnError, match="acoustics must be null"):
        validate_return_manifest(manifest)


def test_reserved_acoustics_field_may_be_omitted():
    manifest = _worked_example()
    del manifest["acoustics"]

    validate_return_manifest(manifest)


def test_multiple_instances_require_an_explicit_valid_anchor():
    manifest = _worked_example()
    second_id = "225df50e-1ff9-4ad0-a816-99643b3a4678"
    manifest["instances"].append(
        _instance(instance_id=second_id, parameter_prefix="wg_tritonia2_")
    )
    manifest["coordinate_system"].pop("solver_anchor_instance_id")

    with pytest.raises(WgReturnError, match="multiple.*require.*anchor"):
        validate_return_manifest(manifest)

    manifest["coordinate_system"]["solver_anchor_instance_id"] = "missing"
    with pytest.raises(WgReturnError, match="does not name an instance"):
        validate_return_manifest(manifest)


def test_unknown_major_and_required_feature_are_refused_loudly():
    manifest = _worked_example()
    manifest["wgreturn_version"] = "2.0"
    with pytest.raises(WgReturnError, match="2.0.*major 1"):
        validate_return_manifest(manifest)

    manifest = _worked_example()
    manifest["required_features"].append("future-verdict-v1")
    with pytest.raises(WgReturnError, match="future-verdict-v1"):
        validate_return_manifest(manifest)


def test_known_major_ignores_additive_unknown_keys():
    manifest = _worked_example()
    manifest["wgreturn_version"] = "1.91"
    manifest["future_display_hint"] = {"colour": "ochre"}

    validate_return_manifest(manifest)


def test_fem_inventory_adds_and_requires_the_feature():
    manifest = _worked_example()
    manifest["files"]["fem/mf-air.step"] = {
        "sha256": "sha256:" + "ab" * 32,
        "size_bytes": 1224,
        "media_type": "model/step",
        "purpose": "fem-air-volume",
    }
    manifest["scope"]["fem_air_volumes"] = [
        {"file": "fem/mf-air.step", "n_bodies_expected": 1}
    ]

    with pytest.raises(WgReturnError, match="fem-air-volume-v1"):
        validate_return_manifest(manifest)

    manifest["required_features"].append("fem-air-volume-v1")
    validate_return_manifest(manifest)


@pytest.mark.parametrize(
    "key,value",
    [
        ("physical_tag", 100),
        ("tag", 101),
        ("tag_map", {"100": "HF"}),
    ],
)
def test_no_physical_tag_can_appear_anywhere_in_manifest(key, value):
    manifest = _worked_example()
    manifest["sources"][0][key] = value

    with pytest.raises(WgReturnError, match="WG-authored verdict data"):
        validate_return_manifest(manifest)


@pytest.mark.parametrize(
    "field",
    ["freshness", "healing", "symmetry", "solver_ready", "solver_readiness"],
)
def test_cad_policy_never_authors_wg_verdict_fields(field):
    manifest = _worked_example()
    manifest["instances"][0][field] = "current"

    with pytest.raises(WgReturnError, match="WG-authored verdict data"):
        validate_return_manifest(manifest)


def test_mirrored_chirality_is_refused_and_names_what_to_fix():
    manifest = _worked_example()
    manifest["instances"][0]["chirality"] = "mirrored"

    with pytest.raises(WgReturnError, match="mirrored.*no producer.*without mirroring"):
        validate_return_manifest(manifest)


def test_missing_or_unresolvable_transform_is_refused_without_identity_fallback():
    manifest = _worked_example()
    del manifest["instances"][0]["assembly_from_link"]
    with pytest.raises(WgReturnError, match="assembly_from_link"):
        validate_return_manifest(manifest)

    manifest = _worked_example()
    manifest["instances"][0]["assembly_from_link"] = [[1.0]]
    with pytest.raises(WgReturnError, match="finite 4x4 matrix in millimetres"):
        validate_return_manifest(manifest)


def test_nonfinite_values_are_refused_during_build_parse_and_serialization():
    manifest = _worked_example()
    manifest["assembly"]["bbox_mm"][0][0] = math.nan
    with pytest.raises(WgReturnError, match="finite number"):
        dumps_return_manifest(manifest)

    text = dumps_return_manifest(_worked_example()).replace("-172.0", "NaN", 1)
    with pytest.raises(WgReturnError, match="non-finite constant"):
        loads_return_manifest(text)


def test_optional_identity_and_body_evidence_fields_may_be_absent_or_null():
    manifest = _worked_example()
    instance = manifest["instances"][0]
    for key in (
        "lineage_id",
        "edit_version",
        "design_hash",
        "geometry_hash",
        "origin_bundle_id",
        "occurrence_path",
    ):
        instance[key] = None
    instance["body_evidence"]["baseline_fingerprint"] = None
    instance["body_evidence"]["observed_fingerprint"] = None
    instance["source_contract"] = None

    validate_return_manifest(manifest)


def test_source_selectors_require_at_least_one_nonempty_mechanism():
    manifest = _worked_example()
    manifest["sources"][0]["selectors"] = {
        "appearance_labels": [],
        "shell_names": [],
    }

    with pytest.raises(WgReturnError, match="at least one mechanism"):
        validate_return_manifest(manifest)


def test_two_identical_exports_remain_distinct_instances_by_id_and_transform():
    manifest = _worked_example()
    second_id = "225df50e-1ff9-4ad0-a816-99643b3a4678"
    translated = deepcopy(IDENTITY_MATRIX)
    translated[0][3] = 600.0
    second = _instance(
        instance_id=second_id,
        parameter_prefix="wg_tritonia2_",
        matrix=translated,
    )
    manifest["instances"].append(second)
    manifest["coordinate_system"]["solver_anchor_instance_id"] = INSTANCE_ID
    manifest["sources"].append(
        _source(
            source_id="source-hf-right",
            instance_id=second_id,
            channel_id="drive-hf-right",
        )
    )

    validate_return_manifest(manifest)

    first, second = manifest["instances"]
    for key in ("design_id", "design_hash", "export_id", "export_sequence"):
        assert first[key] == second[key]
    assert first["instance_id"] != second["instance_id"]
    assert first["assembly_from_link"] != second["assembly_from_link"]


def test_scope_body_count_must_match_assembly_inventory_gate():
    manifest = _worked_example()
    manifest["assembly"]["n_bodies_expected"] = 2

    with pytest.raises(WgReturnError, match="included count"):
        validate_return_manifest(manifest)


def test_launch_feature_vocabulary_is_the_ratified_three_features():
    assert BASE_RETURN_FEATURES == (
        "checksummed-files-v1",
        "assembly-frame-v1",
        "instance-records-v1",
    )


def test_instance_round_trip_preserves_the_wg_formula_and_exact_config() -> None:
    manifest = loads_return_manifest(dumps_return_manifest(_worked_example()))

    assert manifest["instances"][0]["formula"] == "osse"
    assert manifest["instances"][0]["config"]["dimensions"]["length"] == {
        "raw": "130",
        "value": 130.0,
    }


# ------------------------------------------------- declared reduced domain schema


def _domain_manifest(**domain):
    manifest = _worked_example()
    manifest["assembly"]["domain"] = {
        "kind": "half",
        "cut_planes": ["y0"],
        "declared_by": "cad-author",
        "evidence": {"y0": {"min_mm": 0.0, "max_mm": 90.0, "tolerance_mm": 0.05}},
        **domain,
    }
    manifest["required_features"] = [
        *manifest["required_features"],
        "reduced-domain-v1",
    ]
    return manifest


def test_a_declared_half_validates_and_names_its_planes():
    assert validate_return_manifest(_domain_manifest()) is None
    assert validate_domain_record(None) == ()
    assert validate_domain_record(
        {
            "kind": "quarter",
            "cut_planes": ["y0", "x0"],
            "declared_by": "cad-author",
            "evidence": {
                plane: {"min_mm": 0.0, "max_mm": 90.0, "tolerance_mm": 0.05}
                for plane in ("x0", "y0")
            },
        }
    ) == ("x0", "y0")


def test_domain_kind_and_planes_must_agree():
    with pytest.raises(WgReturnError, match="does not match cut_planes"):
        validate_return_manifest(_domain_manifest(kind="quarter"))


def test_a_declared_domain_without_its_feature_is_refused():
    manifest = _domain_manifest()
    manifest["required_features"] = [
        feature
        for feature in manifest["required_features"]
        if feature != "reduced-domain-v1"
    ]
    with pytest.raises(WgReturnError, match="reduced-domain-v1 is required exactly"):
        validate_return_manifest(manifest)


def test_the_feature_without_a_declared_domain_is_refused():
    manifest = _worked_example()
    manifest["required_features"] = [
        *manifest["required_features"],
        "reduced-domain-v1",
    ]
    with pytest.raises(WgReturnError, match="reduced-domain-v1 is required exactly"):
        validate_return_manifest(manifest)


def test_evidence_that_contradicts_the_declaration_is_refused():
    with pytest.raises(WgReturnError, match="negative side of y0"):
        validate_return_manifest(
            _domain_manifest(
                evidence={"y0": {"min_mm": -12.0, "max_mm": 90.0, "tolerance_mm": 0.05}}
            )
        )
    with pytest.raises(WgReturnError, match="no geometry on the positive side"):
        validate_return_manifest(
            _domain_manifest(
                evidence={"y0": {"min_mm": 0.0, "max_mm": 0.0, "tolerance_mm": 0.05}}
            )
        )
    with pytest.raises(WgReturnError, match="exactly the declared planes"):
        validate_return_manifest(_domain_manifest(evidence={}))


def test_an_unsupported_symmetry_plane_cannot_be_declared():
    with pytest.raises(WgReturnError, match="may only name x0, y0"):
        validate_return_manifest(
            _domain_manifest(
                kind="half",
                cut_planes=["z0"],
                evidence={"z0": {"min_mm": 0.0, "max_mm": 1.0, "tolerance_mm": 0.05}},
            )
        )


def test_the_export_frame_is_named_and_checked():
    """The file states which component's frame it is in, or it means the root."""

    manifest = _worked_example()
    validate_return_manifest(manifest)  # absent is the root component

    for frame in ("root-component", "selected-occurrence-component"):
        manifest["coordinate_system"]["export_frame"] = frame
        validate_return_manifest(manifest)

    manifest["coordinate_system"]["export_frame"] = "assembly"
    with pytest.raises(WgReturnError, match="export_frame must be one of"):
        validate_return_manifest(manifest)
