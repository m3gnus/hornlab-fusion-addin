"""CAD-authored source identity (``source-identity-v1``) at the add-in boundary.

WG's contract (Waveguide Generator ``docs/reference/MULTI-INSTANCE-CAD-IDENTITY.md``,
"Cross-export source identity") gives ``sources[].id`` a stronger meaning when a
return requires the feature: each id is the identity of a logical source,
authored in CAD, the same in every later export, and a missing, split or
ambiguous mapping is refused by the writer -- never guessed. These tests hold
the add-in to that with small fakes; what Fusion itself does to an attribute
when a face is split or copied is live-Fusion evidence, not something a fake
can prove.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import types

import pytest

from test_wglink_send import (  # noqa: F401 - send_module is a fixture
    Attribute,
    Attributes,
    Collection,
    ContractExportManager,
    box,
    component,
    send_module,
)


GROUP = "WGLink"
STAMP = "source_identity"


# ----------------------------------------------------------------- fakes


def stamped_face(role, area=2.0, edges=()):
    """A face that can carry attributes, as a native Fusion BRepFace can."""

    return types.SimpleNamespace(
        area=area,
        appearance=None if role is None else types.SimpleNamespace(name=role),
        edges=Collection(edges),
        boundingBox=box((0, 0, 0), (1, 1, 0)),
        attributes=Attributes(),
        objectType="adsk::fusion::BRepFace",
    )


def proxy_of(native):
    """An occurrence proxy of ``native``: its own wrapper, no attributes."""

    return types.SimpleNamespace(
        area=native.area,
        appearance=native.appearance,
        edges=native.edges,
        boundingBox=native.boundingBox,
        attributes=Attributes(),
        nativeObject=native,
        objectType="adsk::fusion::BRepFace",
    )


def solid(name, faces):
    return types.SimpleNamespace(
        name=name,
        isSolid=True,
        isVisible=True,
        faces=Collection(faces),
        attributes=Attributes(),
        boundingBox=box(),
        volume=1.0,
        entityToken=f"token-{name}",
        objectType="adsk::fusion::BRepBody",
    )


class _Found:
    """What ``Design.findAttributes`` hands back: name, value, parent, deleteMe."""

    def __init__(self, parent, key):
        self._handle = Attribute(parent.attributes, key)
        self.groupName, self.name = key
        self.parent = parent

    @property
    def value(self):
        return self._handle.value

    @value.setter
    def value(self, value):
        self._handle.value = value

    def deleteMe(self):
        return self._handle.deleteMe()


class FakeDesign(types.SimpleNamespace):
    """A design whose attribute search walks every entity it was told about."""

    def __init__(self, root, entities=()):
        super().__init__(
            rootComponent=root,
            exportManager=ContractExportManager(),
        )
        self.entities = list(entities)

    def findAttributes(self, group, name):
        found = []
        for entity in self.entities:
            for key in list(entity.attributes.values):
                if key[0] == group and (not name or key[1] == name):
                    found.append(_Found(entity, key))
        return Collection(found)


def stamp_of(face_value):
    raw = face_value.attributes.values.get((GROUP, STAMP))
    return None if raw is None else json.loads(raw)


def put_stamp(faces, role, identity=None, count=None):
    """Stamp faces the way Set WG Source... does, without going through it."""

    identity = identity or "wgs-" + ("T" + role.replace("_", ""))[:20].ljust(20, "0")
    for index, face_value in enumerate(faces):
        face_value.attributes.add(GROUP, STAMP, json.dumps({
            "schema": 1,
            "id": identity,
            "role": role,
            "face": f"{identity}-{index}-{id(face_value)}",
            "faces": len(faces) if count is None else count,
        }))
    return identity


def _app(name="Identity"):
    return types.SimpleNamespace(
        version="2704.1.53", activeDocument=types.SimpleNamespace(name=name)
    )


def _painted_document(send_module, monkeypatch, faces):
    cabinet = solid("cabinet", faces)
    root = component("Identity", [cabinet])
    design = FakeDesign(root, faces)
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    return design


def _send(send_module, tmp_path, *, identity=True, request="r"):
    options = {
        "output_folder": str(tmp_path),
        "capture_document": False,
        "request_id": request,
    }
    if identity:
        options["source_identity"] = True
    report = send_module.send(_app(), options)
    return json.loads(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )


# ------------------------------------------------------------ capability


@pytest.mark.parametrize(
    "payload, expected",
    [
        (None, False),
        ("not json", False),
        ({"schemaVersion": 1}, False),
        ({"schemaVersion": 2, "sourceIdentity": 1}, False),
        ({"schemaVersion": True, "sourceIdentity": 1}, False),
        ({"schemaVersion": 1, "sourceIdentity": True}, False),
        ({"schemaVersion": 1, "sourceIdentity": 0}, False),
        ({"schemaVersion": 1, "sourceIdentity": "1"}, False),
        ({"schemaVersion": 1, "sourceIdentity": 1.0}, False),
        ({"schemaVersion": 1, "sourceIdentity": 1}, True),
        ({"schemaVersion": 1, "sourceIdentity": 2, "later": "ignored"}, True),
    ],
)
def test_source_identity_is_declared_only_when_wg_advertises_an_integer(
    tmp_path, payload, expected
):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"))
    try:
        import wglink_watch
    finally:
        sys.path.pop(0)
    target = tmp_path / wglink_watch.CAPABILITIES_FILENAME
    if isinstance(payload, str):
        target.write_text(payload, encoding="utf-8")
    elif payload is not None:
        target.write_text(json.dumps(payload), encoding="utf-8")

    assert wglink_watch.wg_source_identity(tmp_path) is expected


# --------------------------------------------------------- legacy is unchanged


def test_without_the_capability_the_return_is_byte_identical_to_legacy(
    send_module, tmp_path, monkeypatch
):
    """Stamps present or not, an undeclared return keeps legacy ids and features."""

    hf = stamped_face("HF", area=2.5)
    _painted_document(send_module, monkeypatch, [hf])
    legacy = _send(send_module, tmp_path / "a", identity=False)
    state_before = send_module.return_state(_app(), {})

    put_stamp([hf], "HF")
    stamped = _send(send_module, tmp_path / "b", identity=False)
    state_after = send_module.return_state(_app(), {})

    assert legacy["sources"][0]["id"] == "source-hf"
    assert "source-identity-v1" not in legacy["required_features"]
    for key in ("required_features", "sources", "instances", "scope"):
        assert stamped[key] == legacy[key]
    assert state_after["hash"] == state_before["hash"]


# ------------------------------------------------------------ painted sources


def test_a_stamped_painted_source_exports_one_stable_identity(
    send_module, tmp_path, monkeypatch
):
    shared = object()
    a, b = stamped_face("HF", 2.0, [shared]), stamped_face("HF", 3.0, [shared])
    _painted_document(send_module, monkeypatch, [a, b])
    put_stamp([a, b], "HF")

    first = _send(send_module, tmp_path, request="one")
    second = _send(send_module, tmp_path, request="two")

    assert "source-identity-v1" in first["required_features"]
    (source,) = first["sources"]
    assert source["id"].startswith("wgs-")
    assert len(source["id"].encode("utf-8")) <= 25
    assert second["sources"][0]["id"] == source["id"]
    # Only the id changes meaning; the rest of the source is what it was.
    assert source["default_drive_channel_id"] == "drive-hf"
    assert source["selectors"] == {"appearance_labels": ["HF"]}
    # The face stamps stay CAD-private: neither a face nonce nor the stamp
    # itself reaches the portable manifest (body object ids were already there).
    text = json.dumps(first)
    assert stamp_of(a)["face"] not in text and stamp_of(b)["face"] not in text
    assert "source_identity" not in text and '"faces"' not in text


def test_an_unstamped_painted_source_is_refused_naming_the_remedy(
    send_module, tmp_path, monkeypatch
):
    _painted_document(send_module, monkeypatch, [stamped_face("LF")])

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        _send(send_module, tmp_path)

    text = str(refusal.value)
    assert "LF" in text and "Set WG Source" in text
    assert not list(tmp_path.glob("*.wgreturn"))
    assert not list(tmp_path.glob(".*tmp-*"))


def test_a_legacy_port_exit_face_is_refused_under_its_current_role_name(
    send_module, tmp_path, monkeypatch
):
    _painted_document(send_module, monkeypatch, [stamped_face("PORT_EXIT")])

    with pytest.raises(send_module.wglink_core.WgLinkError, match="PASSIVE_CARDIOID"):
        _send(send_module, tmp_path)


def test_a_face_repainted_by_hand_no_longer_counts_as_its_old_source(
    send_module, tmp_path, monkeypatch
):
    face_value = stamped_face("HF")
    _painted_document(send_module, monkeypatch, [face_value])
    put_stamp([face_value], "HF")
    face_value.appearance = types.SimpleNamespace(name="MF")

    with pytest.raises(send_module.wglink_core.WgLinkError, match="MF"):
        _send(send_module, tmp_path)


def test_two_identities_under_one_role_are_refused_as_ambiguous(
    send_module, tmp_path, monkeypatch
):
    a, b = stamped_face("HF"), stamped_face("HF")
    _painted_document(send_module, monkeypatch, [a, b])
    put_stamp([a], "HF")
    # A stamp from somewhere else: the same role, another identity.
    other = dict(stamp_of(a), id="wgs-0000000000000000000Z", face="elsewhere")
    b.attributes.add(GROUP, STAMP, json.dumps(other))

    with pytest.raises(send_module.wglink_core.WgLinkError, match="ambiguous"):
        _send(send_module, tmp_path)


def test_a_split_or_copied_face_is_refused(send_module, tmp_path, monkeypatch):
    """Fusion copies an attribute onto both halves of a split face and onto a
    pasted copy, so two distinct faces carry one face's stamp."""

    original = stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [original])
    put_stamp([original], "HF")
    half = stamped_face("HF")
    half.attributes.values = dict(original.attributes.values)
    design.rootComponent.bRepBodies[0].faces.append(half)
    design.entities.append(half)

    with pytest.raises(send_module.wglink_core.WgLinkError, match="split or copied"):
        _send(send_module, tmp_path)


