"""The live operation consumer: long poll, claim journal, progress, completion.

Contract: Waveguide Generator ``docs/reference/CADLINK-LIVE-PROTOCOL.md``
section 7 as WG implements it in ``server/cadlink/live/requests.py``. The
in-process ``LiveWG`` below answers the four routes the way WG does, including
the parts that exist to be idempotent: a recorded claim replays its original
answer, a repeated stage or outcome answers ``alreadyRecorded``, and a
different outcome is ``409 outcome_conflict``.

Everything here is Fusion-free. ``tests/test_wglink_addin_lifecycle.py`` covers
the main-thread half, and ``tests/test_wglink_live_against_wg.py`` runs the same
exchanges against a real WG when one is available.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
sys.path.insert(0, str(ROOT / "tests"))
import wglink_live  # noqa: E402

from test_wglink_live import (  # noqa: E402
    ADAPTER_SESSION,
    LIVE,
    Clock,
    FakeWG,
    Request,
    _client,
    _heartbeat as _heartbeat_payload,
    _ipc,
    _refusal,
    _steps,
)


POSIX = os.name == "posix"
DOCUMENT = "fusion:doc-1"
INSTANCE = "instance-1"
STATE_HASH = "sha256:state-1"
EXPORT = "wge_1"


# -- WG's section 7 routes, in process -----------------------------------------


def _update_request(operation_id: str, *, bundle: str, sequence: int = 1) -> dict[str, Any]:
    return {
        "schemaVersion": 3,
        "target": "fusion360",
        "requestId": operation_id,
        "operationId": operation_id,
        "deliverySequence": sequence,
        "bundlePath": bundle,
        "bundleId": "wgb_1",
        "exportId": EXPORT,
        "designId": "wgd_1",
        "expectedDocumentId": DOCUMENT,
        "expectedInstanceId": INSTANCE,
        "expectedReturnStateHash": STATE_HASH,
        "requestedAt": "2026-09-20T10:00:00Z",
    }


def _return_request(operation_id: str, *, sequence: int = 1, session: str = ADAPTER_SESSION) -> dict[str, Any]:
    return {
        "schemaVersion": 3,
        "target": "fusion360",
        "requestId": operation_id,
        "operationId": operation_id,
        "deliverySequence": sequence,
        "sessionId": session,
        "designId": "wgd_1",
        "documentId": DOCUMENT,
        "instanceId": INSTANCE,
        "expectedReturnStateHash": STATE_HASH,
    }


class LiveWG(FakeWG):
    """``FakeWG`` plus the Fusion-bound request routes of protocol section 7."""

    def __init__(self, ipc: Path, **kwargs: Any) -> None:
        super().__init__(ipc, **kwargs)
        self.offers: list[dict[str, Any]] = []
        self.claims: dict[str, dict[str, Any]] = {}
        self.generation: dict[str, int] = {}
        self.state: dict[str, str] = {}
        self.stage: dict[str, str] = {}
        self.recorded: dict[str, dict[str, Any]] = {}
        self.polls = 0
        self.wait_seconds: list[int] = []

    # -- publishing -------------------------------------------------------------

    def publish_request(self, operation_id: str, kind: str, request: dict[str, Any], generation: int = 0) -> None:
        self.offers.append(
            {"operationId": operation_id, "kind": kind, "attemptGeneration": generation, "request": request}
        )
        self.generation[operation_id] = generation
        self.state[operation_id] = "received"

    def withdraw(self, operation_id: str) -> None:
        self.offers = [offer for offer in self.offers if offer["operationId"] != operation_id]

    def summary(self, operation_id: str) -> dict[str, Any]:
        return {
            "operationId": operation_id,
            "kind": "update_link",
            "state": self.state.get(operation_id, "received"),
            "stage": self.stage.get(operation_id),
            "attemptGeneration": self.generation.get(operation_id, 0),
        }

    # -- routing ----------------------------------------------------------------

    def answer(self, request: Request) -> "wglink_live.Answer":
        suffix = request.path[len(LIVE):]
        bare = suffix.split("?", 1)[0]
        if not bare.startswith("/requests"):
            return super().answer(request)
        if self.down:
            raise wglink_live.NetworkFailure("connection refused")
        queued = self.script.get((request.method, bare))
        if queued:
            item = queued.pop(0)
            return item(request) if callable(item) else item
        if request.headers.get("Authorization", "") != f"Bearer {self.current}":
            return _refusal(401, "session_unknown")
        if request.method == "GET":
            self.polls += 1
            if "waitSeconds=" in suffix:
                self.wait_seconds.append(int(suffix.split("waitSeconds=")[1]))
            return wglink_live.Answer(200, {"requests": [dict(offer) for offer in self.offers]})
        parts = bare.split("/")
        operation_id, action = parts[2], parts[3]
        body = request.body
        assert isinstance(body, dict)
        if action == "claim":
            return self._claim(operation_id, body)
        if action == "progress":
            return self._progress(operation_id, body)
        if action == "complete":
            return self._complete(operation_id, body)
        raise AssertionError(f"unexpected live request route {bare}")

    # -- section 7.2 ------------------------------------------------------------

    def _claim(self, operation_id: str, body: dict[str, Any]) -> "wglink_live.Answer":
        generation = body["attemptGeneration"]
        claim_id = body["claimId"]
        assert isinstance(claim_id, str) and 1 <= len(claim_id) <= 64
        recorded = self.claims.get(operation_id)
        if recorded is not None:
            if recorded["claimId"] == claim_id and recorded["attemptGeneration"] == generation + 1:
                return wglink_live.Answer(
                    200, {"attemptGeneration": recorded["attemptGeneration"], "request": recorded["request"]}
                )
            return _refusal(409, "already_claimed")
        offer = next((item for item in self.offers if item["operationId"] == operation_id), None)
        if offer is None:
            return _refusal(409, "claimed_elsewhere")
        if self.state.get(operation_id) != "received" or self.generation.get(operation_id) != generation:
            self.withdraw(operation_id)
            return _refusal(409, "stale_attempt")
        self.withdraw(operation_id)
        self.claims[operation_id] = {
            "claimId": claim_id,
            "attemptGeneration": generation + 1,
            "request": offer["request"],
        }
        self.generation[operation_id] = generation + 1
        self.state[operation_id] = "processing"
        self.stage[operation_id] = "adapter-received"
        return wglink_live.Answer(200, {"attemptGeneration": generation + 1, "request": offer["request"]})

    # -- section 7.3 ------------------------------------------------------------

    _ORDER = {"adapter-received": "queued-for-fusion", "queued-for-fusion": "executing"}
    _WIRE = {"queuedForFusion": "queued-for-fusion", "executing": "executing"}

    def _progress(self, operation_id: str, body: dict[str, Any]) -> "wglink_live.Answer":
        if operation_id not in self.claims:
            return _refusal(409, "claimed_elsewhere")
        if body["attemptGeneration"] != self.generation.get(operation_id):
            return _refusal(409, "stale_attempt")
        if self.state.get(operation_id) not in ("processing", "cancel_requested"):
            return _refusal(409, "stale_attempt")
        wanted = self._WIRE[body["stage"]]
        current = self.stage.get(operation_id)
        if current == wanted:
            return wglink_live.Answer(200, {"operation": self.summary(operation_id), "alreadyRecorded": True})
        if self._ORDER.get(current) != wanted:
            return _refusal(409, "stage_out_of_order")
        self.stage[operation_id] = wanted
        return wglink_live.Answer(200, {"operation": self.summary(operation_id)})

    # -- section 7.4 ------------------------------------------------------------

    def _complete(self, operation_id: str, body: dict[str, Any]) -> "wglink_live.Answer":
        if operation_id not in self.claims:
            return _refusal(409, "claimed_elsewhere")
        if body["attemptGeneration"] != self.generation.get(operation_id):
            return _refusal(409, "stale_attempt")
        assert body["outcome"] in wglink_live.OUTCOMES
        previous = self.recorded.get(operation_id)
        if previous is not None:
            if previous["outcome"] == body["outcome"]:
                return wglink_live.Answer(
                    200, {"operation": self.summary(operation_id), "alreadyRecorded": True}
                )
            return _refusal(409, "outcome_conflict")
        if self.state.get(operation_id) == "cancel_requested":
            # "Dismissal stands": WG records cancelled, and says alreadyRecorded.
            self.recorded[operation_id] = {"outcome": body["outcome"]}
            self.state[operation_id] = "cancelled"
            return wglink_live.Answer(200, {"operation": self.summary(operation_id), "alreadyRecorded": True})
        self.recorded[operation_id] = dict(body)
        self.state[operation_id] = "accepted" if body["outcome"] in ("applied", "reconciled") else "rejected"
        return wglink_live.Answer(200, {"operation": self.summary(operation_id)})


# -- harness -------------------------------------------------------------------


class Notifier:
    def __init__(self) -> None:
        self.fired = 0
        self.threads: list[str] = []

    def __call__(self) -> None:
        self.fired += 1
        self.threads.append(threading.current_thread().name)


def _dispatching(tmp_path: Path, **kwargs: Any):
    """A registered client with the poll wired up, driven step by step."""

    ipc = _ipc(tmp_path)
    wg = LiveWG(ipc)
    wg.publish()
    clock = Clock()
    notify = Notifier()
    client = _client(ipc, wg, clock=clock, notify=notify, **kwargs)
    _steps(client)
    assert client.healthy(), client.status()
    return ipc, wg, client, clock, notify


def _turn(client: "wglink_live.LiveClient", n: int = 6) -> None:
    """One or more poll+send turns, the way the two worker threads interleave."""

    for _ in range(n):
        client.poll_step()
        client.step()


def _journal(ipc: Path) -> dict[str, dict[str, Any]]:
    folder = ipc / wglink_live.CLAIM_DIRECTORY
    if not folder.is_dir():
        return {}
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.json"))
        if not path.name.startswith(".")
    }


def _claim_one(client: "wglink_live.LiveClient") -> "wglink_live.Offer":
    offers = client.offers()
    assert offers, "WG offered nothing"
    client.claim(offers[0])
    return offers[0]


# -- the long poll --------------------------------------------------------------


def test_the_poll_asks_for_wgs_long_poll_window_and_never_more_than_the_maximum(tmp_path: Path) -> None:
    _ipc_folder, wg, client, _clock, _notify = _dispatching(tmp_path)
    client.poll_step()
    assert wg.wait_seconds, "the client never polled"
    assert all(0 <= value <= wglink_live.LONG_POLL_SECONDS for value in wg.wait_seconds)


def test_an_empty_answer_offers_nothing_and_fires_no_event(tmp_path: Path) -> None:
    _ipc_folder, _wg, client, _clock, notify = _dispatching(tmp_path)
    client.poll_step()
    assert client.offers() == []
    assert notify.fired == 0


def test_offers_are_replaced_by_every_poll_and_never_accumulate(tmp_path: Path) -> None:
    _ipc_folder, wg, client, _clock, notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    client.poll_step()
    client.poll_step()
    assert [offer.operation_id for offer in client.offers()] == ["op-1"]
    assert notify.fired >= 1


def test_nothing_is_polled_without_a_healthy_session(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = LiveWG(ipc)
    wg.publish()
    client = _client(ipc, wg, clock=Clock())
    client.poll_step()
    assert wg.polls == 0


def test_a_poll_401_is_left_to_the_sender_which_registers_again(tmp_path: Path) -> None:
    _ipc_folder, wg, client, _clock, _notify = _dispatching(tmp_path)
    wg.script[("GET", "/requests")] = [_refusal(401, "session_unknown")]
    client.poll_step()
    before = len(wg.tokens)
    _steps(client, 3)
    assert len(wg.tokens) > before, "the sender never registered again after the poll 401"


def test_a_poll_network_failure_clears_healthy_at_once(tmp_path: Path) -> None:
    _ipc_folder, wg, client, _clock, _notify = _dispatching(tmp_path)
    wg.down = True
    client.poll_step()
    client.step()
    assert not client.healthy()


# -- the claim journal ----------------------------------------------------------


def test_the_claim_is_journaled_before_it_is_posted(tmp_path: Path) -> None:
    """A claim POST that is never answered still leaves a durable claim."""

    ipc, wg, client, _clock, _notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)
    wg.script[("POST", "/requests/op-1/claim")] = [
        lambda _request: (_ for _ in ()).throw(wglink_live.NetworkFailure("reset"))
    ]
    client.step()
    entry = _journal(ipc).get("op-1")
    assert entry is not None, "no durable claim existed when the claim was sent"
    assert entry["state"] == wglink_live.CLAIM_STATE_CLAIMING
    assert entry["claimId"]
    assert client.take_dispatch() is None, "work was acknowledged before its claim was durable"


def test_a_dispatch_follows_only_a_journalled_claim(tmp_path: Path) -> None:
    ipc, wg, client, _clock, notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)
    _turn(client)
    entry = _journal(ipc)["op-1"]
    assert entry["state"] == wglink_live.CLAIM_STATE_CLAIMED
    assert entry["attemptGeneration"] == 1
    dispatch = client.take_dispatch()
    assert dispatch is not None and dispatch.operation_id == "op-1"
    assert dispatch.attempt_generation == 1
    assert notify.fired >= 1


def test_a_lost_claim_answer_is_replayed_by_the_same_claim_id(tmp_path: Path) -> None:
    ipc, wg, client, clock, _notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)
    seen: list[str] = []

    def record_then_drop(request: Request) -> "wglink_live.Answer":
        body = request.body
        assert isinstance(body, dict)
        seen.append(body["claimId"])
        wg._claim("op-1", body)  # WG recorded it; the answer was lost
        raise wglink_live.NetworkFailure("reset")

    wg.script[("POST", "/requests/op-1/claim")] = [record_then_drop]
    client.step()
    assert _journal(ipc)["op-1"]["state"] == wglink_live.CLAIM_STATE_CLAIMING
    # A network failure sends the client back to the files for a moment; it
    # rediscovers WG, registers again, and the claim replays into that session.
    clock.now += 5.0
    _turn(client, 10)
    assert len(seen) == 1
    posted = [
        r.body["claimId"] for r in wg.requests
        if r.route == ("POST", LIVE + "/requests/op-1/claim") and isinstance(r.body, dict)
    ]
    assert len(set(posted)) == 1, "the replay used a different claim id"
    assert _journal(ipc)["op-1"]["state"] == wglink_live.CLAIM_STATE_CLAIMED


@pytest.mark.parametrize("code", ["claimed_elsewhere", "already_claimed", "stale_attempt", "session_mismatch"])
def test_a_refused_claim_deletes_the_journal_and_runs_nothing(tmp_path: Path, code: str) -> None:
    ipc, wg, client, _clock, _notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)
    wg.script[("POST", "/requests/op-1/claim")] = [_refusal(409, code)]
    _turn(client)
    assert _journal(ipc) == {}
    assert client.take_dispatch() is None


def test_a_busy_store_keeps_the_claim_and_retries(tmp_path: Path) -> None:
    ipc, wg, client, clock, _notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)
    wg.script[("POST", "/requests/op-1/claim")] = [_refusal(503, "store_busy", retryable=True)]
    client.step()
    assert _journal(ipc)["op-1"]["state"] == wglink_live.CLAIM_STATE_CLAIMING
    clock.now += 5.0
    _turn(client, 8)
    assert _journal(ipc)["op-1"]["state"] == wglink_live.CLAIM_STATE_CLAIMED


# -- progress and completion ----------------------------------------------------


def _claimed(tmp_path: Path, kind: str = "update_link"):
    ipc, wg, client, clock, notify = _dispatching(tmp_path)
    request = _update_request("op-1", bundle="/tmp/b") if kind != "request_return" else _return_request("op-1")
    wg.publish_request("op-1", kind, request)
    client.poll_step()
    _claim_one(client)
    _turn(client)
    dispatch = client.take_dispatch()
    assert dispatch is not None
    return ipc, wg, client, clock, dispatch


def test_the_claim_is_followed_by_queued_for_fusion_before_any_dispatch(tmp_path: Path) -> None:
    _ipc_folder, wg, _client_, _clock, _dispatch = _claimed(tmp_path)
    assert wg.stage["op-1"] == "queued-for-fusion"


def test_executing_is_recorded_after_queued_for_fusion(tmp_path: Path) -> None:
    _ipc_folder, wg, client, _clock, dispatch = _claimed(tmp_path)
    client.report_progress(dispatch.operation_id, dispatch.attempt_generation, wglink_live.STAGE_EXECUTING)
    _turn(client)
    assert wg.stage["op-1"] == "executing"
    stages = [
        r.body["stage"] for r in wg.requests
        if r.route == ("POST", LIVE + "/requests/op-1/progress") and isinstance(r.body, dict)
    ]
    assert stages == ["queuedForFusion", "executing"]


def test_a_completion_is_journaled_before_it_is_posted(tmp_path: Path) -> None:
    ipc, wg, client, _clock, dispatch = _claimed(tmp_path)
    wg.script[("POST", "/requests/op-1/complete")] = [
        lambda _request: (_ for _ in ()).throw(wglink_live.NetworkFailure("reset"))
    ]
    client.report_outcome(
        dispatch.operation_id, dispatch.attempt_generation, "applied",
        message="done", evidence={"operationId": "op-1", "exportId": EXPORT},
    )
    client.step()
    entry = _journal(ipc)["op-1"]
    assert entry["state"] == wglink_live.CLAIM_STATE_OUTCOME
    assert entry["outcome"]["outcome"] == "applied"


def test_a_resent_terminal_notification_creates_no_second_result(tmp_path: Path) -> None:
    ipc, wg, client, _clock, dispatch = _claimed(tmp_path)
    client.report_outcome(
        dispatch.operation_id, dispatch.attempt_generation, "applied",
        evidence={"operationId": "op-1", "exportId": EXPORT},
    )
    _turn(client)
    assert _journal(ipc) == {}
    assert wg.recorded["op-1"]["outcome"] == "applied"
    # A duplicate arrives after the connection came back: alreadyRecorded, one result.
    client.report_outcome(
        dispatch.operation_id, dispatch.attempt_generation, "applied",
        evidence={"operationId": "op-1", "exportId": EXPORT},
    )
    _turn(client)
    assert wg.recorded["op-1"]["outcome"] == "applied"
    assert _journal(ipc) == {}


def test_an_outcome_conflict_settles_the_journal_without_a_second_attempt(tmp_path: Path) -> None:
    ipc, wg, client, _clock, dispatch = _claimed(tmp_path)
    wg.recorded["op-1"] = {"outcome": "refused"}
    client.report_outcome(dispatch.operation_id, dispatch.attempt_generation, "applied",
                          evidence={"operationId": "op-1", "exportId": EXPORT})
    _turn(client)
    assert _journal(ipc) == {}
    assert wg.recorded["op-1"]["outcome"] == "refused"


def test_an_unanswered_completion_is_retried_and_kept_durable(tmp_path: Path) -> None:
    ipc, wg, client, clock, dispatch = _claimed(tmp_path)
    wg.down = True
    client.report_outcome(dispatch.operation_id, dispatch.attempt_generation, "refused", message="no")
    client.step()
    assert _journal(ipc)["op-1"]["state"] == wglink_live.CLAIM_STATE_OUTCOME
    wg.down = False
    clock.now += 5.0
    _turn(client, 12)
    assert wg.recorded["op-1"]["outcome"] == "refused"
    assert _journal(ipc) == {}


# -- cancellation ---------------------------------------------------------------


def test_a_cancellation_seen_at_queued_for_fusion_never_reaches_the_main_thread(tmp_path: Path) -> None:
    ipc, wg, client, _clock, _notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)

    def cancel_then_answer(request: Request) -> "wglink_live.Answer":
        body = request.body
        assert isinstance(body, dict)
        answer = wg._claim("op-1", body)
        wg.state["op-1"] = "cancel_requested"
        return answer

    wg.script[("POST", "/requests/op-1/claim")] = [cancel_then_answer]
    _turn(client)
    assert client.take_dispatch() is None, "a dismissed operation was handed to Fusion"
    assert wg.recorded["op-1"]["outcome"] == "discarded"
    assert _journal(ipc) == {}


def test_a_cancellation_seen_at_executing_becomes_a_pending_cancellation(tmp_path: Path) -> None:
    _ipc_folder, wg, client, _clock, dispatch = _claimed(tmp_path)
    assert not client.cancel_pending("op-1")
    wg.state["op-1"] = "cancel_requested"
    client.report_progress(dispatch.operation_id, dispatch.attempt_generation, wglink_live.STAGE_EXECUTING)
    _turn(client)
    assert client.cancel_pending("op-1"), "the main thread was never told about the dismissal"


# -- restart and adoption -------------------------------------------------------


def _restarted(tmp_path: Path, entry: dict[str, Any], prepare=None):
    """A fresh client on an ipc folder that already holds a claim journal entry.

    ``prepare(wg)`` runs before the client registers, so WG is in the state the
    interrupted session left it in by the time the journal is worked.
    """

    ipc = _ipc(tmp_path)
    folder = ipc / wglink_live.CLAIM_DIRECTORY
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / f"{entry['operationId']}.json").write_text(json.dumps(entry), encoding="utf-8")
    wg = LiveWG(ipc)
    wg.publish()
    if prepare is not None:
        prepare(wg)
    clock = Clock()
    notify = Notifier()
    client = _client(ipc, wg, clock=clock, notify=notify)
    _steps(client)
    assert client.healthy()
    return ipc, wg, client, clock


def _entry(state: str, **extra: Any) -> dict[str, Any]:
    entry = {
        "schemaVersion": wglink_live.CLAIM_SCHEMA_VERSION,
        "operationId": "op-1",
        "claimId": "claim-abc",
        "kind": "update_link",
        "attemptGeneration": 0,
        "state": state,
        "request": _update_request("op-1", bundle="/tmp/b"),
        "claimedAt": "2026-09-20T10:00:00Z",
    }
    entry.update(extra)
    return entry


def test_a_claiming_entry_left_by_a_crash_is_replayed_and_then_dispatched(tmp_path: Path) -> None:
    """``claiming`` is written before the POST, so the work provably never ran."""

    ipc, wg, client, _clock = _restarted(
        tmp_path,
        _entry(wglink_live.CLAIM_STATE_CLAIMING),
        # WG restored the visible request when it recovered: the operation is
        # still ``received`` at generation 0, and the claim replays into it.
        lambda wg: wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b")),
    )
    _turn(client, 8)
    posted = [
        r.body["claimId"] for r in wg.requests
        if r.route == ("POST", LIVE + "/requests/op-1/claim") and isinstance(r.body, dict)
    ]
    assert posted == ["claim-abc"]
    dispatch = client.take_dispatch()
    assert dispatch is not None and dispatch.operation_id == "op-1"


def test_a_claiming_entry_whose_operation_is_gone_is_deleted(tmp_path: Path) -> None:
    ipc, wg, client, _clock = _restarted(tmp_path, _entry(wglink_live.CLAIM_STATE_CLAIMING))
    _turn(client, 8)
    assert _journal(ipc) == {}
    assert client.take_dispatch() is None


def test_a_claimed_entry_is_offered_for_settlement_and_never_dispatched(tmp_path: Path) -> None:
    """Protocol section 9: the claim journal is settled read-only, never re-run."""

    ipc, wg, client, _clock = _restarted(
        tmp_path, _entry(wglink_live.CLAIM_STATE_CLAIMED, attemptGeneration=1)
    )
    _turn(client, 6)
    assert client.take_dispatch() is None, "an interrupted claim was re-run"
    adopted = client.take_adoption()
    assert adopted is not None and adopted["operationId"] == "op-1"
    assert adopted["state"] == wglink_live.CLAIM_STATE_CLAIMED
    assert _journal(ipc)["op-1"]["state"] == wglink_live.CLAIM_STATE_CLAIMED


def test_an_outcome_entry_left_by_a_crash_is_resent_once(tmp_path: Path) -> None:
    def claimed(wg: LiveWG) -> None:
        wg.claims["op-1"] = {"claimId": "claim-abc", "attemptGeneration": 1, "request": {}}
        wg.generation["op-1"] = 1
        wg.state["op-1"] = "processing"

    ipc, wg, client, _clock = _restarted(
        tmp_path,
        _entry(
            wglink_live.CLAIM_STATE_OUTCOME,
            attemptGeneration=1,
            outcome={"outcome": "refused", "message": "no"},
        ),
        claimed,
    )
    _turn(client, 8)
    assert wg.recorded["op-1"]["outcome"] == "refused"
    assert _journal(ipc) == {}
    assert client.take_dispatch() is None


# -- ownership and shutdown -----------------------------------------------------


def test_losing_the_lease_bumps_the_epoch_and_drops_later_reports(tmp_path: Path) -> None:
    ipc, wg, client, _clock, dispatch = _claimed(tmp_path)
    owned = [True]
    client._lease_ok = lambda: owned[0]  # the broker lock the main thread reads
    before = client.epoch
    owned[0] = False
    client.step()
    assert client.epoch != before, "the epoch did not move when the connection was lost"
    client.report_outcome(dispatch.operation_id, dispatch.attempt_generation, "applied",
                          evidence={"operationId": "op-1", "exportId": EXPORT}, epoch=before)
    owned[0] = True
    _turn(client, 6)
    assert "op-1" not in wg.recorded, "a report from a stale owner was accepted"


def test_a_token_refresh_keeps_the_epoch_and_the_request_identity(tmp_path: Path) -> None:
    """Section 7: operations are bound to the installation, never to a session."""

    _ipc_folder, wg, client, clock, dispatch = _claimed(tmp_path)
    before = client.epoch
    clock.now += wglink_live.REFRESH_AFTER_SECONDS + 1
    _turn(client, 4)
    assert len(wg.tokens) > 1, "the token was never refreshed"
    assert client.epoch == before
    client.report_outcome(dispatch.operation_id, dispatch.attempt_generation, "applied",
                          evidence={"operationId": "op-1", "exportId": EXPORT})
    _turn(client)
    assert wg.recorded["op-1"]["outcome"] == "applied"


def test_stop_joins_both_workers_within_its_timeout(tmp_path: Path) -> None:
    _ipc_folder, _wg, client, _clock, _notify = _dispatching(tmp_path)
    client.start()
    poll = client._poll_thread
    sender = client._thread
    assert poll is not None and poll.name == "WGLinkLivePoll"
    assert sender is not None and sender.name == "WGLinkLiveSend"
    client.stop(timeout=5.0)
    assert not client.is_running()
    assert not poll.is_alive() and not sender.is_alive()


def test_an_answer_that_arrives_after_stop_changes_nothing(tmp_path: Path) -> None:
    ipc, wg, client, _clock, notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.stop(timeout=1.0)
    fired = notify.fired
    client.poll_step()
    assert client.offers() == []
    assert notify.fired == fired
    client.step()
    assert _journal(ipc) == {}


# -- secrets --------------------------------------------------------------------


def test_no_token_or_proof_reaches_the_claim_journal_or_the_status(tmp_path: Path) -> None:
    ipc, wg, client, _clock, _dispatch = _claimed(tmp_path)
    secrets = [wg.secret, *wg.tokens]
    text = json.dumps(_journal(ipc)) + json.dumps(client.status()) + "\n".join(client.take_log_lines())
    for secret in secrets:
        assert secret not in text


def test_the_journal_folder_is_private_on_posix(tmp_path: Path) -> None:
    if not POSIX:
        pytest.skip("POSIX permission rule")
    ipc, _wg, _client_, _clock, _dispatch = _claimed(tmp_path)
    mode = (ipc / wglink_live.CLAIM_DIRECTORY).stat().st_mode
    assert mode & 0o077 == 0


# -- scheduling fairness --------------------------------------------------------


def test_the_poll_and_the_heartbeat_run_on_different_threads(tmp_path: Path) -> None:
    """A 25 s poll on the sender would push the heartbeat past WG's 20 s window."""

    _ipc_folder, wg, client, _clock, _notify = _dispatching(tmp_path)
    client.offer_heartbeat(_heartbeat_payload())
    client.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
            wg.polls and wg.of("POST", "/heartbeat")
        ):
            time.sleep(0.01)
    finally:
        client.stop(timeout=5.0)
    polls = [r for r in wg.requests if r.method == "GET" and "/requests" in r.path]
    beats = wg.of("POST", "/heartbeat")
    assert polls and beats, (len(polls), len(beats))
    assert {r.thread for r in polls} == {"WGLinkLivePoll"}
    assert "WGLinkLivePoll" not in {r.thread for r in beats}


