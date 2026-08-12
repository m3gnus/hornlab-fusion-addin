"""What the export watcher offers, and what it refuses to offer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

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


def _handoff(root: Path, bundle: Path, export_id: str = "wge_2") -> Path:
    marker = root / wglink_watch.HANDOFF_FILENAME
    marker.write_text(
        json.dumps({
            "schemaVersion": 1,
            "target": "fusion360",
            "bundlePath": str(bundle),
            "bundleId": "wgb_2",
            "exportId": export_id,
            "sequence": 2,
            "designId": "wgd_a",
            "expectedDocumentId": "fusion:doc-a",
            "expectedReturnStateHash": "sha256:return-state",
        }),
        encoding="utf-8",
    )
    return marker


def test_a_scoped_pending_handoff_is_read_and_acknowledged(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "horn", "wge_2", sequence=2)
    marker = _handoff(tmp_path, bundle)

    handoff = wglink_watch.read_pending_handoff(marker)

    assert handoff is not None
    assert handoff.bundle_path == str(bundle)
    assert handoff.export_id == "wge_2"
    assert handoff.sequence == "2"
    assert handoff.design_id == "wgd_a"
    assert handoff.expected_document_id == "fusion:doc-a"
    assert handoff.expected_return_state_hash == "sha256:return-state"
    assert wglink_watch.acknowledge_handoff(handoff) is True
    assert not marker.exists()


def test_acknowledging_an_insert_never_deletes_a_newer_send(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "horn", "wge_2")
    marker = _handoff(tmp_path, bundle)
    handoff = wglink_watch.read_pending_handoff(marker)
    assert handoff is not None

    _handoff(tmp_path, bundle, export_id="wge_3")

    assert wglink_watch.acknowledge_handoff(handoff) is False
    assert json.loads(marker.read_text())["exportId"] == "wge_3"


def test_fusion_status_publishes_document_config_and_parameters_atomically(
    tmp_path: Path,
) -> None:
    marker = wglink_watch.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="Tritonia V",
        links=[{
            "instance_id": "instance-a",
            "design_id": "wgd_a",
            "design_hash": "sha256:config",
            "formula": "r-osse",
            "config_present": "true",
            "parameter_count": "14",
            "parameter_drift_count": "2",
            "local_body_state": "modified",
            "body_fingerprint_hash": "sha256:body",
            "document_signature_hash": "sha256:return-state",
            "document_body_count": "3",
            "source_state_hash": "sha256:sources",
            "export_id": "wge_a",
        }],
        updated_at=datetime(2026, 8, 12, 15, 30, tzinfo=timezone.utc),
    )

    payload = json.loads(marker.read_text())
    assert payload["document"]["name"] == "Tritonia V"
    assert payload["document"]["links"][0] == {
        "bundlePath": None,
        "configPresent": True,
        "designHash": "sha256:config",
        "designId": "wgd_a",
        "designName": None,
        "editVersion": None,
        "exportId": "wge_a",
        "exportSequence": None,
        "formula": "r-osse",
        "instanceId": "instance-a",
        "lineageId": None,
        "parameterCount": 14,
        "parameterDriftCount": 2,
        "localBodyState": "modified",
        "bodyFingerprintHash": "sha256:body",
        "documentSignatureHash": "sha256:return-state",
        "documentBodyCount": 3,
        "sourceStateHash": "sha256:sources",
    }
    assert list(tmp_path.glob(f"{wglink_watch.FUSION_STATUS_FILENAME}.*")) == []
    assert wglink_watch.remove_fusion_status(tmp_path, session_id="other") is False
    assert wglink_watch.remove_fusion_status(tmp_path, session_id="session-a") is True
    assert not marker.exists()


def test_return_request_is_targeted_to_one_addin_session_and_acknowledged(tmp_path: Path) -> None:
    marker = tmp_path / wglink_watch.RETURN_REQUEST_FILENAME
    marker.write_text(json.dumps({
        "schemaVersion": 1,
        "target": "fusion360",
        "requestId": "request-a",
        "sessionId": "session-a",
        "designId": "wgd_a",
        "documentId": "fusion:doc-a",
        "instanceId": "instance-a",
        "expectedReturnStateHash": "sha256:return-state",
    }))

    assert wglink_watch.read_return_request(marker, session_id="other") is None
    request = wglink_watch.read_return_request(marker, session_id="session-a")
    assert request is not None
    assert request.design_id == "wgd_a"
    assert request.document_id == "fusion:doc-a"
    assert request.instance_id == "instance-a"
    assert request.expected_return_state_hash == "sha256:return-state"
    assert wglink_watch.acknowledge_return_request(request) is True
    assert not marker.exists()


def test_handoff_refuses_an_out_of_workspace_bundle(tmp_path: Path) -> None:
    root = tmp_path / "workspace" / "wglink"
    root.mkdir(parents=True)
    outside = _bundle(tmp_path, "outside", "wge_2")
    marker = _handoff(root, outside)

    assert wglink_watch.read_pending_handoff(marker) is None


def test_machine_local_handoff_accepts_only_the_selected_workspace_bundle_root(
    tmp_path: Path,
) -> None:
    bundles = tmp_path / "workspace" / "wglink"
    bundles.mkdir(parents=True)
    bundle = _bundle(bundles, "horn", "wge_2")
    ipc = tmp_path / "data" / "ipc" / "wglink"
    ipc.mkdir(parents=True)
    marker = _handoff(ipc, bundle)

    handoff = wglink_watch.read_pending_handoff(marker, bundle_root=bundles)

    assert handoff is not None
    assert wglink_watch.acknowledge_handoff(handoff, bundle_root=bundles) is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schemaVersion": 2, "target": "fusion360"},
        {"schemaVersion": 1, "target": "other"},
        {"schemaVersion": 1, "target": "fusion360", "bundlePath": ""},
    ],
)
def test_an_invalid_pending_handoff_is_silent(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    marker = tmp_path / wglink_watch.HANDOFF_FILENAME
    marker.write_text(json.dumps(payload))
    assert wglink_watch.read_pending_handoff(marker) is None


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
