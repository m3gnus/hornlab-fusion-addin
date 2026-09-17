"""The durable outbox: WG-bound deliveries over the live session, beside the v3 files.

Contract: Waveguide Generator ``docs/reference/CADLINK-LIVE-PROTOCOL.md``
section 8 ("WG-bound deliveries and the outbox"), 9 and 10, as WG implements
``POST /api/cadlink/live/deliveries`` (``server/cadlink/live/deliveries.py``,
``solve_command.deliver_live``). The rules this file holds the client to:

- an item has one operation id, minted once and never changed, and it is on
  disk before anything is sent;
- a 200 is final for the item whatever the operation's state, a rejected one
  included (shown to the user, then deleted);
- ``503 snapshot_not_readable`` is retried until WG answers 200 at its 30 s
  bound; ``503 store_busy`` a bounded number of times; the retryable 409s with
  backoff; a solve falls back to its v3 file (same id) only on a network
  failure, a 401 a new registration does not cure, or a bounded client guard;
- ``409 operation_conflict`` is never retried under that id;
- a ``receive_snapshot`` never becomes a file.

Most tests drive :meth:`wglink_live.LiveClient.step` synchronously against
``DeliveringWG``, the in-process stand-in of ``test_wglink_live`` extended with
the deliveries route and WG's acceptance by digest. Items, v3 solve files and
bundles are real files. The recorded-exchange test replays answers recorded
from a real WG.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
sys.path.insert(0, str(ROOT / "tests"))
import wglink_live  # noqa: E402
import wglink_watch  # noqa: E402

from test_wglink_live import (  # noqa: E402
    INSTALLATION_HEADER,
    LIVE,
    POSIX,
    Clock,
    FakeWG,
    Replay,
    _fixture,
    _ipc,
    _refusal,
    _replay_client,
    _steps,
    stub_factory,  # noqa: F401 - fixture
)


SOLVE = "prepare_and_solve"
SNAPSHOT = "receive_snapshot"
BODY_FIELDS = {"operationId", "kind", "returnId", "bundlePath", "manifestSha256", "requestedAt"}


# -- WG stand-in -----------------------------------------------------------------


class DeliveringWG(FakeWG):
    """``FakeWG`` plus ``POST /deliveries`` accepted once per operation id by digest.

    ``operations`` is WG's store: id -> {kind, digest, state, reason, message}.
    ``accepted`` counts the acceptances that created an operation.
    """

    def __init__(self, ipc: Path, **kwargs: Any) -> None:
        super().__init__(ipc, **kwargs)
        self.operations: dict[str, dict[str, Any]] = {}
        self.created: list[str] = []

    def accept(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """WG's ``accept_delivery``: created, recovered or conflict."""

        digest = (body["kind"], body["bundlePath"], body["manifestSha256"], body.get("returnId"))
        existing = self.operations.get(body["operationId"])
        if existing is None:
            state = "accepted" if body["kind"] == SNAPSHOT else "received"
            existing = {"kind": body["kind"], "digest": digest, "state": state, "reason": None, "message": None}
            self.operations[body["operationId"]] = existing
            self.created.append(body["operationId"])
            return "created", existing
        if existing["digest"] != digest:
            return "conflict", existing
        return "recovered", existing

    def collect_file(self, path: Path) -> str:
        """WG's v3 file pass for one solve file: accept by the same digest, then delete."""

        payload = json.loads(path.read_text(encoding="utf-8"))
        result, _row = self.accept({
            "operationId": payload["commandId"], "kind": SOLVE, "returnId": payload["returnId"],
            "bundlePath": payload["bundlePath"], "manifestSha256": payload["manifestSha256"],
        })
        path.unlink()
        return result

    def answer(self, request):  # noqa: ANN001
        route = (request.method, request.path[len(LIVE):])
        if route != ("POST", "/deliveries") or self.down:
            return super().answer(request)
        queued = self.script.get(route)
        if queued:
            item = queued.pop(0)
            return item(request) if callable(item) else item
        if not request.headers.get(INSTALLATION_HEADER):
            return _refusal(401, "installation_mismatch")
        if request.headers.get("Authorization") != f"Bearer {self.current}":
            return _refusal(401, "session_unknown")
        return self.deliver(request.body)

    def deliver(self, body: dict[str, Any]) -> "wglink_live.Answer":
        assert set(body) == BODY_FIELDS - ({"returnId"} if body["kind"] == SNAPSHOT else set()), body
        assert all(isinstance(value, str) and value for value in body.values()), body
        result, row = self.accept(body)
        if result == "conflict":
            return _refusal(409, "operation_conflict")
        return wglink_live.Answer(200, {"result": result, "operation": {
            "operationId": body["operationId"], "kind": row["kind"], "state": row["state"],
            "stage": None, "reason": row["reason"], "message": row["message"],
        }})


