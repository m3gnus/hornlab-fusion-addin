"""The add-in's half of WG <-> Fusion delivery, version 3.

Waveguide Generator's contract is docs/architecture/CAD-OPERATIONS.md in that
repository ("Delivery version", "Solve-command delivery" and "WG-produced
Fusion requests"). Both sides speak delivery version 3 and nothing older:
every request is its own file, with no single-slot marker and no twin. WG is
played here by ``WG``, which publishes the way that contract describes, and by
``OldWG``, which writes what a WG before version 3 wrote. The contract strings
are spelled out rather than imported, so a rename on the add-in's side shows
up as a failure.

Every path is under ``tmp_path``. The autouse fixture below also points
``WG2_DATA_DIR`` and ``HOME`` into it, so nothing that resolves WG's data
folder can reach the real one on the machine running the suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_watch  # noqa: E402


CAPABILITIES = "wg-capabilities.json"
SOLVE_FOLDER = ".wg-solve-requests"
SEQUENCE = "deliverySequence"
RETURNS = "return"
HANDOFFS = "handoff"
# kind -> (per-request folder, the single slot a WG before version 3 wrote)
KINDS = {
    RETURNS: (".fusion-return-requests", ".fusion-return-request.json"),
    HANDOFFS: (".fusion-handoffs", ".fusion-handoff.json"),
}
SESSION = "session-a"
ADVERTISED = {
    "schemaVersion": 1,
    "producer": "waveguide-generator",
    "solveCommandDelivery": 3,
    "fusionRequestDelivery": 3,
}


@pytest.fixture(autouse=True)
def _nothing_reaches_the_real_wg_folder(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WG2_DATA_DIR", str(tmp_path / "wg-data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def ipc(tmp_path: Path) -> Path:
    folder = tmp_path / "data" / "ipc" / "wglink"
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def bundles(tmp_path: Path) -> Path:
    folder = tmp_path / "workspace" / "wglink"
    folder.mkdir(parents=True)
    return folder


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.staging.tmp")
    staging.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(staging, path)


def _taken_names(folder: Path) -> list[str]:
    """The names a reader takes: ``*.json`` not starting with "."."""

    if not folder.is_dir():
        return []
    return sorted(
        path.stem for path in folder.iterdir()
        if path.suffix == ".json" and not path.name.startswith(".")
    )


def _hidden_names(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(path.name for path in folder.iterdir() if path.name.startswith("."))


def _sequence_of(payload: Any) -> int:
    value = payload.get(SEQUENCE) if isinstance(payload, dict) else None
    valid = isinstance(value, int) and not isinstance(value, bool) and value >= 1
    return value if valid else 0


def _request_body(
    kind: str,
    n: int,
    bundles: Path,
    session: str,
    *,
    instance: str | None = "instance-a",
) -> dict[str, Any]:
    if kind == RETURNS:
        return {
            "target": "fusion360",
            "sessionId": session,
            "designId": "wgd_a",
            "documentId": "fusion:doc-a",
            "instanceId": "instance-a",
            "expectedReturnStateHash": f"sha256:state-{n}",
        }
    bundle = bundles / f"horn-{n}.wglink"
    bundle.mkdir(parents=True, exist_ok=True)
    body = {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": f"wgb_{n}",
        "exportId": f"wge_{n}",
        "sequence": n,
        "designId": "wgd_a",
    }
    if instance is not None:
        body.update({
            "expectedDocumentId": "fusion:doc-a",
            "expectedInstanceId": instance,
            "expectedReturnStateHash": f"sha256:state-{n}",
        })
    return body


class WG:
    """WG as CAD-OPERATIONS.md describes version 3: one file per request."""

    def __init__(self, ipc: Path, bundles: Path) -> None:
        self.ipc = ipc
        self.bundles = bundles
        _write(ipc / CAPABILITIES, ADVERTISED)

    def publish(
        self,
        kind: str,
        n: int,
        *,
        request_id: str | None = None,
        session: str = SESSION,
        instance: str | None = "instance-a",
    ) -> str:
        folder = self.ipc / KINDS[kind][0]
        folder.mkdir(parents=True, exist_ok=True)
        request_id = request_id or f"req-{kind}-{n}"
        on_disk = [_sequence_of(_read(folder / f"{name}.json")) for name in _taken_names(folder)]
        _write(folder / f"{request_id}.json", {
            **_request_body(kind, n, self.bundles, session, instance=instance),
            "schemaVersion": 3,
            "requestId": request_id,
            "operationId": request_id,
            SEQUENCE: max([0, *on_disk]) + 1,
        })
        return request_id


class OldWG:
    """A WG before version 3: a single slot, and version-2 files with twins."""

    def __init__(self, ipc: Path, bundles: Path) -> None:
        self.ipc = ipc
        self.bundles = bundles
        _write(ipc / CAPABILITIES, {
            "schemaVersion": 1, "solveCommandDelivery": 2, "fusionRequestDelivery": 2,
        })

    def publish(self, kind: str, n: int) -> str:
        folder_name, slot_name = KINDS[kind]
        request_id = f"old-req-{kind}-{n}"
        body = {
            **_request_body(kind, n, self.bundles, SESSION),
            "requestId": request_id,
            "operationId": request_id,
            SEQUENCE: n,
        }
        _write(self.ipc / folder_name / f"{request_id}.json", {**body, "schemaVersion": 2})
        _write(self.ipc / slot_name, {**body, "schemaVersion": 1})
        return request_id


def _next(kind: str, ipc: Path, bundles: Path, session: str = SESSION):
    if kind == RETURNS:
        return wglink_watch.next_return_request(ipc, session_id=session)
    return wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)


def _acknowledge(kind: str, pending) -> bool:
    if kind == RETURNS:
        return wglink_watch.acknowledge_return_request(pending)
    return wglink_watch.acknowledge_handoff(pending)


def _take(
    kind: str,
    ipc: Path,
    bundles: Path,
    *,
    session: str = SESSION,
    while_running: Callable[[str], object] | None = None,
) -> list[str]:
    """One pass of the add-in, driven the way its dispatcher drives the reader.

    Take the next pending request, claim it, run it, acknowledge it. A request
    the pass has already run is not run again; that is the dispatcher's
    once-per-id suppression.
    """

    ran: list[str] = []
    seen: set[str] = set()
    while True:
        if kind == HANDOFFS:
            wglink_watch.discard_superseded_handoffs(ipc, bundle_root=bundles)
        pending = _next(kind, ipc, bundles, session)
        if pending is None:
            return ran
        if pending.request_id in seen:
            return ran
        seen.add(pending.request_id)
        claimed = wglink_watch.claim_request(pending)
        if claimed is None:
            return ran
        ran.append(claimed.request_id)
        if while_running is not None:
            while_running(claimed.request_id)
        _acknowledge(kind, claimed)


# -- solve commands --------------------------------------------------------------


def _return_bundle(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    bundle = workspace / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b'{"document": {}}')
    return workspace, bundle


def _write_solve(ipc: Path, tmp_path: Path, command_id: str = "cmd-1") -> Path:
    workspace = tmp_path / "workspace"
    bundle = workspace / "wgreturn" / "speaker.wgreturn"
    if not bundle.is_dir():
        _return_bundle(tmp_path)
    return wglink_watch.write_solve_request(
        ipc,
        command_id=command_id,
        return_id="wgr_1",
        bundle_path=bundle,
        workspace_root=workspace,
        requested_at=datetime(2026, 9, 13, 1, 0, tzinfo=timezone.utc),
    )


def test_a_solve_command_is_one_version_3_file(ipc: Path, tmp_path: Path) -> None:
    _write(ipc / CAPABILITIES, ADVERTISED)

    written = _write_solve(ipc, tmp_path)

    assert written == ipc / SOLVE_FOLDER / "cmd-1.json"
    assert _read(written) == {
        "schemaVersion": 3,
        "target": "waveguide-generator",
        "commandId": "cmd-1",
        "operationId": "cmd-1",
        "returnId": "wgr_1",
        "bundlePath": "wgreturn/speaker.wgreturn",
        "manifestSha256": "sha256:" + hashlib.sha256(b'{"document": {}}').hexdigest(),
        "requestedAt": "2026-09-13T01:00:00Z",
    }
    assert not (ipc / ".wg-solve-request.json").exists()
    # WG takes every *.json name not starting with "."; staging never matches.
    assert sorted(path.name for path in (ipc / SOLVE_FOLDER).iterdir()) == ["cmd-1.json"]


def test_unknown_capability_fields_and_later_versions_are_still_read(
    ipc: Path, tmp_path: Path
) -> None:
    _write(ipc / CAPABILITIES, {
        "schemaVersion": 1,
        "solveCommandDelivery": 4,
        "fusionRequestDelivery": 4,
        "somethingNew": {"nested": True},
    })

    assert _write_solve(ipc, tmp_path).parent.name == SOLVE_FOLDER


@pytest.mark.parametrize(
    "capability",
    [
        None,
        "not json at all",
        [],
        {"schemaVersion": 2, "solveCommandDelivery": 3},
        {"schemaVersion": True, "solveCommandDelivery": 3},
        {"schemaVersion": 1},
        {"schemaVersion": 1, "solveCommandDelivery": 2},
        {"schemaVersion": 1, "solveCommandDelivery": "3"},
        {"schemaVersion": 1, "solveCommandDelivery": True},
        {"schemaVersion": 1, "solveCommandDelivery": 3.0},
    ],
    ids=[
        "absent",
        "unreadable",
        "not-an-object",
        "unknown-schema",
        "boolean-schema",
        "not-advertised",
        "version-2",
        "string",
        "boolean",
        "float",
    ],
)
def test_a_wg_that_does_not_advertise_version_3_gets_no_solve_command(
    ipc: Path, tmp_path: Path, capability: Any
) -> None:
    """No older format is written instead: WG is asked to update."""

    if isinstance(capability, str):
        (ipc / CAPABILITIES).write_text(capability, encoding="utf-8")
    elif capability is not None:
        _write(ipc / CAPABILITIES, capability)

    with pytest.raises(wglink_watch.WgOutdatedError, match="Update Waveguide Generator"):
        _write_solve(ipc, tmp_path)

    assert not (ipc / ".wg-solve-request.json").exists()
    assert _taken_names(ipc / SOLVE_FOLDER) == []


def test_two_solve_commands_both_survive(ipc: Path, tmp_path: Path) -> None:
    _write(ipc / CAPABILITIES, ADVERTISED)

    _write_solve(ipc, tmp_path, "cmd-1")
    _write_solve(ipc, tmp_path, "cmd-2")

    assert _taken_names(ipc / SOLVE_FOLDER) == ["cmd-1", "cmd-2"]


def test_a_command_id_that_is_not_a_plain_file_name_is_refused(
    ipc: Path, tmp_path: Path
) -> None:
    _write(ipc / CAPABILITIES, ADVERTISED)

    with pytest.raises(ValueError):
        _write_solve(ipc, tmp_path, "../escape")

    assert _taken_names(ipc / SOLVE_FOLDER) == []


# -- WG requests: one file each, run once ----------------------------------------


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_each_request_runs_once_from_its_own_file_in_sequence_order(
    kind: str, ipc: Path, bundles: Path
) -> None:
    wg = WG(ipc, bundles)
    # Name order is the reverse of the order WG published them in, and each
    # handoff targets its own instance, so neither supersedes the other.
    first = wg.publish(kind, 1, request_id="zz-first", instance="instance-a")
    second = wg.publish(kind, 2, request_id="aa-second", instance="instance-b")

    assert _take(kind, ipc, bundles) == [first, second]
    assert _take(kind, ipc, bundles) == []
    # Nothing is left behind: no claim, no twin, no record.
    assert list((ipc / KINDS[kind][0]).iterdir()) == []
    assert not (ipc / KINDS[kind][1]).exists()


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_request_from_an_older_wg_is_never_run(
    kind: str, ipc: Path, bundles: Path
) -> None:
    """Version 2 files and single slots are an older WG's; none of them runs."""

    OldWG(ipc, bundles).publish(kind, 1)

    assert _take(kind, ipc, bundles) == []
    assert _taken_names(ipc / KINDS[kind][0]) == [f"old-req-{kind}-1"]
    assert (ipc / KINDS[kind][1]).exists()


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
@pytest.mark.parametrize("sequence", [None, 0, True, "3"], ids=["none", "zero", "bool", "text"])
def test_a_file_without_a_valid_sequence_is_left_where_it_is(
    kind: str, sequence: Any, ipc: Path, bundles: Path
) -> None:
    folder = ipc / KINDS[kind][0]
    body = {
        **_request_body(kind, 1, bundles, SESSION),
        "schemaVersion": 3,
        "requestId": "not-from-wg",
        "operationId": "not-from-wg",
    }
    if sequence is not None:
        body[SEQUENCE] = sequence
    _write(folder / "not-from-wg.json", body)

    assert _take(kind, ipc, bundles) == []
    assert _taken_names(folder) == ["not-from-wg"]


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_file_whose_operation_id_is_not_its_request_id_is_left_alone(
    kind: str, ipc: Path, bundles: Path
) -> None:
    folder = ipc / KINDS[kind][0]
    _write(folder / "req-a.json", {
        **_request_body(kind, 1, bundles, SESSION),
        "schemaVersion": 3,
        "requestId": "req-a",
        "operationId": "req-b",
        SEQUENCE: 1,
    })

    assert _take(kind, ipc, bundles) == []
    assert _taken_names(folder) == ["req-a"]


