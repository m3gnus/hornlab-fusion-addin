"""The add-in's half of the mixed-version matrix for WG <-> Fusion delivery.

Waveguide Generator's contract is docs/architecture/CAD-OPERATIONS.md in that
repository ("Solve-command delivery", "Capability file" and "WG-produced
Fusion requests"). WG is played here by two fakes: ``NewWG`` publishes each
return request and handoff as its own file plus a legacy twin under the same
id, and ``OldWG`` writes only the legacy slot, as every WG did before per-
request files. The contract strings are spelled out rather than imported, so
a rename on the add-in's side shows up as a failure.

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
SOLVE_SLOT = ".wg-solve-request.json"
SOLVE_FOLDER = ".wg-solve-requests"
SLOT_RECORD = ".legacy-slot.json"
SEQUENCE = "deliverySequence"
RETURNS = "return"
HANDOFFS = "handoff"
# kind -> (legacy slot, per-request folder, the id a legacy acknowledgement checks)
KINDS = {
    RETURNS: (".fusion-return-request.json", ".fusion-return-requests", "requestId"),
    HANDOFFS: (".fusion-handoff.json", ".fusion-handoffs", "exportId"),
}
SESSION = "session-a"


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


def _sequence_of(payload: Any) -> int:
    value = payload.get(SEQUENCE) if isinstance(payload, dict) else None
    valid = isinstance(value, int) and not isinstance(value, bool) and value >= 1
    return value if valid else 0


def _request_body(kind: str, n: int, bundles: Path, session: str) -> dict[str, Any]:
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
    return {
        "target": "fusion360",
        "bundlePath": str(bundle),
        "bundleId": f"wgb_{n}",
        "exportId": f"wge_{n}",
        "sequence": n,
        "designId": "wgd_a",
        "expectedDocumentId": "fusion:doc-a",
        "expectedInstanceId": "instance-a",
        "expectedReturnStateHash": f"sha256:state-{n}",
    }


class NewWG:
    """WG as CAD-OPERATIONS.md describes it: the file, then the twin, then the record."""

    def __init__(self, ipc: Path, bundles: Path) -> None:
        self.ipc = ipc
        self.bundles = bundles

    def publish(
        self,
        kind: str,
        n: int,
        *,
        request_id: str | None = None,
        session: str = SESSION,
        twin: bool = True,
    ) -> str:
        slot_name, folder_name, _ack = KINDS[kind]
        folder = self.ipc / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        request_id = request_id or f"req-{kind}-{n}"
        on_disk = [_sequence_of(_read(folder / f"{name}.json")) for name in _taken_names(folder)]
        on_disk.append(_sequence_of(_read(folder / SLOT_RECORD)))
        on_disk.append(_sequence_of(_read(self.ipc / slot_name)))
        sequence = max(on_disk) + 1
        body = {
            **_request_body(kind, n, self.bundles, session),
            "requestId": request_id,
            "operationId": request_id,
            SEQUENCE: sequence,
        }
        _write(folder / f"{request_id}.json", {**body, "schemaVersion": 2})
        if twin:
            _write(self.ipc / slot_name, {**body, "schemaVersion": 1})
            _write(folder / SLOT_RECORD, {"operationId": request_id, SEQUENCE: sequence})
        return request_id


class OldWG:
    """A WG that predates per-request files: the slot is the only copy."""

    def __init__(self, ipc: Path, bundles: Path) -> None:
        self.ipc = ipc
        self.bundles = bundles

    def publish(self, kind: str, n: int, *, session: str = SESSION) -> str:
        slot_name, _folder, ack_key = KINDS[kind]
        body = {**_request_body(kind, n, self.bundles, session), "schemaVersion": 1}
        if kind == RETURNS:
            body["requestId"] = f"old-req-{n}"
        else:
            body.pop("expectedReturnStateHash")
        _write(self.ipc / slot_name, body)
        return str(body[ack_key])


def _next(kind: str, ipc: Path, bundles: Path, session: str = SESSION):
    if kind == RETURNS:
        return wglink_watch.next_return_request(ipc, session_id=session)
    return wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)


def _acknowledge(kind: str, pending, bundles: Path) -> bool:
    if kind == RETURNS:
        return wglink_watch.acknowledge_return_request(pending)
    return wglink_watch.acknowledge_handoff(pending, bundle_root=bundles)


def _identity(kind: str, pending) -> str:
    if pending.per_request or kind == RETURNS:
        return pending.request_id
    return pending.export_id


def _take(
    kind: str,
    ipc: Path,
    bundles: Path,
    *,
    session: str = SESSION,
    while_running: Callable[[str], object] | None = None,
) -> list[str]:
    """One pass of the add-in, driven the way its dispatcher drives the reader.

    Take the next pending request, claim it when it is a per-request file,
    run it, acknowledge it. A request the pass has already run is not run
    again; that is the dispatcher's once-per-id suppression.
    """

    ran: list[str] = []
    seen: set[str] = set()
    while True:
        pending = _next(kind, ipc, bundles, session)
        if pending is None:
            return ran
        key = _identity(kind, pending)
        if key in seen:
            return ran
        seen.add(key)
        if pending.per_request:
            claimed = wglink_watch.claim_request(pending)
            if claimed is None:
                return ran
            pending = claimed
        ran.append(key)
        if while_running is not None:
            while_running(key)
        _acknowledge(kind, pending, bundles)


# -- solve commands: the capability picks the format -------------------------


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


ADVERTISED = {
    "schemaVersion": 1,
    "producer": "waveguide-generator",
    "solveCommandDelivery": 2,
    "fusionRequestDelivery": 2,
}


def test_an_advertised_wg_gets_one_file_per_solve_command(ipc: Path, tmp_path: Path) -> None:
    _write(ipc / CAPABILITIES, ADVERTISED)

    written = _write_solve(ipc, tmp_path)

    assert written == ipc / SOLVE_FOLDER / "cmd-1.json"
    payload = _read(written)
    assert payload == {
        "schemaVersion": 2,
        "target": "waveguide-generator",
        "commandId": "cmd-1",
        "operationId": "cmd-1",
        "returnId": "wgr_1",
        "bundlePath": "wgreturn/speaker.wgreturn",
        "manifestSha256": "sha256:" + hashlib.sha256(b'{"document": {}}').hexdigest(),
        "requestedAt": "2026-09-13T01:00:00Z",
    }
    assert not (ipc / SOLVE_SLOT).exists()
    # WG takes every *.json name not starting with "."; staging never matches.
    assert sorted(path.name for path in (ipc / SOLVE_FOLDER).iterdir()) == ["cmd-1.json"]


def test_unknown_capability_fields_and_later_versions_still_select_the_file(
    ipc: Path, tmp_path: Path
) -> None:
    _write(ipc / CAPABILITIES, {
        "schemaVersion": 1,
        "solveCommandDelivery": 3,
        "somethingNew": {"nested": True},
    })

    assert _write_solve(ipc, tmp_path).parent.name == SOLVE_FOLDER


@pytest.mark.parametrize(
    "capability",
    [
        None,
        "not json at all",
        [],
        {"schemaVersion": 2, "solveCommandDelivery": 2},
        {"schemaVersion": True, "solveCommandDelivery": 2},
        {"schemaVersion": 1},
        {"schemaVersion": 1, "solveCommandDelivery": 1},
        {"schemaVersion": 1, "solveCommandDelivery": "2"},
        {"schemaVersion": 1, "solveCommandDelivery": True},
        {"schemaVersion": 1, "solveCommandDelivery": 2.0},
    ],
    ids=[
        "absent",
        "unreadable",
        "not-an-object",
        "unknown-schema",
        "boolean-schema",
        "not-advertised",
        "version-1",
        "string",
        "boolean",
        "float",
    ],
)
def test_anything_but_a_clear_advertisement_keeps_the_legacy_slot(
    ipc: Path, tmp_path: Path, capability: Any
) -> None:
    if isinstance(capability, str):
        (ipc / CAPABILITIES).write_text(capability, encoding="utf-8")
    elif capability is not None:
        _write(ipc / CAPABILITIES, capability)

    written = _write_solve(ipc, tmp_path)

    assert written == ipc / SOLVE_SLOT
    assert _read(written)["schemaVersion"] == 1
    assert not (ipc / SOLVE_FOLDER).exists()


def test_two_solve_commands_both_survive_when_wg_reads_files(
    ipc: Path, tmp_path: Path
) -> None:
    """The legacy slot's write-side race ends once the add-in writes files."""

    _write(ipc / CAPABILITIES, ADVERTISED)

    _write_solve(ipc, tmp_path, "cmd-1")
    _write_solve(ipc, tmp_path, "cmd-2")

    assert _taken_names(ipc / SOLVE_FOLDER) == ["cmd-1", "cmd-2"]


