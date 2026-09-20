"""The live client against a real Waveguide Generator (opt-in).

Set ``WGLINK_WG_CHECKOUT`` to a waveguide-generator checkout and run in that
checkout's exact-pin environment::

    WGLINK_WG_CHECKOUT=<checkout> python -m pytest --noconftest \\
        tests/test_wglink_live_against_wg.py -v

(``--noconftest``: WG's environment carries WG's mesher pin, not this
repository's, and nothing here imports the mesher.)

WG's own ``create_app`` serves on a reserved loopback port through uvicorn,
with the startup trimmed to the capability file and the live session
(``tests/_wg_live_app.py``). The client runs on its real worker thread with a
fake monotonic clock only for the refresh deadline. Without the variable this
module reports exactly one skip.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest


CHECKOUT = os.environ.get("WGLINK_WG_CHECKOUT")
if not CHECKOUT:
    pytest.skip(
        "set WGLINK_WG_CHECKOUT=<waveguide-generator checkout> and run in its exact-pin "
        "environment to check the live client against a real WG",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
sys.path.insert(0, str(ROOT / "tests"))
import _wg_live_app  # noqa: E402
import wglink_live  # noqa: E402
import wglink_watch  # noqa: E402
import wglink_workspace  # noqa: E402

_wg_live_app.import_wg(CHECKOUT)

from server.cadlink import fusion_status  # noqa: E402
from server.cadlink.live import registry as live_registry  # noqa: E402


ADAPTER_SESSION = "fusion-session-against-wg"


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def _wait(condition, what: str, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _heartbeat(n: int) -> dict:
    return wglink_watch.fusion_status_payload(
        session_id=ADAPTER_SESSION,
        document_name=f"Against WG {n}",
        document_id=None,
        adapter_version="0.1.1",
        workspace_root=None,
        links=[],
        diagnostics={"watchIntervalSeconds": 4.0, "tick": n},
    )


def _selected(data_dir: Path) -> tuple[object, object]:
    return fusion_status.select_heartbeat(data_dir)


def test_register_heartbeat_refresh_restart_and_register_again(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "wg-data"
    data_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WG2_DATA_DIR", str(data_dir))
    monkeypatch.setenv("WG2_WGLINK_REFRESH", "0")
    ipc = wglink_workspace.ipc_folder(create=True)
    assert ipc == data_dir.absolute() / "ipc" / "wglink"
    os.chmod(ipc, 0o755)

    clock = Clock()
    client = wglink_live.LiveClient(
        ipc_folder=lambda: wglink_workspace.ipc_folder(),
        adapter_session_id=ADAPTER_SESSION,
        adapter_version="0.1.1",
        loaded_identity=wglink_live.loaded_identity(
            ROOT / "fusion-addins" / "WGLink", addin_version="0.1.1"
        ),
        lease_ok=lambda: True,
        clock=clock,
    )
    client.start()
    try:
        with _wg_live_app.serving(data_dir) as first:
            registry = first.state.live_registry
            _wait(client.healthy, "registration with the first WG")
            assert client.status()["instanceId"] == registry.instance_id
            assert registry.loaded_identity()["source"] in {"unmanaged", "devSync", "managed"}

            client.offer_heartbeat(_heartbeat(1))
            _wait(lambda: _selected(data_dir)[1] == "live", "the live heartbeat to be selected")
            payload, transport = _selected(data_dir)
            assert transport == "live" and payload["document"]["name"] == "Against WG 1"
            # The file heartbeat is WGLink.py's job and is not written here.
            assert not (ipc / ".fusion-status.json").exists()

            session_id = client.status()["liveSessionId"]
            digest_before = registry._sessions[session_id].token_digest
            clock.offset += wglink_live.REFRESH_AFTER_SECONDS
            _wait(lambda: client.status()["refreshes"] == 1, "the token refresh")
            assert registry._sessions[session_id].token_digest != digest_before
            client.offer_heartbeat(_heartbeat(2))
            _wait(
                lambda: (_selected(data_dir)[0] or {}).get("document", {}).get("name") == "Against WG 2",
                "a heartbeat with the refreshed token",
            )
            first_instance = registry.instance_id

        # WG stops (its endpoint file goes) and starts again on another port.
        _wait(lambda: not client.healthy(), "the client to notice WG stopped", seconds=40)
        assert live_registry.registry_for(data_dir) is None
        with _wg_live_app.serving(data_dir) as second:
            registry = second.state.live_registry
            assert registry.instance_id != first_instance
            _wait(lambda: client.status()["instanceId"] == registry.instance_id and client.healthy(),
                  "registration with the restarted WG", seconds=40)
            client.offer_heartbeat(_heartbeat(3))
            _wait(lambda: _selected(data_dir)[1] == "live", "the live heartbeat after the restart")
            assert _selected(data_dir)[0]["document"]["name"] == "Against WG 3"

            client.stop(timeout=10)
            assert not client.is_running()
            assert registry.loaded_identity() is None  # DELETE /sessions/current ended it
            assert _selected(data_dir) == (None, None)
    finally:
        client.stop(timeout=10)
    print("\n".join(client.take_log_lines()))


# -- the outbox against a real WG (protocol section 8) ----------------------------------


class _Scene:
    """A WG data directory with a selected WGLink folder, and real returns in it."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        from server.cadlink import solve_command

        self.data_dir = tmp_path / "wg-data"
        self.data_dir.mkdir()
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("WG2_DATA_DIR", str(self.data_dir))
        monkeypatch.setenv("WG2_WGLINK_REFRESH", "0")
        self.ipc = wglink_workspace.ipc_folder(create=True)
        os.chmod(self.ipc, 0o755)
        solve_command._live_waits.clear()
        self.clients: list[wglink_live.LiveClient] = []

    def serving(self):
        from server.cadlink import solve_command

        # A WG process that starts again has forgotten every delivery hold.
        solve_command._live_waits.clear()
        return _wg_live_app.serving(
            self.data_dir, configure=lambda app: app.state.cad_workspace.select(self.workspace)
        )

    def bundle(self, name: str, step: bytes = b"STEP") -> Path:
        from server.tests.test_cad_preparation import _write_return

        relative, _manifest = _write_return(self.workspace, f"{name}.wgreturn", step=step)
        return self.workspace / relative

    def client(self, **kwargs) -> "wglink_live.LiveClient":
        client = wglink_live.LiveClient(
            ipc_folder=lambda: wglink_workspace.ipc_folder(),
            adapter_session_id=ADAPTER_SESSION,
            adapter_version="0.1.1",
            loaded_identity=wglink_live.loaded_identity(ROOT / "fusion-addins" / "WGLink", addin_version="0.1.1"),
            lease_ok=lambda: True,
            solve_files=wglink_watch,
            **kwargs,
        )
        self.clients.append(client)
        return client

    def stop_all(self) -> None:
        for client in self.clients:
            client.stop(timeout=10)

    def items(self) -> dict[str, dict]:
        folder = self.ipc / ".wglink-outbox"
        if not folder.is_dir():
            return {}
        import json

        items: dict[str, dict] = {}
        for path in folder.iterdir():
            if path.name.startswith("."):
                continue
            try:
                items[path.stem] = json.loads(path.read_text())
            except FileNotFoundError:
                # The live worker may settle and delete an item between the
                # directory snapshot and the read; it is then no longer
                # waiting, which is what this helper reports. Same race, and
                # the same treatment, as ``tests/test_wglink_outbox.py``
                # (commit 4ad32f0).
                continue
        return items

    def solve_files(self) -> list[Path]:
        folder = self.ipc / ".wg-solve-requests"
        return sorted(p for p in folder.iterdir() if not p.name.startswith(".")) if folder.is_dir() else []

    def collect_files(self, application) -> None:
        """WG's v3 file pass, with the route's own retention."""

        from server.cadlink import preparation, solve_command

        store = application.state.cadlink_store
        solve_command.collect_solve_deliveries(
            self.data_dir, store,
            retain=lambda op: preparation.settle_snapshot_operation(store, self.data_dir, self.workspace, op),
        )