def test_a_poll_that_answers_instantly_waits_before_asking_again(tmp_path: Path) -> None:
    """Long polling without busy-waiting: an instant answer is not a wait."""

    _ipc_folder, _wg, client, _clock, _notify = _dispatching(tmp_path)
    assert client.poll_step() >= wglink_live.POLL_RETRY_SECONDS


def test_an_outcome_reported_just_before_shutdown_is_journaled(tmp_path: Path) -> None:
    ipc, wg, client, _clock, dispatch = _claimed(tmp_path)
    wg.down = True
    client.report_outcome(dispatch.operation_id, dispatch.attempt_generation, "refused", message="no")
    client.start()
    client.stop(timeout=5.0)
    entry = _journal(ipc).get("op-1")
    assert entry is not None and entry["state"] == wglink_live.CLAIM_STATE_OUTCOME


def test_stopping_does_not_move_the_ownership_generation(tmp_path: Path) -> None:
    """Stopping hands the connection to nobody, so an outcome still journals."""

    _ipc_folder, _wg, client, _clock, _notify = _dispatching(tmp_path)
    before = client.epoch
    client.stop(timeout=1.0)
    assert client.epoch == before


def test_a_claim_whose_record_cannot_be_kept_is_handed_back_not_run(
    tmp_path: Path, monkeypatch
) -> None:
    """No durable record, no work: WG is told at once instead of waiting."""

    ipc, wg, client, _clock, _notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)
    original = wglink_live.ClaimJournal.write
    calls = {"n": 0}

    def write(self, entry):
        calls["n"] += 1
        if entry["state"] == wglink_live.CLAIM_STATE_CLAIMED:
            raise OSError("the journal folder went away")
        return original(self, entry)

    monkeypatch.setattr(wglink_live.ClaimJournal, "write", write)
    _turn(client)

    assert client.take_dispatch() is None, "work ran without a durable record of it"
    assert _journal(ipc) == {}
    assert wg.recorded["op-1"]["outcome"] == "failed"


