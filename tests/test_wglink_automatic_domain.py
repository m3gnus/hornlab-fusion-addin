"""Pure policy tests for automatic-domain timeline evidence."""
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_return as policy  # noqa: E402


def _cut(**changes):
    descriptor = {
        "timeline_index": 3,
        "marker_position": 8,
        "suppressed": False,
        "feature_kind": "split-body",
        "feature_name": "Split Body 3",
        "tool_kind": "origin-plane",
        "origin_plane": "YZ",
        "coincident": True,
        "body_object_ids": ["body-a"],
        "kept_sides": {"body-a": "positive"},
    }
    descriptor.update(changes)
    return descriptor


def _classify(*descriptors):
    return policy.classify_cut_provenance(
        list(descriptors), {"body-a", "body-b"}, "root-component"
    )


def test_split_body_origin_yz_records_x0_and_positive_side():
    assert _classify(_cut()) == [{
        "body_object_id": "body-a",
        "feature": {"kind": "split-body", "name": "Split Body 3"},
        "tool": {"kind": "origin-plane", "origin_plane": "YZ"},
        "plane": "x0",
        "kept_side": "positive",
        "export_frame": "root-component",
    }]


def test_split_body_origin_xz_records_y0():
    assert _classify(_cut(origin_plane="XZ"))[0]["plane"] == "y0"


def test_extrude_cut_through_origin_plane_is_recorded():
    entry = _classify(_cut(
        feature_kind="extrude-cut",
        feature_name="Extrude 7",
        origin_plane="XY",
    ))[0]
    assert entry["feature"] == {"kind": "extrude-cut", "name": "Extrude 7"}
    assert entry["plane"] == "z0"


def test_zero_offset_construction_plane_is_recorded_but_an_offset_one_is_not():
    zero = _cut(tool_kind="construction-plane", coincident=True)
    offset = _cut(tool_kind="construction-plane", coincident=False, feature_name="Split Body 4")
    assert len(_classify(zero)) == 1
    assert _classify(offset) == []


def test_negative_kept_side_is_recorded_for_wg_to_refuse():
    entry = _classify(_cut(kept_sides={"body-a": "negative"}))[0]
    assert entry["kept_side"] == "negative"


def test_suppressed_and_rolled_back_features_are_not_recorded_with_a_positive_control():
    active = _cut()
    suppressed = _cut(suppressed=True, feature_name="Split Body suppressed")
    rolled_back = _cut(timeline_index=8, feature_name="Split Body rolled back")
    assert [entry["feature"]["name"] for entry in _classify(active, suppressed, rolled_back)] == [
        "Split Body 3"
    ]


def test_a_body_outside_the_export_scope_is_not_recorded_with_a_positive_control():
    outside = _cut(
        feature_name="Split Body outside",
        body_object_ids=["body-outside"],
        kept_sides={"body-outside": "positive"},
    )
    assert len(_classify(_cut(), outside)) == 1
