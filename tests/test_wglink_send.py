"""Exercise the read-only Fusion boundary with small fake API objects."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import json
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


def component(name, bodies=()):
    value = types.SimpleNamespace(
        name=name,
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