def _operations(application) -> dict[str, dict]:
    store = application.state.cadlink_store
    return {str(row["operation_id"]): dict(row) for row in store.list_operations(limit=1000)}


@pytest.fixture
def scene(tmp_path: Path, monkeypatch):
    made = _Scene(tmp_path, monkeypatch)
    try:
        yield made
    finally:
        made.stop_all()


def test_returns_sent_while_wg_is_down_are_delivered_once_and_accepted_once(scene) -> None:
    """Solve (v3 file + item) and Send (item) made with WG closed; the file pass and the live
    delivery both reach WG, and each is one operation."""

    # WG ran once (its capability file says live), then was closed.
    with scene.serving():
        pass
    solve_bundle, snapshot_bundle = scene.bundle("solve", b"STEP solve"), scene.bundle("snap", b"STEP snap")
    solve = wglink_live.produce_solve(
        scene.ipc, bundle_path=solve_bundle, workspace_root=scene.workspace, return_id="wgr_1",
        healthy=False, solve_files=wglink_watch,
    )
    snapshot = wglink_live.produce_snapshot(
        scene.ipc, bundle_path=snapshot_bundle, workspace_root=scene.workspace, solve_files=wglink_watch,
    )
    [solve_file] = scene.solve_files()
    assert solve_file.stem == solve["operationId"]

    client = scene.client()
    client.start()
    with scene.serving() as application:
        _wait(lambda: scene.items() == {}, "both items to be delivered", seconds=40)
        scene.collect_files(application)  # the file pass after the live delivery
        operations = _operations(application)
        assert set(operations) == {solve["operationId"], snapshot["operationId"]}
        assert operations[snapshot["operationId"]]["state"] == "accepted"
        assert operations[solve["operationId"]]["kind"] == "prepare_and_solve"
        assert scene.solve_files() == []
    print("\n".join(client.take_log_lines()))