# -- an offer this add-in may not take yet --------------------------------------
#
# WG answers a long poll the moment an offerable request exists, so an offer
# that stays unclaimed makes every "ask again at once" a round trip. The
# contract mandates exactly that state in three cases -- a Fusion command is
# running, no design is ready, no WG workspace is selected -- and A3 requires
# the request to wait and the reason to be published, not the poll to spin.


def test_a_standing_unclaimed_offer_is_not_asked_for_again_at_once(tmp_path: Path) -> None:
    """The first offer wakes Fusion; the same offer unchanged does not."""

    _ipc_folder, wg, client, _clock, notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))

    client.poll_step()
    assert notify.fired == 1, "a new offer must reach Fusion's main thread"
    assert client.offers(), "the main thread is busy; the offer is still WG's to give"

    fired = notify.fired
    polled = wg.polls
    for _ in range(20):
        # The main thread claims nothing: a WGLink command is running.
        assert client.poll_step() >= wglink_live.POLL_MIN_INTERVAL_SECONDS, (
            "an offer that cannot be claimed turned the long poll into a busy loop"
        )
    assert wg.polls - polled == 20
    assert notify.fired == fired, (
        f"the unchanged offer raised {notify.fired - fired} more custom events"
    )

    # A second offer is a change, so Fusion hears about it.
    wg.publish_request("op-2", "update_link", _update_request("op-2", bundle="/tmp/c"))
    client.poll_step()
    assert notify.fired == fired + 1