def test_a_return_request_for_another_session_waits_for_it(
    ipc: Path, bundles: Path
) -> None:
    other = WG(ipc, bundles).publish(RETURNS, 1, session="session-other")

    assert _take(RETURNS, ipc, bundles) == []
    assert _take(RETURNS, ipc, bundles, session="session-other") == [other]


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_publish_while_a_request_runs_loses_nothing(
    kind: str, ipc: Path, bundles: Path
) -> None:
    wg = WG(ipc, bundles)
    first = wg.publish(kind, 1, instance="instance-a")
    published: list[str] = []

    ran = _take(
        kind,
        ipc,
        bundles,
        while_running=lambda _id: published.append(wg.publish(kind, 2, instance="instance-a"))
        if not published else None,
    )

    # The first had started, so the second -- for the same target -- could
    # not supersede it. Both run, in order.
    assert ran == [first, published[0]]
    assert _taken_names(ipc / KINDS[kind][0]) == []


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_failed_claim_leaves_the_request_for_the_next_pass(
    kind: str, ipc: Path, bundles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = WG(ipc, bundles).publish(kind, 1)
    real_rename = os.rename
    refusals = {"left": 1}

    def busy_rename(source, destination, *args, **kwargs):
        if Path(source).name == f"{request_id}.json" and refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError("WG has the file open")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", busy_rename)

    assert _take(kind, ipc, bundles) == []
    assert _taken_names(ipc / KINDS[kind][0]) == [request_id]
    assert _take(kind, ipc, bundles) == [request_id]


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_claim_that_cannot_be_deleted_is_still_never_run_again(
    kind: str, ipc: Path, bundles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = WG(ipc, bundles).publish(kind, 1)
    real_unlink = Path.unlink

    def stuck(self, *args, **kwargs):
        if self.parent.name == KINDS[kind][0]:
            raise PermissionError("held open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stuck)

    assert _take(kind, ipc, bundles) == [request_id]
    assert _take(kind, ipc, bundles) == []


# -- supersession: an unstarted update for the same target ------------------------


def test_only_the_newest_unstarted_update_for_one_target_runs(
    ipc: Path, bundles: Path
) -> None:
    """WG's policy: an unstarted update may be superseded by a newer one for the
    same exact target. Other targets and inserts are never superseded."""

    wg = WG(ipc, bundles)
    older = wg.publish(HANDOFFS, 1, instance="instance-a")
    other_target = wg.publish(HANDOFFS, 2, instance="instance-b")
    insert_one = wg.publish(HANDOFFS, 3, instance=None)
    insert_two = wg.publish(HANDOFFS, 4, instance=None)
    newer = wg.publish(HANDOFFS, 5, instance="instance-a")

    dropped = wglink_watch.discard_superseded_handoffs(ipc, bundle_root=bundles)

    assert dropped == [older]
    assert _take(HANDOFFS, ipc, bundles) == [other_target, insert_one, insert_two, newer]


def test_a_superseded_update_being_claimed_elsewhere_is_left_to_that_claim(
    ipc: Path, bundles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wg = WG(ipc, bundles)
    older = wg.publish(HANDOFFS, 1, instance="instance-a")
    wg.publish(HANDOFFS, 2, instance="instance-a")
    real_rename = os.rename

    def taken_first(source, destination, *args, **kwargs):
        if Path(source).name == f"{older}.json":
            raise FileNotFoundError("another reader claimed it")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", taken_first)

    assert wglink_watch.discard_superseded_handoffs(ipc, bundle_root=bundles) == []


# -- claims an interrupted session left behind ------------------------------------


def test_leftover_claims_are_listed_with_what_reconciliation_reads(
    ipc: Path, bundles: Path
) -> None:
    wg = WG(ipc, bundles)
    handoff = wg.publish(HANDOFFS, 1, instance="instance-a")
    ret = wg.publish(RETURNS, 2)
    claimed_handoff = wglink_watch.claim_request(
        wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)
    )
    claimed_return = wglink_watch.claim_request(
        wglink_watch.next_return_request(ipc, session_id=SESSION)
    )
    assert claimed_handoff is not None and claimed_return is not None

    claims = wglink_watch.leftover_claims(ipc)

    assert [(claim.channel, claim.request_id) for claim in claims] == [
        ("handoff", handoff), ("returnRequest", ret),
    ]
    first = claims[0]
    assert (first.export_id, first.design_id) == ("wge_1", "wgd_a")
    assert (first.expected_document_id, first.expected_instance_id) == (
        "fusion:doc-a", "instance-a",
    )
    assert all(wglink_watch.remove_leftover_claim(claim) for claim in claims)
    assert wglink_watch.leftover_claims(ipc) == []


# -- an older WG is named, not guessed at ------------------------------------------


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_requests_from_an_older_wg_are_reported(
    kind: str, ipc: Path, bundles: Path
) -> None:
    OldWG(ipc, bundles).publish(kind, 1)

    found = wglink_watch.outdated_wg_requests(ipc)

    folder, slot = KINDS[kind]
    assert found == [slot, f"{folder}/old-req-{kind}-1.json"]


def test_old_files_beside_a_version_3_advertisement_are_still_an_older_wgs(
    ipc: Path, bundles: Path
) -> None:
    """After a downgrade the advertisement can still say 3.

    A WG that speaks version 3 removes such files at its start, before it
    advertises, so these were written since -- by an older WG, which never
    rewrites the capability file.
    """

    OldWG(ipc, bundles).publish(HANDOFFS, 1)
    _write(ipc / CAPABILITIES, ADVERTISED)

    assert wglink_watch.wg_speaks_delivery_version(ipc) is True
    assert wglink_watch.outdated_wg_requests(ipc) == [
        ".fusion-handoff.json", ".fusion-handoffs/old-req-handoff-1.json",
    ]


# -- what the reader hands the dispatcher ---------------------------------------


def test_a_handoff_carries_its_operation_identity_and_exact_target(
    ipc: Path, bundles: Path
) -> None:
    wg = WG(ipc, bundles)
    update = wg.publish(HANDOFFS, 7, instance="instance-a")

    pending = wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)

    assert pending is not None
    assert pending.request_id == pending.operation_id == update
    assert pending.delivery_sequence == 1
    assert pending.export_id == "wge_7"
    assert pending.exact_target == ("fusion:doc-a", "instance-a")
    assert pending.expected_return_state_hash == "sha256:state-7"

    wglink_watch.acknowledge_handoff(wglink_watch.claim_request(pending))
    wg.publish(HANDOFFS, 8, instance=None)
    insert = wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)
    assert insert is not None and insert.exact_target is None


def test_a_handoff_outside_the_bundle_folder_is_never_offered(
    ipc: Path, bundles: Path, tmp_path: Path
) -> None:
    request_id = WG(ipc, bundles).publish(HANDOFFS, 1)
    elsewhere = tmp_path / "elsewhere" / "horn.wglink"
    elsewhere.mkdir(parents=True)
    path = ipc / KINDS[HANDOFFS][0] / f"{request_id}.json"
    payload = _read(path)
    payload["bundlePath"] = str(elsewhere)
    _write(path, payload)

    assert wglink_watch.next_pending_handoff(ipc, bundle_root=bundles) is None


# -- the heartbeat ---------------------------------------------------------------


def test_the_heartbeat_publishes_a_links_operation_id_beside_its_export(
    tmp_path: Path,
) -> None:
    marker = wglink_watch.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="waveguide v1",
        links=[
            {"instance_id": "instance-a", "export_id": "wge_2", "operation_id": "req-7"},
            {"instance_id": "instance-b", "export_id": "wge_1", "operation_id": ""},
        ],
    )

    first, second = json.loads(marker.read_text(encoding="utf-8"))["document"]["links"]
    assert (first["exportId"], first["operationId"]) == ("wge_2", "req-7")
    # Omitted, never invented, for a link no WG operation has updated.
    assert "operationId" not in second


def test_the_heartbeat_states_its_delivery_version_and_an_interrupted_operation(
    tmp_path: Path,
) -> None:
    """WG refuses an add-in below its version, and reports recovery from this."""

    marker = wglink_watch.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="waveguide v1",
        links=[],
        applying_operation={
            "operation_id": "req-9", "kind": "update",
            "instance_id": "instance-a", "export_id": "wge_4",
        },
    )

    status = json.loads(marker.read_text(encoding="utf-8"))
    assert status["deliveryVersion"] == 3
    assert status["document"]["applyingOperation"] == {
        "operationId": "req-9", "kind": "update",
        "instanceId": "instance-a", "exportId": "wge_4",
    }

    quiet = json.loads(
        wglink_watch.write_fusion_status(
            tmp_path, session_id="session-a", document_name="waveguide v1", links=[],
        ).read_text(encoding="utf-8")
    )
    assert "applyingOperation" not in quiet["document"]