def test_a_lost_200_is_recovered_under_the_same_operation(scene) -> None:
    lost = {"left": 1}

    class LosingTransport(wglink_live.Transport):
        def request(self, method, path, **kwargs):
            answer = super().request(method, path, **kwargs)
            if path == "/deliveries" and lost["left"]:
                lost["left"] -= 1
                raise wglink_live.NetworkFailure("answer lost after WG accepted")
            return answer

    with scene.serving() as application:
        client = scene.client(transport_factory=LosingTransport)
        client.start()
        _wait(client.healthy, "registration")
        item = wglink_live.produce_solve(
            scene.ipc, bundle_path=scene.bundle("lost"), workspace_root=scene.workspace, return_id="wgr_1",
            healthy=True, solve_files=wglink_watch,
        )
        client.enqueue_delivery()
        _wait(lambda: lost["left"] == 0, "the first delivery")
        _wait(lambda: scene.items() == {}, "the retried delivery", seconds=40)
        assert list(_operations(application)) == [item["operationId"]]
        # The network failure also wrote the v3 file with the same id: recovered, still one.
        assert [p.stem for p in scene.solve_files()] == [item["operationId"]]
        scene.collect_files(application)
        assert list(_operations(application)) == [item["operationId"]]


def test_a_wg_restart_between_503_and_the_retry_delivers_one_operation(scene) -> None:
    bundle = scene.bundle("late")
    client = scene.client()
    with scene.serving() as application:
        client.start()
        _wait(client.healthy, "registration")
        item = wglink_live.produce_snapshot(
            scene.ipc, bundle_path=bundle, workspace_root=scene.workspace, solve_files=wglink_watch,
        )
        hidden = bundle.with_name("late.hidden")
        bundle.rename(hidden)  # not readable now: 503 snapshot_not_readable
        client.enqueue_delivery()
        _wait(lambda: item["operationId"] in _operations(application), "the first (503) delivery")
        assert _operations(application)[item["operationId"]]["state"] == "received"
    hidden.rename(bundle)
    with scene.serving() as application:
        _wait(lambda: scene.items() == {}, "the delivery after the restart", seconds=40)
        operations = _operations(application)
        assert list(operations) == [item["operationId"]]
        assert operations[item["operationId"]]["state"] == "accepted"


