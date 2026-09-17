"""Set WG Source... as the authoring and reassignment of source identity.

The export side (``test_wglink_source_identity.py``) refuses a painted source
that no longer resolves and names a remedy. These tests hold the remedy to its
word: running the command, or Clear, really does make the source resolve, with
the identity kept or replaced exactly as decided (a new identity after a
refusal; kept when faces are only added to a source that still resolves).
"""

from __future__ import annotations

import types

import pytest

from test_wglink_send import Attributes, send_module  # noqa: F401 - fixture
from test_wglink_source_identity import (
    _outside_member_document,
    _outside_options,
    _app,
    _painted_document,
    _send,
    proxy_of,
    stamp_of,
    stamped_face,
)


# ---------------------------------------------------------------- reassignment


def test_adding_a_face_to_a_valid_source_keeps_its_identity(send_module, monkeypatch):
    a, b = stamped_face("HF"), stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [a, b])
    send_module.assign_source_identity(design, [a], "HF")
    kept = stamp_of(a)["id"]

    send_module.assign_source_identity(design, [b], "HF")

    assert stamp_of(b)["id"] == kept
    assert stamp_of(a)["faces"] == stamp_of(b)["faces"] == 2
    assert stamp_of(a)["face"] != stamp_of(b)["face"]


def test_reassigning_after_a_split_starts_a_new_identity_that_sends(
    send_module, tmp_path, monkeypatch
):
    original = stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [original])
    send_module.assign_source_identity(design, [original], "HF")
    old = stamp_of(original)["id"]
    half = stamped_face("HF")
    half.attributes.values = dict(original.attributes.values)
    design.rootComponent.bRepBodies[0].faces.append(half)
    design.entities.append(half)

    send_module.assign_source_identity(design, [original, half], "HF")

    manifest = _send(send_module, tmp_path)
    (source,) = manifest["sources"]
    assert source["id"] != old
    assert stamp_of(original)["face"] != stamp_of(half)["face"]


def test_reassigning_part_of_an_invalid_source_drops_the_stale_stamps(
    send_module, monkeypatch
):
    a, b, c = stamped_face("HF"), stamped_face("HF"), stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [a, b, c])
    send_module.assign_source_identity(design, [a, b, c], "HF")
    design.entities.remove(c)  # removed: the group no longer adds up

    send_module.assign_source_identity(design, [a], "HF")

    assert stamp_of(b) is None
    assert stamp_of(a)["faces"] == 1


def test_clearing_and_repainting_keep_the_remaining_source_consistent(
    send_module, tmp_path, monkeypatch
):
    a, b, c = stamped_face("HF"), stamped_face("HF"), stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [a, b, c])
    send_module.assign_source_identity(design, [a, b, c], "HF")
    kept = stamp_of(a)["id"]

    c.appearance = None
    send_module.clear_source_identity(design, [c])
    b.appearance = types.SimpleNamespace(name="MF")
    send_module.assign_source_identity(design, [b], "MF")

    assert stamp_of(c) is None
    assert stamp_of(a) == dict(stamp_of(a), id=kept, faces=1)
    manifest = _send(send_module, tmp_path)
    assert {source["role"]: source["id"] for source in manifest["sources"]}["HF"] == kept


def test_a_decrement_never_hides_an_earlier_removal(send_module, tmp_path, monkeypatch):
    a, b, c = stamped_face("HF"), stamped_face("HF"), stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [a, b, c])
    send_module.assign_source_identity(design, [a, b, c], "HF")
    design.rootComponent.bRepBodies[0].faces.remove(c)
    design.entities.remove(c)
    b.appearance = None
    send_module.clear_source_identity(design, [b])

    with pytest.raises(send_module.wglink_core.WgLinkError, match="removed"):
        _send(send_module, tmp_path)


def test_a_proxy_selection_stamps_the_native_face(send_module, monkeypatch):
    native = stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [native])
    proxy = proxy_of(native)

    send_module.assign_source_identity(design, [proxy, proxy_of(native)], "HF")

    assert proxy.attributes.values == {}
    assert stamp_of(native)["faces"] == 1


def test_a_managed_throat_face_is_never_stamped(send_module, monkeypatch):
    throat = stamped_face("HF")
    send_module.wglink_core._set_attribute(throat, "face_role", "HF")
    design = _painted_document(send_module, monkeypatch, [throat])

    send_module.assign_source_identity(design, [throat], "HF")

    assert stamp_of(throat) is None



# ------------------------------------- the remedy a refusal names must work


def _stripped_paint_document(send_module, monkeypatch):
    a, b = stamped_face("HF"), stamped_face("HF")
    design = _painted_document(send_module, monkeypatch, [a, b])
    send_module.assign_source_identity(design, [a, b], "HF")
    return design, a, b


