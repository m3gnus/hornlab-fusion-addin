"""WG's acknowledgement of a Send or Solve (S4-D2, handoff H9, add-in half).

WG writes ``<ipc>/.wg-solve-acks/<requestId>.json`` once it holds or refuses a
request. Until that file appears WGLink says only that the request was written
to WG's inbox; it never says "solving". The fixtures below have the exact shape
of WG's ``write_acknowledgement()`` output.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from test_wglink_transfer import (
    SNAPSHOT,
    SOLVE,
    _advertise,
    _deliver_followups,
    _inbox,
    _run,
    _transfer,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_watch  # noqa: E402


def _ack_payload(request_id: str, outcome: str, reason: str | None = None, **extra) -> dict:
    payload = {
        "schemaVersion": 1,
        "commandId": request_id,
        "operationId": request_id,
        "outcome": outcome,
        "reason": reason,
        "jobId": None,
        "digest": None if reason else "sha256:" + "b" * 64,
        "at": "2026-09-29T10:00:00Z",
    }
    payload.update(extra)
    return payload


def _write_ack(ipc: Path, request_id: str, payload: object) -> Path:
    folder = ipc / wglink_watch.ACK_DIRECTORY
    folder.mkdir(exist_ok=True)
    path = folder / f"{request_id}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True))
    return path


def _advertise_acks(ipc: Path, value: object = 1) -> None:
    path = ipc / wglink_watch.CAPABILITIES_FILENAME
    payload = json.loads(path.read_text())
    payload["solveAcknowledgement"] = value
    path.write_text(json.dumps(payload))


def _acknowledging(monkeypatch, tmp_path: Path, name: str, operation: str = "solve"):
    fixture = _transfer(monkeypatch, tmp_path, name)
    _advertise_acks(fixture.ipc)
    _run(fixture.module, operation)
    [payload] = _inbox(fixture.ipc)
    return fixture, payload["operationId"]


def _tick(fixture) -> list[str]:
    """Run the timers now due and deliver the follow-ups; the new titles."""

    before = len(fixture.ui.messages)
    due = list(fixture.timers)
    fixture.timers.clear()
    for _delay, function in due:
        function()
    _deliver_followups(fixture.module, fixture.app)
    return [title for title, _text in fixture.ui.messages[before:]]


def _claim(fixture, request_id: str) -> None:
    folder = fixture.ipc / wglink_watch.SOLVE_REQUESTS_DIRECTORY
    (folder / f"{request_id}.json").rename(folder / f".wglink-claim-{request_id}.json")


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_command_time_wording_says_only_written_to_the_inbox(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, f"WGLink_ack_words_{operation}", operation)

    text = fixture.ui.messages[-1][1]
    assert f"Written to WG's inbox (request {request_id[:8]})" in text
    assert "solving" not in text.lower() and "Sent to" not in text


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_accepted_reads_wg_accepted_never_solving(monkeypatch, tmp_path: Path, operation: str) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, f"WGLink_ack_ok_{operation}", operation)
    _claim(fixture, request_id)
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "accepted"))

    logged: list[str] = []
    monkeypatch.setattr(fixture.module, "_log", logged.append)
    before = len(fixture.ui.messages)

    assert _tick(fixture) == []  # accepted is never a message box

    assert len(fixture.ui.messages) == before
    [line] = logged
    assert "WG accepted the request" in line and "solving" not in line.lower()
    assert fixture.module._recent_outcomes[-1]["outcome"] == "accepted"
    assert fixture.timers == []


@pytest.mark.parametrize(
    "reason",
    [
        "This add-in is older than Waveguide Generator; update WGLink.",
        "The request is not a valid CAD Link request.",
        "Another operation already holds this return.",
    ],
    ids=["old-addin", "invalid-request", "conflict"],
)
def test_refused_shows_wg_reason_verbatim(monkeypatch, tmp_path: Path, reason: str) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_refused")
    _claim(fixture, request_id)
    # A conflict refusal carries digest null, like the others' refusals.
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "refused", reason))

    assert _tick(fixture) == ["WGLink request refused"]

    assert fixture.ui.messages[-1][1] == reason
    assert fixture.module._recent_outcomes[-1]["outcome"] == "refused"


def test_an_ack_that_appears_late_moves_the_wording_on(monkeypatch, tmp_path: Path) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_late")
    _claim(fixture, request_id)

    for _ in range(3):
        assert _tick(fixture) == []
        assert len(fixture.timers) == 1  # one timer re-armed, no thread
        assert fixture.timers[0][0] == wglink_watch.ACK_POLL_SECONDS
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "accepted"))

    assert _tick(fixture) == []
    assert fixture.module._recent_outcomes[-1]["outcome"] == "accepted"
    assert fixture.timers == []


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"schemaVersion": 1'], ids=["empty", "junk", "list", "cut"])
def test_an_unparsable_ack_is_not_yet(monkeypatch, tmp_path: Path, content: str) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_junk")
    _claim(fixture, request_id)
    _write_ack(fixture.ipc, request_id, content)

    assert _tick(fixture) == []
    assert len(fixture.timers) == 1


def test_no_ack_after_the_wait_is_taken_not_confirmed(monkeypatch, tmp_path: Path) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_timeout")
    _claim(fixture, request_id)
    monkeypatch.setattr(fixture.module.wglink_watch, "ACK_WAIT_SECONDS", 0.0)

    assert _tick(fixture) == ["WGLink request not confirmed"]

    text = fixture.ui.messages[-1][1].lower()
    assert "took the request but has not confirmed" in text and "cad link panel" in text
    assert "refused" not in text and "solving" not in text
    assert fixture.module._recent_outcomes[-1]["outcome"] == "takenUnconfirmed"
    assert fixture.timers == []


def test_a_request_still_in_the_inbox_after_a_minute_is_the_not_picked_up_notice(
    monkeypatch, tmp_path: Path
) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_untaken")
    monkeypatch.setattr(fixture.module.wglink_watch, "SOLVE_PICKUP_NOTICE_SECONDS", 0.0)

    assert _tick(fixture) == ["WGLink request waiting"]
    assert fixture.module._recent_outcomes[-1]["outcome"] == "notTaken"
    assert len(_inbox(fixture.ipc)) == 1


@pytest.mark.parametrize(
    "extra",
    [{"manifestSha256": "sha256:" + "c" * 64}, {"kind": "receive_snapshot"}],
    ids=["other-manifest", "other-kind"],
)
def test_an_ack_naming_another_manifest_or_kind_is_ignored(
    monkeypatch, tmp_path: Path, extra: dict
) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_mismatch")  # a Solve
    _claim(fixture, request_id)
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "accepted", **extra))

    assert _tick(fixture) == []
    assert len(fixture.timers) == 1


def test_matching_manifest_and_kind_are_accepted(monkeypatch, tmp_path: Path) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_match")
    [payload] = [p for p in [_inbox(fixture.ipc)[0]]]
    _claim(fixture, request_id)
    _write_ack(
        fixture.ipc,
        request_id,
        _ack_payload(request_id, "accepted", manifestSha256=payload["manifestSha256"], kind=SOLVE),
    )

    assert _tick(fixture) == []
    assert fixture.module._recent_outcomes[-1]["outcome"] == "accepted"


def test_unknown_fields_and_other_schema_versions(tmp_path: Path) -> None:
    rid = "0b1c2d3e-0000-4000-8000-000000000001"
    _write_ack(tmp_path, rid, _ack_payload(rid, "accepted", jobId="job-1", future="x"))
    assert wglink_watch.read_acknowledgement(tmp_path, rid) == ("accepted", None)

    _write_ack(tmp_path, rid, _ack_payload(rid, "accepted", schemaVersion=2))
    assert wglink_watch.read_acknowledgement(tmp_path, rid) is None

    _write_ack(tmp_path, rid, _ack_payload(rid, "maybe"))
    assert wglink_watch.read_acknowledgement(tmp_path, rid) is None

    _write_ack(tmp_path, rid, _ack_payload(rid, "accepted", operationId="another"))
    assert wglink_watch.read_acknowledgement(tmp_path, rid) is None

    assert wglink_watch.read_acknowledgement(tmp_path, "../escape") is None
    assert wglink_watch.read_acknowledgement(tmp_path / "missing", rid) is None


@pytest.mark.parametrize("value", [None, 0, True, "1"], ids=["absent", "zero", "bool", "string"])
def test_only_a_capability_of_at_least_one_enables_ack_reading(tmp_path: Path, value: object) -> None:
    _advertise(tmp_path, 4)
    if value is not None:
        _advertise_acks(tmp_path, value)
    assert wglink_watch.wg_acknowledges(tmp_path) is False

    _advertise_acks(tmp_path, 1)
    assert wglink_watch.wg_acknowledges(tmp_path) is True
    payload = json.loads((tmp_path / wglink_watch.CAPABILITIES_FILENAME).read_text())
    payload["schemaVersion"] = 2
    (tmp_path / wglink_watch.CAPABILITIES_FILENAME).write_text(json.dumps(payload))
    assert wglink_watch.wg_acknowledges(tmp_path) is False


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_without_the_capability_the_legacy_path_stays_and_never_says_solving(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_ack_legacy_{operation}")
    _run(fixture.module, operation)
    [payload] = _inbox(fixture.ipc)
    # Even an ack file left in the folder is not read.
    _write_ack(fixture.ipc, payload["operationId"], _ack_payload(payload["operationId"], "refused", "no"))

    text = fixture.ui.messages[-1][1]
    assert "solving" not in text.lower() and "Written to WG's inbox" not in text
    assert f"(request {payload['operationId'][:8]})" in text
    [(delay, _function)] = fixture.timers
    assert delay == wglink_watch.SOLVE_PICKUP_NOTICE_SECONDS
    assert _tick(fixture) == ["WGLink request waiting"]  # today's not-taken notice only
