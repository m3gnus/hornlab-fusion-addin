"""Adopting paint that predates source identities, and refusing everything else.

A document painted before ``source-identity-v1`` existed carries roles but no
stamps, so every source refuses and the only remedy offered is to re-mark each
one by hand. These tests hold the narrowed refusal to its two halves:

* a role group in which **no face carries a source identity at all** is the
  pre-stamp case. There is no competing identity to mis-bind to and the paint is
  unanimous, so the user is asked once and the group is adopted;
* a group in which **some** face already carries an identity -- for this role or
  another -- stays refused, with the message unchanged. Which source the
  unidentified faces belong to cannot be told from the paint, and guessing binds
  the wrong faces and silently solves the wrong thing.

The second half is the one that matters. Everything an adoption is allowed to do
is undone by getting that boundary wrong.

What Fusion itself does when these attributes are written to a real document --
and in particular what an externally referenced, read-only component does -- is
live-Fusion evidence, not something these fakes can prove.
"""

from __future__ import annotations

import json
from pathlib import Path
import types

import pytest

from test_wglink_send import send_module  # noqa: F401 - fixture
from test_wglink_source_identity import (
    GROUP,
    STAMP,
    _app,
    _painted_document,
    _send,
    put_stamp,
    stamp_of,
    stamped_face,
)
from test_wglink_source_reassignment import _ReadOnlyAttributes