def test_a_face_whose_paint_was_removed_by_hand_is_reassigned_by_running_again(
    send_module, tmp_path, monkeypatch
):
    design, a, b = _stripped_paint_document(send_module, monkeypatch)
    old = stamp_of(a)["id"]
    b.appearance = None
    with pytest.raises(send_module.wglink_core.WgLinkError, match="removed or repainted"):
        _send(send_module, tmp_path, request="refused")

    result = send_module.assign_source_identity(design, [a], "HF")

    assert result["kept"] is False
    assert stamp_of(b) is None
    (source,) = _send(send_module, tmp_path, request="after")["sources"]
    assert source["id"] == stamp_of(a)["id"] != old


def test_clearing_a_face_that_lost_its_paint_takes_it_out_and_keeps_the_rest(
    send_module, tmp_path, monkeypatch
):
    design, a, b = _stripped_paint_document(send_module, monkeypatch)
    kept = stamp_of(a)["id"]
    b.appearance = None

    assert send_module.clear_source_identity(design, [b]) == 1

    assert stamp_of(b) is None
    (source,) = _send(send_module, tmp_path)["sources"]
    assert source["id"] == kept


def test_a_face_repainted_to_another_role_by_hand_is_reassigned_role_by_role(
    send_module, tmp_path, monkeypatch
):
    design, a, b = _stripped_paint_document(send_module, monkeypatch)
    old = stamp_of(a)["id"]
    b.appearance = types.SimpleNamespace(name="MF")
    with pytest.raises(send_module.wglink_core.WgLinkError):
        _send(send_module, tmp_path, request="hf")
    # Following the HF refusal's remedy on the HF face it still sees...
    with pytest.raises(send_module.wglink_core.WgLinkError, match="removed or repainted"):
        send_module._painted_source_identity("HF", [a], design)

    send_module.assign_source_identity(design, [a], "HF")
    send_module._painted_source_identity("HF", [a], design)  # ...now resolves
    with pytest.raises(send_module.wglink_core.WgLinkError, match="carry no WG source identity for MF"):
        _send(send_module, tmp_path, request="mf")
    send_module.assign_source_identity(design, [b], "MF")

    ids = {source["role"]: source["id"] for source in _send(send_module, tmp_path)["sources"]}
    assert ids["HF"] == stamp_of(a)["id"] != old
    assert ids["MF"] == stamp_of(b)["id"]


class _ReadOnlyAttributes(Attributes):
    """An externally referenced component: Fusion refuses the write."""

    def add(self, group, name, value):
        raise RuntimeError("the component is read-only")


def test_a_refused_write_leaves_every_stamp_as_it_was(send_module, monkeypatch):
    a, b = stamped_face("HF"), stamped_face("HF")
    locked = stamped_face("HF")
    locked.attributes = _ReadOnlyAttributes()
    design = _painted_document(send_module, monkeypatch, [a, b, locked])
    send_module.assign_source_identity(design, [a, b], "HF")
    before = {name: dict(face.attributes.values) for name, face in (("a", a), ("b", b))}

    with pytest.raises(send_module.wglink_core.WgLinkError, match="Every source identity stamp was restored"):
        send_module.assign_source_identity(design, [a, locked], "HF")

    assert {name: dict(face.attributes.values) for name, face in (("a", a), ("b", b))} == before
    assert locked.attributes.values == {}


def test_clearing_the_faces_outside_the_export_is_the_remedy_the_refusal_names(
    send_module, tmp_path, monkeypatch
):
    design, occurrence, a, b = _outside_member_document(send_module, monkeypatch, stamp=False)
    send_module.assign_source_identity(design, [a, b], "HF")
    kept = stamp_of(a)["id"]
    with pytest.raises(send_module.wglink_core.WgLinkError, match="outside"):
        send_module.send(_app(), _outside_options(tmp_path, occurrence, "refused"))

    # Running Set WG Source on the faces it can see changes nothing, as the
    # message implies; clearing the out-of-scope face is the remedy it names.
    send_module.assign_source_identity(design, [a], "HF")
    assert stamp_of(a)["id"] == kept
    send_module.clear_source_identity(design, [b])

    report = send_module.send(_app(), _outside_options(tmp_path, occurrence, "after"))
    assert [source["id"] for source in report["sources"]] == [kept]


class _UndeletableAttributes(Attributes):
    """Writes succeed, but Fusion answers False when asked to delete."""

    def itemByName(self, group, name):
        handle = super().itemByName(group, name)
        if handle is not None:
            handle.deleteMe = lambda: False
        return handle


def test_a_rollback_that_fusion_refuses_is_reported_not_called_restored(
    send_module, monkeypatch
):
    fresh, locked = stamped_face("HF"), stamped_face("HF")
    fresh.attributes = _UndeletableAttributes()
    locked.attributes = _ReadOnlyAttributes()
    design = _painted_document(send_module, monkeypatch, [fresh, locked])

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        send_module.assign_source_identity(design, [fresh, locked], "HF")

    text = str(refusal.value)
    assert "1 face(s) could not be restored" in text
    assert "restored." not in text.replace("could not be restored", "")
