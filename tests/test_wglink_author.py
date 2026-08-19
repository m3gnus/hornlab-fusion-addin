"""The authoring decisions, exercised without Fusion.

``wglink_author`` imports no ``adsk``, so unlike the rest of the add-in it can
be imported directly. Everything here is the contract the Set WG Source, the
Declare Body and the Send/Solve pre-flight dialogs hold WGLink.py to.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ADDIN = Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"


@pytest.fixture
def author(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "wglink_author_unit_test", ADDIN / "wglink_author.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves a class's module out of sys.modules, so the module
    # has to be registered before it executes.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def faces(*roles, area=100.0):
    return [
        {"index": index, "current_role": role, "area_mm2": area}
        for index, role in enumerate(roles)
    ]


def bodies(*declarations, body_kind="surface"):
    return [
        {"index": index, "name": f"body {index}", "current": current, "body_kind": body_kind}
        for index, current in enumerate(declarations)
    ]


def bounds(low, high):
    return {"min": list(low), "max": list(high)}


# ------------------------------------------------------------- source roles


def test_the_appearance_name_is_the_role_spelled_exactly(author):
    """The export matches an upper-cased appearance name against four roles.

    Anything the dialog offers therefore has to round-trip to that exact name,
    or the painted face is silently not a source.
    """

    for role in author.SOURCE_ROLES:
        assert author.role_appearance_name(role) == role
    assert author.role_appearance_name("hf") == "HF"
    assert author.role_appearance_name("port exit") == "PORT_EXIT"
    assert author.role_appearance_name("port-exit") == "PORT_EXIT"


def test_an_unknown_role_is_refused_rather_than_painted(author):
    with pytest.raises(author.AuthorError, match="not a WG source role"):
        author.role_appearance_name("throat")
    with pytest.raises(author.AuthorError, match="Choose a WG source role"):
        author.role_appearance_name("")
    with pytest.raises(author.AuthorError, match="no appearance name"):
        author.role_appearance_name(author.CLEAR_SOURCE_LABEL)


def test_the_dropdown_offers_four_roles_and_a_clear(author):
    assert author.source_choices() == ("LF", "MF", "HF", "PORT_EXIT", "Clear WG source")
    assert author.DEFAULT_SOURCE_ROLE in author.SOURCE_ROLES
    assert author.resolve_source_choice(author.CLEAR_SOURCE_LABEL) is None


def test_the_dialog_copy_teaches_the_convention_in_one_line(author):
    text = author.source_help_text("HF")

    assert "HF" in text and "WG solves" in text
    assert "\n" not in text
    assert "appearance" in author.source_help_text("PORT_EXIT")
    assert "back to" in author.source_help_text(author.CLEAR_SOURCE_LABEL)


def test_painting_replaces_a_different_role_and_skips_the_matching_one(author):
    plan = author.plan_source_assignment(faces(None, "MF", "HF"), "HF")

    assert plan.appearance_name == "HF"
    assert plan.paint == (0, 1)
    assert plan.clear == ()
    assert plan.unchanged == (2,)
    assert "300.0 mm²" in plan.summary


def test_repainting_only_matching_faces_reports_that_nothing_changed(author):
    plan = author.plan_source_assignment(faces("HF"), "HF")

    assert plan.paint == () and plan.clear == ()
    assert "already carried HF" in plan.summary


def test_clear_strips_only_faces_that_carry_a_wg_role(author):
    """A Clear that swept up an ordinary painted face must not repaint it.

    Clearing sets the face appearance back to None, which is destructive for a
    face the user painted with their own material, so the plan only ever names
    faces whose appearance is one of the four roles.
    """

    plan = author.plan_source_assignment(
        faces("HF", None, "Steel - Satin", "PORT_EXIT"), author.CLEAR_SOURCE_LABEL
    )

    assert plan.role is None and plan.appearance_name is None
    assert plan.paint == ()
    assert plan.clear == (0, 3)
    assert plan.unchanged == (1, 2)
    assert "2 faces" in plan.summary


def test_clearing_nothing_says_so_instead_of_claiming_a_change(author):
    plan = author.plan_source_assignment(faces(None), author.CLEAR_SOURCE_LABEL)

    assert plan.clear == ()
    assert "nothing changed" in plan.summary


def test_an_empty_face_selection_is_refused_with_the_reason(author):
    with pytest.raises(author.AuthorError, match="at least one face"):
        author.plan_source_assignment([], "HF")


def test_an_unmeasured_face_drops_the_total_rather_than_lying(author):
    plan = author.plan_source_assignment(
        [{"index": 0, "current_role": None, "area_mm2": None}], "LF"
    )

    assert plan.paint == (0,)
    assert "mm²" not in plan.summary


# --------------------------------------------------------- body declarations


def test_declaration_choices_resolve_from_label_and_from_raw_value(author):
    labels = author.declaration_choices()

    assert len(labels) == 3
    assert author.resolve_declaration_choice(labels[0]) == "exterior-shell"
    assert author.resolve_declaration_choice(labels[1]) == "exclude"
    assert author.resolve_declaration_choice(labels[2]) is None
    assert author.resolve_declaration_choice("exclude") == "exclude"
    with pytest.raises(author.AuthorError, match="not a body declaration"):
        author.resolve_declaration_choice("interior")


def test_declaring_writes_only_the_bodies_that_change(author):
    plan = author.plan_body_declaration(
        bodies(None, "exclude", "exterior-shell"), "exterior-shell"
    )

    assert plan.declaration == "exterior-shell"
    assert plan.write == (0, 1)
    assert plan.clear == ()
    assert plan.unchanged == (2,)
    assert "Declared 2 bodies as exterior-shell" in plan.summary


def test_declaring_a_solid_as_the_shell_says_it_was_already_included(author):
    plan = author.plan_body_declaration(bodies(None, body_kind="solid"), "exterior-shell")

    assert plan.write == (0,)
    assert "already included automatically" in plan.summary


def test_clearing_a_declaration_removes_it_only_where_one_exists(author):
    plan = author.plan_body_declaration(
        bodies("exclude", None), author.CLEAR_DECLARATION_LABEL
    )

    assert plan.declaration is None
    assert plan.write == () and plan.clear == (0,) and plan.unchanged == (1,)
    assert "Cleared the WG declaration on 1 body" in plan.summary


def test_an_empty_body_selection_is_refused(author):
    with pytest.raises(author.AuthorError, match="at least one body"):
        author.plan_body_declaration([], "exclude")


# ------------------------------------------------------------- solver frame


def clean_scope(**overrides):
    scope = {
        "selection": "root",
        "instance_ids": [],
        "included": [{"name": "horn", "body_kind": "solid"}],
        "sources": [{"role": "HF", "area_mm2": 506.7, "face_count": 1, "instance_id": None}],
        "scope_error": None,
        "source_error": None,
        "bounds_mm": bounds((-100.0, -100.0, 0.0), (100.0, 100.0, 180.0)),
        "source_bounds_mm": bounds((-12.7, -12.7, 0.0), (12.7, 12.7, 0.0)),
    }
    scope.update(overrides)
    return scope


def codes(findings):
    return [finding.code for finding in findings]


def test_a_model_in_the_solver_frame_reports_no_findings(author):
    scope = clean_scope()

    assert author.frame_findings(scope["bounds_mm"], scope["source_bounds_mm"]) == []


def test_a_model_pointing_the_wrong_way_names_the_axis(author):
    findings = author.frame_findings(
        bounds((-100.0, -100.0, -180.0), (100.0, 100.0, 0.0)),
        bounds((-12.7, -12.7, 0.0), (12.7, 12.7, 0.0)),
    )

    assert codes(findings) == [author.FRAME_AXIS]
    assert "+Z" in findings[0].message


def test_a_sourceless_model_entirely_behind_the_origin_still_names_the_axis(author):
    findings = author.frame_findings(bounds((-10.0, -10.0, -50.0), (10.0, 10.0, -5.0)))

    assert codes(findings) == [author.FRAME_AXIS]


def test_a_throat_off_the_origin_plane_is_reported_with_its_offset(author):
    findings = author.frame_findings(
        bounds((-100.0, -100.0, 25.0), (100.0, 100.0, 205.0)),
        bounds((-12.7, -12.7, 25.0), (12.7, 12.7, 25.0)),
    )

    assert codes(findings) == [author.FRAME_THROAT_Z]
    assert "z = 25.0 mm" in findings[0].message


def test_an_off_centre_bounding_box_names_both_axes(author):
    findings = author.frame_findings(
        bounds((0.0, 40.0, 0.0), (200.0, 240.0, 180.0)),
        bounds((87.3, 127.3, 0.0), (112.7, 152.7, 0.0)),
    )

    assert codes(findings) == [author.FRAME_CENTRING]
    assert "x = 100.0 mm" in findings[0].message
    assert "y = 140.0 mm" in findings[0].message


def test_a_small_centring_offset_stays_quiet(author):
    findings = author.frame_findings(
        bounds((-99.5, -100.0, 0.0), (100.5, 100.0, 180.0)),
        bounds((-12.7, -12.7, 0.0), (12.7, 12.7, 0.0)),
    )

    assert findings == []


def test_unreadable_bounds_produce_no_findings_rather_than_a_guess(author):
    assert author.frame_findings(None, None) == []
    assert author.frame_findings({"min": [0, 0], "max": [1, 1, 1]}) == []
    assert author.frame_findings({"min": [0, 0, float("nan")], "max": [1, 1, 1]}) == []


# ---------------------------------------------------------------- pre-flight


def test_a_clean_unlinked_scope_states_the_frame_and_warns_about_nothing(author):
    summary = author.preflight_summary(clean_scope())

    assert summary.warnings == ()
    assert "Scope: root assembly" in summary.lines[0]
    assert "Bodies included: 1 solid" in summary.lines[1]
    assert "unlinked (Fusion-first) return" in summary.lines[2]
    assert "Source: HF — 506.7 mm² over 1 face" in summary.lines[3]
    assert "Solver frame: axis +Z" in summary.lines[4]


def test_a_linked_scope_names_its_links_and_skips_the_frame_check(author):
    """A linked return carries its own throat frame, so the convention does not
    apply to the assembly frame at all."""

    summary = author.preflight_summary(clean_scope(
        instance_ids=["wgi_one", "wgi_two"],
        sources=[{"role": "HF", "area_mm2": 506.7, "face_count": 1, "instance_id": "wgi_one"}],
        bounds_mm=bounds((-100.0, -100.0, -180.0), (100.0, 100.0, 0.0)),
    ))

    assert "WG links in scope: 2 (wgi_one, wgi_two)" in summary.lines[2]
    assert "from link wgi_one" in summary.lines[3]
    assert summary.frame == ()
    assert summary.warnings == ()


def test_every_frame_finding_reaches_the_dialog_as_a_warning(author):
    summary = author.preflight_summary(clean_scope(
        bounds_mm=bounds((0.0, 40.0, -180.0), (200.0, 240.0, 25.0)),
        source_bounds_mm=bounds((87.3, 127.3, 25.0), (112.7, 152.7, 25.0)),
    ))

    assert codes(summary.frame) == [
        author.FRAME_AXIS, author.FRAME_THROAT_Z, author.FRAME_CENTRING
    ]
    assert len(summary.warnings) == 3
    assert all(warning.startswith("Solver frame: ") for warning in summary.warnings)


def test_a_scope_with_no_source_warns_before_ok_instead_of_after(author):
    """This used to be a modal raised only once the export had been asked for."""

    summary = author.preflight_summary(clean_scope(sources=[]))

    assert "Sources: none" in summary.lines[3]
    assert summary.warnings[0] == author.NO_SOURCE_WARNING


def test_the_exports_own_source_refusal_is_preferred_when_it_reported_one(author):
    summary = author.preflight_summary(
        clean_scope(sources=[], source_error="Return export has no drivable source.")
    )

    assert summary.warnings[0] == "Return export has no drivable source."


def test_an_unclassified_surface_body_points_at_the_command_that_fixes_it(author):
    summary = author.preflight_summary(clean_scope(
        scope_error="visible surface body 'Shell' is unclassified; mark it 'exterior-shell' or exclude it",
        included=[],
        sources=[],
    ))

    assert "Export is blocked" in summary.warnings[0]
    assert author.DECLARE_BODY_HINT in summary.warnings[0]


def test_mixed_body_kinds_are_counted_by_name(author):
    summary = author.preflight_summary(clean_scope(included=[
        {"name": "a", "body_kind": "solid"},
        {"name": "b", "body_kind": "solid"},
        {"name": "c", "body_kind": "surface"},
    ]))

    assert "Bodies included: 2 solids, 1 surface body" in summary.lines[1]


def test_the_summary_renders_as_escaped_html_with_bold_warnings(author):
    summary = author.preflight_summary(clean_scope(
        selection="Root<Assembly>", sources=[]
    ))

    rendered = summary.html()
    assert "&lt;Assembly&gt;" in rendered and "<Assembly>" not in rendered
    assert "<br>" in rendered
    assert rendered.count("<b>⚠") == 1
    assert "⚠" in summary.text()


def test_pre_flight_needs_a_gathered_report(author):
    with pytest.raises(author.AuthorError, match="gathered scope report"):
        author.preflight_summary("root")


# ------------------------------------------------------------ failure copy


def test_a_failure_message_is_one_human_line_plus_where_to_look(author):
    message = author.failure_message(
        "Set WG Source…", RuntimeError("Fusion refused\nthe appearance")
    )

    assert message.startswith("Set WG Source… hit an unexpected error: ")
    assert "Fusion refused the appearance" in message
    assert "Traceback" not in message
    assert author.LOG_HINT in message


def test_a_long_or_empty_failure_detail_stays_readable(author):
    long_message = author.failure_message("Send to WG", RuntimeError("x" * 500))
    assert len(long_message.splitlines()[0]) < 250
    assert long_message.splitlines()[0].endswith("…")

    assert "RuntimeError" in author.failure_message("Send to WG", RuntimeError(""))
    assert "no details" in author.failure_message("Send to WG", None)


def test_an_unsurveyable_model_says_so_in_the_pre_flight_box(author):
    assert author.preflight_unavailable(RuntimeError("no active design")) == (
        "Pre-flight unavailable: no active design"
    )
