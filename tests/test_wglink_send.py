"""Exercise the read-only Fusion boundary with small fake API objects."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import json
import math
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGLink"


@pytest.fixture
def send_module(monkeypatch):
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

    core_spec = importlib.util.spec_from_file_location(
        "wglink_core_send_test", ADDIN / "wglink_core.py"
    )
    assert core_spec and core_spec.loader
    core = importlib.util.module_from_spec(core_spec)
    monkeypatch.setitem(sys.modules, core_spec.name, core)
    monkeypatch.setitem(sys.modules, "wglink_core", core)
    core_spec.loader.exec_module(core)

    send_spec = importlib.util.spec_from_file_location(
        "wglink_send_unit_test", ADDIN / "wglink_send.py"
    )
    assert send_spec and send_spec.loader
    module = importlib.util.module_from_spec(send_spec)
    monkeypatch.setitem(sys.modules, send_spec.name, module)
    send_spec.loader.exec_module(module)
    return module


class Collection(list):
    @property
    def count(self):
        return len(self)

    def item(self, index):
        return self[index]

    def itemByName(self, name):
        return next((item for item in self if getattr(item, "name", None) == name), None)


class Attribute:
    """Fusion's attribute handle: it writes through, and it can delete itself."""

    def __init__(self, owner, key):
        self._owner, self._key = owner, key

    @property
    def value(self):
        return self._owner.values.get(self._key)

    @value.setter
    def value(self, value):
        self._owner.values[self._key] = value

    def deleteMe(self):
        self._owner.values.pop(self._key, None)
        return True


class Attributes:
    def __init__(self):
        self.values = {}

    def itemByName(self, group, name):
        key = (group, name)
        return Attribute(self, key) if self.values.get(key) is not None else None

    def add(self, group, name, value):
        self.values[(group, name)] = value


def point(x, y, z):
    return types.SimpleNamespace(x=x, y=y, z=z)


def box(low=(0, 0, 0), high=(1, 1, 1)):
    return types.SimpleNamespace(minPoint=point(*low), maxPoint=point(*high))


def face(role, area=2.0, edges=()):
    return types.SimpleNamespace(
        area=area,
        appearance=types.SimpleNamespace(name=role),
        edges=Collection(edges),
        boundingBox=box((0, 0, 0), (1, 1, 0)),
    )


def body(name, *, solid=True, visible=True, faces=()):
    return types.SimpleNamespace(
        name=name,
        isSolid=solid,
        isVisible=visible,
        faces=Collection(faces),
        attributes=Attributes(),
        boundingBox=box(),
        volume=1.0,
        entityToken=f"token-{name}",
        objectType="adsk::fusion::BRepBody",
    )


