"""Exercise the read-only Fusion boundary with small fake API objects."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
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


class Attributes:
    def __init__(self):
        self.values = {}

    def itemByName(self, group, name):
        value = self.values.get((group, name))
        return types.SimpleNamespace(value=value) if value is not None else None

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