def _transient(retry_after: str | None = "1") -> "wglink_live.Answer":
    answer = _refusal(503, "snapshot_not_readable", retryable=True)
    answer.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return answer


def _lost_after_accepting(wg: DeliveringWG):
    """WG accepted the delivery, and the answer never reached the client."""

    def lose(request):  # noqa: ANN001
        wg.deliver(request.body)
        raise wglink_live.NetworkFailure("connection reset")

    return lose


# -- helpers -----------------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "wgreturn").mkdir(parents=True, exist_ok=True)
    return workspace


def _bundle(workspace: Path, name: str = "speaker", content: bytes = b'{"return": 1}') -> Path:
    bundle = workspace / "wgreturn" / f"{name}.wgreturn"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "wgreturn.json").write_bytes(content)
    return bundle


def _client(ipc: Path, wg: DeliveringWG, clock: Clock, **kwargs: Any) -> "wglink_live.LiveClient":
    from test_wglink_live import _client as base_client

    kwargs.setdefault("solve_files", wglink_watch)
    return base_client(ipc, wg, clock=clock, **kwargs)


def _setup(tmp_path: Path, *, live: bool = True, **kwargs: Any):
    ipc = _ipc(tmp_path)
    wg = DeliveringWG(ipc)
    wg.publish()
    clock = Clock()
    client = _client(ipc, wg, clock, **kwargs)
    if live:
        _steps(client, 2)
        assert client.healthy(), client.status()
    return ipc, wg, client, clock, _workspace(tmp_path)


def _solve_item(ipc: Path, workspace: Path, bundle: Path, *, healthy: bool, operation_id: str | None = None,
                clock: Clock | None = None) -> dict[str, Any]:
    """What ``WGLink._request_wg_solve`` does on the main thread (no network)."""

    return wglink_live.produce_solve(
        ipc,
        bundle_path=bundle,
        workspace_root=workspace,
        return_id="wgr_1",
        healthy=healthy,
        solve_files=wglink_watch,
        operation_id=operation_id,
        wall=(clock.wall if clock is not None else None),
    )


def _snapshot_item(ipc: Path, workspace: Path, bundle: Path, clock: Clock | None = None) -> dict[str, Any]:
    return wglink_live.produce_snapshot(
        ipc, bundle_path=bundle, workspace_root=workspace, solve_files=wglink_watch,
        wall=(clock.wall if clock is not None else None),
    )


def _outbox(ipc: Path) -> Path:
    return ipc / ".wglink-outbox"


def _items(ipc: Path) -> dict[str, dict[str, Any]]:
    folder = _outbox(ipc)
    if not folder.is_dir():
        return {}
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in folder.iterdir()
        if path.suffix == ".json" and not path.name.startswith(".")
    }


def _solve_files(ipc: Path) -> list[Path]:
    folder = ipc / ".wg-solve-requests"
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if path.suffix == ".json" and not path.name.startswith("."))


def _deliveries(wg: DeliveringWG) -> list[Any]:
    return wg.of("POST", "/deliveries")


# -- item identity and persistence ---------------------------------------------------