class _Confirmer:
    """A stand-in for the Send dialog's question, which records being asked."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[tuple[str, int]] = []

    def __call__(self, role: str, faces: int) -> bool:
        self.asked.append((role, faces))
        return self.answer


def _send_with(send_module, tmp_path, confirmer, *, request="r"):
    report = send_module.send(
        _app(),
        {
            "output_folder": str(tmp_path),
            "capture_document": False,
            "request_id": request,
            "source_identity": True,
        },
        confirm_adoption=confirmer,
    )
    return json.loads(
        (Path(report["bundle_path"]) / "wgreturn.json").read_text(encoding="utf-8")
    )


def _nothing_was_written(tmp_path) -> bool:
    return not list(tmp_path.glob("*.wgreturn")) and not list(tmp_path.glob(".*tmp-*"))


def _unresolved(missing: int, total: int, painted: str, canonical: str) -> str:
    """The refusal a group WGLink cannot resolve has always carried, verbatim.

    Written out here rather than imported from the module under test: a test
    that asks the code what it says agrees with it by construction. This is the
    message as it stood at 599ef82d, and it must not move for the ambiguous
    case.
    """

    return (
        f"{missing} of {total} face(s) painted {painted} carry no WG source "
        f"identity for {canonical} (painted by hand, repainted, or marked before "
        f"source identities existed). Select them and run Set WG Source… "
        f"{canonical}; a face added to a source that still resolves keeps that "
        "source's identity."
    )


# ------------------------------------------------- the behaviour being changed


def test_a_wholly_unstamped_painted_source_refuses_today(
    send_module, tmp_path, monkeypatch
):
    """The defect, stated as it is before the fix: no adoption path exists.

    Kept after the fix as the no-confirmer behaviour: a caller that cannot ask
    -- the preview, the fingerprint, a headless shell -- still refuses.
    """

    _painted_document(send_module, monkeypatch, [stamped_face("LF"), stamped_face("LF")])

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        _send(send_module, tmp_path)

    assert "2 of 2 face(s) painted LF" in str(refusal.value)
    assert _nothing_was_written(tmp_path)


# ------------------------------------------------------------------- adoption


def test_a_wholly_unstamped_group_is_adopted_once_the_user_confirms(
    send_module, tmp_path, monkeypatch
):
    a, b = stamped_face("LF"), stamped_face("LF")
    design = _painted_document(send_module, monkeypatch, [a, b])
    original_bodies = tuple(design.rootComponent.bRepBodies)
    confirmer = _Confirmer(True)

    manifest = _send_with(send_module, tmp_path, confirmer)

    assert confirmer.asked == [("LF", 2)]
    (source,) = manifest["sources"]
    assert source["id"].startswith("wgs-")
    assert stamp_of(a)["id"] == stamp_of(b)["id"] == source["id"]
    assert tuple(design.rootComponent.bRepBodies) == original_bodies


def test_an_adopted_group_records_the_count_it_was_marked_on(
    send_module, tmp_path, monkeypatch
):
    faces = [stamped_face("HF"), stamped_face("HF"), stamped_face("HF")]
    _painted_document(send_module, monkeypatch, faces)

    first = _send_with(send_module, tmp_path, _Confirmer(True), request="one")

    stamps = [stamp_of(value) for value in faces]
    assert {stamp["faces"] for stamp in stamps} == {3}
    assert {stamp["role"] for stamp in stamps} == {"HF"}
    assert len({stamp["face"] for stamp in stamps}) == 3
    assert len({stamp["id"] for stamp in stamps}) == 1

    # Adoption happens once. The next send resolves without asking again.
    never = _Confirmer(False)
    second = _send_with(send_module, tmp_path, never, request="two")

    assert never.asked == []
    assert second["sources"][0]["id"] == first["sources"][0]["id"]


def test_a_legacy_port_exit_group_adopts_under_its_canonical_role(
    send_module, tmp_path, monkeypatch
):
    face_value = stamped_face("PORT_EXIT")
    _painted_document(send_module, monkeypatch, [face_value])
    confirmer = _Confirmer(True)

    manifest = _send_with(send_module, tmp_path, confirmer)

    assert confirmer.asked == [("PASSIVE_CARDIOID", 1)]
    assert stamp_of(face_value)["role"] == "PASSIVE_CARDIOID"
    assert manifest["sources"][0]["id"] == stamp_of(face_value)["id"]


def test_each_painted_role_group_is_asked_for_on_its_own(
    send_module, tmp_path, monkeypatch
):
    low, high = stamped_face("LF"), stamped_face("HF")
    _painted_document(send_module, monkeypatch, [low, high])
    confirmer = _Confirmer(True)

    manifest = _send_with(send_module, tmp_path, confirmer)

    assert sorted(confirmer.asked) == [("HF", 1), ("LF", 1)]
    ids = {source["role"]: source["id"] for source in manifest["sources"]}
    assert ids["LF"] == stamp_of(low)["id"] != stamp_of(high)["id"] == ids["HF"]


# ----------------------------------------------------- what is never adopted


def test_a_mixed_group_is_refused_unchanged_and_never_even_asked(
    send_module, tmp_path, monkeypatch
):
    """The assertion that stops this fix becoming a silent mis-binding.

    One of two HF faces carries an identity. Whether the other belongs to that
    source or was painted separately cannot be told from the paint, so the
    refusal stands whatever the user would have answered -- and the question is
    not put, because there is no answer to it that would be safe.
    """

    stamped, unstamped = stamped_face("HF"), stamped_face("HF")
    _painted_document(send_module, monkeypatch, [stamped, unstamped])
    kept = put_stamp([stamped], "HF", count=1)
    confirmer = _Confirmer(True)

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        _send_with(send_module, tmp_path, confirmer)

    # Byte for byte what it said before adoption existed: no hint of an
    # adoption, because none is on offer for this group.
    assert str(refusal.value) == _unresolved(1, 2, "HF", "HF")
    assert confirmer.asked == []
    assert stamp_of(unstamped) is None
    assert stamp_of(stamped)["id"] == kept
    assert _nothing_was_written(tmp_path)


def test_a_face_stamped_for_another_role_is_never_adopted(
    send_module, tmp_path, monkeypatch
):
    """A repaint carries a competing identity, so it is not pre-stamp paint.

    The whole group is ``missing`` for MF -- every face fails the role test --
    yet adopting it would take this face out of the HF source without anyone
    saying so.
    """

    face_value = stamped_face("HF")
    _painted_document(send_module, monkeypatch, [face_value])
    kept = put_stamp([face_value], "HF")
    face_value.appearance = types.SimpleNamespace(name="MF")
    confirmer = _Confirmer(True)

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        _send_with(send_module, tmp_path, confirmer)

    assert str(refusal.value) == _unresolved(1, 1, "MF", "MF")
    assert confirmer.asked == []
    assert stamp_of(face_value)["id"] == kept
    assert stamp_of(face_value)["role"] == "HF"


def test_an_unreadable_stamp_is_a_corruption_and_is_never_adopted(
    send_module, tmp_path, monkeypatch
):
    """Pre-stamp paint is an attribute that was never written, not a broken one.

    A value that will not parse establishes nothing about whether the face is
    claimed, so it is refused rather than overwritten.
    """

    face_value = stamped_face("LF")
    _painted_document(send_module, monkeypatch, [face_value])
    face_value.attributes.add(GROUP, STAMP, "{not json")
    confirmer = _Confirmer(True)

    with pytest.raises(send_module.wglink_core.WgLinkError):
        _send_with(send_module, tmp_path, confirmer)

    assert confirmer.asked == []
    assert face_value.attributes.values[(GROUP, STAMP)] == "{not json"


def test_declining_leaves_the_document_untouched_and_refuses_as_before(
    send_module, tmp_path, monkeypatch
):
    a, b = stamped_face("LF"), stamped_face("LF")
    _painted_document(send_module, monkeypatch, [a, b])
    confirmer = _Confirmer(False)

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        _send_with(send_module, tmp_path, confirmer)

    assert confirmer.asked == [("LF", 2)]
    # Asked and declined: the refusal is the one it always was, with no
    # second-guessing sentence about an offer the user has just turned down.
    assert str(refusal.value) == _unresolved(2, 2, "LF", "LF")
    assert a.attributes.values == b.attributes.values == {}
    assert _nothing_was_written(tmp_path)


# ------------------------------------------------ the readers never write


def test_the_preview_reports_the_refusal_and_writes_nothing(
    send_module, monkeypatch
):
    a, b = stamped_face("MF"), stamped_face("MF")
    _painted_document(send_module, monkeypatch, [a, b])

    report = send_module.preflight_scope(_app(), {"source_identity": True})

    assert "Set WG Source" in report["source_error"]
    assert "adopt" in report["source_error"].lower()
    assert a.attributes.values == b.attributes.values == {}


def test_the_return_state_fingerprint_never_adopts(send_module, monkeypatch):
    a, b = stamped_face("MF"), stamped_face("MF")
    _painted_document(send_module, monkeypatch, [a, b])

    state = send_module.return_state(_app(), {"source_identity": True})

    assert state["hash"] is None
    assert a.attributes.values == b.attributes.values == {}


# ------------------------------------------------------- all or nothing


def test_a_read_only_face_leaves_every_adopted_stamp_as_it_was(
    send_module, tmp_path, monkeypatch
):
    a, b = stamped_face("LF"), stamped_face("LF")
    locked = stamped_face("LF")
    locked.attributes = _ReadOnlyAttributes()
    design = _painted_document(send_module, monkeypatch, [a, b, locked])
    original_bodies = tuple(design.rootComponent.bRepBodies)

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        _send_with(send_module, tmp_path, _Confirmer(True))

    assert "Every source identity stamp was restored" in str(refusal.value)
    assert a.attributes.values == b.attributes.values == {}
    assert locked.attributes.values == {}
    assert tuple(design.rootComponent.bRepBodies) == original_bodies
    assert _nothing_was_written(tmp_path)