def transform(rows=None):
    """A Fusion ``Matrix3D`` as the export path actually reads it.

    ``Matrix3D.asArray()`` returns 16 row-major numbers whose translation
    column is in Fusion's internal centimetres. A placeholder object with no
    ``asArray`` is not a transform an occurrence could have, and treating one
    as identity is what let the frame question go unasked here.
    """

    values = rows or [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    return types.SimpleNamespace(asArray=lambda: list(values))


def moved_transform(x_cm=0.0, y_cm=0.0, z_cm=0.0):
    return transform([
        1.0, 0.0, 0.0, x_cm,
        0.0, 1.0, 0.0, y_cm,
        0.0, 0.0, 1.0, z_cm,
        0.0, 0.0, 0.0, 1.0,
    ])


def component(name, bodies=()):
    value = types.SimpleNamespace(
        name=name,
        objectType="adsk::fusion::Component",
        bRepBodies=Collection(bodies),
        meshBodies=Collection(),
        constructionPlanes=Collection(),
        constructionAxes=Collection(),
        sketches=Collection(),
        occurrences=Collection(),
        allOccurrences=Collection(),
    )
    for item in bodies:
        item.parentComponent = value
    return value


def test_declaration_round_trip_and_validation(send_module):
    candidate = body("shell", solid=False)

    send_module.declare_body(candidate, "exterior-shell")

    assert send_module.read_declaration(candidate) == "exterior-shell"
    with pytest.raises(send_module.wglink_core.WgLinkError, match="declaration"):
        send_module.declare_body(candidate, "helper")


def test_a_declaration_can_be_taken_back_off(send_module):
    """Declare Body needs an undo: a body left 'exclude' by mistake is
    invisible to every later export, and nothing could remove the attribute."""

    candidate = body("shell", solid=False)
    send_module.declare_body(candidate, "exclude")

    send_module.clear_declaration(candidate)

    assert send_module.read_declaration(candidate) is None
    assert candidate.attributes.values == {}
    # Clearing a body that never carried one is a no-op, not a refusal.
    send_module.clear_declaration(candidate)


def test_occurrence_proxy_uses_native_attributes_but_remains_geometry_handle(
    send_module,
):
    """Match Fusion's measured proxy split instead of putting tags on fakes.

    A BRepBody proxy has the occurrence-frame geometry that STEP export needs,
    but its attribute collection is empty; WG identity and declarations live
    only on ``nativeObject``.
    """

    native = body("Horn", faces=[face("HF")])
    send_module.wglink_core._set_attribute(native, "instance_id", "wgi-proxy")
    send_module.wglink_core._set_attribute(native, "role", "waveguide")
    send_module.declare_body(native, "exterior-shell")
    native_component = component("Horn component", [native])
    proxy = body("Horn", faces=[face("HF")])
    proxy.attributes = Attributes()  # measured Fusion behavior: zero attributes
    proxy.nativeObject = native
    proxy.parentComponent = native_component
    occurrence = types.SimpleNamespace(
        name="Horn:1",
        fullPathName="Speaker/Horn:1",
        component=native_component,
        bRepBodies=Collection([proxy]),
        meshBodies=Collection(),
        childOccurrences=Collection(),
        transform2=transform(),
        isVisible=True,
        isSuppressed=False,
        objectType="adsk::fusion::Occurrence",
    )
    design = types.SimpleNamespace(rootComponent=component("Speaker"))

    walk = send_module._scope_walk(design, occurrence)

    candidate = next(item for item in walk["candidates"] if item["kind"] == "body")
    assert candidate["wglink_instance_id"] == "wgi-proxy"
    assert candidate["wglink_role"] == "waveguide"
    assert candidate["declaration"] == "exterior-shell"
    assert walk["bodies"][candidate["object_id"]] is proxy
    assert proxy.attributes.values == {}


def _freestanding_insertion(send_module, instance_id="wgi-free", helper_faces=()):
    """The two bodies a freestanding insertion leaves in the document.

    ``_close_and_thicken`` stitches the loft and the throat patch into one
    surface body, thickens that into the solid, and stamps both with the same
    instance id -- the shell keeps role ``cut_tool``.
    """

    core = send_module.wglink_core
    final = body("WGLink freestanding waveguide", solid=True, faces=[face("HF")])
    core._set_attribute(final, "instance_id", instance_id)
    core._set_attribute(final, "role", "waveguide")
    core._set_attribute(final, "face_role", "HF")
    stitched = body(
        "WGLink stitched waveguide body", solid=False, faces=list(helper_faces)
    )
    core._set_attribute(stitched, "instance_id", instance_id)
    core._set_attribute(stitched, "role", "cut_tool")
    root = component("waveguide v1", [final, stitched])
    design = types.SimpleNamespace(
        rootComponent=root, findAttributes=lambda _group, _name: Collection()
    )
    return design, final, stitched


def test_managed_helper_does_not_block_the_return_it_helped_build(send_module):
    """A cut tool carries the instance id of the body it built.

    Measured 2026-09-04 from a freestanding insertion: the leftover stitched
    shell claimed both the solver anchor and a required source, so wglink_return
    skipped it by role and then refused the skip -- every freestanding model
    refused its own return with "cannot skip 'waveguide v1/WGLink stitched
    waveguide body'".
    """

    design, _final, _stitched = _freestanding_insertion(send_module)

    walk = send_module._scope_walk(design, "root")
    send_module._mark_solver_anchor(walk["candidates"], "wgi-free")
    plan = send_module.plan_export_scope(walk["selection"], walk["candidates"])

    assert [reason["reason"] for reason in plan.refusals] == []
    scope = plan.manifest_scope()
    assert [record["name"] for record in scope["included"]] == [
        "WGLink freestanding waveguide"
    ]
    assert [record["kind"] for record in scope["skipped"]] == ["wglink_helper"]


def test_the_anchor_and_the_source_land_on_the_final_body_only(send_module):
    design, _final, _stitched = _freestanding_insertion(send_module)

    walk = send_module._scope_walk(design, "root")
    send_module._mark_solver_anchor(walk["candidates"], "wgi-free")
    flags = {
        item["name"]: (
            item["contains_solver_anchor"],
            item["contains_required_source"],
            item["only_enclosing_exterior"],
        )
        for item in walk["candidates"]
    }

    assert flags["WGLink freestanding waveguide"] == (True, True, True)
    assert flags["WGLink stitched waveguide body"] == (False, False, False)


def test_a_helper_repeating_the_final_bodys_own_source_does_not_refuse(send_module):
    """Reproduction of the 2026-09-06 acceptance failure.

    ``_close_and_thicken`` stitches the loft and the *throat patch* into one
    surface body before thickening it, so on a throat-opened model the leftover
    shell carries the very HF face the final solid also carries.  Measured from
    ``260308Tritonia-M`` (one required source, ``HF``, ``throat_opened: true``):
    the helper's duplicate HF flagged ``contains_required_source``, the skip by
    role then became a terminal refusal, and every such return failed with
    "cannot skip 'waveguide v1/WGLink stitched waveguide body'".

    A role the final body already carries is not evidence WG can lose.
    """

    design, _final, _stitched = _freestanding_insertion(
        send_module, helper_faces=[face("HF")]
    )

    walk = send_module._scope_walk(design, "root")
    send_module._mark_solver_anchor(walk["candidates"], "wgi-free")
    plan = send_module.plan_export_scope(walk["selection"], walk["candidates"])

    assert [reason["reason"] for reason in plan.refusals] == []
    scope = plan.manifest_scope()
    assert [record["name"] for record in scope["included"]] == [
        "WGLink freestanding waveguide"
    ]
    assert [record["kind"] for record in scope["skipped"]] == ["wglink_helper"]


def test_a_helper_that_carries_a_painted_source_still_refuses_the_skip(send_module):
    """Role is not licence to drop a real source.

    Only the bare instance id stops flagging a helper; a painted face on one is
    evidence WG would otherwise lose silently, so it still converts the skip
    into a refusal the user can read.
    """

    design, _final, _stitched = _freestanding_insertion(
        send_module, helper_faces=[face("LF")]
    )

    walk = send_module._scope_walk(design, "root")
    send_module._mark_solver_anchor(walk["candidates"], "wgi-free")
    plan = send_module.plan_export_scope(walk["selection"], walk["candidates"])

    with pytest.raises(
        send_module.WgReturnError, match="stitched waveguide body.*required source"
    ):
        plan.manifest_scope()


def _refuses_the_stitched_skip(plan):
    return any(
        "stitched waveguide body" in refusal["reason"]
        and "required source" in refusal["reason"]
        for refusal in plan.refusals
    )


def test_a_role_only_another_helper_carries_is_still_a_loss(send_module):
    """Cover has to come from a body the STEP actually carries.

    Two helpers agreeing about an LF face is no reason to drop it: both are
    skipped by role, so WG would receive a file with no LF face anywhere.
    """

    core = send_module.wglink_core
    design, _final, _stitched = _freestanding_insertion(
        send_module, helper_faces=[face("LF")]
    )
    twin = body("WGLink throat patch", solid=False, faces=[face("LF")])
    core._set_attribute(twin, "instance_id", "wgi-free")
    core._set_attribute(twin, "role", "cut_tool")
    twin.parentComponent = design.rootComponent
    design.rootComponent.bRepBodies.append(twin)

    walk = send_module._scope_walk(design, "root")
    send_module._mark_solver_anchor(walk["candidates"], "wgi-free")
    plan = send_module.plan_export_scope(walk["selection"], walk["candidates"])

    assert _refuses_the_stitched_skip(plan)


def test_another_insertions_hf_does_not_cover_this_helpers(send_module):
    """Cover is per insertion, not per role name.

    A second waveguide's HF face sits somewhere else entirely; exporting it
    does not hand WG the face this helper is about to take with it.
    """

    core = send_module.wglink_core
    design, final, _stitched = _freestanding_insertion(
        send_module, helper_faces=[face("HF")]
    )
    core._set_attribute(final, "instance_id", "wgi-other")

    walk = send_module._scope_walk(design, "root")
    send_module._mark_solver_anchor(walk["candidates"], "wgi-free")
    plan = send_module.plan_export_scope(walk["selection"], walk["candidates"])

    assert _refuses_the_stitched_skip(plan)


@pytest.mark.parametrize("withheld", ["hidden", "suppressed"])
def test_a_body_that_never_reaches_wg_covers_nothing(send_module, withheld):
    """The final body's HF only redeems the helper's while it is exported.

    Hide or suppress it and the export loses HF twice over, so the helper's
    duplicate becomes the loss it looks like and the refusal comes back.
    """

    design, final, _stitched = _freestanding_insertion(
        send_module, helper_faces=[face("HF")]
    )
    if withheld == "hidden":
        final.isVisible = False
    else:
        final.isSuppressed = True

    walk = send_module._scope_walk(design, "root")
    send_module._mark_solver_anchor(walk["candidates"], "wgi-free")
    plan = send_module.plan_export_scope(walk["selection"], walk["candidates"])

    assert _refuses_the_stitched_skip(plan)


def _design_of(root, monkeypatch, send_module):
    design = types.SimpleNamespace(
        rootComponent=root, findAttributes=lambda _group, _name: Collection()
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    return types.SimpleNamespace(version="2704.1.53")


def test_preflight_gathers_the_numbers_the_send_dialog_previews(
    send_module, monkeypatch
):
    """No Fusion object leaves this function: the wording is composed by
    wglink_author from exactly these plain values."""

    painted = face("LF", area=2.5)
    horn = body("horn", faces=[painted, face("Steel - Satin")])
    app = _design_of(component("Speaker", [horn]), monkeypatch, send_module)

    report = send_module.preflight_scope(app, {"selection": "root"})

    assert report["selection"] == "root"
    assert report["instance_ids"] == []
    assert report["included"] == [{"name": "Speaker/horn", "body_kind": "solid"}]
    assert report["sources"] == [
        {"role": "LF", "area_mm2": 250.0, "face_count": 1, "instance_id": None}
    ]
    assert report["scope_error"] is None and report["source_error"] is None
    # Fusion reports centimetres; every number here is millimetres.
    assert report["bounds_mm"] == {"min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 10.0]}
    assert report["source_bounds_mm"] == {
        "min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 0.0]
    }


def test_preflight_reports_a_missing_source_instead_of_raising_it(
    send_module, monkeypatch
):
    """The refusal used to reach the user only after they pressed OK."""

    plain = body("horn", faces=[face("Steel - Satin")])
    app = _design_of(component("Speaker", [plain]), monkeypatch, send_module)

    report = send_module.preflight_scope(app)

    assert report["sources"] == []
    assert "no drivable source" in report["source_error"]
    assert report["source_bounds_mm"] is None


def test_preflight_reports_an_unclassified_surface_body_as_scope_text(
    send_module, monkeypatch
):
    shell = body("Shell", solid=False, faces=[face("HF")])
    app = _design_of(component("Speaker", [shell]), monkeypatch, send_module)

    report = send_module.preflight_scope(app)

    assert "unclassified" in report["scope_error"]
    assert report["included"] == [] and report["sources"] == []

    # Declare Body is the remedy, and it unblocks the same scope.
    send_module.declare_body(shell, "exterior-shell")
    cleared = send_module.preflight_scope(app)
    assert cleared["scope_error"] is None
    assert cleared["included"] == [{"name": "Speaker/Shell", "body_kind": "surface"}]
    assert [source["role"] for source in cleared["sources"]] == ["HF"]


def test_step_body_counter_handles_solid_shell_mixed_and_ignores_names(send_module):
    snippet = """ISO-10303-21;
    #1=MANIFOLD_SOLID_BREP('SHELL_BASED_SURFACE_MODEL',#2);
    #3=SHELL_BASED_SURFACE_MODEL('',(#4));
    /* #5=MANIFOLD_SOLID_BREP('',#6); */
    END-ISO-10303-21;
    """

    assert send_module.count_step_bodies(snippet) == 2
    assert send_module.count_step_bodies("#1=OPEN_SHELL('',());") == 0


def test_adjacent_painted_faces_form_one_component(send_module):
    shared = object()
    faces = [face("LF", edges=[shared]), face("LF", edges=[shared])]

    assert send_module._connected_components(faces) == 1
    assert send_module._connected_components([face("LF"), face("LF")]) == 2


def test_strict_transform_refuses_instead_of_using_identity(send_module):
    class Broken:
        def asArray(self):
            raise RuntimeError("not readable")

    with pytest.raises(
        send_module.wglink_core.WgLinkError, match="instance-7.*unreadable"
    ):
        send_module._strict_matrix_rows(Broken(), "instance-7")


def test_heartbeat_identity_summary_uses_exact_return_ids_and_entity_tokens(
    send_module,
):
    managed = body("managed")
    matrix = [
        [1.0, 0.0, 0.0, 25.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]

    summaries = send_module._instance_identity_summaries(
        included_pairs=[({
            "object_id": managed.entityToken,
            "wglink_instance_id": "instance-a",
        }, managed)],
        instances=[{
            "instance_id": "instance-a",
            "assembly_from_link": matrix,
        }],
        sources=[{
            "instance_id": "instance-a",
            "id": "source-hf",
            "default_drive_channel_id": "drive-hf",
        }],
    )

    assert summaries == {
        "instance-a": {
            "body_object_ids": ["token-managed"],
            "transform_hash": send_module._canonical_hash(matrix),
            "source_ids": ["source-hf"],
            "drive_channel_ids": ["drive-hf"],
        }
    }


def test_heartbeat_identity_summary_never_promotes_fallback_body_labels(
    send_module,
):
    managed = body("managed")

    summaries = send_module._instance_identity_summaries(
        included_pairs=[({
            "object_id": "body-0001",
            "wglink_instance_id": "instance-a",
        }, managed)],
        instances=[{
            "instance_id": "instance-a",
            "assembly_from_link": [[1.0, 0.0, 0.0, 0.0]],
        }],
        sources=[{
            "instance_id": "instance-a",
            "id": "source-hf",
            "default_drive_channel_id": "",
        }],
    )

    assert "instance-a" not in summaries


def test_ulid_shape_uniqueness_and_time_prefix(send_module):
    first = send_module.generate_return_id(1_000)
    second = send_module.generate_return_id(2_000)

    assert re.fullmatch(r"wgr_[0-9A-HJKMNP-TV-Z]{26}", first)
    assert first != send_module.generate_return_id(1_000)
    assert first[4:14] < second[4:14]


def test_stored_wg_config_is_echoed_as_structured_data(send_module):
    config = {
        "root": {"formula": "OSSE"},
        "dimensions": {"coverage": {"raw": "45 - 5*cos(p)^5", "value": 45}},
    }

    assert send_module._stored_config(__import__("json").dumps(config)) == config
    assert send_module._stored_config("") is None
    assert send_module._stored_config("not json") is None


def test_user_source_inventory_converts_area_and_excludes_claimed_throat(send_module):
    shared = object()
    throat = face("HF", area=5.06707)
    lf_faces = [face("LF", area=10.0, edges=[shared]), face("LF", area=20.0, edges=[shared])]
    managed = body("speaker", faces=[throat, *lf_faces])
    record = {
        "instance_id": "instance-1",
        "body": managed,
        "payload": {
            "source_role": "HF",
            "expected_throat_area_mm2": "506.707",
            "throat_z_mm": "0",
        },
    }

    sources = send_module._sources([record], [managed])

    assert [source["id"] for source in sources] == ["source-hf", "source-lf"]
    assert sources[0]["observed"]["total_area_mm2"] == pytest.approx(506.707)
    assert sources[1]["observed"]["total_area_mm2"] == pytest.approx(3000.0)
    assert sources[1]["expected_connected_components"] == 1
    assert all("advanced_face_indices" not in source["selectors"] for source in sources)


def test_face_role_recognises_the_new_name_and_the_legacy_one(send_module):
    """PORT_EXIT is the retired name for PASSIVE_CARDIOID.  A face painted
    under either name is a recognised source, and _face_role reports back
    whichever name is literally painted -- never rewriting the legacy one.
    """

    assert send_module._face_role(face("PASSIVE_CARDIOID")) == "PASSIVE_CARDIOID"
    assert send_module._face_role(face("PORT_EXIT")) == "PORT_EXIT"
    assert send_module._face_role(face("Steel - Satin")) is None


def test_a_legacy_port_exit_face_still_exports_as_port_exit(send_module):
    """The exported role feeds the return-state identity: a face nobody
    repainted has to keep exporting under its original name, or every
    existing model built before the rename would read as changed.
    """

    legacy = body("port", faces=[face("PORT_EXIT", area=2.5)])

    sources = send_module._sources([], [legacy])

    assert [source["role"] for source in sources] == ["PORT_EXIT"]
    assert sources[0]["suggested_resolution_mm"] == pytest.approx(25.0)


def test_a_newly_painted_cardioid_face_exports_under_the_new_name(send_module):
    fresh = body("port", faces=[face("PASSIVE_CARDIOID", area=2.5)])

    sources = send_module._sources([], [fresh])

    assert [source["role"] for source in sources] == ["PASSIVE_CARDIOID"]
    assert sources[0]["suggested_resolution_mm"] == pytest.approx(25.0)


def test_shape_fingerprint_includes_source_roles_and_face_geometry(send_module):
    candidate = body("speaker", faces=[face("MF", area=2.5)])

    fingerprint = send_module._shape_fingerprint(candidate)

    assert fingerprint["face_count"] == 1
    assert fingerprint["faces"][0]["source_role"] == "MF"
    assert fingerprint["faces"][0]["area_mm2"] == pytest.approx(250.0)


def test_schema_1_1_stamp_stays_paired_with_fail_closed_fingerprint_guard(
    send_module, tmp_path, monkeypatch
):
    exterior = body("cabinet", faces=[face("LF")])
    root = component("Pairing", [exterior])

    class ExportManager:
        def createSTEPExportOptions(self, path, geometry=None):
            return path, geometry

        def execute(self, options):
            Path(options[0]).write_text(
                "ISO-10303-21;\n#1=MANIFOLD_SOLID_BREP('',#2);\nEND-ISO-10303-21;\n",
                encoding="utf-8",
            )
            return True

    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=ExportManager(),
        findAttributes=lambda _group, _name: Collection(),
    )
    app = types.SimpleNamespace(
        version="2704.1.53",
        activeDocument=types.SimpleNamespace(name="Pairing"),
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(app, {"output_folder": str(tmp_path)})
    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert manifest["wgreturn_version"] == "1.1"

    # These are one change: an old WG silently accepts a hash-less 1.1 bundle
    # while a new WG refuses it, so this guard must never relax while stamped 1.1.
    monkeypatch.setattr(
        send_module,
        "return_state",
        lambda _app, _options: {"hash": None, "reason": "simulated failure"},
    )
    with pytest.raises(
        send_module.wglink_core.WgLinkError,
        match="Could not fingerprint.*simulated failure",
    ):
        send_module.send(
            app,
            {"output_folder": str(tmp_path), "request_id": "fingerprint-failure"},
        )
    assert not (tmp_path / "Pairing-fingerprint-failure.wgreturn").exists()


def test_full_unlinked_send_avoids_collisions_and_uses_full_request_ids(
    send_module, tmp_path, monkeypatch
):
    painted = face("LF", area=2.5)
    exterior = body("cabinet", faces=[painted])
    root = component("Speaker", [exterior])

    class ExportManager:
        def createSTEPExportOptions(self, path, geometry=None):
            return path, geometry

        def execute(self, options):
            Path(options[0]).write_text(
                "ISO-10303-21;\n#1=MANIFOLD_SOLID_BREP('',#2);\nEND-ISO-10303-21;\n",
                encoding="utf-8",
            )
            return True

    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=ExportManager(),
        findAttributes=lambda _group, _name: Collection(),
    )
    app = types.SimpleNamespace(
        version="2704.1.53",
        activeDocument=types.SimpleNamespace(name="My Speaker"),
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(app, {"output_folder": str(tmp_path)})

    target = tmp_path / "My Speaker.wgreturn"
    assert Path(report["bundle_path"]) == target
    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (target / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert manifest["instances"] == []
    assert "solver_anchor_instance_id" not in manifest["coordinate_system"]
    assert manifest["sources"][0]["observed"]["total_area_mm2"] == 250.0
    step = target / "assembly.step"
    expected = "sha256:" + __import__("hashlib").sha256(step.read_bytes()).hexdigest()
    assert manifest["files"]["assembly.step"]["sha256"] == expected
    assert not list(tmp_path.glob(".My Speaker.wgreturn.tmp-*"))
    assert not list(tmp_path.glob("*.reserve"))

    collision_report = send_module.send(app, {"output_folder": str(tmp_path)})
    assert Path(collision_report["bundle_path"]) == tmp_path / "My Speaker-2.wgreturn"
    assert target.is_dir()

    marker = target / "old-bundle-marker"
    marker.write_text("replace me", encoding="utf-8")
    replacement_report = send_module.send(
        app, {"output_folder": str(tmp_path), "overwrite": True}
    )
    assert Path(replacement_report["bundle_path"]) == target
    assert replacement_report["return_id"] != report["return_id"]
    assert not marker.exists()

    request_a = "request-collision-prefix-alpha"
    request_b = "request-collision-prefix-beta"
    assert request_a[:12] == request_b[:12]
    request_a_report = send_module.send(
        app,
        {"output_folder": str(tmp_path), "overwrite": True, "request_id": request_a},
    )
    request_b_report = send_module.send(
        app,
        {"output_folder": str(tmp_path), "overwrite": True, "request_id": request_b},
    )
    assert Path(request_a_report["bundle_path"]) == (
        tmp_path / f"My Speaker-{request_a}.wgreturn"
    )
    assert Path(request_b_report["bundle_path"]) == (
        tmp_path / f"My Speaker-{request_b}.wgreturn"
    )


def test_return_name_reservation_is_atomic_without_a_published_target(
    send_module, tmp_path: Path,
) -> None:
    target = tmp_path / "Speaker.wgreturn"

    first, first_reservation = send_module._reserve_target(target, overwrite=False)
    second, second_reservation = send_module._reserve_target(target, overwrite=False)

    assert first == target
    assert second == tmp_path / "Speaker-2.wgreturn"
    assert first_reservation.is_file()
    assert second_reservation.is_file()
    first_reservation.unlink()
    second_reservation.unlink()


def test_stale_publish_cleanup_recovers_backup_and_removes_debris(
    send_module, tmp_path: Path,
) -> None:
    target = tmp_path / "Speaker.wgreturn"
    backup = tmp_path / ".Speaker.wgreturn.old-deadbeef"
    backup.mkdir()
    (backup / "wgreturn.json").write_text("recovered", encoding="utf-8")
    temporary = tmp_path / ".Speaker.wgreturn.tmp-deadbeef"
    temporary.mkdir()
    reservation = tmp_path / ".Speaker-2.wgreturn.reserve"
    reservation.write_text("abandoned", encoding="ascii")
    for candidate in (backup, temporary, reservation):
        os.utime(candidate, (0, 0))

    send_module._cleanup_stale_publish_artifacts(
        tmp_path,
        now=100_000,
        stale_after_seconds=1,
    )

    assert (target / "wgreturn.json").read_text(encoding="utf-8") == "recovered"
    assert not backup.exists()
    assert not temporary.exists()
    assert not reservation.exists()

    completed_backup = tmp_path / ".Speaker.wgreturn.old-complete"
    completed_backup.mkdir()
    os.utime(completed_backup, (0, 0))
    send_module._cleanup_stale_publish_artifacts(
        tmp_path,
        now=100_000,
        stale_after_seconds=1,
    )
    assert not completed_backup.exists()


def test_count_gate_failure_leaves_no_target(send_module, tmp_path, monkeypatch):
    exterior = body("cabinet", faces=[face("LF")])
    root = component("Mismatch", [exterior])

    class ExportManager:
        def createSTEPExportOptions(self, path, geometry=None):
            return path

        def execute(self, path):
            Path(path).write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
            return True

    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=ExportManager(),
        findAttributes=lambda _group, _name: Collection(),
    )
    app = types.SimpleNamespace(version="1", activeDocument=types.SimpleNamespace(name="Mismatch"))
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    with pytest.raises(send_module.wglink_core.WgLinkError, match="count gate"):
        send_module.send(app, {"output_folder": str(tmp_path)})

    assert not (tmp_path / "Mismatch.wgreturn").exists()
    assert not list(tmp_path.glob(".Mismatch.wgreturn.tmp-*"))


def test_visible_excluded_body_refuses_before_step_export(
    send_module, tmp_path, monkeypatch
):
    exterior = body("cabinet", faces=[face("LF")])
    excluded = body("measurement jig", visible=True)
    send_module.declare_body(excluded, "exclude")
    root = component("ExcludedVisible", [exterior, excluded])

    class ExportManager:
        def __init__(self):
            self.create_calls = []
            self.execute_calls = []

        def createSTEPExportOptions(self, path, geometry=None):
            self.create_calls.append((path, geometry))
            return path, geometry

        def execute(self, options):
            self.execute_calls.append(options)
            raise AssertionError("policy must refuse before Fusion STEP export")

    manager = ExportManager()
    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=manager,
        findAttributes=lambda _group, _name: Collection(),
    )
    app = types.SimpleNamespace(
        version="1", activeDocument=types.SimpleNamespace(name="ExcludedVisible")
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    with pytest.raises(
        send_module.wglink_core.WgLinkError,
        match="measurement jig.*still visible.*hide the body.*clear the 'exclude' declaration",
    ):
        send_module.send(app, {"output_folder": str(tmp_path)})

    assert manager.create_calls == []
    assert manager.execute_calls == []
    assert excluded.isVisible is True
    assert not (tmp_path / "ExcludedVisible.wgreturn").exists()


def test_hidden_excluded_body_remains_allowed_without_visibility_mutation(
    send_module, tmp_path, monkeypatch
):
    exterior = body("cabinet", faces=[face("LF")])
    excluded = body("measurement jig", visible=False)
    send_module.declare_body(excluded, "exclude")
    root = component("ExcludedHidden", [exterior, excluded])

    class ExportManager:
        def __init__(self):
            self.execute_calls = []

        def createSTEPExportOptions(self, path, geometry=None):
            return path, geometry

        def execute(self, options):
            self.execute_calls.append(options)
            Path(options[0]).write_text(
                "ISO-10303-21;\n#1=MANIFOLD_SOLID_BREP('',#2);\nEND-ISO-10303-21;\n",
                encoding="utf-8",
            )
            return True

    manager = ExportManager()
    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=manager,
        findAttributes=lambda _group, _name: Collection(),
    )
    app = types.SimpleNamespace(
        version="1", activeDocument=types.SimpleNamespace(name="ExcludedHidden")
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(
        app, {"output_folder": str(tmp_path), "capture_document": False}
    )

    assert Path(report["bundle_path"]).is_dir()
    assert len(manager.execute_calls) == 1
    assert excluded.isVisible is False


def _capture_design(root, *, archive: bool | str = True):
    """A design whose STEP export works and whose archive export is switchable."""

    class ExportManager:
        def createSTEPExportOptions(self, path, geometry=None):
            return path, geometry

        def execute(self, options):
            Path(options[0]).write_text(
                "ISO-10303-21;\n#1=MANIFOLD_SOLID_BREP('',#2);\nEND-ISO-10303-21;\n",
                encoding="utf-8",
            )
            return True

    class ArchivingExportManager(ExportManager):
        def createFusionArchiveExportOptions(self, path):
            if archive == "raise":
                raise RuntimeError("archive export is unavailable")
            return ("archive", path)

        def execute(self, options):
            if options[0] != "archive":
                return super().execute(options)
            if archive == "refuse":
                return False
            Path(options[1]).write_bytes(b"fusion-archive-bytes")
            return True

    manager = ArchivingExportManager() if archive is not False else ExportManager()
    return types.SimpleNamespace(
        rootComponent=root,
        exportManager=manager,
        findAttributes=lambda _group, _name: Collection(),
    )


def _capture_app(name):
    return types.SimpleNamespace(
        version="2704.1.53", activeDocument=types.SimpleNamespace(name=name)
    )


def test_a_return_carries_the_fusion_document_it_came_from(
    send_module, tmp_path, monkeypatch
):
    root = component("Captured", [body("cabinet", faces=[face("LF")])])
    monkeypatch.setattr(
        send_module.wglink_core, "_design", lambda _app: _capture_design(root)
    )

    report = send_module.send(_capture_app("Captured"), {"output_folder": str(tmp_path)})

    bundle = Path(report["bundle_path"])
    document = bundle / "document.f3d"
    assert report["document_captured"] is True
    assert report["document_capture_error"] is None
    assert document.read_bytes() == b"fusion-archive-bytes"
    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (bundle / "wgreturn.json").read_text(encoding="utf-8")
    )
    record = manifest["files"]["document.f3d"]
    assert record["purpose"] == "cad-document"
    assert record["media_type"] == "application/vnd.autodesk.fusion360"
    assert record["sha256"] == "sha256:" + __import__("hashlib").sha256(
        document.read_bytes()
    ).hexdigest()
    # The document is the user's copy, not solver input: the assembly the mesher
    # reads is still the only geometry the return declares.
    assert manifest["assembly"]["file"] == "assembly.step"


def test_the_capture_can_be_declined_and_leaves_the_bundle_as_it_was(
    send_module, tmp_path, monkeypatch
):
    root = component("Declined", [body("cabinet", faces=[face("LF")])])
    monkeypatch.setattr(
        send_module.wglink_core, "_design", lambda _app: _capture_design(root)
    )

    report = send_module.send(
        _capture_app("Declined"),
        {"output_folder": str(tmp_path), "capture_document": False},
    )

    bundle = Path(report["bundle_path"])
    assert report["document_captured"] is False
    assert report["document_capture_error"] is None
    assert not (bundle / "document.f3d").exists()
    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (bundle / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert set(manifest["files"]) == {"assembly.step"}


@pytest.mark.parametrize(
    ("archive", "reason"),
    [
        (False, "this Fusion build has no archive export"),
        ("raise", "archive export is unavailable"),
        ("refuse", "Fusion reported no archive file"),
    ],
)
def test_a_failed_capture_reports_why_instead_of_costing_the_return(
    send_module, tmp_path, monkeypatch, archive, reason
):
    root = component("Degraded", [body("cabinet", faces=[face("LF")])])
    monkeypatch.setattr(
        send_module.wglink_core,
        "_design",
        lambda _app: _capture_design(root, archive=archive),
    )

    report = send_module.send(_capture_app("Degraded"), {"output_folder": str(tmp_path)})

    bundle = Path(report["bundle_path"])
    assert bundle.is_dir()
    assert report["document_captured"] is False
    assert reason in report["document_capture_error"]
    assert not (bundle / "document.f3d").exists()
    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (bundle / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert set(manifest["files"]) == {"assembly.step"}


def test_the_capture_setting_is_wgs_and_is_read_from_wgs_own_settings_file(
    send_module, tmp_path, monkeypatch
):
    """One switch, set in WG, read where the add-in already looks.

    Offering the same choice in both applications is how two settings for one
    thing start disagreeing, so the add-in never stores its own copy.
    """

    workspace = sys.modules["wglink_workspace"]
    data = tmp_path / "WaveguideGenerator"
    data.mkdir()
    monkeypatch.setattr(workspace, "data_dir", lambda **_kwargs: data)

    # No settings file at all, and an unreadable one, both mean capture: an
    # add-in that quietly stopped capturing would be the harder failure to see.
    assert workspace.capture_document() is True
    (data / "cadlink_settings.json").write_text("{not json", encoding="utf-8")
    assert workspace.capture_document() is True

    (data / "cadlink_settings.json").write_text(
        json.dumps({"schemaVersion": 1, "captureDocument": False}), encoding="utf-8"
    )
    assert workspace.capture_document() is False

    (data / "cadlink_settings.json").write_text(
        json.dumps({"schemaVersion": 1, "cadLinkPath": str(tmp_path)}), encoding="utf-8"
    )
    assert workspace.capture_document() is True


# --------------------------------------------------- Fusion's export contract


class ContractExportManager:
    """An ExportManager that enforces what Fusion documents, and nothing less.

    ``ExportManager.createSTEPExportOptions(filename, geometry)`` says of its
    second argument: "The geometry to export. Valid geometry for this is
    currently a Component object." (Autodesk Fusion 360 API Python definitions,
    ``adsk/fusion.py``.) The exporters that came before it -- every fake in this
    file -- accept whatever they are handed, which is why an Occurrence reached
    a real Fusion and came back as

        Fusion STEP export failed for assembly.step: 3 : invlid argument geometry.

    3 is Fusion's invalid-argument code and "invlid" is its own spelling. This
    fake reproduces exactly that, so the contract is a test and not a comment.
    """

    def __init__(self, bodies=1):
        self.geometries = []
        self.bodies = bodies

    def createSTEPExportOptions(self, path, geometry=None):
        # An omitted argument stays legal here because Autodesk documents it as
        # legal. This fake refuses exactly one thing -- a geometry that is not a
        # Component -- so what it proves is the documented contract and not a
        # stricter rule invented to make a test pass.
        kind = None if geometry is None else getattr(geometry, "objectType", None)
        if geometry is not None and kind != "adsk::fusion::Component":
            raise RuntimeError("3 : invlid argument geometry.")
        self.geometries.append(geometry)
        return path, geometry

    def execute(self, options):
        entities = "".join(
            f"#{index}=MANIFOLD_SOLID_BREP('',#{index + 100});\n"
            for index in range(1, self.bodies + 1)
        )
        Path(options[0]).write_text(
            f"ISO-10303-21;\n{entities}END-ISO-10303-21;\n", encoding="utf-8"
        )
        return True


def _contract_design(root, manager=None):
    manager = manager or ContractExportManager()
    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=manager,
        findAttributes=lambda _group, _name: Collection(),
    )
    app = types.SimpleNamespace(
        version="2704.1.53",
        activeDocument=types.SimpleNamespace(name=root.name),
    )
    return design, app, manager


def _occurrence(
    component_value, bodies, *, placement=None, name="Horn:1", children=()
):
    return types.SimpleNamespace(
        name=name,
        fullPathName=f"Speaker/{name}",
        objectType="adsk::fusion::Occurrence",
        component=component_value,
        bRepBodies=Collection(bodies),
        meshBodies=Collection(),
        childOccurrences=Collection(children),
        occurrences=Collection(children),
        transform2=placement or transform(),
        isVisible=True,
        isSuppressed=False,
    )


def _proxy_of(native, *, offset_mm=(0.0, 0.0, 0.0), component_value=None):
    """A body proxy: the same shape, seen in the assembly's coordinates.

    Fusion's own words for the other half of this pair: "The NativeObject is
    the object outside the context of an assembly." The proxy therefore reports
    the body where it *sits* in the assembly, and the native reports it in its
    own component -- which is the frame a Component's STEP export writes. The
    attribute split is the measured one this file already documents: the proxy
    carries none.
    """

    low = native.boundingBox.minPoint
    high = native.boundingBox.maxPoint
    proxy = body(native.name, solid=native.isSolid, faces=list(native.faces))
    proxy.boundingBox = box(
        (
            low.x + offset_mm[0] / 10.0,
            low.y + offset_mm[1] / 10.0,
            low.z + offset_mm[2] / 10.0,
        ),
        (
            high.x + offset_mm[0] / 10.0,
            high.y + offset_mm[1] / 10.0,
            high.z + offset_mm[2] / 10.0,
        ),
    )
    proxy.attributes = Attributes()  # measured Fusion behavior: zero attributes
    proxy.nativeObject = native
    proxy.parentComponent = component_value
    return proxy


def test_root_scope_export_names_the_root_component(send_module, tmp_path, monkeypatch):
    """The optional argument is passed, not omitted.

    Omitting it is documented to mean the root component and the fake accepts
    that, so this is not a bug fix -- it is removing the one call shape no
    Autodesk sample uses from a path that is already hard to observe.
    """

    root = component("Party", [body("shell", faces=[face("HF")])])
    design, app, manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(
        app, {"output_folder": str(tmp_path), "capture_document": False}
    )

    assert manager.geometries == [root]
    assert Path(report["bundle_path"]).is_dir()


def test_selected_occurrence_exports_its_component_not_the_occurrence(
    send_module, tmp_path, monkeypatch
):
    """The reported failure, reproduced and fixed in one test.

    Before the fix this raised ``3 : invlid argument geometry`` from the fake
    above, exactly as the user's Fusion did.
    """

    native = body("Horn", faces=[face("HF")])
    inner = component("Horn component", [native])
    proxy = body("Horn", faces=[face("HF")])
    proxy.nativeObject = native
    proxy.parentComponent = inner
    occurrence = _occurrence(inner, [proxy])
    root = component("Speaker")
    root.occurrences = Collection([occurrence])
    design, app, manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(
        app,
        {
            "output_folder": str(tmp_path),
            "capture_document": False,
            "selection": occurrence,
        },
    )

    assert manager.geometries == [inner]
    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert manifest["scope"]["selection"] == "Speaker/Horn:1"


def test_export_step_refuses_a_non_component_before_calling_fusion(send_module):
    calls = []

    class Watchful(ContractExportManager):
        def createSTEPExportOptions(self, path, geometry=None):
            calls.append(geometry)
            return super().createSTEPExportOptions(path, geometry)

    manager = Watchful()
    design = types.SimpleNamespace(exportManager=manager)
    occurrence = _occurrence(component("Horn component"), [])

    with pytest.raises(
        send_module.wglink_core.WgLinkError, match="Occurrence.*takes a Component"
    ):
        send_module._export_step(design, Path("assembly.step"), occurrence)

    assert calls == []


def _placed_horn(monkeypatch, send_module, *, children=()):
    """One wrapper occurrence, moved 120 mm along +Y in the assembly.

    This is WGLink's own documented workflow -- Insert puts a wrapper
    occurrence in the document, the user moves and joints *that*, and then
    returns it -- so it is the placement an ordinary return actually has.
    """

    native = body("Horn", faces=[face("HF")])
    native.boundingBox = box((-3.0, 0.0, 0.0), (3.0, 4.0, 6.0))
    inner = component("Horn component", [native])
    proxy = _proxy_of(native, offset_mm=(0.0, 120.0, 0.0), component_value=inner)
    occurrence = _occurrence(
        inner, [proxy], placement=moved_transform(y_cm=12.0), children=children
    )
    root = component("Speaker")
    root.occurrences = Collection([occurrence])
    root.allOccurrences = Collection([occurrence])
    design, app, manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    return occurrence, native, inner, design, app, manager


def test_a_placed_occurrence_exports_its_component_frame_and_says_which_frame(
    send_module, tmp_path, monkeypatch
):
    """The ordinary moved wrapper is supported, and the file says what it is.

    Fusion exports a Component in the component's OWN coordinates and offers no
    way to export one in its assembly placement. Nothing here composes a
    transform to paper over that: every coordinate written is read from the
    native object, which Fusion defines as the body "outside the context of an
    assembly" -- the same frame the STEP is in.
    """

    occurrence, native, inner, _design, app, manager = _placed_horn(
        monkeypatch, send_module
    )

    report = send_module.send(
        app,
        {
            "output_folder": str(tmp_path),
            "capture_document": False,
            "selection": occurrence,
        },
    )

    assert manager.geometries == [inner]
    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert manifest["coordinate_system"]["export_frame"] == "selected-occurrence-component"
    # The native box, not the proxy's 120 mm higher one: the file is the
    # component, so its bounding box is the component's.
    assert manifest["assembly"]["bbox_mm"] == [[-30.0, 0.0, 0.0], [30.0, 40.0, 60.0]]


def test_the_root_scope_still_names_the_root_frame(send_module, tmp_path, monkeypatch):
    root = component("Party", [body("shell", faces=[face("HF")])])
    design, app, _manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(
        app, {"output_folder": str(tmp_path), "capture_document": False}
    )

    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert manifest["coordinate_system"]["export_frame"] == "root-component"


def test_a_placed_occurrence_with_sub_assemblies_is_refused_with_a_real_remedy(
    send_module, tmp_path, monkeypatch
):
    """The one shape that cannot be read natively is the one that is refused.

    A child occurrence's bodies are native to *its* component, so reaching the
    exported frame from there means composing the placement chain -- the
    arithmetic ``_strict_assembly_from_link`` refuses for the same reason. The
    remedy has to be something that actually moves the occurrence: Fusion's
    Ground freezes an occurrence where it already is and never returns it to
    the origin, so it must not be offered here.
    """

    child_native = body("Bracket")
    child_component = component("Bracket component", [child_native])
    child = _occurrence(
        child_component,
        [_proxy_of(child_native, component_value=child_component)],
        name="Bracket:1",
    )
    occurrence, _native, _inner, _design, app, manager = _placed_horn(
        monkeypatch, send_module, children=[child]
    )

    with pytest.raises(
        send_module.wglink_core.WgLinkError,
        match="placed away from the assembly origin and contains sub-assemblies",
    ) as refusal:
        send_module.send(
            app,
            {
                "output_folder": str(tmp_path),
                "capture_document": False,
                "selection": occurrence,
            },
        )

    message = str(refusal.value)
    assert "Leave Assembly scope empty" in message
    assert "move the occurrence back onto the assembly origin" in message
    assert "ground" not in message.casefold()
    assert manager.geometries == []
    assert not list(tmp_path.iterdir())


# ------------------------------------------------------- declared pre-cut domain


def half_body(name, *, low=(-40.0, 0.0, 0.0), high=(40.0, 90.0, 120.0), faces=()):
    """A body whose bounding box is a half about y = 0, in millimetres.

    ``_bbox_values`` reads Fusion's internal centimetres and multiplies by ten,
    so the fixture is written in millimetres and divided back down here -- the
    same direction the real code travels.
    """

    candidate = body(name, solid=True, faces=list(faces))
    candidate.boundingBox = box(
        tuple(value / 10.0 for value in low), tuple(value / 10.0 for value in high)
    )
    return candidate


def test_a_declared_half_is_measured_and_recorded_with_its_feature(
    send_module, tmp_path, monkeypatch
):
    shell = half_body("PartyMEH", faces=[face("HF")])
    root = component("PartyMEH", [shell])
    design, app, _manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(
        app,
        {
            "output_folder": str(tmp_path),
            "capture_document": False,
            "domain": ["y0"],
        },
    )

    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    domain = manifest["assembly"]["domain"]
    assert domain["kind"] == "half"
    assert domain["cut_planes"] == ["y0"]
    assert domain["declared_by"] == "cad-author"
    assert domain["evidence"]["y0"]["min_mm"] == pytest.approx(0.0)
    assert domain["evidence"]["y0"]["max_mm"] == pytest.approx(90.0)
    # The gate an older reader trips on rather than solving a half as a full
    # model.
    assert "reduced-domain-v1" in manifest["required_features"]


def test_a_full_model_declaring_a_half_is_refused_with_the_measurement(
    send_module, tmp_path, monkeypatch
):
    shell = half_body("Whole", low=(-40.0, -90.0, 0.0), high=(40.0, 90.0, 120.0),
                      faces=[face("HF")])
    root = component("Whole", [shell])
    design, app, manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    with pytest.raises(
        send_module.wglink_core.WgLinkError,
        match=r"declared a reduced domain about y = 0.*reach 90 mm onto the negative side",
    ):
        send_module.send(
            app,
            {
                "output_folder": str(tmp_path),
                "capture_document": False,
                "domain": "y0",
            },
        )

    assert manager.geometries == []
    assert not list(tmp_path.iterdir())


def test_a_half_kept_on_the_wrong_side_is_refused_rather_than_mirrored(
    send_module, tmp_path, monkeypatch
):
    shell = half_body("Mirrored", low=(-40.0, -90.0, 0.0), high=(40.0, 0.0, 120.0),
                      faces=[face("HF")])
    root = component("Mirrored", [shell])
    design, app, _manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    with pytest.raises(
        send_module.wglink_core.WgLinkError,
        match="no extent on the positive side",
    ):
        send_module.send(
            app,
            {
                "output_folder": str(tmp_path),
                "capture_document": False,
                "domain": ["y0"],
            },
        )


# --- a linked model that was really cut before it was sent ------------------
#
# The add-in refused these before the fix: `_throat_faces` measured every
# candidate against the FULL throat disc, on both the geometric and the painted
# branch, so a linked source that a declared cut had genuinely halved matched
# nothing and the export stopped at "resolved to 0 faces". WG's own ingest
# accepted the same reduced return, so the two sides disagreed about a model the
# user is being told to build.

DISC_DIAMETER_MM = 25.4
FULL_DISC_MM2 = math.pi * DISC_DIAMETER_MM * DISC_DIAMETER_MM / 4.0


def _linked_attribute(owner, name, value):
    holder = types.SimpleNamespace(name=name, value=value, parent=owner)
    return holder


def _linked_cut_design(
    send_module,
    *,
    retained_fraction,
    low,
    high,
    instance_id="wgi-cut",
    throat_normal=(0.0, 0.0, 1.0),
    throat_origin_cm=(0.0, 0.0, 0.0),
):
    """A linked wrapper whose managed body carries a really-reduced throat face.

    The face's area is the share of the disc the declared cut leaves, which is
    what a user who cut the model in CAD actually has. Everything else -- the
    stored contract, the diameter parameter, the datums -- still describes the
    whole disc, because the contract records the design's throat and not what
    survived of it.
    """

    throat = face("HF", area=FULL_DISC_MM2 * retained_fraction / 100.0)
    shell = half_body("PartyMEH", low=low, high=high, faces=[throat])
    root = component("PartyMEH", [shell])
    core = send_module.wglink_core
    core._set_attribute(shell, "instance_id", instance_id)
    core._set_attribute(shell, "role", "waveguide")
    core._set_attribute(shell, "face_role", "HF")
    root.constructionPlanes = Collection([
        types.SimpleNamespace(
            name="WG_THROAT_PLANE",
            geometry=types.SimpleNamespace(
                origin=point(*throat_origin_cm), normal=point(*throat_normal)
            ),
        )
    ])
    root.constructionAxes = Collection([
        types.SimpleNamespace(
            name="WG_AXIS",
            geometry=types.SimpleNamespace(
                origin=point(*throat_origin_cm), direction=point(0.0, 0.0, 1.0)
            ),
        )
    ])
    payload = {
        "instance_id": instance_id,
        "design_id": "design-1",
        "export_id": "export-1",
        "export_sequence": "1",
        "build_mode": "freestanding",
        "parameter_prefix": "wg_",
        "source_role": "HF",
        "expected_throat_area_mm2": f"{FULL_DISC_MM2:.6f}",
        "throat_z_mm": "0",
        "wrapper": "root",
    }
    attributes = [
        _linked_attribute(root, "link_payload", json.dumps(payload)),
        *[
            _linked_attribute(shell, name, str(value))
            for name, value in shell.attributes.values.items()
        ],
    ]
    for attribute in attributes:
        if isinstance(attribute.name, tuple):
            attribute.name = attribute.name[1]
    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=ContractExportManager(),
        findAttributes=lambda _group, _name: Collection(attributes),
        userParameters=Collection([
            types.SimpleNamespace(name="wg_throat_dia", value=DISC_DIAMETER_MM / 10.0)
        ]),
    )
    app = types.SimpleNamespace(
        version="2704.1.53",
        activeDocument=types.SimpleNamespace(name="PartyMEH"),
    )
    return design, app


def _send_linked_cut(send_module, tmp_path, monkeypatch, *, fraction, low, high, domain):
    design, app = _linked_cut_design(
        send_module, retained_fraction=fraction, low=low, high=high
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    return send_module.send(
        app,
        {
            "output_folder": str(tmp_path),
            "capture_document": False,
            "overwrite": True,
            "domain": domain,
        },
    )


@pytest.mark.parametrize(
    "fraction, domain, low",
    [
        (0.5, ["y0"], (-40.0, 0.0, 0.0)),
        (0.25, ["x0", "y0"], (0.0, 0.0, 0.0)),
    ],
    ids=["declared-half", "declared-quarter"],
)
def test_a_linked_source_cut_by_the_declared_domain_still_exports(
    send_module, tmp_path, monkeypatch, fraction, domain, low
):
    """The release blocker, at the real entry point.

    Before the fix this raised "required throat source resolved to 0 faces" out
    of `send()` -- the whole export, not a parser -- because the half face was
    measured against the whole disc.
    """

    report = _send_linked_cut(
        send_module,
        tmp_path,
        monkeypatch,
        fraction=fraction,
        low=low,
        high=(40.0, 90.0, 120.0),
        domain=domain,
    )

    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert manifest["assembly"]["domain"]["cut_planes"] == sorted(domain)
    source = next(item for item in manifest["sources"] if item["role"] == "HF")
    # The retained area is recorded as observed, because that is what is there.
    assert source["observed"]["total_area_mm2"] == pytest.approx(
        FULL_DISC_MM2 * fraction, rel=1e-6
    )
    assert source["instance_id"] == "wgi-cut"
    # The contract still describes the WHOLE disc: it records the design's
    # throat, and the reader derives the same reduction from it independently.
    contract = manifest["instances"][0]["source_contract"]
    assert contract["expected_disc_area_mm2"] == pytest.approx(FULL_DISC_MM2, rel=1e-6)
    assert contract["throat_diameter_mm"] == pytest.approx(DISC_DIAMETER_MM, rel=1e-6)


def test_the_full_disc_is_still_required_when_nothing_was_declared(
    send_module, tmp_path, monkeypatch
):
    """The reduction is not a loosened tolerance: undeclared still means whole."""

    with pytest.raises(
        send_module.wglink_core.WgLinkError, match="resolved to 0 faces"
    ):
        _send_linked_cut(
            send_module,
            tmp_path,
            monkeypatch,
            fraction=0.5,
            low=(-40.0, 0.0, 0.0),
            high=(40.0, 90.0, 120.0),
            domain=None,
        )


def test_a_declared_half_does_not_accept_a_source_the_cut_never_reached(
    send_module, tmp_path, monkeypatch
):
    """An untouched offset source keeps its whole disc, as WG's reader does.

    A pair of drivers mirrored about y = 0, cut to keep one, leaves a source
    whose disc is entirely still there. Halving every source on sight would
    refuse that legitimate model; here the declared plane misses the disc, so
    the full area is still what is demanded -- and a half-area face fails.
    """

    design, app = _linked_cut_design(
        send_module,
        retained_fraction=0.5,
        low=(-40.0, 0.0, 0.0),
        high=(40.0, 90.0, 120.0),
        throat_origin_cm=(0.0, 6.0, 0.0),
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    with pytest.raises(
        send_module.wglink_core.WgLinkError, match="resolved to 0 faces"
    ):
        send_module.send(
            app,
            {
                "output_folder": str(tmp_path),
                "capture_document": False,
                "domain": ["y0"],
            },
        )


def test_an_off_centre_declared_cut_is_refused_with_its_measurement(
    send_module, tmp_path, monkeypatch
):
    """A plane that clips a disc off-centre leaves a segment of no known area."""

    design, app = _linked_cut_design(
        send_module,
        retained_fraction=0.5,
        low=(-40.0, 0.0, 0.0),
        high=(40.0, 90.0, 120.0),
        throat_origin_cm=(0.0, -0.5, 0.0),
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    with pytest.raises(send_module.wglink_core.WgLinkError, match="off its centre"):
        send_module.send(
            app,
            {
                "output_folder": str(tmp_path),
                "capture_document": False,
                "domain": ["y0"],
            },
        )


def test_a_throat_axis_askew_to_its_own_plane_refuses_a_declared_cut(
    send_module, tmp_path, monkeypatch
):
    design, app = _linked_cut_design(
        send_module,
        retained_fraction=0.5,
        low=(-40.0, 0.0, 0.0),
        high=(40.0, 90.0, 120.0),
        throat_normal=(0.0, 1.0, 1.0),
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    with pytest.raises(
        send_module.wglink_core.WgLinkError, match=r"not\s+perpendicular"
    ):
        send_module.send(
            app,
            {
                "output_folder": str(tmp_path),
                "capture_document": False,
                "domain": ["y0"],
            },
        )


def test_an_undeclared_return_carries_no_domain_and_no_feature(
    send_module, tmp_path, monkeypatch
):
    shell = half_body("Undeclared", faces=[face("HF")])
    root = component("Undeclared", [shell])
    design, app, _manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(
        app, {"output_folder": str(tmp_path), "capture_document": False}
    )

    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert "domain" not in manifest["assembly"]
    assert "reduced-domain-v1" not in manifest["required_features"]
    assert report["domain"] is None


def test_a_quarter_declares_both_planes_in_a_fixed_order(send_module):
    assert send_module.resolve_domain_planes("y0+x0") == ("x0", "y0")
    assert send_module.resolve_domain_planes(["y0", "x0"]) == ("x0", "y0")
    assert send_module.resolve_domain_planes("full") == ()
    assert send_module.resolve_domain_planes(None) == ()
    with pytest.raises(send_module.wglink_core.WgLinkError, match="x0, y0"):
        send_module.resolve_domain_planes("z0")


def test_the_preflight_previews_a_domain_refusal_instead_of_raising(
    send_module, monkeypatch
):
    shell = half_body("Whole", low=(-40.0, -90.0, 0.0), high=(40.0, 90.0, 120.0),
                      faces=[face("HF")])
    root = component("Whole", [shell])
    design, app, _manager = _contract_design(root)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.preflight_scope(app, {"domain": ["y0"]})

    assert report["domain"] is None
    assert "negative side" in report["domain_error"]


def test_a_declared_domain_changes_no_body_inventory_and_no_helper_verdict(
    send_module, tmp_path, monkeypatch
):
    """Declaring a half says what the model IS; it never changes what is sent.

    If a declaration could move a body between included and skipped, or change
    which faces drive, it would be a way to alter an export by relabelling it.
    Both halves of that are checked: the inventory is identical with and
    without the declaration, and a painted helper -- which is a terminal
    refusal on its own -- stays a refusal rather than becoming a source.
    """

    design, final, _stitched = _freestanding_insertion(send_module)
    final.boundingBox = box((-3.0, 0.0, 0.0), (3.0, 4.0, 6.0))
    design.exportManager = ContractExportManager()
    app = types.SimpleNamespace(
        version="2704.1.53",
        activeDocument=types.SimpleNamespace(name="Helper"),
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    def manifest_for(**extra):
        report = send_module.send(
            app,
            {
                "output_folder": str(tmp_path),
                "capture_document": False,
                "overwrite": True,
                **extra,
            },
        )
        return sys.modules["wglink_return"].loads_return_manifest(
            (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
        )

    full = manifest_for()
    declared = manifest_for(domain=["y0"])

    assert declared["assembly"]["domain"]["cut_planes"] == ["y0"]
    assert "domain" not in full["assembly"]
    assert declared["scope"] == full["scope"]
    assert (
        declared["assembly"]["n_bodies_expected"]
        == full["assembly"]["n_bodies_expected"]
    )
    assert declared["sources"] == full["sources"]
    assert [source["role"] for source in full["sources"]] == ["HF"]
    assert "wglink_helper" in {
        record["kind"] for record in full["scope"]["skipped"]
    }

    # The same document with a LOST source on the helper refuses either way: a
    # face WGLink would skip cannot also be the only carrier of a source, and a
    # domain declaration is not a way around that. The paint is LF, a role the
    # final body does not carry -- an HF helper duplicates the final body's own
    # throat patch on every throat-opened model, which is not a loss and no
    # longer refuses (see
    # test_a_helper_repeating_the_final_bodys_own_source_does_not_refuse).
    painted, painted_final, _helper = _freestanding_insertion(
        send_module, instance_id="wgi-painted", helper_faces=[face("LF", area=9.0)]
    )
    painted_final.boundingBox = box((-3.0, 0.0, 0.0), (3.0, 4.0, 6.0))
    painted.exportManager = ContractExportManager()
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: painted)
    for options in ({}, {"domain": ["y0"]}):
        with pytest.raises(
            send_module.wglink_core.WgLinkError,
            match="cannot skip .* selector candidates for a required source",
        ):
            send_module.send(
                app,
                {
                    "output_folder": str(tmp_path),
                    "capture_document": False,
                    "overwrite": True,
                    **options,
                },
            )


def test_a_child_occurrence_is_measured_where_it_sits_not_in_its_own_component(
    send_module, tmp_path, monkeypatch
):
    """The frame a nested body is measured in follows the placement, not the scope.

    A body proxy reports the root document frame; a native reports its own
    component's. When the selected occurrence sits at the origin those are the
    same frame, so the proxies are right for everything -- including a child
    occurrence that has been moved, whose native is in the *child's* frame and
    would put the bounding box and the declared-domain measurement somewhere
    the exported STEP does not have geometry.

    The child here is moved 200 mm along +Y inside its parent. Measured
    natively it would look like it sits at the origin; measured where it sits
    it reaches 240 mm.
    """

    parent_native = body("Horn")
    parent_native.boundingBox = box((-3.0, 0.0, 0.0), (3.0, 4.0, 6.0))
    parent_component = component("Horn component", [parent_native])
    parent_proxy = _proxy_of(parent_native, component_value=parent_component)

    child_native = body("Mouth ring", faces=[face("HF")])
    child_native.boundingBox = box((-2.0, 0.0, 0.0), (2.0, 4.0, 1.0))
    child_component = component("Ring component", [child_native])
    child = _occurrence(
        child_component,
        [_proxy_of(child_native, offset_mm=(0.0, 200.0, 0.0), component_value=child_component)],
        placement=moved_transform(y_cm=20.0),
        name="Ring:1",
    )

    occurrence = _occurrence(
        parent_component, [parent_proxy], children=[child], name="Horn:1"
    )
    root = component("Speaker")
    root.occurrences = Collection([occurrence])
    root.allOccurrences = Collection([occurrence])
    design, app, manager = _contract_design(root, ContractExportManager(bodies=2))
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)

    report = send_module.send(
        app,
        {
            "output_folder": str(tmp_path),
            "capture_document": False,
            "selection": occurrence,
            "domain": ["y0"],
        },
    )

    assert manager.geometries == [parent_component]
    manifest = sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    # 240 mm, where the ring actually is inside the exported component -- not
    # the 40 mm its own component would report.
    assert manifest["assembly"]["bbox_mm"] == [[-30.0, 0.0, 0.0], [30.0, 240.0, 60.0]]
    assert manifest["assembly"]["domain"]["evidence"]["y0"]["max_mm"] == 240.0
    assert manifest["coordinate_system"]["export_frame"] == "selected-occurrence-component"


def test_a_moved_parent_holding_sub_assemblies_is_refused_in_the_walk_too(
    send_module, monkeypatch
):
    """The invariant the measurement relies on is enforced where it is used.

    ``_selection`` refuses this pair, so the walk can only reach it if the
    document changed under an open dialog. It refuses again rather than
    measuring a child's native geometry in the wrong frame.
    """

    child_native = body("Bracket")
    child_component = component("Bracket component", [child_native])
    child = _occurrence(
        child_component,
        [_proxy_of(child_native, component_value=child_component)],
        name="Bracket:1",
    )
    parent_native = body("Horn", faces=[face("HF")])
    parent_component = component("Horn component", [parent_native])
    occurrence = _occurrence(
        parent_component,
        [_proxy_of(parent_native, offset_mm=(0.0, 120.0, 0.0), component_value=parent_component)],
        placement=moved_transform(y_cm=12.0),
        children=[child],
    )
    design = types.SimpleNamespace(rootComponent=component("Speaker"))

    # Bypass _selection the way a mid-dialog document change would, and confirm
    # the walk still refuses rather than mis-measuring.
    monkeypatch.setattr(
        send_module,
        "_selection",
        lambda _design, _value: (
            {"kind": "occurrence", "path": "Speaker/Horn:1"},
            parent_component,
            occurrence,
            send_module.OCCURRENCE_EXPORT_FRAME,
        ),
    )
    with pytest.raises(
        send_module.wglink_core.WgLinkError,
        match="contains sub-assemblies; its bodies cannot be measured",
    ):
        send_module._scope_walk(design, occurrence)