def test_an_outbox_item_is_on_disk_with_a_fixed_id_before_anything_is_sent(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    bundle = _bundle(workspace)
    item = _solve_item(ipc, workspace, bundle, healthy=True, clock=clock)

    assert _deliveries(wg) == []
    stored = _items(ipc)
    assert list(stored) == [item["operationId"]]
    manifest = "sha256:" + hashlib.sha256((bundle / "wgreturn.json").read_bytes()).hexdigest()
    assert {k: stored[item["operationId"]][k] for k in ("kind", "returnId", "bundlePath", "manifestSha256")} == {
        "kind": SOLVE, "returnId": "wgr_1", "bundlePath": "wgreturn/speaker.wgreturn", "manifestSha256": manifest,
    }
    assert stored[item["operationId"]]["fileWritten"] is False
    # Healthy: the live POST is the carrier; no v3 file yet.
    assert _solve_files(ipc) == []

    client.enqueue_delivery()
    client.step()
    posts = _deliveries(wg)
    assert len(posts) == 1
    assert posts[0].body == {
        "operationId": item["operationId"], "kind": SOLVE, "returnId": "wgr_1",
        "bundlePath": "wgreturn/speaker.wgreturn", "manifestSha256": manifest,
        "requestedAt": stored[item["operationId"]]["requestedAt"],
    }
    assert posts[0].headers["Authorization"] == f"Bearer {wg.current}"
    assert _items(ipc) == {}
    assert wg.created == [item["operationId"]]


def test_a_snapshot_body_names_no_return_id_and_never_becomes_a_file(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path, live=False)
    item = _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    wg.down = True
    for _ in range(40):
        clock.now += 2
        client.step()
    assert _solve_files(ipc) == []
    assert list(_items(ipc)) == [item["operationId"]]
    wg.down = False
    for _ in range(40):
        clock.now += 2
        client.step()
    posts = _deliveries(wg)
    assert len(posts) == 1 and "returnId" not in posts[0].body and posts[0].body["kind"] == SNAPSHOT
    assert _items(ipc) == {} and _solve_files(ipc) == []
    assert wg.operations[item["operationId"]]["state"] == "accepted"


def test_items_are_delivered_oldest_first(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path, live=False)
    ids = []
    for n in range(3):
        ids.append(_snapshot_item(ipc, workspace, _bundle(workspace, f"s{n}"), clock)["operationId"])
        clock.now += 1
    _steps(client, 8)
    assert [p.body["operationId"] for p in _deliveries(wg)] == ids


def test_the_outbox_is_private_and_never_holds_session_secrets(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    wg.script[("POST", "/deliveries")] = [_transient()]
    client.enqueue_delivery()
    client.step()
    assert len(_items(ipc)) == 1
    text = "".join(path.read_text() for path in _outbox(ipc).iterdir())
    for secret in [*wg.tokens, wg.secret]:
        assert secret not in text
    if POSIX:
        assert stat.S_IMODE(os.stat(_outbox(ipc)).st_mode) == 0o700
        for path in _outbox(ipc).iterdir():
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_nothing_is_queued_when_wg_does_not_advertise_the_live_protocol(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    (ipc / "wg-capabilities.json").write_text(json.dumps(
        {"schemaVersion": 1, "solveCommandDelivery": 3, "fusionRequestDelivery": 3}
    ))
    workspace = _workspace(tmp_path)
    assert wglink_live.advertises_live(ipc) is False
    assert _snapshot_item(ipc, workspace, _bundle(workspace)) is None
    assert _solve_item(ipc, workspace, _bundle(workspace), healthy=False) is None
    assert _items(ipc) == {}


# -- answers ------------------------------------------------------------------------------


def test_a_lost_200_is_retried_under_the_same_id_and_recovered_once(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.script[("POST", "/deliveries")] = [_lost_after_accepting(wg)]
    client.enqueue_delivery()
    client.step()
    assert not client.healthy()
    assert list(_items(ipc)) == [item["operationId"]]
    for _ in range(10):
        clock.now += 2
        client.step()
    assert [p.body["operationId"] for p in _deliveries(wg)] == [item["operationId"]] * 2
    assert wg.created == [item["operationId"]]
    assert _items(ipc) == {}
    # The network failure also wrote the v3 file under the same id; WG recovers it.
    [path] = _solve_files(ipc)
    assert path.stem == item["operationId"]
    assert wg.collect_file(path) == "recovered"
    assert list(wg.operations) == [item["operationId"]]


def test_snapshot_not_readable_is_retried_until_wg_answers_200_at_its_bound(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.script[("POST", "/deliveries")] = [_transient() for _ in range(30)]
    client.enqueue_delivery()
    for _ in range(31):
        client.step()
        clock.now += 1
    posts = _deliveries(wg)
    assert len(posts) == 31
    assert {p.body["operationId"] for p in posts} == {item["operationId"]}
    assert _solve_files(ipc) == [], "a transient answer never becomes a file"
    assert _items(ipc) == {}
    assert client.healthy()


def test_snapshot_not_readable_honours_retry_after(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    wg.script[("POST", "/deliveries")] = [_transient("3")]
    client.enqueue_delivery()
    client.step()
    for _ in range(4):
        clock.now += 0.5
        client.step()
    assert len(_deliveries(wg)) == 1
    clock.now += 1.1
    client.step()
    assert len(_deliveries(wg)) == 2


def test_a_wg_that_keeps_answering_not_readable_past_its_bound_gets_the_file(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.script[("POST", "/deliveries")] = [_transient() for _ in range(200)]
    client.enqueue_delivery()
    for _ in range(59):
        client.step()
        clock.now += 1
        client.offer_heartbeat({"n": 1})
    assert _solve_files(ipc) == []
    for _ in range(3):
        client.step()
        clock.now += 1
    [path] = _solve_files(ipc)
    assert path.stem == item["operationId"]
    assert _items(ipc)[item["operationId"]]["fileWritten"] is True
    posts = len(_deliveries(wg))
    for _ in range(20):
        clock.now += 1
        client.offer_heartbeat({"n": 2})
        client.step()
    assert len(_deliveries(wg)) == posts, "past the guard the item slows down"


def test_a_wg_restart_between_503_and_the_retry_delivers_the_same_operation(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    wg.script[("POST", "/deliveries")] = [_transient()]
    client.enqueue_delivery()
    client.step()
    assert len(_items(ipc)) == 1

    restarted = DeliveringWG(ipc, instance="b" * 32, secret="wg-secret-2", port=41002)
    restarted.operations = wg.operations  # the store persists across the restart
    restarted.created = wg.created
    wg.down = True
    restarted.publish()
    client._transport_factory = restarted.transport  # noqa: SLF001 - the new endpoint's transport
    for _ in range(10):
        clock.now += 1
        client.step()
    assert client.status()["instanceId"] == restarted.instance
    assert [p.body["operationId"] for p in _deliveries(restarted)] == [item["operationId"]]
    assert wg.created == [item["operationId"]]
    assert _items(ipc) == {}


def test_the_not_readable_guard_starts_again_with_a_restarted_wg(tmp_path: Path) -> None:
    """A WG restart forgets its 30 s bound; the client's guard is per WG instance."""

    ipc, wg, client, clock, workspace = _setup(tmp_path)
    _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.script[("POST", "/deliveries")] = [_transient() for _ in range(100)]
    client.enqueue_delivery()
    for _ in range(50):
        client.step()
        clock.now += 1
        client.offer_heartbeat({"n": 1})
    restarted = DeliveringWG(ipc, instance="b" * 32, secret="wg-secret-2", port=41002)
    restarted.script[("POST", "/deliveries")] = [_transient() for _ in range(100)]
    wg.down = True
    restarted.publish()
    client._transport_factory = restarted.transport  # noqa: SLF001
    for _ in range(25):
        client.step()
        clock.now += 1
        client.offer_heartbeat({"n": 2})
    assert client.status()["instanceId"] == restarted.instance
    assert len(_deliveries(restarted)) >= 20
    assert _solve_files(ipc) == [], "a new WG instance starts a new bound"


def test_store_busy_is_retried_a_bounded_number_of_times_then_a_solve_gets_its_file(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    solve = _solve_item(ipc, workspace, _bundle(workspace, "a"), healthy=True, clock=clock)
    clock.now += 1
    snapshot = _snapshot_item(ipc, workspace, _bundle(workspace, "b"), clock)
    busy = _refusal(503, "store_busy", retryable=True)
    wg.script[("POST", "/deliveries")] = [busy] * 100
    client.enqueue_delivery()
    for _ in range(60):
        client.step()
        clock.now += 1
        client.offer_heartbeat({"n": 1})
    by_id: dict[str, int] = {}
    for post in _deliveries(wg):
        by_id[post.body["operationId"]] = by_id.get(post.body["operationId"], 0) + 1
    assert 5 <= by_id[solve["operationId"]] <= 8
    assert [p.stem for p in _solve_files(ipc)] == [solve["operationId"]]
    stored = _items(ipc)
    assert stored[solve["operationId"]]["fileWritten"] is True
    assert "fileWritten" not in stored[snapshot["operationId"]] or stored[snapshot["operationId"]]["fileWritten"] is False
    assert client.healthy()


@pytest.mark.parametrize("code", ["wglink_folder_not_selected", "update_restart_pending"])
def test_retryable_refusals_are_retried_over_http_and_never_become_a_file(tmp_path: Path, code: str) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.script[("POST", "/deliveries")] = [_refusal(409, code, retryable=True)] * 6
    client.enqueue_delivery()
    for _ in range(80):
        client.step()
        clock.now += 1
        client.offer_heartbeat({"n": 1})
    assert _solve_files(ipc) == []
    assert _items(ipc) == {}
    assert len(_deliveries(wg)) == 7
    assert wg.created == [item["operationId"]]


def test_an_operation_conflict_is_never_retried_and_is_shown_then_deleted(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    bundle = _bundle(workspace)
    first = _solve_item(ipc, workspace, bundle, healthy=True, clock=clock)
    client.enqueue_delivery()
    client.step()
    assert wg.created == [first["operationId"]]
    assert [(n["outcome"], n["state"], n["durable"]) for n in client.take_delivery_notices()] == [
        ("delivered", "received", False)
    ]
    # The same id for a different return: a bug, never retried.
    (bundle / "wgreturn.json").write_bytes(b'{"changed": true}')
    second = _solve_item(ipc, workspace, bundle, healthy=True, operation_id=first["operationId"], clock=clock)
    client.enqueue_delivery()
    for _ in range(20):
        client.step()
        clock.now += 5
    assert len(_deliveries(wg)) == 2
    assert _solve_files(ipc) == []
    notices = client.take_delivery_notices()
    assert [(n["operationId"], n["outcome"]) for n in notices] == [(second["operationId"], "conflict")]
    # Durable until the main thread has shown it.
    assert _items(ipc)[second["operationId"]]["answer"]["outcome"] == "conflict"
    client.acknowledge_delivery_notice(second["operationId"])
    client.step()
    assert _items(ipc) == {}
    assert len(_deliveries(wg)) == 2


def test_a_rejected_200_is_final_its_reason_is_shown_and_the_item_deleted(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    message = "WG could not read this snapshot in the WGLink folder for 24 hours. Send it again from Fusion."
    wg.script[("POST", "/deliveries")] = [wglink_live.Answer(200, {"result": "recovered", "operation": {
        "operationId": item["operationId"], "kind": SNAPSHOT, "state": "rejected", "stage": None,
        "reason": "snapshot_unavailable", "message": message,
    }})]
    client.enqueue_delivery()
    for _ in range(10):
        client.step()
        clock.now += 5
    assert len(_deliveries(wg)) == 1
    [notice] = client.take_delivery_notices()
    assert (notice["outcome"], notice["state"], notice["reason"], notice["message"]) == (
        "rejected", "rejected", "snapshot_unavailable", message,
    )
    assert notice["kind"] == SNAPSHOT
    client.acknowledge_delivery_notice(item["operationId"])
    client.step()
    assert _items(ipc) == {}
    assert len(_deliveries(wg)) == 1


def test_an_unacknowledged_rejection_is_shown_again_after_a_restart(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    wg.script[("POST", "/deliveries")] = [wglink_live.Answer(200, {"result": "created", "operation": {
        "operationId": item["operationId"], "kind": SNAPSHOT, "state": "rejected", "stage": None,
        "reason": "snapshot_invalid", "message": "malformed",
    }})]
    client.enqueue_delivery()
    client.step()
    assert len(client.take_delivery_notices()) == 1
    client.end()  # Fusion quits before the notice was shown

    again = _client(ipc, wg, clock)
    _steps(again, 3)
    [notice] = again.take_delivery_notices()
    assert (notice["operationId"], notice["reason"]) == (item["operationId"], "snapshot_invalid")
    assert len(_deliveries(wg)) == 1, "an answered item is never delivered again"
    again.acknowledge_delivery_notice(item["operationId"])
    again.step()
    assert _items(ipc) == {}


def test_a_200_with_an_unexpected_body_is_still_final(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    wg.script[("POST", "/deliveries")] = [wglink_live.Answer(200, {"surprise": True})]
    client.enqueue_delivery()
    _steps(client, 4)
    assert _items(ipc) == {}
    assert len(_deliveries(wg)) == 1


@pytest.mark.parametrize(
    "answer", [_refusal(400, "invalid_request"), _refusal(413, "request_too_large"),
               wglink_live.Answer(500, None), _refusal(409, "something_new")],
    ids=["400", "413", "500", "unknown-409"],
)
def test_an_unexpected_refusal_keeps_the_item_gives_a_solve_its_file_and_slows_down(tmp_path: Path, answer) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    solve = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.script[("POST", "/deliveries")] = [answer] * 50
    client.enqueue_delivery()
    for _ in range(20):
        client.step()
        clock.now += 1
        client.offer_heartbeat({"n": 1})
    assert len(_deliveries(wg)) == 1
    assert [p.stem for p in _solve_files(ipc)] == [solve["operationId"]]
    assert list(_items(ipc)) == [solve["operationId"]]
    assert client.healthy()
    clock.now += 31
    client.step()
    assert len(_deliveries(wg)) == 2


# -- sessions, offline and the v3 file ------------------------------------------------------


def test_a_solve_made_with_no_healthy_session_is_the_v3_file_with_the_same_id(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path, live=False)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=False, clock=clock)
    [path] = _solve_files(ipc)
    payload = json.loads(path.read_text())
    stored = _items(ipc)[item["operationId"]]
    assert path.stem == payload["commandId"] == payload["operationId"] == item["operationId"]
    assert {k: payload[k] for k in ("returnId", "bundlePath", "manifestSha256", "requestedAt")} == {
        k: stored[k] for k in ("returnId", "bundlePath", "manifestSha256", "requestedAt")
    }
    assert stored["fileWritten"] is True

    # WG starts: it takes the file first, and the live delivery recovers the same operation.
    assert wg.collect_file(path) == "created"
    _steps(client, 3)
    [post] = _deliveries(wg)
    assert post.body["operationId"] == item["operationId"]
    assert wg.created == [item["operationId"]]
    assert _items(ipc) == {}


def test_file_and_live_both_present_are_one_operation(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path, live=False)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=False, clock=clock)
    _steps(client, 3)  # live first
    [path] = _solve_files(ipc)
    assert wg.collect_file(path) == "recovered"  # then the file pass
    assert list(wg.operations) == [item["operationId"]] and wg.created == [item["operationId"]]


def test_a_network_failure_mid_delivery_writes_the_file_with_the_same_id(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.down = True
    client.enqueue_delivery()
    client.step()
    assert not client.healthy()
    client.step()
    [path] = _solve_files(ipc)
    assert path.stem == item["operationId"]
    assert _items(ipc)[item["operationId"]]["fileWritten"] is True


def test_nothing_is_written_as_a_file_while_a_401_is_being_cured(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.script[("POST", "/deliveries")] = [_refusal(401, "token_expired")]
    client.enqueue_delivery()
    client.step()
    assert not client.healthy()
    client.step()  # registers again
    client.step()  # delivers the same item
    assert _solve_files(ipc) == []
    assert [p.body["operationId"] for p in _deliveries(wg)] == [item["operationId"]] * 2
    assert len(wg.of("POST", "/sessions")) == 2
    assert _items(ipc) == {}


def test_a_401_a_new_registration_does_not_cure_writes_the_file(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    wg.script[("POST", "/deliveries")] = [_refusal(401, "session_superseded")] * 10
    client.enqueue_delivery()
    for _ in range(6):
        client.step()
        clock.now += 1
    assert len(wg.of("POST", "/sessions")) == 2
    [path] = _solve_files(ipc)
    assert path.stem == item["operationId"]


def test_while_offline_a_file_wg_has_taken_settles_its_item(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path, live=False)
    wg.down = True
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=False, clock=clock)
    _steps(client, 3)
    assert list(_items(ipc)) == [item["operationId"]]
    [path] = _solve_files(ipc)
    path.unlink()  # WG (without a live session) took it
    clock.now += 6
    _steps(client, 2)
    assert _items(ipc) == {}


def test_an_add_in_restart_with_a_pending_item_delivers_it_once(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path)
    item = _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    client.end()  # stopped before the worker delivered it

    again = _client(ipc, wg, clock)
    for _ in range(6):
        again.step()
        clock.now += 1
    assert [p.body["operationId"] for p in _deliveries(wg)] == [item["operationId"]]
    assert _items(ipc) == {} and wg.created == [item["operationId"]]


def test_the_standby_never_touches_the_outbox(tmp_path: Path) -> None:
    ipc, wg, _client_unused, clock, workspace = _setup(tmp_path, live=False)
    item = _solve_item(ipc, workspace, _bundle(workspace), healthy=True, clock=clock)
    standby = _client(ipc, wg, clock, lease=lambda: False)
    for _ in range(10):
        clock.now += 5
        standby.step()
    assert _deliveries(wg) == [] and _solve_files(ipc) == []
    assert _items(ipc)[item["operationId"]]["fileWritten"] is False


# -- bounds ---------------------------------------------------------------------------------


def test_the_outbox_is_bounded_and_a_full_one_still_writes_the_solve_file(tmp_path: Path) -> None:
    ipc, wg, _client_unused, clock, workspace = _setup(tmp_path, live=False)
    bundle = _bundle(workspace)
    for _ in range(wglink_live.OUTBOX_MAX_ITEMS):
        assert _snapshot_item(ipc, workspace, bundle, clock) is not None
    with pytest.raises(wglink_live.OutboxFull):
        _snapshot_item(ipc, workspace, bundle, clock)
    with pytest.raises(wglink_live.OutboxFull):
        _solve_item(ipc, workspace, bundle, healthy=True, clock=clock)
    assert len(_items(ipc)) == wglink_live.OUTBOX_MAX_ITEMS
    # The solve still went out as its v3 file, the carrier that needs no outbox.
    assert len(_solve_files(ipc)) == 1


def test_an_item_older_than_the_age_bound_is_removed_with_a_notice(tmp_path: Path) -> None:
    ipc, wg, client, clock, workspace = _setup(tmp_path, live=False)
    wg.down = True
    item = _snapshot_item(ipc, workspace, _bundle(workspace), clock)
    _steps(client, 2)
    assert client.take_delivery_notices() == []
    clock.now += wglink_live.OUTBOX_MAX_AGE_SECONDS + 10
    _steps(client, 2)
    [notice] = client.take_delivery_notices()
    assert (notice["operationId"], notice["outcome"]) == (item["operationId"], "expired")
    client.acknowledge_delivery_notice(item["operationId"])
    client.step()
    assert _items(ipc) == {}
    wg.down = False
    _steps(client, 4)
    assert _deliveries(wg) == []


# -- threads and recorded exchanges -----------------------------------------------------------


def test_producing_an_item_does_no_network_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    ipc, _wg, _client_unused, clock, workspace = _setup(tmp_path, live=False)

    def refuse(*_args, **_kwargs):
        raise AssertionError("network I/O while producing an outbox item")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert _solve_item(ipc, workspace, _bundle(workspace, "a"), healthy=True, clock=clock) is not None
    assert _solve_item(ipc, workspace, _bundle(workspace, "b"), healthy=False, clock=clock) is not None
    assert _snapshot_item(ipc, workspace, _bundle(workspace, "c"), clock) is not None


def test_recorded_deliveries_replay_against_the_client(tmp_path: Path, stub_factory) -> None:  # noqa: F811
    """Hello, registration, then WG's recorded delivery answers, request bodies checked."""

    fixture = _fixture()
    names = ["restart_hello", "restart_register", "deliver_solve_created", "deliver_solve_recovered"]
    replay = Replay(fixture, names, lambda: stub.port)
    stub = stub_factory(replay)
    clock = Clock()
    ipc, client = _replay_client(tmp_path, fixture, stub.port, clock, "restartedEndpoint")
    client._solve_files = wglink_watch  # noqa: SLF001
    recorded = fixture["exchanges"]["deliver_solve_created"]["request"]["body"]
    item = wglink_live.delivery_item(
        SOLVE, operation_id=recorded["operationId"], return_id=recorded["returnId"],
        bundle_path=recorded["bundlePath"], manifest_sha256=recorded["manifestSha256"],
        requested_at=recorded["requestedAt"], created_at="2026-09-17T10:00:00Z", file_written=False,
    )
    outbox = wglink_live.Outbox(ipc)
    outbox.add(item)
    _steps(client, 3)
    assert replay.served == names[:3]
    assert _items(ipc) == {}
    # The same item again (a lost answer): recovered, still final.
    outbox.add(item)
    client.enqueue_delivery()
    client.step()
    assert replay.mismatches == []
    assert replay.served == names
    assert _items(ipc) == {}


@pytest.mark.parametrize(
    ("name", "kept"),
    [("deliver_conflict", False), ("deliver_snapshot_not_readable", True), ("deliver_store_busy", True),
     ("deliver_folder_not_selected", True), ("deliver_update_restart_pending", True),
     ("deliver_snapshot_unavailable", False), ("deliver_snapshot_not_readable_at_bound", False),
     ("deliver_snapshot_created", False), ("deliver_invalid_request", True)],
)
def test_recorded_delivery_answers_are_classified(tmp_path: Path, stub_factory, name: str, kept: bool) -> None:  # noqa: F811
    fixture = _fixture()
    exchange = fixture["exchanges"][name]
    names = ["restart_hello", "restart_register", name]
    replay = Replay(fixture, names, lambda: stub.port)
    stub = stub_factory(replay)
    clock = Clock()
    ipc, client = _replay_client(tmp_path, fixture, stub.port, clock, "restartedEndpoint")
    client._solve_files = wglink_watch  # noqa: SLF001
    body = exchange["request"]["body"]
    item = wglink_live.delivery_item(
        body["kind"], operation_id=body["operationId"], return_id=body.get("returnId"),
        bundle_path=body["bundlePath"], manifest_sha256=body["manifestSha256"],
        requested_at=body["requestedAt"], created_at="2026-09-17T10:00:00Z", file_written=False,
    )
    wglink_live.Outbox(ipc).add(item)
    _steps(client, 3)
    assert replay.served == names
    assert all("unexpected extra request" not in m for m in replay.mismatches), replay.mismatches
    if name != "deliver_invalid_request":  # recorded with a transport field WGLink never sends
        assert replay.mismatches == []
    stored = _items(ipc).get(body["operationId"])
    if kept:
        assert stored is not None and stored.get("answer") is None
    else:
        assert stored is None or stored["answer"]["outcome"] in {"conflict", "rejected"}
    if name == "deliver_snapshot_unavailable":
        [notice] = client.take_delivery_notices()
        assert notice["reason"] == "snapshot_unavailable"
    if name in {"deliver_snapshot_not_readable", "deliver_store_busy"}:
        assert exchange["response"]["headers"]["Retry-After"] == "1"


def test_the_live_worker_delivers_off_the_producing_thread(tmp_path: Path, stub_factory) -> None:  # noqa: F811
    """Real loopback HTTP and the real worker thread."""

    from test_wglink_live import _capabilities, _endpoint, _mac, _write_private
    import time

    ipc = _ipc(tmp_path)
    workspace = _workspace(tmp_path)
    secret, instance = "outbox-secret", "c" * 32
    delivered = threading.Event()
    threads: list[str] = []

    def handle(_stub, method, path, headers, body):
        if (method, path) == ("GET", LIVE + "/endpoint"):
            return 200, {}, json.dumps({"schemaVersion": 1, "producer": "waveguide-generator",
                                        "instanceId": instance, "liveProtocol": 1, "deliveryVersion": 3}).encode()
        if (method, path) == ("POST", LIVE + "/sessions"):
            sent = json.loads(body)
            proof = _mac(secret, "wglink-server", sent["clientNonce"], instance, headers["x-wglink-installation"])
            return 201, {}, json.dumps({"liveSessionId": "s", "sessionToken": "tok", "serverProof": proof,
                                        "instanceId": instance, "liveProtocol": 1}).encode()
        if (method, path) == ("POST", LIVE + "/deliveries"):
            threads.append(threading.current_thread().name)
            sent = json.loads(body)
            delivered.set()
            return 200, {}, json.dumps({"result": "created", "operation": {
                "operationId": sent["operationId"], "kind": sent["kind"], "state": "accepted",
                "reason": None, "message": None}}).encode()
        return 204, {}, b""

    stub = stub_factory(handle)
    _write_private(ipc / "wg-capabilities.json", _capabilities())
    _write_private(ipc / "wg-endpoint.json", _endpoint(instance, secret, stub.port))
    client = wglink_live.LiveClient(
        ipc_folder=lambda: ipc, adapter_session_id="s", adapter_version="0.1.1",
        loaded_identity={"source": "unmanaged", "sourceCommit": None, "addinVersion": "0.1.1",
                         "managedBy": None, "waveguideGeneratorRoot": None, "loadedAt": "2026-09-17T10:00:00Z"},
        lease_ok=lambda: True, solve_files=wglink_watch,
    )
    client.start()
    try:
        deadline = time.monotonic() + 10
        while not client.healthy() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.healthy()
        _snapshot_item(ipc, workspace, _bundle(workspace))
        client.enqueue_delivery()
        assert delivered.wait(10)
        deadline = time.monotonic() + 5
        while _items(ipc) and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        client.stop(timeout=5)
    assert _items(ipc) == {}
    assert len(threads) == 1