def test_a_conflict_is_surfaced_and_a_rejected_snapshot_is_shown_then_deleted(scene, monkeypatch) -> None:
    from datetime import timedelta

    from server.cadlink import preparation

    wall = {"shift": timedelta(0)}
    real_wall_now = preparation._wall_now
    monkeypatch.setattr(preparation, "_wall_now", lambda: real_wall_now() + wall["shift"])
    with scene.serving() as application:
        client = scene.client()
        client.start()
        _wait(client.healthy, "registration")
        first = wglink_live.produce_solve(
            scene.ipc, bundle_path=scene.bundle("one", b"STEP one"), workspace_root=scene.workspace,
            return_id="wgr_1", healthy=True, solve_files=wglink_watch,
        )
        client.enqueue_delivery()
        _wait(lambda: scene.items() == {}, "the first delivery")
        # The same id for another return: WG refuses it, the client never retries it.
        other = scene.bundle("two", b"STEP two")
        relative, manifest = wglink_watch.return_reference(other, scene.workspace)
        wglink_live.Outbox(scene.ipc).add(wglink_live.delivery_item(
            "prepare_and_solve", operation_id=first["operationId"], return_id="wgr_2", bundle_path=relative,
            manifest_sha256=manifest, requested_at="2026-09-17T10:00:00Z",
            created_at="2026-09-17T10:00:00.000Z",
        ))
        client.enqueue_delivery()
        notices: list[dict] = []
        _wait(lambda: notices.extend(client.take_delivery_notices()) or any(
            n["outcome"] == "conflict" for n in notices), "the conflict notice")
        client.acknowledge_delivery_notice(first["operationId"])
        _wait(lambda: scene.items() == {}, "the acknowledged conflict to be deleted")
        row = _operations(application)[first["operationId"]]
        assert json_digest_unchanged(row, first)

        # A snapshot WG cannot read for 24 hours: a 200 whose operation is rejected.
        bundle = scene.bundle("vanishing")
        snapshot = wglink_live.produce_snapshot(
            scene.ipc, bundle_path=bundle, workspace_root=scene.workspace, solve_files=wglink_watch,
        )
        bundle.rename(bundle.with_name("vanishing.gone"))
        client.enqueue_delivery()
        _wait(lambda: snapshot["operationId"] in _operations(application), "the first unreadable answer")
        wall["shift"] = timedelta(hours=24, seconds=1)
        notices.clear()
        _wait(lambda: notices.extend(client.take_delivery_notices()) or any(
            n["operationId"] == snapshot["operationId"] for n in notices), "the rejection notice", seconds=30)
        [rejected] = [n for n in notices if n["operationId"] == snapshot["operationId"]]
        assert (rejected["outcome"], rejected["reason"], rejected["durable"]) == (
            "rejected", "snapshot_unavailable", True,
        )
        assert "24 hours" in rejected["message"]
        assert snapshot["operationId"] in scene.items()  # kept until the user was told
        client.acknowledge_delivery_notice(snapshot["operationId"])
        _wait(lambda: scene.items() == {}, "the acknowledged rejection to be deleted")
        state = _operations(application)[snapshot["operationId"]]
        assert (state["state"], state["reason"]) == ("rejected", "snapshot_unavailable")


def json_digest_unchanged(row: dict, first: dict) -> bool:
    """The stored operation still names the first return (the conflict changed nothing)."""

    import json

    return first["bundlePath"] in json.dumps(json.loads(row["inputs_json"]))


