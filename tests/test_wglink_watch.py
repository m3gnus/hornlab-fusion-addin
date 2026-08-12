"""What the export watcher offers, and what it refuses to offer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_watch  # noqa: E402


def _bundle(root: Path, name: str, export_id: str, sequence: int = 1) -> Path:
    bundle = root / f"{name}.wglink"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "wglink.json").write_text(
        json.dumps({"export": {"id": export_id, "sequence": sequence}}),
        encoding="utf-8",
    )
    return bundle


def _touch(bundle: Path, when: float) -> None:
    os.utime(bundle / "wglink.json", (when, when))


def _link(bundle: Path, export_id: str, instance_id: str = "instance-a") -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "bundle_path": str(bundle),
        "export_id": export_id,
    }


def test_a_newer_export_is_announced_once(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "horn", "wge_2", sequence=2)
    watcher = wglink_watch.ExportWatcher()

    found = watcher.survey([_link(bundle, "wge_1")])
    assert [item.available_export_id for item in found] == ["wge_2"]
    assert found[0].available_sequence == "2"

    # Nothing changed on disk, and even once it is re-read the same export must
    # not nag a second time.
    assert watcher.survey([_link(bundle, "wge_1")]) == []
    _touch(bundle, 5_000)
    assert watcher.survey([_link(bundle, "wge_1")]) == []


def test_the_matching_export_is_never_announced(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "horn", "wge_1")
    watcher = wglink_watch.ExportWatcher()
    assert watcher.survey([_link(bundle, "wge_1")]) == []


def test_a_link_with_no_stored_export_is_left_alone(tmp_path: Path) -> None:
    """Announcing it would be guessing: nothing says the bundle is its source."""

    bundle = _bundle(tmp_path, "horn", "wge_2")
    watcher = wglink_watch.ExportWatcher()
    assert watcher.survey([_link(bundle, "")]) == []


def test_a_later_export_after_an_announcement_is_announced_again(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "horn", "wge_2", sequence=2)
    watcher = wglink_watch.ExportWatcher()
    assert len(watcher.survey([_link(bundle, "wge_1")])) == 1

    _bundle(tmp_path, "horn", "wge_3", sequence=3)
    _touch(bundle, 9_000)
    found = watcher.survey([_link(bundle, "wge_1")])
    assert [item.available_export_id for item in found] == ["wge_3"]


def test_forget_lets_a_failed_update_be_offered_again(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "horn", "wge_2")
    watcher = wglink_watch.ExportWatcher()
    assert len(watcher.survey([_link(bundle, "wge_1")])) == 1

    watcher.forget("instance-a")
    _touch(bundle, 7_000)
    assert len(watcher.survey([_link(bundle, "wge_1")])) == 1


@pytest.mark.parametrize(
    "payload",
    ['{"export": {}}', '{"export": {"id": ""}}', "not json at all", '{"export": 4}'],
)
def test_an_unreadable_manifest_is_silent(tmp_path: Path, payload: str) -> None:
    bundle = tmp_path / "horn.wglink"
    bundle.mkdir()
    (bundle / "wglink.json").write_text(payload, encoding="utf-8")
    watcher = wglink_watch.ExportWatcher()
    assert watcher.survey([_link(bundle, "wge_1")]) == []


def test_a_missing_bundle_is_silent(tmp_path: Path) -> None:
    watcher = wglink_watch.ExportWatcher()
    assert watcher.survey([_link(tmp_path / "gone.wglink", "wge_1")]) == []


def test_several_links_are_surveyed_independently(tmp_path: Path) -> None:
    fresh = _bundle(tmp_path, "fresh", "wge_9", sequence=9)
    same = _bundle(tmp_path, "same", "wge_1")
    watcher = wglink_watch.ExportWatcher()
    found = watcher.survey([
        _link(fresh, "wge_1", "instance-fresh"),
        _link(same, "wge_1", "instance-same"),
    ])
    assert [item.instance_id for item in found] == ["instance-fresh"]


def test_prompt_text_names_the_sequence_and_promises_undo(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "horn", "wge_2", sequence=6)
    watcher = wglink_watch.ExportWatcher()
    text = wglink_watch.prompt_text(watcher.survey([_link(bundle, "wge_1")]))
    assert "sequence 6" in text
    assert "instance-a" in text
    assert "Undo" in text
    assert "creates and deletes no features" in text


def test_prompt_text_lists_every_link_when_several_moved(tmp_path: Path) -> None:
    first = _bundle(tmp_path, "one", "wge_5", sequence=5)
    second = _bundle(tmp_path, "two", "wge_7", sequence=7)
    watcher = wglink_watch.ExportWatcher()
    announcements = watcher.survey([
        _link(first, "wge_1", "instance-one"),
        _link(second, "wge_1", "instance-two"),
    ])
    text = wglink_watch.prompt_text(announcements)
    assert "instance-one" in text and "instance-two" in text
    assert "sequence 5" in text and "sequence 7" in text
