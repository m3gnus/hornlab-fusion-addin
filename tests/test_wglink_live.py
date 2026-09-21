"""The live CAD Link session client: discovery, mutual proof, tokens, heartbeat.

Contract: Waveguide Generator ``docs/reference/CADLINK-LIVE-PROTOCOL.md`` as
implemented by WG's ``server/cadlink/live`` (recorded from WG ``a3328f40`` in
``fixtures/live-protocol-v1/exchanges.json``). Every failure keeps WGLink on
the v3 files, which never stop.

Most tests drive :meth:`wglink_live.LiveClient.step` synchronously against an
in-process stand-in for WG's live routes (``FakeWG``) with a fake monotonic
clock. The transport, thread and replay tests use real loopback HTTP.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import time
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_live  # noqa: E402
import wglink_watch  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "live-protocol-v1" / "exchanges.json"
#: sha256 of the recorded exchanges; a re-recording must update this on purpose.
EXCHANGES_SHA256 = "fd60989925560dd0b53da9231dabf73938e1ebdfff545eadedf5d8cdd7a01f96"
LIVE = "/api/cadlink/live"
INSTALLATION_HEADER = "X-WGLink-Installation"
ADAPTER_SESSION = "fusion-session-under-test"
POSIX = os.name == "posix"
#: The wall clock the fakes and the recording share (``record_exchanges.WALL_T0``)
#: at fake monotonic 1000.
WALL_AT_1000 = 1_789_000_000.0


# -- helpers -------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mac(secret: str, label: str, nonce: str, instance: str, installation: str) -> str:
    """The proof exactly as the protocol document spells it, independent of the module."""

    message = f"{label}\n{nonce}\n{instance}\n{installation}".encode("utf-8")
    return _b64(hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest())


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def wall(self) -> float:
        return WALL_AT_1000 + (self.now - 1000.0)


def _utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _write_private(path: Path, payload: object) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _ipc(tmp_path: Path) -> Path:
    folder = tmp_path / "data" / "ipc" / "wglink"
    folder.mkdir(parents=True)
    if POSIX:
        os.chmod(folder, 0o755)
    return folder


def _capabilities(**overrides: Any) -> dict[str, Any]:
    document = {
        "schemaVersion": 1,
        "producer": "waveguide-generator",
        "solveCommandDelivery": 3,
        "fusionRequestDelivery": 3,
        "sourceIdentity": 1,
        "liveProtocol": 1,
    }
    document.update(overrides)
    return {key: value for key, value in document.items() if value is not ...}


def _endpoint(instance: str, secret: str, port: int, **overrides: Any) -> dict[str, Any]:
    document = {
        "schemaVersion": 1,
        "producer": "waveguide-generator",
        "instanceId": instance,
        "pid": 4242,
        "baseUrl": f"http://127.0.0.1:{port}",
        "liveProtocol": 1,
        "startedAt": "2026-09-17T10:00:00Z",
        "registrationSecret": secret,
    }
    document.update(overrides)
    return {key: value for key, value in document.items() if value is not ...}


def _heartbeat(session_id: str = ADAPTER_SESSION, n: int = 1) -> dict[str, Any]:
    return wglink_watch.fusion_status_payload(
        session_id=session_id,
        document_name=f"Doc {n}",
        document_id=None,
        adapter_version="0.1.1",
        workspace_root=None,
        links=[],
        diagnostics={"watchIntervalSeconds": 4.0, "tick": n},
    )


class Request:
    def __init__(self, method: str, path: str, headers: dict[str, str], body: object, thread: str) -> None:
        self.method, self.path, self.headers, self.body, self.thread = method, path, headers, body, thread

    @property
    def route(self) -> tuple[str, str]:
        return self.method, self.path


def _refusal(status: int, code: str, retryable: bool = False) -> "wglink_live.Answer":
    return wglink_live.Answer(
        status,
        {"detail": code, "error": {"code": code, "stage": "cadlink-live", "message": code, "retryable": retryable}},
    )


class FakeWG:
    """WG's live routes, in process, answering the way ``server/cadlink/live`` does.

    ``script[(method, path)]`` holds answers (or callables) served before the
    default behaviour; ``down`` makes every request a network failure.
    """

    def __init__(self, ipc: Path, *, instance: str = "a" * 32, secret: str = "wg-secret-1", port: int = 41001) -> None:
        self.ipc, self.instance, self.secret, self.port = ipc, instance, secret, port
        self.requests: list[Request] = []
        self.script: dict[tuple[str, str], list[object]] = {}
        self.down = False
        self.tokens: list[str] = []
        self.current: str | None = None
        self.proof_override: object = None
        #: What registration and refresh report; far away, so 600 s governs.
        self.refresh_after = "2099-01-01T00:00:00Z"

    def publish(self, *, capabilities: dict | None = None, **endpoint: Any) -> None:
        _write_private(self.ipc / "wg-capabilities.json", capabilities or _capabilities())
        _write_private(self.ipc / "wg-endpoint.json", _endpoint(self.instance, self.secret, self.port, **endpoint))

    def transport(self, base_url: str) -> "FakeTransport":
        return FakeTransport(self, base_url)

    def of(self, method: str, path: str) -> list[Request]:
        return [r for r in self.requests if r.route == (method, LIVE + path)]

    def answer(self, request: Request) -> "wglink_live.Answer":
        if self.down:
            raise wglink_live.NetworkFailure("connection refused")
        queued = self.script.get((request.method, request.path[len(LIVE):]))
        if queued:
            item = queued.pop(0)
            return item(request) if callable(item) else item
        route = (request.method, request.path[len(LIVE):])
        if route == ("GET", "/endpoint"):
            return wglink_live.Answer(200, {
                "schemaVersion": 1, "producer": "waveguide-generator", "instanceId": self.instance,
                "liveProtocol": 1, "deliveryVersion": 3,
            })
        installation = request.headers.get(INSTALLATION_HEADER)
        if route == ("POST", "/sessions"):
            body = request.body
            assert isinstance(body, dict)
            assert installation == body["installationId"]
            assert body["clientProof"] == _mac(
                self.secret, "wglink-client", body["clientNonce"], self.instance, installation
            ), "the client proof is not the documented HMAC"
            token = f"token-{len(self.tokens) + 1}-{self.instance[:4]}"
            self.tokens.append(token)
            self.current = token
            proof = _mac(self.secret, "wglink-server", body["clientNonce"], self.instance, installation)
            if self.proof_override is not None:
                proof = self.proof_override
            answer = {
                "liveSessionId": "live-1", "sessionToken": token,
                "expiresAt": "2099-01-01T00:05:00Z", "refreshAfter": self.refresh_after,
                "idleTimeoutSeconds": 60, "heartbeatIntervalSeconds": 4, "longPollSeconds": 25,
                "instanceId": self.instance, "serverProof": proof, "liveProtocol": 1,
                "capabilities": _capabilities(), "loadedIdentity": {**body["loadedIdentity"], "matchesPin": False},
            }
            if proof is ...:
                del answer["serverProof"]
            return wglink_live.Answer(201, answer)
        bearer = request.headers.get("Authorization", "")
        if not installation:
            return _refusal(401, "installation_mismatch")
        if bearer != f"Bearer {self.current}":
            return _refusal(401, "session_unknown")
        if route == ("POST", "/sessions/refresh"):
            token = f"token-{len(self.tokens) + 1}-{self.instance[:4]}"
            self.tokens.append(token)
            self.current = token
            return wglink_live.Answer(200, {
                "liveSessionId": "live-1", "sessionToken": token,
                "expiresAt": "2099-01-01T00:05:00Z", "refreshAfter": self.refresh_after,
            })
        if route == ("POST", "/heartbeat"):
            return wglink_live.Answer(204, None)
        if route == ("DELETE", "/sessions/current"):
            self.current = None
            return wglink_live.Answer(204, None)
        raise AssertionError(f"unexpected request {route}")


class FakeTransport:
    def __init__(self, wg: FakeWG, base_url: str) -> None:
        self.wg, self.base_url = wg, base_url

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None,
                body: object = None, timeout: float = 5.0) -> "wglink_live.Answer":
        assert self.base_url == f"http://127.0.0.1:{self.wg.port}", "the client left the endpoint's baseUrl"
        assert 0 < timeout <= wglink_live.DELIVERY_TIMEOUT_SECONDS
        assert not path.startswith(LIVE), "paths are relative to the live prefix"
        request = Request(method, LIVE + path, dict(headers or {}), body, threading.current_thread().name)
        self.wg.requests.append(request)
        return self.wg.answer(request)


def _client(
    ipc: Path | Callable[[], Path | None],
    wgs: FakeWG | Callable[[], FakeWG],
    *,
    clock: Clock | None = None,
    lease: Callable[[], bool] = lambda: True,
    nonce: Callable[[int], bytes] | None = None,
    **kwargs: Any,
) -> "wglink_live.LiveClient":
    resolve = wgs if callable(wgs) and not isinstance(wgs, FakeWG) else (lambda: wgs)
    clock = clock or Clock()
    kwargs.setdefault("wall", clock.wall)
    return wglink_live.LiveClient(
        ipc_folder=ipc if callable(ipc) else (lambda: ipc),
        adapter_session_id=ADAPTER_SESSION,
        adapter_version="0.1.1",
        loaded_identity={
            "source": "unmanaged", "sourceCommit": None, "addinVersion": "0.1.1",
            "managedBy": None, "waveguideGeneratorRoot": None, "loadedAt": "2026-09-17T09:59:00Z",
        },
        lease_ok=lease,
        clock=clock,
        transport_factory=lambda base_url: resolve().transport(base_url),
        **({"nonce": nonce} if nonce is not None else {}),
        **kwargs,
    )


def _steps(client: "wglink_live.LiveClient", n: int = 4) -> None:
    for _ in range(n):
        client.step()


def _live(tmp_path: Path, **kwargs: Any) -> tuple[Path, FakeWG, "wglink_live.LiveClient", Clock]:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    clock = Clock()
    client = _client(ipc, wg, clock=clock, **kwargs)
    _steps(client)
    assert client.healthy(), client.status()
    return ipc, wg, client, clock


# -- proofs --------------------------------------------------------------------


def test_proofs_match_wg_vectors() -> None:
    """The vectors of WG ``server/tests/test_cadlink_live_session.py``
    ``test_proofs_match_the_documented_encoding`` at WG commit a3328f40 (the same values as at 212568df)."""

    secret = "Zm9vYmFyLXNlY3JldC0wMTIzNDU2Nzg5LWFiY2RlZmdoaWo"
    nonce = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    instance = "0123456789abcdef0123456789abcdef"
    installation = "inst-A_1"
    client = "wwUPXItpbKryw4TTit3YBX8tryoIOyuUIKPaaM7DJUY"
    server = "ncNzsZnz_77enGRnmE95tK87NH1DGnuBluYff-AEII0"

    assert wglink_live.client_proof(secret, nonce, instance, installation) == client
    assert wglink_live.server_proof(secret, nonce, instance, installation) == server
    assert wglink_live.server_proof_matches(secret, nonce, server, instance, installation)
    assert not wglink_live.server_proof_matches(secret, nonce, client, instance, installation)
    assert not wglink_live.server_proof_matches(secret, nonce, server + "=", instance, installation)
    assert not wglink_live.server_proof_matches(secret + " ", nonce, server, instance, installation)
    standard = base64.b64encode(base64.urlsafe_b64decode(server + "=")).decode().rstrip("=")
    assert wglink_live.decode_32(server) is not None
    assert wglink_live.decode_32(standard) is None
    assert wglink_live.decode_32(server + "=") is None
    assert wglink_live.encode(bytes(range(32))) == nonce


def test_recorded_proofs_verify_with_the_recorded_secret() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    register = fixture["exchanges"]["register"]
    endpoint = fixture["endpoint"]
    body = register["request"]["body"]
    assert body["clientProof"] == wglink_live.client_proof(
        endpoint["registrationSecret"], body["clientNonce"], endpoint["instanceId"], fixture["installationId"]
    )
    assert wglink_live.server_proof_matches(
        endpoint["registrationSecret"], body["clientNonce"], register["response"]["body"]["serverProof"],
        endpoint["instanceId"], fixture["installationId"],
    )


# -- discovery -----------------------------------------------------------------


GO_LIVE_REFUSALS: dict[str, dict[str, Any]] = {
    "no capability file": {"remove": "wg-capabilities.json"},
    "capability without liveProtocol": {"capabilities": _capabilities(liveProtocol=...)},
    "capability liveProtocol is a boolean": {"capabilities": _capabilities(liveProtocol=True)},
    "capability liveProtocol 0": {"capabilities": _capabilities(liveProtocol=0)},
    "capability schema 2": {"capabilities": _capabilities(schemaVersion=2)},
    "no endpoint file": {"remove": "wg-endpoint.json"},
    "endpoint not json": {"endpoint_text": "{not json"},
    "endpoint schema 2": {"endpoint": {"schemaVersion": 2}},
    "endpoint schema is a boolean": {"endpoint": {"schemaVersion": True}},
    "endpoint producer": {"endpoint": {"producer": "someone-else"}},
    "endpoint liveProtocol 2": {"endpoint": {"liveProtocol": 2}},
    "endpoint https": {"endpoint": {"baseUrl": "https://127.0.0.1:41001"}},
    "endpoint localhost": {"endpoint": {"baseUrl": "http://localhost:41001"}},
    "endpoint other host": {"endpoint": {"baseUrl": "http://10.0.0.2:41001"}},
    "endpoint port 0": {"endpoint": {"baseUrl": "http://127.0.0.1:0"}},
    "endpoint port 65536": {"endpoint": {"baseUrl": "http://127.0.0.1:65536"}},
    "endpoint path": {"endpoint": {"baseUrl": "http://127.0.0.1:41001/x"}},
    "endpoint trailing newline": {"endpoint": {"baseUrl": "http://127.0.0.1:41001\n"}},
    "endpoint instance not hex": {"endpoint": {"instanceId": "z" * 32}},
    "endpoint pid is a string": {"endpoint": {"pid": "4242"}},
    "endpoint pid is a boolean": {"endpoint": {"pid": True}},
    "endpoint startedAt missing": {"endpoint": {"startedAt": ...}},
    "endpoint secret empty": {"endpoint": {"registrationSecret": ""}},
    "endpoint secret missing": {"endpoint": {"registrationSecret": ...}},
    "hello 500": {"hello": wglink_live.Answer(500, {"detail": "boom"})},
    "hello 404": {"hello": wglink_live.Answer(404, None)},
    "hello store busy": {"hello": _refusal(503, "store_busy", True)},
    "hello not json": {"hello": wglink_live.Answer(200, wglink_live.UNPARSEABLE)},
    "hello other instance": {"hello_instance": "b" * 32},
    "hello other producer": {"hello_fields": {"producer": "x"}},
    "hello delivery version 2": {"hello_fields": {"deliveryVersion": 2}},
    "hello network failure": {"down": True},
}


@pytest.mark.parametrize("case", sorted(GO_LIVE_REFUSALS))
def test_each_go_live_rule_failure_stays_in_file_mode(tmp_path: Path, case: str) -> None:
    spec = GO_LIVE_REFUSALS[case]
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish(capabilities=spec.get("capabilities"), **spec.get("endpoint", {}))
    if "endpoint_text" in spec:
        _write_private(ipc / "wg-endpoint.json", spec["endpoint_text"])
    if "remove" in spec:
        (ipc / spec["remove"]).unlink()
    if "hello" in spec:
        wg.script[("GET", "/endpoint")] = [spec["hello"]] * 20
    if "hello_instance" in spec or "hello_fields" in spec:
        hello = {"schemaVersion": 1, "producer": "waveguide-generator", "instanceId": wg.instance,
                 "liveProtocol": 1, "deliveryVersion": 3}
        hello["instanceId"] = spec.get("hello_instance", wg.instance)
        hello.update(spec.get("hello_fields", {}))
        wg.script[("GET", "/endpoint")] = [wglink_live.Answer(200, hello)] * 20
    wg.down = spec.get("down", False)
    client = _client(ipc, wg)

    _steps(client, 8)

    assert not client.healthy()
    assert wg.of("POST", "/sessions") == []
    assert [r.route for r in wg.requests if r.route != ("GET", LIVE + "/endpoint")] == []
    assert client.status()["mode"] == "file"


@pytest.mark.skipif(not POSIX, reason="endpoint-file integrity is a POSIX rule; Windows relies on the profile ACL")
@pytest.mark.parametrize(
    "case",
    ["group-writable file", "group-readable file", "other-readable file", "symlinked file",
     "group-writable folder", "other-writable folder", "symlinked folder", "other owner"],
)
def test_posix_endpoint_file_mode_owner_symlink_and_folder_permissions_refuse_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    endpoint = ipc / "wg-endpoint.json"
    folder_for_client = ipc
    if case == "group-writable file":
        os.chmod(endpoint, 0o620)
    elif case == "group-readable file":
        os.chmod(endpoint, 0o640)
    elif case == "other-readable file":
        os.chmod(endpoint, 0o604)
    elif case == "symlinked file":
        real = tmp_path / "elsewhere.json"
        endpoint.rename(real)
        endpoint.symlink_to(real)
    elif case == "group-writable folder":
        os.chmod(ipc, 0o775)
    elif case == "other-writable folder":
        os.chmod(ipc, 0o757)
    elif case == "symlinked folder":
        folder_for_client = tmp_path / "linked-ipc"
        folder_for_client.symlink_to(ipc, target_is_directory=True)
    elif case == "other owner":
        uid = os.getuid()
        monkeypatch.setattr(wglink_live.os, "getuid", lambda: uid + 1)
    client = _client(folder_for_client, wg)

    _steps(client, 6)

    assert not client.healthy()
    assert wg.requests == []
    assert any("endpoint" in line or "folder" in line for line in client.take_log_lines())


@pytest.mark.skipif(not POSIX, reason="POSIX rule")
def test_a_private_endpoint_file_in_a_private_folder_goes_live(tmp_path: Path) -> None:
    ipc, wg, client, _clock = _live(tmp_path)
    assert stat.S_IMODE(os.lstat(ipc / "wg-endpoint.json").st_mode) == 0o600
    assert [r.route for r in wg.requests] == [("GET", LIVE + "/endpoint"), ("POST", LIVE + "/sessions")]


# -- registration --------------------------------------------------------------


@pytest.mark.parametrize("proof", ["wrong", "missing", "padded", "for another nonce"])
def test_a_wrong_server_proof_discards_the_token_and_sends_nothing_else(tmp_path: Path, proof: str) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    right = None

    def register_then_lie(request: Request) -> "wglink_live.Answer":
        nonlocal right
        wg.script.pop(("POST", "/sessions"), None)
        answer = wg.answer(request)
        right = answer.body["serverProof"]
        if proof == "wrong":
            answer.body["serverProof"] = _mac("impostor", "wglink-server", request.body["clientNonce"], wg.instance, request.headers[INSTALLATION_HEADER])
        elif proof == "missing":
            del answer.body["serverProof"]
        elif proof == "padded":
            answer.body["serverProof"] = right + "="
        else:
            answer.body["serverProof"] = _mac(wg.secret, "wglink-server", _b64(b"x" * 32), wg.instance, request.headers[INSTALLATION_HEADER])
        return answer

    wg.script[("POST", "/sessions")] = [register_then_lie]
    clock = Clock()
    client = _client(ipc, wg, clock=clock)
    _steps(client, 3)
    token = wg.tokens[0]
    client.offer_heartbeat(_heartbeat())
    for _ in range(10):
        clock.now += 31
        client.step()

    assert not client.healthy()
    assert [r.route for r in wg.requests] == [("GET", LIVE + "/endpoint"), ("POST", LIVE + "/sessions")]
    assert token not in json.dumps(client.status())
    assert all(token not in line for line in client.take_log_lines())
    # A changed endpoint file (a new WG start) is looked at again.
    wg.instance = "c" * 32
    wg.publish(startedAt="2026-09-17T11:00:00Z")
    client.step()
    assert len(wg.of("POST", "/sessions")) == 2
    assert client.healthy()


def test_every_registration_uses_a_fresh_nonce(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    clock = Clock()
    client = _client(ipc, wg, clock=clock)  # the default nonce source
    for _ in range(4):
        _steps(client, 2)
        assert client.healthy()
        wg.current = None  # WG forgets the session: the next heartbeat is 401
        clock.now += 40
        client.offer_heartbeat(_heartbeat())
        client.step()
    nonces = [r.body["clientNonce"] for r in wg.of("POST", "/sessions")]
    assert len(nonces) >= 4
    assert len(set(nonces)) == len(nonces)
    assert all(wglink_live.decode_32(n) is not None for n in nonces)


def test_the_registration_body_is_exactly_the_documented_fields(tmp_path: Path) -> None:
    _ipc_folder, wg, _client_, _clock = _live(tmp_path)
    request = wg.of("POST", "/sessions")[0]
    assert set(request.body) == {
        "cadApplication", "liveProtocol", "deliveryVersion", "installationId", "adapterSessionId",
        "adapterVersion", "clientNonce", "clientProof", "loadedIdentity",
    }
    assert (request.body["cadApplication"], request.body["liveProtocol"], request.body["deliveryVersion"]) == ("fusion360", 1, 3)
    assert request.body["adapterSessionId"] == ADAPTER_SESSION
    assert request.headers["Content-Type"] == "application/json"
    assert "Authorization" not in request.headers


def test_installation_id_is_created_once_and_sent_on_every_request_except_hello(tmp_path: Path) -> None:
    ipc, wg, client, clock = _live(tmp_path)
    stored = json.loads((ipc / ".wglink-installation.json").read_text(encoding="utf-8"))
    installation = stored["installationId"]
    client.offer_heartbeat(_heartbeat())
    client.step()
    clock.now += 600
    client.step()
    wg.current = None
    client.offer_heartbeat(_heartbeat(n=2))
    _steps(client, 3)

    assert len(wg.of("POST", "/sessions")) == 2
    for request in wg.requests:
        if request.route == ("GET", LIVE + "/endpoint"):
            assert INSTALLATION_HEADER not in request.headers
        else:
            assert request.headers[INSTALLATION_HEADER] == installation
        assert "Origin" not in request.headers
    assert wglink_live.installation_id(ipc) == installation
    # A second client (a later Fusion session) keeps the same installation.
    assert json.loads((ipc / ".wglink-installation.json").read_text(encoding="utf-8")) == stored


def test_an_invalid_installation_file_is_replaced(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    (ipc / ".wglink-installation.json").write_text('{"schemaVersion": 1, "installationId": "../bad"}')
    first = wglink_live.installation_id(ipc)
    assert first is not None and first != "../bad"
    assert wglink_live.installation_id(ipc) == first


@pytest.mark.parametrize(
    "answer",
    [
        _refusal(409, "addin_outdated"),
        _refusal(409, "protocol_unsupported"),
        _refusal(400, "invalid_request"),
        _refusal(400, "installation_mismatch"),
    ],
    ids=["addin_outdated", "protocol_unsupported", "invalid_request", "installation_mismatch"],
)
def test_a_refused_registration_falls_back_to_files_until_a_file_changes(tmp_path: Path, answer) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    wg.script[("POST", "/sessions")] = [answer]
    clock = Clock()
    client = _client(ipc, wg, clock=clock)
    for _ in range(20):
        client.step()
        clock.now += 31

    assert not client.healthy()
    assert len(wg.of("POST", "/sessions")) == 1
    assert client.status()["mode"] == "file"
    wg.publish(startedAt="2026-09-17T12:00:00Z")
    client.step()
    assert len(wg.of("POST", "/sessions")) == 2
    assert client.healthy()


@pytest.mark.parametrize(
    "answer",
    [
        wglink_live.Answer(500, {"detail": "Internal Server Error"}),
        _refusal(403, "origin_not_allowed"),
        wglink_live.Answer(404, None),
        _refusal(401, "session_unknown"),
        wglink_live.Answer(201, wglink_live.UNPARSEABLE),
        wglink_live.Answer(200, {"sessionToken": "x"}),
    ],
    ids=["500", "403", "404", "401 other", "201 unparseable", "200"],
)
def test_a_temporary_registration_refusal_is_retried_after_30_seconds(tmp_path: Path, answer) -> None:
    """Only a refusal no retry can change waits for WG to restart; the rest try again."""

    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    wg.script[("POST", "/sessions")] = [answer]
    clock = Clock()
    client = _client(ipc, wg, clock=clock)
    for _ in range(5):
        client.step()
        clock.now += 5
    assert not client.healthy()
    assert len(wg.of("POST", "/sessions")) == 1
    clock.now += 10  # 35 s after the refusal, no file changed
    _steps(client, 2)
    assert len(wg.of("POST", "/sessions")) == 2
    assert client.healthy()


def test_a_bad_registration_proof_rereads_the_files_once_then_waits(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    wg.script[("POST", "/sessions")] = [_refusal(401, "registration_proof_invalid")] * 5
    clock = Clock()
    client = _client(ipc, wg, clock=clock)
    for _ in range(12):
        client.step()
        clock.now += 31
    assert len(wg.of("POST", "/sessions")) == 2
    assert not client.healthy()


def test_store_busy_at_registration_is_retried_briefly(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    wg.script[("POST", "/sessions")] = [_refusal(503, "store_busy", True)] * 2
    clock = Clock()
    client = _client(ipc, wg, clock=clock)
    for _ in range(6):
        client.step()
        clock.now += 1.1
    assert client.healthy()
    assert len(wg.of("POST", "/sessions")) == 3


# -- session -------------------------------------------------------------------


def test_refresh_happens_before_expiry_and_swaps_the_token(tmp_path: Path) -> None:
    _ipc_folder, wg, client, clock = _live(tmp_path)
    first = wg.current
    clock.now += 599
    client.step()
    assert wg.of("POST", "/sessions/refresh") == []
    clock.now += 1
    client.step()
    refresh = wg.of("POST", "/sessions/refresh")
    assert len(refresh) == 1
    assert refresh[0].headers["Authorization"] == f"Bearer {first}"
    assert refresh[0].body is None
    client.offer_heartbeat(_heartbeat())
    client.step()
    assert wg.of("POST", "/heartbeat")[-1].headers["Authorization"] == f"Bearer {wg.current}"
    assert wg.current != first
    assert client.healthy()
    # The next refresh is 600 s after this one, not after registration.
    clock.now += 599
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 1
    clock.now += 1
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 2


def test_refresh_honours_an_earlier_refresh_after_from_wg(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    clock = Clock()
    wg.refresh_after = _utc(clock.wall() + 120)
    client = _client(ipc, wg, clock=clock)
    _steps(client, 2)
    clock.now += 119
    client.step()
    assert wg.of("POST", "/sessions/refresh") == []
    clock.now += 1
    wg.refresh_after = _utc(clock.wall() + 900)  # later than 600 s: 600 s governs
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 1
    clock.now += 599
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 1
    clock.now += 1
    wg.refresh_after = _utc(clock.wall() - 3600)  # a wall clock that jumped: at least 30 s
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 2
    clock.now += 29
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 2
    clock.now += 1
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 3
    assert client.healthy()


def test_a_busy_wg_refresh_is_retried_before_expiry(tmp_path: Path) -> None:
    _ipc_folder, wg, client, clock = _live(tmp_path)
    wg.script[("POST", "/sessions/refresh")] = [_refusal(503, "store_busy", True)]
    clock.now += 600
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 1
    clock.now += 1
    client.step()
    assert len(wg.of("POST", "/sessions/refresh")) == 2
    assert len(wg.of("POST", "/sessions")) == 1
    client.offer_heartbeat(_heartbeat())
    client.step()
    assert client.healthy()


@pytest.mark.parametrize("gap", [900, 901, 5000])
def test_an_expired_session_is_never_refreshed_but_registered_again(tmp_path: Path, gap: float) -> None:
    _ipc_folder, wg, client, clock = _live(tmp_path)
    clock.now += gap  # e.g. the machine slept through the refresh
    _steps(client, 3)
    assert wg.of("POST", "/sessions/refresh") == []
    assert len(wg.of("POST", "/sessions")) == 2
    assert client.healthy()


def test_a_401_re_registers_once_then_falls_back_to_files(tmp_path: Path) -> None:
    _ipc_folder, wg, client, clock = _live(tmp_path)
    wg.script[("POST", "/heartbeat")] = [_refusal(401, "session_superseded")] * 10
    client.offer_heartbeat(_heartbeat(n=1))
    client.step()
    assert not client.healthy()
    client.step()
    assert len(wg.of("POST", "/sessions")) == 2
    clock.now += 4
    client.offer_heartbeat(_heartbeat(n=2))
    client.step()
    for _ in range(5):
        clock.now += 4
        client.step()
    assert len(wg.of("POST", "/sessions")) == 2
    assert not client.healthy()
    assert client.status()["mode"] == "file"
    clock.now += 31
    wg.script.clear()
    _steps(client, 2)
    assert len(wg.of("POST", "/sessions")) == 3
    assert client.healthy()


def test_heartbeat_posts_exactly_the_offered_payload_once(tmp_path: Path) -> None:
    _ipc_folder, wg, client, _clock = _live(tmp_path)
    first = _heartbeat(n=1)
    expected = json.loads(json.dumps(first))
    client.offer_heartbeat(first)
    first["document"]["name"] = "changed after offering"
    _steps(client, 3)
    posts = wg.of("POST", "/heartbeat")
    assert len(posts) == 1
    assert posts[0].body == expected
    assert posts[0].headers["Content-Type"] == "application/json"
    client.offer_heartbeat(_heartbeat(n=2))
    client.offer_heartbeat(_heartbeat(n=3))
    _steps(client, 2)
    assert [p.body["diagnostics"]["tick"] for p in wg.of("POST", "/heartbeat")] == [1, 3]


def test_a_stale_heartbeat_is_dropped_and_a_session_mismatch_registers_again(tmp_path: Path) -> None:
    _ipc_folder, wg, client, _clock = _live(tmp_path)
    wg.script[("POST", "/heartbeat")] = [_refusal(409, "heartbeat_stale")]
    client.offer_heartbeat(_heartbeat(n=1))
    _steps(client, 2)
    assert len(wg.of("POST", "/heartbeat")) == 1
    assert len(wg.of("POST", "/sessions")) == 1
    assert client.healthy()
    wg.script[("POST", "/heartbeat")] = [_refusal(409, "session_mismatch")]
    client.offer_heartbeat(_heartbeat(n=2))
    _steps(client, 2)
    assert len(wg.of("POST", "/sessions")) == 2
    assert client.healthy()


@pytest.mark.parametrize(
    "answer",
    [_refusal(409, "addin_outdated"), _refusal(413, "request_too_large"), _refusal(403, "origin_not_allowed"),
     _refusal(400, "invalid_request"), wglink_live.Answer(200, {"unexpected": True})],
    ids=["addin_outdated", "413", "403", "400", "200"],
)
def test_a_heartbeat_refusal_uses_the_files_then_recovers(tmp_path: Path, answer) -> None:
    ipc, wg, client, clock = _live(tmp_path)
    token = wg.current
    wg.script[("POST", "/heartbeat")] = [answer]
    client.offer_heartbeat(_heartbeat())
    client.step()
    assert not client.healthy()
    assert client.status()["mode"] == "file"
    ends = wg.of("DELETE", "/sessions/current")
    assert len(ends) == 1 and ends[0].headers["Authorization"] == f"Bearer {token}"
    for _ in range(5):
        clock.now += 5
        client.step()
    assert len(wg.of("POST", "/sessions")) == 1
    clock.now += 6  # 31 s after the refusal, no file changed
    _steps(client, 2)
    assert len(wg.of("POST", "/sessions")) == 2
    assert client.healthy()
    client.offer_heartbeat(_heartbeat(n=2))
    client.step()
    assert wg.of("POST", "/heartbeat")[-1].headers["Authorization"] == f"Bearer {wg.current}"
    assert client.healthy()


def test_a_second_session_mismatch_soon_after_uses_the_files_for_30_seconds(tmp_path: Path) -> None:
    """Two Fusion processes on one data directory supersede each other; the flapping is bounded."""

    _ipc_folder, wg, client, clock = _live(tmp_path)
    wg.script[("POST", "/heartbeat")] = [_refusal(409, "session_mismatch")] * 10
    client.offer_heartbeat(_heartbeat(n=1))
    _steps(client, 2)
    assert len(wg.of("POST", "/sessions")) == 2
    clock.now += 4
    client.offer_heartbeat(_heartbeat(n=2))
    client.step()
    for _ in range(6):
        clock.now += 4
        client.offer_heartbeat(_heartbeat(n=3))
        client.step()
    assert len(wg.of("POST", "/sessions")) == 2
    assert not client.healthy()
    clock.now += 31
    wg.script.clear()
    _steps(client, 2)
    assert len(wg.of("POST", "/sessions")) == 3
    assert client.healthy()


def test_a_network_failure_clears_healthy_at_once_and_rediscovers(tmp_path: Path) -> None:
    _ipc_folder, wg, client, clock = _live(tmp_path)
    wg.down = True
    client.offer_heartbeat(_heartbeat())
    client.step()
    assert not client.healthy()
    assert client.status()["mode"] == "file"
    wg.down = False
    for _ in range(6):
        clock.now += 2
        client.step()
    assert client.healthy()
    assert len(wg.of("POST", "/sessions")) == 2


def test_healthy_lapses_without_an_authenticated_exchange_for_30_seconds(tmp_path: Path) -> None:
    _ipc_folder, _wg, client, clock = _live(tmp_path)
    clock.now += 30
    assert client.healthy()
    clock.now += 0.5
    assert not client.healthy()


def test_a_wg_restart_is_followed_by_rediscovery_and_registration(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    first = FakeWG(ipc, instance="1" * 32, secret="first-secret", port=41001)
    first.publish()
    current = {"wg": first}
    clock = Clock()
    client = _client(ipc, lambda: current["wg"], clock=clock)
    _steps(client, 2)
    assert client.healthy()

    # WG quits (connection refused) and a new start writes a new endpoint file.
    first.down = True
    client.offer_heartbeat(_heartbeat(n=1))
    client.step()
    assert not client.healthy()
    second = FakeWG(ipc, instance="2" * 32, secret="second-secret", port=41002)
    second.publish(startedAt="2026-09-17T10:05:00Z")
    current["wg"] = second
    _steps(client, 3)

    assert client.healthy()
    assert len(second.of("POST", "/sessions")) == 1
    assert client.status()["instanceId"] == "2" * 32
    client.offer_heartbeat(_heartbeat(n=2))
    client.step()
    assert second.of("POST", "/heartbeat")[0].headers["Authorization"] == f"Bearer {second.current}"


def test_a_changed_endpoint_file_while_live_drops_the_session(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    first = FakeWG(ipc, instance="1" * 32, secret="first-secret", port=41001)
    first.publish()
    current = {"wg": first}
    client = _client(ipc, lambda: current["wg"])
    _steps(client, 2)
    second = FakeWG(ipc, instance="2" * 32, secret="second-secret", port=41002)
    second.publish(startedAt="2026-09-17T10:05:00Z")
    current["wg"] = second
    client.step()
    client.step()
    assert client.status()["instanceId"] == "2" * 32
    assert first.of("POST", "/heartbeat") == []


def test_only_the_lease_owner_registers_and_losing_the_lease_ends_the_session(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    lease = {"owner": False}
    client = _client(ipc, wg, lease=lambda: lease["owner"])
    client.offer_heartbeat(_heartbeat())
    _steps(client, 5)
    assert wg.requests == []
    assert not client.healthy()

    lease["owner"] = True
    _steps(client, 2)
    assert client.healthy()
    token = wg.current

    lease["owner"] = False
    _steps(client, 3)
    ends = wg.of("DELETE", "/sessions/current")
    assert len(ends) == 1 and ends[0].headers["Authorization"] == f"Bearer {token}"
    assert not client.healthy()
    count = len(wg.requests)
    client.offer_heartbeat(_heartbeat(n=2))
    _steps(client, 5)
    assert len(wg.requests) == count


def test_losing_the_lease_while_the_hello_is_in_flight_sends_no_registration(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    lease = {"owner": True}

    def hello_then_lose_lease(request: Request) -> "wglink_live.Answer":
        lease["owner"] = False  # a standby is promoted meanwhile
        wg.script.pop(("GET", "/endpoint"), None)
        return wg.answer(request)

    wg.script[("GET", "/endpoint")] = [hello_then_lose_lease]
    client = _client(ipc, wg, lease=lambda: lease["owner"])
    _steps(client, 4)
    assert wg.of("POST", "/sessions") == []
    assert not client.healthy()


def test_losing_the_lease_during_registration_ends_the_new_session_at_once(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc)
    wg.publish()
    lease = {"owner": True}

    def register_then_lose_lease(request: Request) -> "wglink_live.Answer":
        wg.script.pop(("POST", "/sessions"), None)
        answer = wg.answer(request)
        lease["owner"] = False
        return answer

    wg.script[("POST", "/sessions")] = [register_then_lose_lease]
    client = _client(ipc, wg, lease=lambda: lease["owner"])
    client.offer_heartbeat(_heartbeat())
    client.step()  # hello and registration
    # The session WG issued is never kept, not even for one step.
    assert not client.healthy()
    ends = wg.of("DELETE", "/sessions/current")
    assert len(ends) == 1 and ends[0].headers["Authorization"] == f"Bearer {wg.tokens[0]}"
    _steps(client, 3)
    assert len(wg.of("DELETE", "/sessions/current")) == 1
    assert wg.of("POST", "/heartbeat") == []


def test_no_token_secret_or_proof_reaches_logs_status_or_files(tmp_path: Path) -> None:
    ipc = _ipc(tmp_path)
    wg = FakeWG(ipc, secret="very-private-registration-secret")
    wg.publish()
    clock = Clock()
    client = _client(ipc, wg, clock=clock)
    lines: list[str] = []
    _steps(client, 2)
    client.offer_heartbeat(_heartbeat())
    client.step()
    clock.now += 600
    client.step()
    wg.script[("POST", "/heartbeat")] = [_refusal(401, "token_expired"), _refusal(409, "addin_outdated")]
    client.offer_heartbeat(_heartbeat(n=2))
    _steps(client, 3)
    client.offer_heartbeat(_heartbeat(n=3))
    _steps(client, 3)
    lines += client.take_log_lines()
    assert lines, "the client logged nothing, so this test proves nothing"

    secrets_seen = set(wg.tokens) | {wg.secret}
    for request in wg.of("POST", "/sessions"):
        secrets_seen |= {request.body["clientNonce"], request.body["clientProof"]}
        secrets_seen.add(_mac(wg.secret, "wglink-server", request.body["clientNonce"], wg.instance, request.headers[INSTALLATION_HEADER]))
    published = "\n".join(lines) + json.dumps(client.status()) + repr(client)
    for value in secrets_seen:
        assert value not in published
    for path in ipc.rglob("*"):
        if path.is_file() and path.name != "wg-endpoint.json":
            text = path.read_text(encoding="utf-8", errors="replace")
            for value in secrets_seen:
                assert value not in text, path.name


# -- transport (real loopback HTTP) --------------------------------------------


class DropConnection(Exception):
    """Tell the loopback stub to close without writing an HTTP response."""


class StubServer:
    """A loopback HTTP server whose answers a test chooses."""

    def __init__(self, handle: Callable[["StubServer", str, str, dict, bytes], tuple[int, dict, bytes]]) -> None:
        self.handle = handle
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                # Header names as lower case: HTTP names are case-insensitive.
                headers = {k.lower(): v for k, v in self.headers.items()}
                stub.requests.append((self.command, self.path, headers, body))
                try:
                    status, extra, payload = stub.handle(stub, self.command, self.path, headers, body)
                except DropConnection:
                    self.close_connection = True
                    return
                except Exception as exc:  # noqa: BLE001 - surfaced by the test
                    stub.errors.append(exc)
                    status, extra, payload = 599, {}, b""
                self.send_response(status)
                for name, value in extra.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = _serve

        self.errors: list[Exception] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, name="StubWG", daemon=True
        )
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def stub_factory():
    made: list[StubServer] = []

    def make(handle) -> StubServer:
        stub = StubServer(handle)
        made.append(stub)
        return stub

    yield make
    for stub in made:
        stub.close()


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_client_never_follows_redirects_and_bypasses_proxies(
    stub_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handle(_stub, method, path, _headers, _body):
        if path == LIVE + "/endpoint":
            return 200, {"Content-Type": "application/json"}, b'{"ok": true}'
        if path == LIVE + "/heartbeat":
            return 302, {"Location": LIVE + "/elsewhere"}, b""
        return 200, {"Content-Type": "application/json"}, b'{"followed": true}'

    stub = stub_factory(handle)
    proxy = f"http://127.0.0.1:{_closed_port()}"
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    transport = wglink_live.Transport(stub.base_url)

    hello = transport.request("GET", "/endpoint")
    redirected = transport.request("POST", "/heartbeat", headers={"Content-Type": "application/json"}, body={"a": 1})

    assert hello == wglink_live.Answer(200, {"ok": True})
    assert redirected.status == 302
    assert [path for _m, path, _h, _b in stub.requests] == [LIVE + "/endpoint", LIVE + "/heartbeat"]
    assert all("origin" not in headers for _m, _p, headers, _b in stub.requests)


def test_the_transport_refuses_a_base_url_that_is_not_loopback_http() -> None:
    for base in ("https://127.0.0.1:1", "http://localhost:1", "http://127.0.0.1:1/x", "http://127.0.0.2:1"):
        with pytest.raises(ValueError):
            wglink_live.Transport(base)


def test_a_closed_port_or_a_timeout_is_a_network_failure(stub_factory) -> None:
    with pytest.raises(wglink_live.NetworkFailure):
        wglink_live.Transport(f"http://127.0.0.1:{_closed_port()}").request("GET", "/endpoint", timeout=1)

    def slow(_stub, *_args):
        time.sleep(1.5)
        # The caller has timed out and closed its socket by now. Model that
        # network failure without making the background handler write to the
        # closed connection after this test has already finished.
        raise DropConnection

    stub = stub_factory(slow)
    started = time.monotonic()
    with pytest.raises(wglink_live.NetworkFailure):
        wglink_live.Transport(stub.base_url).request("GET", "/endpoint", timeout=0.3)
    assert time.monotonic() - started < 1.4


def test_network_io_never_runs_on_the_thread_that_offers_heartbeats(
    tmp_path: Path, stub_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ipc = _ipc(tmp_path)
    secret, instance = "thread-secret", "d" * 32
    heartbeats = threading.Event()

    def handle(_stub, method, path, headers, body):
        if (method, path) == ("GET", LIVE + "/endpoint"):
            return 200, {}, json.dumps({"schemaVersion": 1, "producer": "waveguide-generator",
                                        "instanceId": instance, "liveProtocol": 1, "deliveryVersion": 3}).encode()
        if (method, path) == ("POST", LIVE + "/sessions"):
            sent = json.loads(body)
            proof = _mac(secret, "wglink-server", sent["clientNonce"], instance, headers[INSTALLATION_HEADER.lower()])
            return 201, {}, json.dumps({"liveSessionId": "s", "sessionToken": "tok", "serverProof": proof,
                                        "instanceId": instance, "liveProtocol": 1}).encode()
        if (method, path) == ("POST", LIVE + "/heartbeat"):
            heartbeats.set()
            return 204, {}, b""
        return 204, {}, b""

    stub = stub_factory(handle)
    _write_private(ipc / "wg-capabilities.json", _capabilities())
    _write_private(ipc / "wg-endpoint.json", _endpoint(instance, secret, stub.port))
    caller = threading.current_thread()
    connecting: list[threading.Thread] = []
    original = socket.socket.connect

    def recording_connect(self, address):
        connecting.append(threading.current_thread())
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", recording_connect)
    client = wglink_live.LiveClient(
        ipc_folder=lambda: ipc, adapter_session_id=ADAPTER_SESSION, adapter_version="0.1.1",
        loaded_identity={"source": "unmanaged", "sourceCommit": None, "addinVersion": "0.1.1",
                         "managedBy": None, "waveguideGeneratorRoot": None, "loadedAt": "2026-09-17T10:00:00Z"},
        lease_ok=lambda: True,
    )
    client.start()
    try:
        deadline = time.monotonic() + 10
        while not client.healthy() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.healthy(), (client.status(), client.take_log_lines(), stub.requests, stub.errors)
        for n in range(20):
            client.offer_heartbeat(_heartbeat(n=n))
            time.sleep(0.01)
        assert heartbeats.wait(10)
    finally:
        client.stop(timeout=5)
    assert not client.is_running()
    assert len(connecting) >= 3
    assert caller not in connecting
    assert ("DELETE", LIVE + "/sessions/current") in [(m, p) for m, p, _h, _b in stub.requests]


@pytest.mark.parametrize("slow", ["hello", "registration"])
def test_stop_during_a_slow_exchange_returns_in_time_and_registers_nothing_after(
    tmp_path: Path, stub_factory, slow: str
) -> None:
    ipc = _ipc(tmp_path)
    secret, instance = "slow-secret", "f" * 32
    in_flight = threading.Event()
    delay = 1.5

    def handle(_stub, method, path, headers, body):
        if (method, path) == ("GET", LIVE + "/endpoint"):
            if slow == "hello":
                in_flight.set()
                time.sleep(delay)
            return 200, {}, json.dumps({"schemaVersion": 1, "producer": "waveguide-generator",
                                        "instanceId": instance, "liveProtocol": 1, "deliveryVersion": 3}).encode()
        if (method, path) == ("POST", LIVE + "/sessions"):
            if slow == "registration":
                in_flight.set()
                time.sleep(delay)
            sent = json.loads(body)
            proof = _mac(secret, "wglink-server", sent["clientNonce"], instance, headers[INSTALLATION_HEADER.lower()])
            return 201, {}, json.dumps({"liveSessionId": "s", "sessionToken": "slow-token", "serverProof": proof,
                                        "instanceId": instance, "liveProtocol": 1}).encode()
        return 204, {}, b""

    stub = stub_factory(handle)
    _write_private(ipc / "wg-capabilities.json", _capabilities())
    _write_private(ipc / "wg-endpoint.json", _endpoint(instance, secret, stub.port))
    client = wglink_live.LiveClient(
        ipc_folder=lambda: ipc, adapter_session_id=ADAPTER_SESSION, adapter_version="0.1.1",
        loaded_identity={"source": "unmanaged", "sourceCommit": None, "addinVersion": "0.1.1",
                         "managedBy": None, "waveguideGeneratorRoot": None, "loadedAt": "2026-09-17T10:00:00Z"},
        lease_ok=lambda: True,
    )
    client.offer_heartbeat(_heartbeat())
    client.start()
    assert in_flight.wait(10)
    started = time.monotonic()
    client.stop(timeout=3)
    assert time.monotonic() - started < 3.2
    deadline = time.monotonic() + 10
    while client.is_running() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not client.is_running()
    assert not client.healthy()
    routes = [(m, p) for m, p, _h, _b in stub.requests]
    if slow == "hello":
        assert routes == [("GET", LIVE + "/endpoint")]
    else:
        # The token WG issued meanwhile is ended at once; nothing else is sent.
        assert routes == [("GET", LIVE + "/endpoint"), ("POST", LIVE + "/sessions"),
                          ("DELETE", LIVE + "/sessions/current")]


def test_an_oversized_answer_is_not_parsed(stub_factory) -> None:
    padded = b"{}" + b" " * wglink_live.MAX_RESPONSE_BYTES
    stub = stub_factory(lambda *_a: (200, {"Content-Type": "application/json"}, padded))
    answer = wglink_live.Transport(stub.base_url).request("GET", "/endpoint")
    assert answer.status == 200 and answer.body is wglink_live.UNPARSEABLE


# -- recorded exchanges from WG ------------------------------------------------


def _fixture() -> dict[str, Any]:
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXCHANGES_SHA256, "the recorded exchanges changed"
    return json.loads(raw)


class Replay:
    """Serves recorded WG answers, in order, and checks each request against the recording."""

    def __init__(self, fixture: dict[str, Any], names: list[str], port_of: Callable[[], int]) -> None:
        self.fixture, self.names, self.port_of = fixture, list(names), port_of
        self.served: list[str] = []
        self.mismatches: list[str] = []

    def __call__(self, _stub, method: str, path: str, headers: dict[str, str], body: bytes):
        if not self.names:
            self.mismatches.append(f"unexpected extra request {method} {path}")
            return 599, {}, b""
        name = self.names.pop(0)
        exchange = self.fixture["exchanges"][name]
        expected = exchange["request"]
        problems = []
        if (method, path) != (expected["method"], expected["path"]):
            problems.append(f"{method} {path} != {expected['method']} {expected['path']}")
        lowered = {k.lower(): v for k, v in headers.items()}
        for header in ("Content-Type", "Authorization", "X-WGLink-Installation", "Origin"):
            want = expected["headers"].get(header)
            got = lowered.get(header.lower())
            if want is not None and header == "Origin":
                want = want.replace("{port}", str(self.port_of()))
            if want != got:
                problems.append(f"header {header}: {got!r} != {want!r}")
        sent = json.loads(body) if body else None
        if sent != expected["body"]:
            problems.append(f"body differs: {sent!r} != {expected['body']!r}")
        if problems:
            self.mismatches.append(f"{name}: " + "; ".join(problems))
        self.served.append(name)
        response = exchange["response"]
        payload = json.dumps(response["body"]).encode() if response["body"] is not None else b""
        headers = {"Content-Type": "application/json"} if payload else {}
        headers.update(response.get("headers") or {})
        return response["status"], headers, payload


def _replay_client(tmp_path: Path, fixture: dict[str, Any], port: int, clock: Clock, endpoint_key: str = "endpoint"):
    ipc = _ipc(tmp_path)
    endpoint = dict(fixture[endpoint_key])
    endpoint["baseUrl"] = endpoint["baseUrl"].replace("{port}", str(port))
    _write_private(ipc / "wg-capabilities.json", fixture["capabilities"])
    _write_private(ipc / "wg-endpoint.json", endpoint)
    _write_private(ipc / ".wglink-installation.json",
                   {"schemaVersion": 1, "installationId": fixture["installationId"]})
    nonce = bytes.fromhex(fixture["clientNonceHex"])
    client = wglink_live.LiveClient(
        ipc_folder=lambda: ipc,
        adapter_session_id=fixture["adapterSessionId"],
        adapter_version="0.1.1",
        loaded_identity=fixture["loadedIdentity"],
        lease_ok=lambda: True,
        clock=clock,
        wall=clock.wall,
        nonce=lambda size: nonce[:size],
    )
    return ipc, client


def test_recorded_exchanges_replay_against_the_client(tmp_path: Path, stub_factory) -> None:
    """Hello, registration (byte-exact proofs), heartbeat, refresh and end, as WG answered them."""

    fixture = _fixture()
    replay = Replay(fixture, ["hello", "register", "heartbeat", "refresh", "heartbeat_after_refresh", "end"],
                    lambda: stub.port)
    stub = stub_factory(replay)
    clock = Clock()
    _ipc_folder, client = _replay_client(tmp_path, fixture, stub.port, clock)

    _steps(client, 2)
    assert client.healthy()
    client.offer_heartbeat(fixture["exchanges"]["heartbeat"]["request"]["body"])
    client.step()
    clock.now += 600
    client.step()
    client.offer_heartbeat(fixture["exchanges"]["heartbeat_after_refresh"]["request"]["body"])
    client.step()
    assert client.healthy()
    client.end()

    assert replay.mismatches == []
    assert replay.served == ["hello", "register", "heartbeat", "refresh", "heartbeat_after_refresh", "end"]
    assert not client.healthy()


@pytest.mark.parametrize(
    "refusal", ["register_bad_proof", "register_protocol_unsupported", "register_addin_outdated",
                "register_unknown_field", "register_reused_nonce"],
)
def test_recorded_registration_refusals_leave_the_client_on_files(tmp_path: Path, stub_factory, refusal: str) -> None:
    fixture = _fixture()
    answers = {"hello": fixture["exchanges"]["hello"]["response"],
               "refusal": fixture["exchanges"][refusal]["response"]}
    served: list[str] = []

    def handle(_stub, method, path, _headers, _body):
        name = "hello" if path.endswith("/endpoint") else "refusal"
        served.append(f"{method} {path}")
        response = answers[name]
        return response["status"], {}, json.dumps(response["body"]).encode()

    stub = stub_factory(handle)
    clock = Clock()
    _ipc_folder, client = _replay_client(tmp_path, fixture, stub.port, clock)
    for _ in range(10):
        client.step()
        clock.now += 31
    assert not client.healthy()
    registrations = [s for s in served if s.endswith("/sessions")]
    assert 1 <= len(registrations) <= 2  # a proof refusal re-reads the files once
    assert client.status()["mode"] == "file"


@pytest.mark.parametrize(
    "refusal", ["heartbeat_after_idle", "heartbeat_after_end", "restart_old_token", "heartbeat_without_installation"],
)
def test_recorded_session_401s_register_again(tmp_path: Path, stub_factory, refusal: str) -> None:
    fixture = _fixture()
    exchanges = fixture["exchanges"]
    queue = ["hello", "register", refusal, "hello", "register"]
    served: list[str] = []

    def handle(_stub, method, path, _headers, _body):
        name = queue.pop(0)
        served.append(name)
        response = exchanges[name]["response"]
        payload = json.dumps(response["body"]).encode() if response["body"] is not None else b""
        return response["status"], {}, payload

    stub = stub_factory(handle)
    clock = Clock()
    _ipc_folder, client = _replay_client(tmp_path, fixture, stub.port, clock)
    # The recorded nonce is fixed, so the re-registration replays the recorded answer.
    _steps(client, 2)
    client.offer_heartbeat(exchanges["heartbeat"]["request"]["body"])
    _steps(client, 3)
    assert served == ["hello", "register", refusal, "hello", "register"]
    assert client.healthy()


def test_the_recorded_restart_registers_with_the_new_secret(tmp_path: Path, stub_factory) -> None:
    fixture = _fixture()
    replay = Replay(fixture, ["restart_hello", "restart_register"], lambda: stub.port)
    stub = stub_factory(replay)
    _ipc_folder, client = _replay_client(tmp_path, fixture, stub.port, Clock(), endpoint_key="restartedEndpoint")
    _steps(client, 2)
    assert replay.mismatches == []
    assert client.healthy()
    assert client.status()["instanceId"] == fixture["restartedEndpoint"]["instanceId"]


def test_recorded_store_busy_hello_is_not_live(tmp_path: Path, stub_factory) -> None:
    fixture = _fixture()
    response = fixture["exchanges"]["hello_store_busy"]["response"]
    stub = stub_factory(lambda *_a: (response["status"], {}, json.dumps(response["body"]).encode()))
    _ipc_folder, client = _replay_client(tmp_path, fixture, stub.port, Clock())
    _steps(client, 4)
    assert not client.healthy()
    assert all(path.endswith("/endpoint") for _m, path, _h, _b in stub.requests)


# -- loaded identity -----------------------------------------------------------


def test_loaded_identity_with_no_markers_is_unmanaged(tmp_path: Path) -> None:
    addin = tmp_path / "WGLink"
    addin.mkdir()
    identity = wglink_live.loaded_identity(addin, addin_version="0.1.1", loaded_at="2026-09-17T10:00:00Z")
    assert identity == {"source": "unmanaged", "sourceCommit": None, "addinVersion": "0.1.1",
                        "managedBy": None, "waveguideGeneratorRoot": None,
                        "loadedAt": "2026-09-17T10:00:00Z"}


def test_loaded_identity_with_both_markers_prefers_dev_sync(tmp_path: Path) -> None:
    addin = tmp_path / "WGLink"
    addin.mkdir()
    dev_commit = "04b2524b461929f0657348ac07971e556cccf2f9"
    managed_commit = "9f8e7d6c5b4a392817161514131211100f0e0d0c"
    (addin / "wglink_dev.json").write_text(json.dumps({"sourceCommit": dev_commit, "sourceRoot": "/x"}))
    (addin / "wglink_install.json").write_text(json.dumps({
        "schema": 1, "managedBy": "waveguide-generator", "waveguideGeneratorRoot": "/Applications/WG",
        "waveguideGeneratorVersion": "0.3.3", "sourceCommit": managed_commit, "addinVersion": "0.1.1",
    }))
    identity = wglink_live.loaded_identity(addin, addin_version="0.1.1", loaded_at="2026-09-17T10:00:00Z")
    assert identity == {"source": "devSync", "sourceCommit": dev_commit, "addinVersion": "0.1.1",
                        "managedBy": None, "waveguideGeneratorRoot": None,
                        "loadedAt": "2026-09-17T10:00:00Z"}


def test_loaded_identity_with_only_managed_marker_is_managed(tmp_path: Path) -> None:
    addin = tmp_path / "WGLink"
    addin.mkdir()
    commit = "04b2524b461929f0657348ac07971e556cccf2f9"
    (addin / "wglink_install.json").write_text(json.dumps({
        "schema": 1, "managedBy": "waveguide-generator", "waveguideGeneratorRoot": "/Applications/WG",
        "sourceCommit": commit, "addinVersion": "0.1.1",
    }))
    identity = wglink_live.loaded_identity(addin, addin_version="0.1.1", loaded_at="2026-09-17T10:00:00Z")
    assert (identity["source"], identity["sourceCommit"]) == ("managed", commit)


def test_loaded_identity_with_only_dev_marker_is_dev_sync(tmp_path: Path) -> None:
    addin = tmp_path / "WGLink"
    addin.mkdir()
    commit = "04b2524b461929f0657348ac07971e556cccf2f9"
    (addin / "wglink_dev.json").write_text(json.dumps({"sourceCommit": commit, "sourceRoot": "/x"}))
    identity = wglink_live.loaded_identity(addin, addin_version="0.1.1", loaded_at="2026-09-17T10:00:00Z")
    assert (identity["source"], identity["sourceCommit"]) == ("devSync", commit)
    # A dev commit outside WG's registration grammar is sent as null, not as-is.
    (addin / "wglink_dev.json").write_text(json.dumps({"sourceCommit": "04b2524-dirty", "sourceRoot": "/x"}))
    dirty = wglink_live.loaded_identity(addin, addin_version="0.1.1", loaded_at="2026-09-17T10:00:00Z")
    assert (dirty["source"], dirty["sourceCommit"]) == ("devSync", None)


def test_loaded_identity_with_invalid_dev_marker_falls_back_to_managed(tmp_path: Path) -> None:
    addin = tmp_path / "WGLink"
    addin.mkdir()
    commit = "04b2524b461929f0657348ac07971e556cccf2f9"
    (addin / "wglink_dev.json").write_text(json.dumps(["not", "a", "mapping"]))
    (addin / "wglink_install.json").write_text(json.dumps({
        "schema": 1, "managedBy": "waveguide-generator", "waveguideGeneratorRoot": "/Applications/WG",
        "sourceCommit": commit, "addinVersion": "0.1.1",
    }))
    identity = wglink_live.loaded_identity(addin, addin_version="0.1.1", loaded_at="2026-09-17T10:00:00Z")
    assert (identity["source"], identity["sourceCommit"]) == ("managed", commit)


def test_the_module_never_imports_fusion() -> None:
    source = (ROOT / "fusion-addins" / "WGLink" / "wglink_live.py").read_text(encoding="utf-8")
    assert "import adsk" not in source and "from adsk" not in source