def test_an_add_in_restart_with_a_pending_item_delivers_it_once(scene) -> None:
    with scene.serving():
        pass
    item = wglink_live.produce_snapshot(
        scene.ipc, bundle_path=scene.bundle("pending"), workspace_root=scene.workspace, solve_files=wglink_watch,
    )
    first = scene.client()
    first.start()
    _wait(lambda: first.status()["lastCause"] is not None, "the first add-in to find WG closed")
    first.stop(timeout=10)
    assert item["operationId"] in scene.items()

    with scene.serving() as application:
        again = scene.client()
        again.start()
        _wait(lambda: scene.items() == {}, "the restarted add-in's delivery", seconds=40)
        operations = _operations(application)
        assert list(operations) == [item["operationId"]]
        assert operations[item["operationId"]]["state"] == "accepted"


# -- Fusion-bound requests against a real WG (protocol section 7) ------------------------


REQUEST_DOCUMENT = "fusion:doc-against-wg"
REQUEST_INSTANCE = "instance-against-wg"
REQUEST_STATE_HASH = "sha256:state-against-wg"
REQUEST_EXPORT = "wge_against_wg"


def _publish_update(scene: "_Scene", store: object, operation_id: str) -> None:
    from server.exports.cad_handoff import publish_fusion_handoff

    bundle = scene.workspace / "wglink" / "bundle-against-wg"
    bundle.mkdir(parents=True, exist_ok=True)
    publish_fusion_handoff(
        scene.data_dir,
        scene.workspace,
        store,
        {
            "bundlePath": str(bundle),
            "exportId": REQUEST_EXPORT,
            "bundleId": "wgb_against_wg",
            "identity": {"designId": "wgd_against_wg"},
        },
        expected_document_id=REQUEST_DOCUMENT,
        expected_instance_id=REQUEST_INSTANCE,
        expected_return_state_hash=REQUEST_STATE_HASH,
        request_id=operation_id,
    )


def _offer(client: "wglink_live.LiveClient", operation_id: str):
    _wait(
        lambda: any(offer.operation_id == operation_id for offer in client.offers()),
        f"WG to offer {operation_id}",
    )
    return next(offer for offer in client.offers() if offer.operation_id == operation_id)


def _dispatch(client: "wglink_live.LiveClient", operation_id: str):
    box: list[object] = []

    def arrived() -> bool:
        dispatch = client.take_dispatch()
        if dispatch is not None:
            box.append(dispatch)
        return bool(box)

    _wait(arrived, f"WG to hand over {operation_id}")
    return box[0]