def test_an_offer_still_standing_after_a_lost_poll_wakes_fusion_again(tmp_path: Path) -> None:
    """A poll that did not answer proves nothing about what WG still holds."""

    _ipc_folder, wg, client, _clock, notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    assert notify.fired == 1
    client.poll_step()
    assert notify.fired == 1, "the same unchanged offer was announced twice"

    wg.down = True
    client.poll_step()
    wg.down = False
    client.poll_step()
    assert notify.fired == 2, "the standing offer was never announced again after a lost poll"


def test_an_offer_the_main_thread_leaves_alone_bounds_the_polls_and_the_events(
    tmp_path: Path,
) -> None:
    """A bound, not a snapshot: one wall-clock second of a busy Fusion command.

    The poll thread runs for real here. Against WG each answer costs a full
    store-and-IPC scan, and every event is queued onto Fusion's main thread
    while the user has a command open.
    """

    _ipc_folder, wg, client, _clock, notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.start()
    try:
        time.sleep(1.0)
    finally:
        client.stop(timeout=5.0)

    assert wg.polls <= 8, f"the long poll asked WG {wg.polls} times in one second"
    assert notify.fired <= 8, f"Fusion's main thread was woken {notify.fired} times in one second"


# -- what a journal entry that will not delete costs ----------------------------
#
# Count requests, not outcomes. WG deduplicates a repeated outcome into
# ``alreadyRecorded``, so a client that re-posts forever still leaves exactly
# one recorded result -- the defect is invisible in ``wg.recorded`` and visible
# only at the transport boundary.