def test_a_removed_face_of_a_multi_face_source_is_refused(
    send_module, tmp_path, monkeypatch
):
    a, b = stamped_face("HF"), stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [a, b])
    put_stamp([a, b], "HF")
    design.rootComponent.bRepBodies[0].faces.remove(b)
    design.entities.remove(b)

    with pytest.raises(send_module.wglink_core.WgLinkError, match="removed"):
        _send(send_module, tmp_path)


def test_a_source_whose_faces_are_all_gone_simply_leaves_the_return(
    send_module, tmp_path, monkeypatch
):
    """Decision P3: no registry. The source is absent and WG sees the change."""

    hf, lf = stamped_face("HF"), stamped_face("LF")
    design = _painted_document(send_module, monkeypatch, [hf, lf])
    put_stamp([hf], "HF")
    put_stamp([lf], "LF")
    design.rootComponent.bRepBodies[0].faces.remove(hf)
    design.entities.remove(hf)

    manifest = _send(send_module, tmp_path)

    assert [source["role"] for source in manifest["sources"]] == ["LF"]


def test_one_face_placed_twice_by_one_component_stays_one_source(
    send_module, tmp_path, monkeypatch
):
    """Decision P4: two proxies of one native face are one authored face."""

    native = stamped_face("HF")
    _painted_document(send_module, monkeypatch, [native])
    put_stamp([native], "HF")
    first, second = proxy_of(native), proxy_of(native)

    sources = send_module._sources([], [solid("left", [first]), solid("right", [second])], source_identity=True)

    assert len(sources) == 1
    assert sources[0]["id"] == stamp_of(native)["id"]