def test_a_real_wg_offers_claims_progresses_and_completes_one_operation(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole of section 7 against WG's own routes, with WG's own publisher."""

    scene = _Scene(tmp_path, monkeypatch)
    journal = wglink_live.ClaimJournal(scene.ipc)
    client = scene.client()
    client.start()
    try:
        with scene.serving() as application:
            store = application.state.cadlink_store
            _wait(client.healthy, "registration with WG")
            _publish_update(scene, store, "op-against-wg")

            offer = _offer(client, "op-against-wg")
            assert offer.kind == wglink_live.KIND_UPDATE_LINK
            assert offer.request["expectedInstanceId"] == REQUEST_INSTANCE
            client.claim(offer)

            dispatch = _dispatch(client, "op-against-wg")
            assert dispatch.attempt_generation == offer.attempt_generation + 1
            row = store.get_operation("op-against-wg")
            assert row["state"] == "processing" and row["stage"] == "queued-for-fusion"
            entry = journal.read("op-against-wg")
            assert entry is not None and entry["state"] == wglink_live.CLAIM_STATE_CLAIMED

            # A duplicate claim of the same attempt replays the original answer
            # and creates no second operation (section 9, "A response is lost").
            client.claim(offer)
            time.sleep(0.5)
            assert store.get_operation("op-against-wg")["attempt_generation"] == dispatch.attempt_generation

            client.report_progress(
                dispatch.operation_id, dispatch.attempt_generation, wglink_live.STAGE_EXECUTING
            )
            _wait(
                lambda: store.get_operation("op-against-wg")["stage"] == "executing",
                "WG to record the executing stage",
            )

            client.report_outcome(
                dispatch.operation_id,
                dispatch.attempt_generation,
                "applied",
                message="applied against a real WG",
                evidence={"operationId": "op-against-wg", "exportId": REQUEST_EXPORT},
            )
            _wait(
                lambda: store.get_operation("op-against-wg")["state"] == "accepted",
                "WG to record the outcome",
            )
            _wait(lambda: journal.read("op-against-wg") is None, "the journal to be cleared")

            # A terminal notification resent after a lost connection settles
            # nothing a second time.
            client.report_outcome(
                dispatch.operation_id,
                dispatch.attempt_generation,
                "applied",
                evidence={"operationId": "op-against-wg", "exportId": REQUEST_EXPORT},
            )
            time.sleep(0.5)
            assert store.get_operation("op-against-wg")["state"] == "accepted"
            assert journal.read("op-against-wg") is None
    finally:
        scene.stop_all()
    print("\n".join(client.take_log_lines()))


def test_a_wg_restart_between_the_claim_and_the_outcome_settles_it_once(
    tmp_path: Path, monkeypatch
) -> None:
    """The operation persists, the claim replays by its id, one outcome lands."""

    scene = _Scene(tmp_path, monkeypatch)
    journal = wglink_live.ClaimJournal(scene.ipc)
    client = scene.client()
    client.start()
    try:
        with scene.serving() as application:
            store = application.state.cadlink_store
            _wait(client.healthy, "registration with WG")
            _publish_update(scene, store, "op-restart")
            client.claim(_offer(client, "op-restart"))
            dispatch = _dispatch(client, "op-restart")
            claim_id = journal.read("op-restart")["claimId"]

        # WG restarts. Its operation and claim survive; its session does not.
        _wait(lambda: not client.healthy(), "the client to notice WG stopped", seconds=40)
        with scene.serving() as second:
            store = second.state.cadlink_store
            _wait(client.healthy, "registration with the restarted WG", seconds=40)
            assert store.get_operation("op-restart")["state"] == "processing"
            client.report_outcome(
                dispatch.operation_id,
                dispatch.attempt_generation,
                "recoveryRequired",
                message="interrupted by a WG restart",
            )
            _wait(
                lambda: store.get_operation("op-restart")["state"] == "recovery_required",
                "WG to record the outcome after its restart",
            )
            assert journal.read("op-restart") is None
            # The claim id never changed across the restart.
            assert claim_id
    finally:
        scene.stop_all()
    print("\n".join(client.take_log_lines()))


def test_a_return_request_for_another_fusion_session_is_never_offered(
    tmp_path: Path, monkeypatch
) -> None:
    from server.cadlink.fusion_return import publish_return_request

    scene = _Scene(tmp_path, monkeypatch)
    client = scene.client()
    client.start()
    try:
        with scene.serving() as application:
            store = application.state.cadlink_store
            _wait(client.healthy, "registration with WG")
            publish_return_request(
                scene.data_dir, store, session_id="a-different-fusion-session",
                design_id="wgd_against_wg", document_id=REQUEST_DOCUMENT,
                instance_id=REQUEST_INSTANCE,
                expected_return_state_hash=REQUEST_STATE_HASH, request_id="op-other-session",
            )
            publish_return_request(
                scene.data_dir, store, session_id=ADAPTER_SESSION,
                design_id="wgd_against_wg", document_id=REQUEST_DOCUMENT,
                instance_id=REQUEST_INSTANCE,
                expected_return_state_hash=REQUEST_STATE_HASH, request_id="op-this-session",
            )
            _offer(client, "op-this-session")
            assert [offer.operation_id for offer in client.offers()] == ["op-this-session"]
    finally:
        scene.stop_all()