def _held_journal_files(monkeypatch) -> None:
    """Every journal entry refuses to be deleted, as a Windows reader makes it."""

    original = Path.unlink

    def held(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.parent.name == wglink_live.CLAIM_DIRECTORY and self.suffix == ".json":
            raise OSError("the file is open in another process")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", held)


def test_a_completion_is_posted_once_even_when_its_entry_cannot_be_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    ipc, wg, client, clock, dispatch = _claimed(tmp_path)
    _held_journal_files(monkeypatch)
    client.report_outcome(
        dispatch.operation_id, dispatch.attempt_generation, "applied",
        evidence={"operationId": "op-1", "exportId": EXPORT},
    )
    _turn(client, 12)

    posts = wg.of("POST", "/requests/op-1/complete")
    assert len(posts) == 1, f"one outcome, {len(posts)} completion POSTs"
    assert wg.recorded["op-1"]["outcome"] == "applied"
    assert _journal(ipc)["op-1"]["state"] == wglink_live.CLAIM_STATE_OUTCOME
    assert any("could not" in line.lower() for line in client.take_log_lines()), (
        "a journal entry that could not be cleared was never reported"
    )

    # A backoff, not a drop: it is tried again later, and still boundedly.
    clock.now += 3600.0
    _turn(client, 12)
    assert len(wg.of("POST", "/requests/op-1/complete")) <= 3


def test_a_refused_claim_is_posted_once_even_when_its_entry_cannot_be_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    _ipc_folder, wg, client, _clock, _notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)
    # WG already handed this operation to somebody else.
    wg.claims["op-1"] = {"claimId": "another-claim", "attemptGeneration": 1, "request": {}}
    _held_journal_files(monkeypatch)
    _turn(client, 12)

    claims = wg.of("POST", "/requests/op-1/claim")
    assert len(claims) == 1, f"one refused claim, {len(claims)} claim POSTs"
    assert client.take_dispatch() is None, "a refused claim was handed to Fusion"