# -------------------------------------------------------------- linked throats


def _throat_record(send_module, instance_id):
    throat = stamped_face("HF", area=5.06707)
    managed = solid(f"horn-{instance_id}", [throat])
    return {
        "instance_id": instance_id,
        "body": managed,
        "payload": {
            "source_role": "HF",
            "expected_throat_area_mm2": "506.707",
            "throat_z_mm": "0",
        },
    }, managed


def test_a_linked_throat_identity_follows_its_instance_and_nothing_else(send_module):
    record_a, body_a = _throat_record(send_module, "3f1c7a52-0d6b-4d0e-9d7e-5b4b1c1e2a10")
    record_b, body_b = _throat_record(send_module, "9a0e1b44-7c2d-4f5a-8e61-2d3c4b5a6f70")

    forward = send_module._sources([record_a, record_b], [body_a, body_b], source_identity=True)
    backward = send_module._sources([record_b, record_a], [body_b, body_a], source_identity=True)

    ids = {source["instance_id"]: source["id"] for source in forward}
    assert ids == {source["instance_id"]: source["id"] for source in backward}
    assert len(set(ids.values())) == 2
    assert all(value.startswith("wgs-") and len(value) <= 25 for value in ids.values())
    # Nothing was written to the document to get it.
    assert body_a.faces[0].attributes.values == {}


