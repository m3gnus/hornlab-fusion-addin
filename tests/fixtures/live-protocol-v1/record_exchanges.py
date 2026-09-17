"""Record live-protocol exchanges from a real Waveguide Generator.

Run in a Waveguide Generator checkout's exact-pin environment::

    python tests/fixtures/live-protocol-v1/record_exchanges.py <wg checkout> [--check]

It serves WG's own application (``tests/_wg_live_app.py``) on loopback and
sends raw HTTP requests -- deliberately not through ``wglink_live``, so the
recording says what WG answers, not what the client expects. Time, instance
ids, secrets and tokens are made deterministic by replacing the clock and the
random sources of ``server.cadlink.live.registry`` (and the heartbeat clock of
``server.cadlink.fusion_status``), so the same WG commit records the same file.
Machine-specific values are normalized: the port in ``baseUrl`` becomes
``{port}``, the endpoint file's ``pid`` becomes 12345, and the wall-clock
``createdAt``/``updatedAt`` of an operation in a delivery answer become
``{time}``.

Deliveries (protocol section 8) are recorded against a selected WGLink folder
holding real ``.wgreturn`` bundles written by WG's own test helper, retained
into WG's own storage. WG's transient-bound clock and its snapshot wall clock
are replaced so the 30 s bound and the 24 h bound are reached deterministically;
``store_busy`` is produced by making WG's live acceptance raise SQLite's
"database is locked", and ``update_restart_pending`` by an approved restart,
both answered by WG's real route.

``--check`` regenerates in memory and fails if ``exchanges.json`` differs.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import _wg_live_app  # noqa: E402

LIVE = "/api/cadlink/live"
INSTALLATION = "5b0c2f7e-3d1a-4e8b-9c6f-0a1b2c3d4e5f"
ADAPTER_SESSION = "9d7e5c3a-1b2f-4a6e-8d0c-2e4f6a8b0c1d"
CLIENT_NONCE_BYTES = bytes(range(32, 64))
SECOND_NONCE_BYTES = bytes(range(64, 96))
WALL_T0 = 1_789_000_000.0  # 2026-09-10T00:26:40Z
RECORDED_HEADERS = ("Content-Type", "Authorization", "X-WGLink-Installation", "Origin")


class _Sequence:
    def __init__(self, prefix: str) -> None:
        self._counter = itertools.count(1)
        self._prefix = prefix

    def next_hex(self, width: int) -> str:
        return f"{self._prefix}{next(self._counter):0{width - len(self._prefix)}x}"


class _Uuid:
    def __init__(self) -> None:
        self._seq = _Sequence("a1")

    def uuid4(self):
        import uuid

        return uuid.UUID(hex=self._seq.next_hex(32))


class _Secrets:
    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def token_urlsafe(self, _nbytes: int = 32) -> str:
        return f"recorded-token-{next(self._counter):04d}-" + "x" * 24


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mac(secret: str, label: str, nonce: str, instance: str, installation: str) -> str:
    import hashlib
    import hmac

    message = f"{label}\n{nonce}\n{instance}\n{installation}".encode("utf-8")
    return _b64(hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest())


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _heartbeat(updated_at: float, session_id: str = ADAPTER_SESSION) -> dict:
    return {
        "schemaVersion": 1,
        "cadApplication": "fusion360",
        "sessionId": session_id,
        "adapterVersion": "0.1.1",
        "deliveryVersion": 3,
        "workspaceRoot": None,
        "updatedAt": _iso(updated_at),
        "document": {"name": "Untitled", "id": None, "links": []},
        "diagnostics": {"watchIntervalSeconds": 4.0, "lastTickMs": {"snapshot_ms": 0.1}},
    }


class _Wire:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def send(self, method: str, path: str, headers: dict[str, str] | None = None, body=None) -> dict:
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.base + LIVE + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                status, raw = response.status, response.read()
                retry_after = response.headers.get("Retry-After")
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
            retry_after = error.headers.get("Retry-After")
        answer = {"status": status, "body": json.loads(raw) if raw else None}
        if retry_after is not None:
            answer["headers"] = {"Retry-After": retry_after}
        return {
            "request": {
                "method": method,
                "path": LIVE + path,
                "headers": {name: headers[name] for name in RECORDED_HEADERS if name in headers},
                "body": body,
            },
            "response": answer,
        }


def _identity() -> dict:
    return {
        "source": "unmanaged",
        "sourceCommit": None,
        "addinVersion": "0.1.1",
        "managedBy": None,
        "waveguideGeneratorRoot": None,
        "loadedAt": _iso(WALL_T0 - 60),
    }


def _registration(endpoint: dict, nonce_bytes: bytes, **overrides) -> dict:
    nonce = _b64(nonce_bytes)
    body = {
        "cadApplication": "fusion360",
        "liveProtocol": 1,
        "deliveryVersion": 3,
        "installationId": INSTALLATION,
        "adapterSessionId": ADAPTER_SESSION,
        "adapterVersion": "0.1.1",
        "clientNonce": nonce,
        "clientProof": _mac(
            endpoint["registrationSecret"], "wglink-client", nonce, endpoint["instanceId"], INSTALLATION
        ),
        "loadedIdentity": _identity(),
    }
    body.update(overrides)
    return body


def _normalized_endpoint(data_dir: Path, port: int) -> dict:
    raw = json.loads((data_dir / "ipc" / "wglink" / "wg-endpoint.json").read_text(encoding="utf-8"))
    assert raw["baseUrl"] == f"http://127.0.0.1:{port}", raw["baseUrl"]
    raw["baseUrl"] = "http://127.0.0.1:{port}"
    raw["pid"] = 12345
    return raw


def record(checkout: Path) -> dict:
    _wg_live_app.import_wg(checkout)
    from server.cadlink import fusion_status
    from server.cadlink.live import registry as live_registry

    clock = {"now": WALL_T0}
    live_registry._now = lambda: clock["now"]
    live_registry._wall = lambda: clock["now"]
    live_registry.uuid = _Uuid()
    live_registry.secrets = _Secrets()
    fusion_status._utc_now = lambda: datetime.fromtimestamp(clock["now"], timezone.utc)

    exchanges: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as temporary:
        data_dir = Path(temporary) / "data"
        data_dir.mkdir()
        with _wg_live_app.serving(data_dir) as application:
            port = application.state.test_port
            wire = _Wire(port)
            endpoint_raw = json.loads(
                (data_dir / "ipc" / "wglink" / "wg-endpoint.json").read_text(encoding="utf-8")
            )
            endpoint = _normalized_endpoint(data_dir, port)
            capabilities = json.loads(
                (data_dir / "ipc" / "wglink" / "wg-capabilities.json").read_text(encoding="utf-8")
            )
            install = {"X-WGLink-Installation": INSTALLATION}

            exchanges["hello"] = wire.send("GET", "/endpoint")
            exchanges["hello_with_origin"] = wire.send(
                "GET", "/endpoint", {"Origin": f"http://127.0.0.1:{port}"}
            )
            exchanges["hello_with_origin"]["request"]["headers"]["Origin"] = "http://127.0.0.1:{port}"
            bad = _registration(endpoint_raw, SECOND_NONCE_BYTES)
            bad["clientProof"] = _mac("wrong", "wglink-client", bad["clientNonce"], endpoint_raw["instanceId"], INSTALLATION)
            exchanges["register_bad_proof"] = wire.send("POST", "/sessions", install, bad)
            exchanges["register_protocol_unsupported"] = wire.send(
                "POST", "/sessions", install, _registration(endpoint_raw, SECOND_NONCE_BYTES, liveProtocol=2)
            )
            exchanges["register_addin_outdated"] = wire.send(
                "POST", "/sessions", install, _registration(endpoint_raw, SECOND_NONCE_BYTES, deliveryVersion=2)
            )
            exchanges["register_unknown_field"] = wire.send(
                "POST", "/sessions", install, {**_registration(endpoint_raw, SECOND_NONCE_BYTES), "extra": 1}
            )
            exchanges["register"] = wire.send(
                "POST", "/sessions", install, _registration(endpoint_raw, CLIENT_NONCE_BYTES)
            )
            exchanges["register_reused_nonce"] = wire.send(
                "POST", "/sessions", install, _registration(endpoint_raw, CLIENT_NONCE_BYTES)
            )
            token = exchanges["register"]["response"]["body"]["sessionToken"]
            session = {"Authorization": f"Bearer {token}", **install}

            exchanges["heartbeat"] = wire.send("POST", "/heartbeat", session, _heartbeat(clock["now"]))
            exchanges["heartbeat_stale"] = wire.send(
                "POST", "/heartbeat", session, _heartbeat(clock["now"] - 3600)
            )
            exchanges["heartbeat_session_mismatch"] = wire.send(
                "POST", "/heartbeat", session, _heartbeat(clock["now"], session_id="another-fusion-session")
            )
            exchanges["heartbeat_without_installation"] = wire.send(
                "POST", "/heartbeat", {"Authorization": f"Bearer {token}"}, _heartbeat(clock["now"])
            )
            # Any time before expiry; idle (60 s) would end the session first.
            clock["now"] += 30
            exchanges["refresh"] = wire.send("POST", "/sessions/refresh", session)
            new_token = exchanges["refresh"]["response"]["body"]["sessionToken"]
            exchanges["refresh_with_replaced_token"] = wire.send("POST", "/sessions/refresh", session)
            session = {"Authorization": f"Bearer {new_token}", **install}
            exchanges["heartbeat_after_refresh"] = wire.send(
                "POST", "/heartbeat", session, _heartbeat(clock["now"])
            )
            exchanges["end"] = wire.send("DELETE", "/sessions/current", session)
            exchanges["heartbeat_after_end"] = wire.send(
                "POST", "/heartbeat", session, _heartbeat(clock["now"])
            )
            nonce3 = bytes(range(96, 128))
            exchanges["register_again"] = wire.send(
                "POST", "/sessions", install, _registration(endpoint_raw, nonce3)
            )
            third = exchanges["register_again"]["response"]["body"]["sessionToken"]
            session = {"Authorization": f"Bearer {third}", **install}
            clock["now"] += 61
            exchanges["heartbeat_after_idle"] = wire.send(
                "POST", "/heartbeat", session, _heartbeat(clock["now"])
            )
            registry = application.state.live_registry
            application.state.live_registry = None
            exchanges["hello_store_busy"] = wire.send("GET", "/endpoint")
            application.state.live_registry = registry

        first_endpoint = endpoint
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        bound_clock = {"now": 5_000.0}
        wall_clock = {"now": datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc)}
        from server.cadlink import preparation, solve_command

        solve_command._now = lambda: bound_clock["now"]
        preparation._wall_now = lambda: wall_clock["now"]

        def select(application) -> None:
            application.state.cad_workspace.select(workspace)

        with _wg_live_app.serving(data_dir, configure=select) as application:
            port = application.state.test_port
            wire = _Wire(port)
            restarted_raw = json.loads(
                (data_dir / "ipc" / "wglink" / "wg-endpoint.json").read_text(encoding="utf-8")
            )
            restarted = _normalized_endpoint(data_dir, port)
            install = {"X-WGLink-Installation": INSTALLATION}
            exchanges["restart_old_token"] = wire.send(
                "POST", "/heartbeat", {"Authorization": f"Bearer {third}", **install}, _heartbeat(clock["now"])
            )
            old_secret = _registration(endpoint_raw, bytes(range(128, 160)))
            old_secret["clientProof"] = _mac(
                endpoint_raw["registrationSecret"], "wglink-client", old_secret["clientNonce"],
                restarted_raw["instanceId"], INSTALLATION,
            )
            exchanges["restart_register_old_secret"] = wire.send("POST", "/sessions", install, old_secret)
            exchanges["restart_hello"] = wire.send("GET", "/endpoint")
            exchanges["restart_register"] = wire.send(
                "POST", "/sessions", install, _registration(restarted_raw, CLIENT_NONCE_BYTES)
            )
            session = {
                "Authorization": f"Bearer {exchanges['restart_register']['response']['body']['sessionToken']}",
                **install,
            }
            _record_deliveries(exchanges, wire, application, session, workspace, bound_clock, wall_clock, third)

    return {
        "schemaVersion": 1,
        "recordedFrom": {
            "repository": "waveguide-generator",
            "commit": _head(checkout),
            "recorder": "tests/fixtures/live-protocol-v1/record_exchanges.py",
        },
        "installationId": INSTALLATION,
        "adapterSessionId": ADAPTER_SESSION,
        "clientNonceHex": CLIENT_NONCE_BYTES.hex(),
        "loadedIdentity": _identity(),
        "capabilities": capabilities,
        "endpoint": first_endpoint,
        "restartedEndpoint": restarted,
        "heartbeat": exchanges["heartbeat"]["request"]["body"],
        "exchanges": exchanges,
    }


SOLVE_ID = "0b5a1c7e-5d2f-4c3a-8e1b-000000000001"
SNAPSHOT_ID = "0b5a1c7e-5d2f-4c3a-8e1b-000000000002"
UNREADABLE_ID = "0b5a1c7e-5d2f-4c3a-8e1b-000000000003"
BUSY_ID = "0b5a1c7e-5d2f-4c3a-8e1b-000000000004"
REFUSED_ID = "0b5a1c7e-5d2f-4c3a-8e1b-000000000005"
REQUESTED_AT = "2026-09-17T10:00:00Z"


def _delivery(operation_id: str, kind: str, bundle_path: str, manifest: str, **extra) -> dict:
    body = {
        "operationId": operation_id,
        "kind": kind,
        "bundlePath": bundle_path,
        "manifestSha256": manifest,
        "requestedAt": REQUESTED_AT,
    }
    if kind == "prepare_and_solve":
        body["returnId"] = "wgr_1"
    body.update(extra)
    return body


def _normalized_delivery(exchange: dict) -> dict:
    operation = (exchange["response"]["body"] or {}).get("operation")
    if isinstance(operation, dict):
        for key in ("createdAt", "updatedAt"):
            if operation.get(key) is not None:
                operation[key] = "{time}"
    return exchange


def _record_deliveries(exchanges, wire, application, session, workspace, bound_clock, wall_clock, old_token) -> None:
    """Section 8 answers from WG's real route, in an order that needs no reset."""

    import sqlite3
    from datetime import timedelta

    from server.cadlink import solve_command
    from server.tests.test_cad_preparation import _write_return

    first_path, first_manifest = _write_return(workspace, "first.wgreturn", step=b"STEP first")
    second_path, second_manifest = _write_return(workspace, "second.wgreturn", step=b"STEP second")
    missing_path, missing_manifest = "wgreturn/missing.wgreturn", "sha256:" + "4" * 64

    def deliver(name: str, body: dict, headers: dict | None = None) -> None:
        exchanges[name] = _normalized_delivery(wire.send("POST", "/deliveries", headers or session, body))

    solve = _delivery(SOLVE_ID, "prepare_and_solve", first_path, first_manifest)
    deliver("deliver_solve_created", solve)
    deliver("deliver_solve_recovered", solve)
    deliver("deliver_conflict", _delivery(SOLVE_ID, "prepare_and_solve", second_path, second_manifest))
    deliver("deliver_snapshot_created", _delivery(SNAPSHOT_ID, "receive_snapshot", second_path, second_manifest))
    unreadable = _delivery(UNREADABLE_ID, "receive_snapshot", missing_path, missing_manifest)
    deliver("deliver_snapshot_not_readable", unreadable)
    bound_clock["now"] += solve_command.LIVE_TRANSIENT_BOUND_S
    deliver("deliver_snapshot_not_readable_at_bound", unreadable)

    real = solve_command.deliver_live

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    solve_command.deliver_live = locked
    try:
        deliver("deliver_store_busy", _delivery(BUSY_ID, "prepare_and_solve", first_path, first_manifest))
    finally:
        solve_command.deliver_live = real

    refused = _delivery(REFUSED_ID, "prepare_and_solve", first_path, first_manifest)
    cad_workspace = application.state.cad_workspace
    selected = cad_workspace._selected
    cad_workspace._selected = None
    try:
        deliver("deliver_folder_not_selected", refused)
    finally:
        cad_workspace._selected = selected
    restart = application.state.update_restart
    restart.refusal = lambda: "An update restart is pending."
    try:
        deliver("deliver_update_restart_pending", refused)
    finally:
        del restart.refusal
    deliver("deliver_invalid_request", {**refused, "attemptGeneration": 1})
    deliver("deliver_with_an_old_token", refused, {**session, "Authorization": f"Bearer {old_token}"})

    # The unreadable snapshot, 24 hours later: rejected, and still a 200.
    wall_clock["now"] += timedelta(hours=24)
    bound_clock["now"] += solve_command.LIVE_TRANSIENT_BOUND_S + solve_command.LIVE_DEADLINE_MEMORY_S
    deliver("deliver_snapshot_unavailable", unreadable)


def _head(checkout: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=HERE / "exchanges.json")
    args = parser.parse_args(argv)
    os.environ.setdefault("WG2_WGLINK_REFRESH", "0")
    text = json.dumps(record(args.checkout), indent=2, sort_keys=True) + "\n"
    if args.check:
        current = args.output.read_text(encoding="utf-8")
        if current != text:
            print(f"{args.output} differs from a fresh recording", file=sys.stderr)
            return 1
        print(f"{args.output} matches a fresh recording")
        return 0
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