def test_one_outcome_reaches_wg_as_exactly_one_completion_post(tmp_path: Path) -> None:
    """The idempotency claim, measured where a deduplicating WG cannot hide it.

    A repeat of a settled outcome does not even leave the machine: its journal
    entry is gone, and a terminal report without a claim is never sent. WG's
    ``alreadyRecorded`` is the safety net, not the mechanism -- which is only
    visible by counting the POSTs.
    """

    ipc, wg, client, _clock, dispatch = _claimed(tmp_path)
    for _ in range(2):
        client.report_outcome(
            dispatch.operation_id, dispatch.attempt_generation, "applied",
            evidence={"operationId": "op-1", "exportId": EXPORT},
        )
        _turn(client)
    posts = wg.of("POST", "/requests/op-1/complete")
    assert len(posts) == 1, f"one outcome, {len(posts)} completion POSTs"
    assert wg.recorded["op-1"]["outcome"] == "applied"
    assert _journal(ipc) == {}


# -- a momentary journal failure is not a reason to abandon the session ---------


def test_a_dismissal_whose_outcome_cannot_be_journaled_keeps_the_live_session(
    tmp_path: Path, monkeypatch
) -> None:
    """Every other journal write survives a held file; this one dropped to files.

    An unguarded ``OSError`` here reaches the worker's blanket handler, which
    drops the session and falls back to the file transport for a reader that
    held one file open for a moment.
    """

    _ipc_folder, wg, client, _clock, _notify = _dispatching(tmp_path)
    wg.publish_request("op-1", "update_link", _update_request("op-1", bundle="/tmp/b"))
    client.poll_step()
    _claim_one(client)

    def cancel_then_answer(request: Request) -> "wglink_live.Answer":
        body = request.body
        assert isinstance(body, dict)
        answer = wg._claim("op-1", body)
        wg.state["op-1"] = "cancel_requested"
        return answer

    wg.script[("POST", "/requests/op-1/claim")] = [cancel_then_answer]
    original = wglink_live.ClaimJournal.write

    def write(self: "wglink_live.ClaimJournal", entry: Any) -> None:
        if entry["state"] == wglink_live.CLAIM_STATE_OUTCOME:
            raise OSError("the file is open in another process")
        return original(self, entry)

    monkeypatch.setattr(wglink_live.ClaimJournal, "write", write)
    _turn(client)

    assert client.healthy(), "a momentary journal failure dropped the live session"
    assert client.take_dispatch() is None, "a dismissed operation was handed to Fusion"