def test_a_duplicated_link_identity_is_refused_not_chosen(send_module):
    record_a, body_a = _throat_record(send_module, "same-instance")
    record_b, body_b = _throat_record(send_module, "same-instance")

    with pytest.raises(send_module.wglink_core.WgLinkError, match="unique"):
        send_module._sources([record_a, record_b], [body_a, body_b], source_identity=True)


def test_the_whole_mesh_name_is_bounded_like_wg_bounds_it(send_module):
    source = {
        "id": "wgs-" + "A" * 21,  # 25 bytes
        "role": "PASSIVE_CARDIOID",
        "instance_id": "3f1c7a52-0d6b-4d0e-9d7e-5b4b1c1e2a10",
    }
    send_module._check_source_identities([source])  # exactly 128 bytes: accepted

    with pytest.raises(send_module.wglink_core.WgLinkError, match="25"):
        send_module._check_source_identities([dict(source, id="wgs-" + "A" * 22)])
    with pytest.raises(send_module.wglink_core.WgLinkError, match="128"):
        send_module._check_source_identities(
            [dict(source, id="wgs-A", instance_id="x" * 60)]
        )
    with pytest.raises(send_module.wglink_core.WgLinkError, match="trimmed"):
        send_module._check_source_identities([dict(source, id=" wgs-A")])


# ----------------------------------------------- the real export entry points


def test_a_linked_send_and_its_heartbeat_carry_the_same_derived_identity(
    send_module, tmp_path, monkeypatch
):
    """WG compares the heartbeat's source ids with the return listing, so the
    token path and the export path must agree in identity mode too."""

    from test_wglink_send import _linked_cut_design

    design, app = _linked_cut_design(
        send_module, retained_fraction=1.0, low=(-40.0, -40.0, 0.0), high=(40.0, 90.0, 120.0)
    )
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    options = {
        "output_folder": str(tmp_path),
        "capture_document": False,
        "source_identity": True,
    }

    report = send_module.send(app, options)
    state = send_module.return_state(app, {"source_identity": True})
    legacy_state = send_module.return_state(app, {})

    manifest = json.loads(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )
    assert "source-identity-v1" in manifest["required_features"]
    (source,) = manifest["sources"]
    assert source["id"] == send_module._throat_source_identity("wgi-cut")
    assert state["instance_identities"]["wgi-cut"]["source_ids"] == [source["id"]]
    assert legacy_state["instance_identities"]["wgi-cut"]["source_ids"] == ["source-hf"]
    # The written bundle passes the add-in's own reader with the feature.
    sys.modules["wglink_return"].loads_return_manifest(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )


def test_the_preflight_names_an_identity_refusal_before_ok(send_module, monkeypatch):
    _painted_document(send_module, monkeypatch, [stamped_face("MF")])

    report = send_module.preflight_scope(_app(), {"source_identity": True})
    legacy = send_module.preflight_scope(_app(), {})

    assert "Set WG Source" in report["source_error"]
    assert legacy.get("source_error") is None


# ------------------------------------------ a source only partly in the export


def _outside_member_document(send_module, monkeypatch, stamp=True):
    """HF marked on a horn face and a baffle face; only the horn is selected."""

    from test_wglink_send import _occurrence, _proxy_of

    a, b = stamped_face("HF"), stamped_face("HF")
    native = solid("Horn", [a])
    inner = component("Horn component", [native])
    occurrence = _occurrence(inner, [_proxy_of(native, component_value=inner)])
    root = component("Speaker", [solid("baffle", [b])])
    root.occurrences = Collection([occurrence])
    root.allOccurrences = Collection([occurrence])
    design = FakeDesign(root, [a, b])
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    if stamp:
        put_stamp([a, b], "HF")
    return design, occurrence, a, b


def _outside_options(tmp_path, occurrence, request):
    return {
        "output_folder": str(tmp_path),
        "capture_document": False,
        "selection": occurrence,
        "source_identity": True,
        "request_id": request,
    }


def test_a_source_face_outside_the_export_is_named_as_outside_not_removed(
    send_module, tmp_path, monkeypatch
):
    _design, occurrence, _a, _b = _outside_member_document(send_module, monkeypatch)

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        send_module.send(_app(), _outside_options(tmp_path, occurrence, "refused"))
    assert "outside what is being sent" in str(refusal.value)
    assert "Clear" in str(refusal.value)
    state = send_module.return_state(_app(), _outside_options(tmp_path, occurrence, "state"))
    assert state["hash"] is None and "outside" in state["reason"]
    # The root scope sees both faces, so its heartbeat token is unaffected.
    assert send_module.return_state(_app(), {"source_identity": True})["hash"]