def test_a_per_command_write_leaves_a_waiting_legacy_command_alone(
    ipc: Path, tmp_path: Path
) -> None:
    waiting = _write_solve(ipc, tmp_path, "cmd-old")
    _write(ipc / CAPABILITIES, ADVERTISED)

    _write_solve(ipc, tmp_path, "cmd-new")

    assert _read(waiting)["commandId"] == "cmd-old"
    assert _taken_names(ipc / SOLVE_FOLDER) == ["cmd-new"]


def test_a_command_id_that_is_not_a_plain_file_name_is_refused(
    ipc: Path, tmp_path: Path
) -> None:
    _write(ipc / CAPABILITIES, ADVERTISED)

    with pytest.raises(ValueError):
        _write_solve(ipc, tmp_path, "../escape")

    assert not (ipc / SOLVE_FOLDER).exists() or _taken_names(ipc / SOLVE_FOLDER) == []


# -- new WG, new add-in ---------------------------------------------------------


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_each_request_runs_once_from_its_own_file_in_sequence_order(
    kind: str, ipc: Path, bundles: Path
) -> None:
    wg = NewWG(ipc, bundles)
    # Name order is the reverse of the order WG published them in.
    first = wg.publish(kind, 1, request_id="zz-first")
    second = wg.publish(kind, 2, request_id="aa-second")

    assert _take(kind, ipc, bundles) == [first, second]
    assert _take(kind, ipc, bundles) == []

    folder = ipc / KINDS[kind][1]
    assert _taken_names(folder) == []
    assert [path.name for path in folder.iterdir() if path.name.startswith(".")] == [SLOT_RECORD]
    # The twin stays: only a legacy reader takes it.
    assert _read(ipc / KINDS[kind][0])["operationId"] == second
    assert _read(folder / SLOT_RECORD) == {"operationId": second, SEQUENCE: 2}


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_twin_is_never_run_and_never_deleted(
    kind: str, ipc: Path, bundles: Path
) -> None:
    request_id = NewWG(ipc, bundles).publish(kind, 1)
    # Another per-request reader already took the file.
    (ipc / KINDS[kind][1] / f"{request_id}.json").unlink()
    twin = (ipc / KINDS[kind][0]).read_bytes()

    assert _take(kind, ipc, bundles) == []
    assert (ipc / KINDS[kind][0]).read_bytes() == twin


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
@pytest.mark.parametrize("sequence", [None, 0, True, "3"], ids=["none", "zero", "bool", "text"])
def test_a_file_without_a_valid_sequence_is_left_where_it_is(
    kind: str, sequence: Any, ipc: Path, bundles: Path
) -> None:
    folder = ipc / KINDS[kind][1]
    body = {
        **_request_body(kind, 1, bundles, SESSION),
        "schemaVersion": 2,
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
    folder = ipc / KINDS[kind][1]
    body = {
        **_request_body(kind, 1, bundles, SESSION),
        "schemaVersion": 2,
        "requestId": "req-a",
        "operationId": "req-b",
        SEQUENCE: 1,
    }
    _write(folder / "req-a.json", body)

    assert _take(kind, ipc, bundles) == []
    assert _taken_names(folder) == ["req-a"]


def test_a_return_request_for_another_session_waits_for_it(
    ipc: Path, bundles: Path
) -> None:
    other = NewWG(ipc, bundles).publish(RETURNS, 1, session="session-other")

    assert _take(RETURNS, ipc, bundles) == []
    assert _take(RETURNS, ipc, bundles, session="session-other") == [other]


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_publish_while_a_request_runs_loses_nothing(
    kind: str, ipc: Path, bundles: Path
) -> None:
    wg = NewWG(ipc, bundles)
    first = wg.publish(kind, 1)
    published: list[str] = []

    ran = _take(
        kind,
        ipc,
        bundles,
        while_running=lambda _id: published.append(wg.publish(kind, 2))
        if not published else None,
    )

    assert ran == [first, published[0]]
    assert _taken_names(ipc / KINDS[kind][1]) == []


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_failed_claim_leaves_the_request_for_the_next_pass(
    kind: str, ipc: Path, bundles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = NewWG(ipc, bundles).publish(kind, 1)
    real_rename = os.rename
    refusals = {"left": 1}

    def busy_rename(source, destination, *args, **kwargs):
        if Path(source).name == f"{request_id}.json" and refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError("WG has the file open")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", busy_rename)

    assert _take(kind, ipc, bundles) == []
    assert _taken_names(ipc / KINDS[kind][1]) == [request_id]
    assert _take(kind, ipc, bundles) == [request_id]


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_claim_that_cannot_be_deleted_is_still_never_run_again(
    kind: str, ipc: Path, bundles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = NewWG(ipc, bundles).publish(kind, 1)
    real_unlink = Path.unlink

    def stuck(self, *args, **kwargs):
        if self.parent.name == KINDS[kind][1] and self.name != SLOT_RECORD:
            raise PermissionError("held open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stuck)

    assert _take(kind, ipc, bundles) == [request_id]
    assert _take(kind, ipc, bundles) == []


# -- new WG after an old add-in: discard what a legacy reader took --------------


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_requests_a_legacy_reader_took_or_never_saw_are_discarded_not_run(
    kind: str, ipc: Path, bundles: Path
) -> None:
    wg = NewWG(ipc, bundles)
    folder = ipc / KINDS[kind][1]
    first = wg.publish(kind, 1)
    second = wg.publish(kind, 2)
    # An add-in that reads only the slot took the second request's twin; the
    # first had been replaced in the slot before it looked.
    (ipc / KINDS[kind][0]).unlink()
    # WG is part-way through publishing a third: its file exists, its twin not yet.
    third = wg.publish(kind, 3, twin=False)
    _write(folder / "unsequenced.json", {"schemaVersion": 2})

    assert _take(kind, ipc, bundles) == [third]
    assert sorted({first, second} & set(_taken_names(folder))) == []
    assert _taken_names(folder) == ["unsequenced"]


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_the_record_is_read_before_the_slot_so_a_publish_in_between_survives(
    kind: str, ipc: Path, bundles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking the slot first can discard a request WG publishes in between."""

    wg = NewWG(ipc, bundles)
    wg.publish(kind, 1)
    (ipc / KINDS[kind][0]).unlink()  # a legacy reader took it
    slot_name = KINDS[kind][0]
    published: list[str] = []
    real_lexists = os.path.lexists

    def publish_after_the_slot_check(path) -> bool:
        present = real_lexists(path)
        if Path(path).name == slot_name and not published:
            published.append(wg.publish(kind, 2))
        return present

    monkeypatch.setattr(os.path, "lexists", publish_after_the_slot_check)

    ran = _take(kind, ipc, bundles)

    assert published and ran == [published[0]]


# -- old WG, new add-in ---------------------------------------------------------


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
def test_a_slot_without_an_operation_id_is_the_only_copy_and_runs_once(
    kind: str, ipc: Path, bundles: Path
) -> None:
    ran = OldWG(ipc, bundles).publish(kind, 1)

    assert _take(kind, ipc, bundles) == [ran]
    assert not (ipc / KINDS[kind][0]).exists()
    assert _take(kind, ipc, bundles) == []


@pytest.mark.parametrize("kind", [RETURNS, HANDOFFS])
@pytest.mark.parametrize("replacement", ["old-wg", "new-wg"])
def test_a_slot_replaced_while_its_request_ran_is_not_deleted(
    kind: str, replacement: str, ipc: Path, bundles: Path
) -> None:
    first = OldWG(ipc, bundles).publish(kind, 1)
    replaced: list[str] = []

    def replace_the_slot(_id: str) -> None:
        if replaced:
            return
        if replacement == "old-wg":
            replaced.append(OldWG(ipc, bundles).publish(kind, 2))
        else:
            replaced.append(NewWG(ipc, bundles).publish(kind, 2))

    ran = _take(kind, ipc, bundles, while_running=replace_the_slot)

    # The older request's acknowledgement found another request in the slot
    # and deleted nothing, so the newer one still ran, once.
    assert ran == [first, replaced[0]]
    if replacement == "new-wg":
        # It ran from its own file; its twin stays for a legacy reader.
        assert _read(ipc / KINDS[kind][0])["operationId"] == replaced[0]
    else:
        assert not (ipc / KINDS[kind][0]).exists()


def test_an_old_wg_handoff_ran_then_its_legacy_acknowledgement_still_works(
    ipc: Path, bundles: Path
) -> None:
    OldWG(ipc, bundles).publish(HANDOFFS, 1)
    pending = wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)

    assert pending is not None and pending.per_request is False
    assert pending.operation_id == ""
    assert wglink_watch.acknowledge_handoff(pending, bundle_root=bundles) is True


# -- what the reader hands the dispatcher ---------------------------------------


def test_a_per_request_handoff_carries_its_operation_identity(
    ipc: Path, bundles: Path
) -> None:
    request_id = NewWG(ipc, bundles).publish(HANDOFFS, 7)

    pending = wglink_watch.next_pending_handoff(ipc, bundle_root=bundles)

    assert pending is not None
    assert pending.per_request is True
    assert pending.request_id == pending.operation_id == request_id
    assert pending.delivery_sequence == 1
    assert pending.export_id == "wge_7"
    assert pending.expected_instance_id == "instance-a"


def test_a_per_request_handoff_outside_the_bundle_folder_is_never_offered(
    ipc: Path, bundles: Path, tmp_path: Path
) -> None:
    request_id = NewWG(ipc, bundles).publish(HANDOFFS, 1)
    elsewhere = tmp_path / "elsewhere" / "horn.wglink"
    elsewhere.mkdir(parents=True)
    path = ipc / KINDS[HANDOFFS][1] / f"{request_id}.json"
    payload = _read(path)
    payload["bundlePath"] = str(elsewhere)
    _write(path, payload)

    assert wglink_watch.next_pending_handoff(ipc, bundle_root=bundles) is None


# -- the heartbeat carries the reconciliation evidence -------------------------


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
