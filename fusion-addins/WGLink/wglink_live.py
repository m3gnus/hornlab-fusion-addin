"""The live CAD Link session with Waveguide Generator, beside the v3 files.

Contract: WG ``docs/reference/CADLINK-LIVE-PROTOCOL.md`` (protocol version 1),
as WG implements it in ``server/cadlink/live``. This module is the add-in half
of sections 2-6: endpoint discovery and the go-live rule, registration with
mutual proof, session tokens and their refresh, and the heartbeat posted over
HTTP. The v3 files are never replaced: WGLink keeps writing its file heartbeat
and keeps using every v3 request file whatever happens here, and any failure --
a missing or untrustworthy endpoint file, a refusal, a proof that does not
verify, a network error -- simply leaves it on the files.

Deliberately free of ``adsk`` and of any call into ``WGLink.py``: every network
exchange runs on this module's own worker thread, and Fusion's API belongs to
the main thread. ``WGLink.py`` owns the lifecycle and only ever calls the
non-blocking methods of :class:`LiveClient` (``offer_heartbeat``, ``healthy``,
``take_log_lines``, ``status``) plus ``start``/``stop``.

Secrets: the registration secret stays in WG's endpoint file and in memory for
the duration of one registration; the session token, proofs and nonces live
only in memory. None of them is logged, written to a file, or reported by
:meth:`LiveClient.status`.

Section 8, WG-bound deliveries, is the outbox: :func:`produce_solve` and
:func:`produce_snapshot` write an item on the main thread (files only, no
network), and the worker delivers it over the session under its fixed
operation id, writes a solve's v3 file when live delivery is not possible, and
reports WG's final answer through :meth:`LiveClient.take_delivery_notices`.
The Fusion-bound request long poll attaches to the same session later; it is
not implemented here.
"""

from __future__ import annotations

import base64
import binascii
from collections import deque
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import queue
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request
import uuid


LIVE_PREFIX = "/api/cadlink/live"
INSTALLATION_HEADER = "X-WGLink-Installation"
CAPABILITIES_FILENAME = "wg-capabilities.json"
ENDPOINT_FILENAME = "wg-endpoint.json"
INSTALLATION_FILENAME = ".wglink-installation.json"
PRODUCER = "waveguide-generator"
CAPABILITIES_SCHEMA_VERSION = 1
ENDPOINT_SCHEMA_VERSION = 1
INSTALLATION_SCHEMA_VERSION = 1
LIVE_PROTOCOL = 1
LIVE_PROTOCOL_CAPABILITY = "liveProtocol"
DELIVERY_VERSION = 3
CAD_APPLICATION = "fusion360"
CLIENT_LABEL = "wglink-client"
SERVER_LABEL = "wglink-server"

#: WG's session numbers (``server/cadlink/live/registry.py``): a token lives
#: 15 minutes and is meant to be refreshed after 10. WG measures both on its
#: monotonic clock; ``expiresAt``/``refreshAfter`` in its answers are wall-clock
#: times for information only, so the client keeps its own monotonic deadlines
#: from the moment it received the token.
REFRESH_AFTER_SECONDS = 10 * 60
SESSION_LIFETIME_SECONDS = 15 * 60
#: The shortest refresh delay taken from WG's ``refreshAfter``: a wall clock
#: that jumped must not turn refreshing into a request per second.
MIN_REFRESH_SECONDS = 30.0
#: A stale endpoint is looked at again when either file changes, or this often.
STALE_RECHECK_SECONDS = 30.0
#: Healthy means an authenticated exchange succeeded at most this long ago.
HEALTHY_WINDOW_SECONDS = 30.0
#: A second session loss (401 or ``session_mismatch``) this soon after
#: re-registering sends the client to the files for STALE_RECHECK_SECONDS. Two
#: Fusion processes on one data directory share an installation id and so
#: supersede each other's sessions; this bounds that flapping.
REREGISTER_WINDOW_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 5.0
END_TIMEOUT_SECONDS = 2.0
STORE_BUSY_RETRIES = 5
STORE_BUSY_RETRY_SECONDS = 1.0
#: Idle wake-up of the worker; heartbeats and stop wake it at once.
IDLE_STEP_SECONDS = 1.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ENDPOINT_BYTES = 64 * 1024
LOG_LINES_KEPT = 32

_BASE_URL = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})")
_INSTANCE_ID = re.compile(r"[0-9a-f]{32}")
#: WG's installation id grammar (``server/cadlink/live/api.py``).
_INSTALLATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
#: What WG accepts after ``Bearer `` (``server/cadlink/live/api.py``).
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,256}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_ENCODED_32_BYTES = re.compile(r"[A-Za-z0-9_-]{43}")
_UTC_SECONDS = "%Y-%m-%dT%H:%M:%SZ"
#: Registration refusals that no retry can change while this WG runs: the
#: client waits for WG to rewrite a discovery file (a new start) instead.
_REGISTRATION_REFUSALS_UNTIL_RESTART = {
    (409, "addin_outdated"),
    (409, "protocol_unsupported"),
}


# -- proofs (protocol section 3, "Mutual proof") -------------------------------


def encode(raw: bytes) -> str:
    """Unpadded base64url."""

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_32(text: object) -> bytes | None:
    """The 32 bytes a canonical unpadded base64url string holds, or None."""

    if not isinstance(text, str) or _ENCODED_32_BYTES.fullmatch(text) is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=")
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 32 or encode(raw) != text:
        return None
    return raw


def _mac(secret: str, label: str, nonce: str, instance_id: str, installation_id: str) -> bytes:
    message = f"{label}\n{nonce}\n{instance_id}\n{installation_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def client_proof(secret: str, nonce: str, instance_id: str, installation_id: str) -> str:
    return encode(_mac(secret, CLIENT_LABEL, nonce, instance_id, installation_id))


def server_proof(secret: str, nonce: str, instance_id: str, installation_id: str) -> str:
    return encode(_mac(secret, SERVER_LABEL, nonce, instance_id, installation_id))


def server_proof_matches(
    secret: str, nonce: str, proof: object, instance_id: str, installation_id: str
) -> bool:
    """Whether WG's ``serverProof`` proves it read the same secret."""

    presented = decode_32(proof)
    if presented is None:
        return False
    expected = _mac(secret, SERVER_LABEL, nonce, instance_id, installation_id)
    return hmac.compare_digest(expected, presented)


# -- discovery (protocol section 2, "Client go-live rule") ---------------------


@dataclass(frozen=True)
class Endpoint:
    instance_id: str
    base_url: str
    secret: str = field(repr=False)


@dataclass(frozen=True)
class Stale:
    """Why the endpoint files do not allow going live; ``reason`` is safe to log."""

    reason: str


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def files_fingerprint(ipc_folder: Path) -> tuple:
    """What changes when WG rewrites either discovery file (or removes it)."""

    marks = []
    for name in (CAPABILITIES_FILENAME, ENDPOINT_FILENAME):
        try:
            st = os.lstat(Path(ipc_folder) / name)
        except OSError:
            marks.append(None)
            continue
        marks.append((st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size))
    return tuple(marks)


def _advertises_live(ipc_folder: Path) -> bool:
    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if not isinstance(payload, Mapping):
        return False
    schema = payload.get("schemaVersion")
    if not _is_int(schema) or schema != CAPABILITIES_SCHEMA_VERSION:
        return False
    value = payload.get(LIVE_PROTOCOL_CAPABILITY)
    return _is_int(value) and value >= 1


def _read_endpoint_bytes(ipc_folder: Path, posix: bool) -> bytes | Stale:
    path = Path(ipc_folder) / ENDPOINT_FILENAME
    if not posix:
        # Windows relies on the per-user profile ACL of the data directory.
        try:
            with open(path, "rb") as stream:
                return stream.read(MAX_ENDPOINT_BYTES + 1)
        except OSError:
            return Stale("no readable endpoint file")
    try:
        folder = os.lstat(ipc_folder)
    except OSError:
        return Stale("the ipc folder is unavailable")
    if not stat.S_ISDIR(folder.st_mode):
        return Stale("the ipc folder is a symlink or not a folder")
    if folder.st_mode & 0o022:
        return Stale("the ipc folder is writable by group or other")
    try:
        before = os.lstat(path)
    except OSError:
        return Stale("no endpoint file")
    if not stat.S_ISREG(before.st_mode):
        return Stale("the endpoint file is a symlink or not a regular file")
    if before.st_uid != os.getuid():
        return Stale("the endpoint file is owned by another user")
    if before.st_mode & 0o077:
        return Stale("the endpoint file is accessible to group or other")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return Stale("the endpoint file could not be opened")
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return Stale("the endpoint file changed while it was read")
        chunks = []
        remaining = MAX_ENDPOINT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_endpoint(ipc_folder: Path, *, posix: bool | None = None) -> Endpoint | Stale:
    """Go-live rules 1, 2 and 5: the capability, the endpoint file, its integrity."""

    posix = (os.name == "posix") if posix is None else posix
    if not _advertises_live(ipc_folder):
        return Stale("WG does not advertise the live protocol")
    raw = _read_endpoint_bytes(ipc_folder, posix)
    if isinstance(raw, Stale):
        return raw
    if len(raw) > MAX_ENDPOINT_BYTES:
        return Stale("the endpoint file is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return Stale("the endpoint file is not JSON")
    if not isinstance(payload, Mapping):
        return Stale("the endpoint file is not an object")
    schema = payload.get("schemaVersion")
    protocol = payload.get("liveProtocol")
    base_url = payload.get("baseUrl")
    instance_id = payload.get("instanceId")
    secret = payload.get("registrationSecret")
    if not _is_int(schema) or schema != ENDPOINT_SCHEMA_VERSION:
        return Stale("the endpoint file has another schema")
    if payload.get("producer") != PRODUCER:
        return Stale("the endpoint file names another producer")
    if not _is_int(protocol) or protocol != LIVE_PROTOCOL:
        return Stale("the endpoint file names another live protocol")
    if not _loopback_base_url(base_url):
        return Stale("the endpoint file's baseUrl is not http://127.0.0.1:<port>")
    if not isinstance(instance_id, str) or _INSTANCE_ID.fullmatch(instance_id) is None:
        return Stale("the endpoint file's instanceId is malformed")
    if not _is_int(payload.get("pid")):
        return Stale("the endpoint file's pid is malformed")
    if not isinstance(payload.get("startedAt"), str):
        return Stale("the endpoint file's startedAt is malformed")
    if not isinstance(secret, str) or not secret:
        return Stale("the endpoint file has no registration secret")
    return Endpoint(instance_id=instance_id, base_url=str(base_url), secret=secret)


def _loopback_base_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = _BASE_URL.fullmatch(value)
    return match is not None and int(match.group(1)) <= 65535


def _hello_matches(body: object, endpoint: Endpoint) -> bool:
    """Go-live rule 3: the same schema and the file's instance."""

    if not isinstance(body, Mapping):
        return False
    schema, protocol, delivery = (
        body.get("schemaVersion"), body.get("liveProtocol"), body.get("deliveryVersion"),
    )
    return (
        _is_int(schema)
        and schema == ENDPOINT_SCHEMA_VERSION
        and body.get("producer") == PRODUCER
        and _is_int(protocol)
        and protocol == LIVE_PROTOCOL
        and _is_int(delivery)
        and delivery >= DELIVERY_VERSION
        and body.get("instanceId") == endpoint.instance_id
    )


# -- installation and loaded identity ------------------------------------------


def installation_id(ipc_folder: Path) -> str | None:
    """This machine's WGLink installation id, created once in the ipc folder."""

    path = Path(ipc_folder) / INSTALLATION_FILENAME
    existing = _read_json(path)
    if _valid_installation(existing):
        return existing["installationId"]
    document = {"schemaVersion": INSTALLATION_SCHEMA_VERSION, "installationId": str(uuid.uuid4())}
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{INSTALLATION_FILENAME.lstrip('.')}.", suffix=".tmp", dir=ipc_folder
        )
    except OSError:
        return None
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if existing is None and not path.exists():
            try:
                # Exclusive: a racing writer's id wins and is read back.
                os.link(temporary, path)
            except FileExistsError:
                pass
            except OSError:
                os.replace(temporary, path)
        else:
            os.replace(temporary, path)
    except OSError:
        return None
    finally:
        temporary.unlink(missing_ok=True)
    stored = _read_json(path)
    return stored["installationId"] if _valid_installation(stored) else None


def _valid_installation(payload: object) -> bool:
    return (
        isinstance(payload, Mapping)
        and _is_int(payload.get("schemaVersion"))
        and payload.get("schemaVersion") == INSTALLATION_SCHEMA_VERSION
        and isinstance(payload.get("installationId"), str)
        and _INSTALLATION_ID.fullmatch(payload["installationId"]) is not None
    )


def _text(value: object, limit: int) -> str | None:
    return value if isinstance(value, str) and value and len(value) <= limit else None


def loaded_identity(addin_dir: Path, *, addin_version: str | None, loaded_at: str | None = None) -> dict[str, Any]:
    """What this add-in loaded: the managed marker, else the dev marker, else unmanaged.

    Values outside WG's registration grammar are sent as null, because WG
    refuses a registration with an invalid identity field.
    """

    loaded_at = loaded_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    version = _text(addin_version, 128)
    managed = _read_json(Path(addin_dir) / "wglink_install.json")
    if isinstance(managed, Mapping) and _is_int(managed.get("schema")) and managed.get("schema") == 1:
        commit = managed.get("sourceCommit")
        return {
            "source": "managed",
            "sourceCommit": commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None,
            "addinVersion": _text(managed.get("addinVersion"), 128) or version,
            "managedBy": _text(managed.get("managedBy"), 128),
            "waveguideGeneratorRoot": _text(managed.get("waveguideGeneratorRoot"), 4096),
            "loadedAt": loaded_at,
        }
    developer = _read_json(Path(addin_dir) / "wglink_dev.json")
    if isinstance(developer, Mapping):
        commit = developer.get("sourceCommit")
        return {
            "source": "devSync",
            "sourceCommit": commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None,
            "addinVersion": version,
            "managedBy": None,
            "waveguideGeneratorRoot": None,
            "loadedAt": loaded_at,
        }
    return {
        "source": "unmanaged",
        "sourceCommit": None,
        "addinVersion": version,
        "managedBy": None,
        "waveguideGeneratorRoot": None,
        "loadedAt": loaded_at,
    }


# -- transport -----------------------------------------------------------------


class NetworkFailure(Exception):
    """No HTTP answer: refused, reset, timed out, or unreadable."""


class _Unparseable:
    def __repr__(self) -> str:
        return "UNPARSEABLE"


#: The body of an answer that was not JSON.
UNPARSEABLE = _Unparseable()


@dataclass
class Answer:
    status: int
    body: Any = None
    #: The response headers the client reads (only ``Retry-After``).
    headers: dict = field(default_factory=dict)

    @property
    def code(self) -> str | None:
        """The error envelope's ``error.code``, if the body is one."""

        if isinstance(self.body, Mapping):
            error = self.body.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("code"), str):
                return error["code"]
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class Transport:
    """HTTP to exactly one loopback ``baseUrl``: no proxies, no redirects."""

    def __init__(self, base_url: str) -> None:
        if not _loopback_base_url(base_url):
            raise ValueError("the live transport only speaks http://127.0.0.1:<port>")
        self.base_url = base_url
        # An explicit empty ProxyHandler replaces the default one, which would
        # honour HTTP_PROXY/ALL_PROXY (and the system settings) even for loopback.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: object = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Answer:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + LIVE_PREFIX + path, data=data, headers=dict(headers or {}), method=method
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read(MAX_RESPONSE_BYTES + 1)
                kept = _kept_headers(response.headers)
        except urllib.error.HTTPError as error:
            kept = _kept_headers(error.headers)
            try:
                status, raw = error.code, error.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException):
                status, raw = error.code, b""
            finally:
                error.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
            raise NetworkFailure(type(error).__name__) from None
        if not raw:
            return Answer(status, None, kept)
        if len(raw) > MAX_RESPONSE_BYTES:
            return Answer(status, UNPARSEABLE, kept)
        try:
            return Answer(status, json.loads(raw.decode("utf-8")), kept)
        except (UnicodeError, ValueError):
            return Answer(status, UNPARSEABLE, kept)


def _kept_headers(headers: Any) -> dict[str, str]:
    value = headers.get("Retry-After") if headers is not None else None
    return {"Retry-After": value} if isinstance(value, str) else {}


# -- the outbox (protocol section 8, "The add-in's outbox") ---------------------


KIND_SOLVE = "prepare_and_solve"
KIND_SNAPSHOT = "receive_snapshot"
OUTBOX_DIRECTORY = ".wglink-outbox"
OUTBOX_SCHEMA_VERSION = 1
#: The outbox holds at most this many items, each for at most this long. A
#: full outbox refuses new items (a solve still goes out as its v3 file); an
#: item past its age is removed and the user told.
OUTBOX_MAX_ITEMS = 100
OUTBOX_MAX_AGE_SECONDS = 7 * 24 * 3600
#: Items the main thread adds are seen at once when it wakes the worker, and
#: otherwise by a rescan this often.
OUTBOX_RESCAN_SECONDS = 5.0
#: Windows refuses to unlink a file any reader still holds open, and the
#: readers of an outbox item are all momentary: a virus scanner, a backup
#: agent, an indexer, this suite's own inspection. Held is not lost: a settled
#: item's removal is retried for this long inline, and on every later step
#: after that, so it is never left on disk to be delivered a second time.
DELETE_RETRY_SECONDS = 0.5
DELETE_RETRY_STEP_SECONDS = 0.01
#: WG retains the return into its own storage before it answers a delivery.
DELIVERY_TIMEOUT_SECONDS = 30.0
#: WG answers ``503 snapshot_not_readable`` for at most 30 s from the first
#: such answer (``solve_command.LIVE_TRANSIENT_BOUND_S``), then 200. Past this
#: client guard, measured against the same WG instance, WG is not keeping that
#: bound: a solve goes out as its v3 file and the item slows down.
TRANSIENT_GUARD_SECONDS = 60.0
#: ``503 store_busy`` has no bound from WG: this many answers in a row, then a
#: solve goes out as its v3 file and the item slows down.
STORE_BUSY_DELIVERY_ANSWERS = 5
#: The retryable 409s are retried quickly for this long, then slowly.
RETRYABLE_REFUSAL_SECONDS = 60.0
FAST_RETRY_MAX_SECONDS = 10.0
#: The slow retry: 30 s, doubling, at most 5 minutes.
SLOW_RETRY_START_SECONDS = 30.0
SLOW_RETRY_MAX_SECONDS = 300.0
_RETRYABLE_REFUSALS = {(409, "wglink_folder_not_selected"), (409, "update_restart_pending")}
_KINDS = (KIND_SOLVE, KIND_SNAPSHOT)
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class OutboxFull(Exception):
    """The outbox holds OUTBOX_MAX_ITEMS; nothing was queued.

    ``solve_file`` is the v3 solve file written for a solve anyway, or None.
    """

    def __init__(self, operation_id: str, solve_file: Path | None = None) -> None:
        super().__init__(f"WGLink's outbox already holds {OUTBOX_MAX_ITEMS} deliveries")
        self.operation_id = operation_id
        self.solve_file = solve_file


def advertises_live(ipc_folder: Path | None) -> bool:
    """Whether WG's capability file advertises the live protocol (rule 1)."""

    return ipc_folder is not None and _advertises_live(Path(ipc_folder))


def _utc_millis(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_whole_seconds(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime(_UTC_SECONDS)


def _parse_utc(text: object) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def delivery_item(
    kind: str,
    *,
    operation_id: str,
    bundle_path: str,
    manifest_sha256: str,
    requested_at: str,
    created_at: str,
    return_id: str | None = None,
    file_written: bool = False,
) -> dict[str, Any]:
    """One outbox item. ``operationId`` is its identity for ever."""

    item: dict[str, Any] = {
        "schemaVersion": OUTBOX_SCHEMA_VERSION,
        "operationId": str(operation_id),
        "kind": kind,
        "bundlePath": str(bundle_path),
        "manifestSha256": str(manifest_sha256),
        "requestedAt": str(requested_at),
        "createdAt": str(created_at),
        "answer": None,
    }
    if kind == KIND_SOLVE:
        item["returnId"] = str(return_id or "")
        item["fileWritten"] = bool(file_written)
    return item


def valid_item(item: object) -> bool:
    if not isinstance(item, Mapping):
        return False
    kind = item.get("kind")
    return (
        _is_int(item.get("schemaVersion"))
        and item.get("schemaVersion") == OUTBOX_SCHEMA_VERSION
        and isinstance(item.get("operationId"), str)
        and _OPERATION_ID.fullmatch(item["operationId"]) is not None
        and kind in _KINDS
        and all(
            isinstance(item.get(name), str) and item.get(name)
            for name in ("bundlePath", "manifestSha256", "requestedAt", "createdAt")
        )
        and (item.get("answer") is None or isinstance(item.get("answer"), Mapping))
        and (
            kind != KIND_SOLVE
            or (isinstance(item.get("returnId"), str) and isinstance(item.get("fileWritten"), bool))
        )
    )


def delivery_body(item: Mapping[str, Any]) -> dict[str, Any]:
    """The protocol's delivery fields and nothing else (no client state, no transport)."""

    body = {name: item[name] for name in ("operationId", "kind", "bundlePath", "manifestSha256", "requestedAt")}
    if item["kind"] == KIND_SOLVE:
        body["returnId"] = item["returnId"]
    return body


class Outbox:
    """``<ipc>/.wglink-outbox/<operationId>.json``: one private file per item.

    WG never reads this folder. Every write is atomic (a private temporary
    file, fsync, replace), so a reader sees an item whole or not at all.
    After an item is created only the IPC lease owner's live worker changes
    or deletes it.
    """

    def __init__(self, ipc_folder: Path) -> None:
        self.folder = Path(ipc_folder) / OUTBOX_DIRECTORY

    def path(self, operation_id: str) -> Path:
        return self.folder / f"{operation_id}.json"

    def _names(self) -> list[Path]:
        try:
            return [
                path for path in self.folder.iterdir()
                if path.suffix == ".json" and not path.name.startswith(".")
            ]
        except OSError:
            return []

    def count(self) -> int:
        return len(self._names())

    def scan(self) -> tuple[list[dict[str, Any]], list[Path]]:
        """Valid items oldest first (``createdAt``, then id), and files that are not items."""

        items: list[dict[str, Any]] = []
        foreign: list[Path] = []
        for path in self._names()[: OUTBOX_MAX_ITEMS * 4]:
            item = _read_json(path)
            if valid_item(item) and path.stem == item["operationId"]:
                items.append(dict(item))
            else:
                foreign.append(path)
        items.sort(key=lambda item: (_parse_utc(item["createdAt"]) or 0.0, item["operationId"]))
        return items, foreign

    def read(self, operation_id: str) -> dict[str, Any] | None:
        item = _read_json(self.path(operation_id))
        return dict(item) if valid_item(item) and item["operationId"] == operation_id else None

    def add(self, item: Mapping[str, Any]) -> None:
        if self.count() >= OUTBOX_MAX_ITEMS:
            raise OutboxFull(str(item["operationId"]))
        self.write(item)

    def write(self, item: Mapping[str, Any]) -> None:
        if not valid_item(item):
            raise ValueError("not an outbox item")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.folder, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{item['operationId']}.", suffix=".tmp", dir=self.folder
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(dict(item), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(item["operationId"]))
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, operation_id: str) -> bool:
        """Remove an item; ``False`` means it is still on disk, so retry later.

        A refusal is never reported as success. On Windows an open reader is
        enough to refuse the unlink, so the bounded retry here settles the
        momentary holders; :meth:`LiveClient._forget` carries the rest.
        """

        path = self.path(operation_id)
        deadline = time.monotonic() + DELETE_RETRY_SECONDS
        while True:
            try:
                path.unlink(missing_ok=True)
                return True
            except OSError:
                if not path.exists():
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(DELETE_RETRY_STEP_SECONDS)


def produce_solve(
    ipc_folder: Path,
    *,
    bundle_path: Path,
    workspace_root: Path,
    return_id: str,
    healthy: bool,
    solve_files: Any,
    operation_id: str | None = None,
    wall: Callable[[], float] | None = None,
) -> dict[str, Any] | None:
    """"Solve in WG" on the main thread: queue the item, and the v3 file unless live.

    No network. None when WG does not advertise the live protocol (the caller
    writes the v3 file exactly as before). With a healthy live session the
    item alone is written and the worker delivers it; otherwise the v3 solve
    file is written first and the item records it, both under one operation
    id and one return reference. ``OutboxFull`` still leaves the v3 file.
    """

    if not advertises_live(ipc_folder):
        return None
    solve_files.require_solve_delivery(ipc_folder)
    relative, manifest = solve_files.return_reference(bundle_path, workspace_root)
    now = (wall or time.time)()
    operation_id = operation_id or str(uuid.uuid4())
    outbox = Outbox(ipc_folder)
    full = outbox.count() >= OUTBOX_MAX_ITEMS
    item = delivery_item(
        KIND_SOLVE,
        operation_id=operation_id,
        return_id=return_id,
        bundle_path=relative,
        manifest_sha256=manifest,
        requested_at=_utc_whole_seconds(now),
        created_at=_utc_millis(now),
        file_written=not healthy or full,
    )
    written = _write_solve_file(solve_files, ipc_folder, item) if item["fileWritten"] else None
    try:
        if full:
            raise OutboxFull(operation_id)
        outbox.add(item)
    except OutboxFull as exc:
        exc.solve_file = written or _write_solve_file(solve_files, ipc_folder, item)
        raise
    return item


def produce_snapshot(
    ipc_folder: Path,
    *,
    bundle_path: Path,
    workspace_root: Path,
    solve_files: Any,
    operation_id: str | None = None,
    wall: Callable[[], float] | None = None,
) -> dict[str, Any] | None:
    """"Send to WG" on the main thread: queue a ``receive_snapshot`` (never a file)."""

    if not advertises_live(ipc_folder):
        return None
    relative, manifest = solve_files.return_reference(bundle_path, workspace_root)
    now = (wall or time.time)()
    item = delivery_item(
        KIND_SNAPSHOT,
        operation_id=operation_id or str(uuid.uuid4()),
        bundle_path=relative,
        manifest_sha256=manifest,
        requested_at=_utc_whole_seconds(now),
        created_at=_utc_millis(now),
    )
    Outbox(ipc_folder).add(item)
    return item


def item_waiting_live(ipc_folder: Path, operation_id: str) -> bool:
    """An unanswered solve item that only the live delivery carries (no v3 file)."""

    item = Outbox(ipc_folder).read(operation_id)
    return item is not None and item.get("answer") is None and not item.get("fileWritten")


def _write_solve_file(solve_files: Any, ipc_folder: Path, item: Mapping[str, Any]) -> Path:
    return solve_files.write_solve_request_fields(
        ipc_folder,
        command_id=item["operationId"],
        return_id=item["returnId"],
        bundle_relative=item["bundlePath"],
        manifest_sha256=item["manifestSha256"],
        requested_at=item["requestedAt"],
    )


@dataclass
class _Retry:
    """One item's retry state, in memory (a restart retries at once)."""

    next_at: float = 0.0
    transient: tuple[str, float] | None = None  # (WG instance, first 503 not readable)
    busy: int = 0
    refused_since: float | None = None
    refusals: int = 0
    slow: float = 0.0


def _retry_after(answer: Answer) -> float:
    value = (answer.headers or {}).get("Retry-After")
    try:
        return max(0.0, min(float(value), FAST_RETRY_MAX_SECONDS)) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# -- the client ----------------------------------------------------------------


@dataclass
class _Session:
    endpoint: Endpoint = field(repr=False)
    transport: Any = field(repr=False)
    installation_id: str
    live_session_id: str
    token: str = field(repr=False)
    fingerprint: tuple = field(repr=False)
    refresh_at: float = 0.0
    expires_at: float = 0.0
    registered_at: float = 0.0
    last_ok: float = 0.0
    #: Registered to recover from a 401; a second 401 soon after ends live use.
    recovering: bool = False


class LiveClient:
    """One add-in registration's live session, run on its own worker thread.

    :meth:`step` is the whole state machine and performs every network
    exchange; the worker thread calls it in a loop. Tests may call it directly.
    """

    def __init__(
        self,
        *,
        ipc_folder: Callable[[], Path | None],
        adapter_session_id: str,
        adapter_version: str,
        loaded_identity: Mapping[str, Any],
        lease_ok: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        nonce: Callable[[int], bytes] = secrets.token_bytes,
        transport_factory: Callable[[str], Any] = Transport,
        posix: bool | None = None,
        solve_files: Any = None,
    ) -> None:
        self._ipc_folder = ipc_folder
        self._adapter_session_id = str(adapter_session_id)
        self._adapter_version = str(adapter_version)
        self._loaded_identity = copy.deepcopy(dict(loaded_identity))
        self._lease_ok = lease_ok
        self._clock = clock
        self._wall = wall
        self._nonce = nonce
        self._transport_factory = transport_factory
        self._posix = posix
        #: ``wglink_watch``: writes a solve item's v3 file. Without it the
        #: outbox is still delivered, and a solve never falls back to a file.
        self._solve_files = solve_files

        self._lock = threading.Lock()
        self._session: _Session | None = None
        self._heartbeat: tuple[int, str] | None = None
        self._heartbeat_sent = 0
        self._heartbeat_offers = 0
        # Discovery state (worker only).
        self._blocked: tuple | None = None  # files fingerprint to wait past
        self._stale_fingerprint: tuple | None = None
        self._recheck_at = 0.0
        self._network_backoff = 0.0
        self._busy_retries = 0
        self._proof_reread = False
        self._recover_next = False
        self._refreshes = 0
        self._last_cause: str | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES_KEPT)
        # Outbox (worker only, except the two queues and the dirty flag).
        #: Discovery or the session ended on the files: a solve item then goes
        #: out as its v3 file. Not set while registering again after one 401.
        self._offline = False
        self._ipc: Path | None = None
        self._items: list[dict[str, Any]] | None = None
        self._scanned_at = float("-inf")
        self._outbox_dirty = True
        self._retries: dict[str, _Retry] = {}
        self._settled: set[str] = set()
        self._noticed: set[str] = set()
        self._notices: list[dict[str, Any]] = []
        self._acknowledged: queue.SimpleQueue[str] = queue.SimpleQueue()

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def __repr__(self) -> str:
        return f"<LiveClient mode={self.status()['mode']}>"

    # -- main-thread API (never blocks on the network) --------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="WGLinkLiveSend", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """End the session from the worker and wait for it to finish."""

        thread = self._thread
        self._stop.set()
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread is None or not thread.is_alive():
            self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def healthy(self) -> bool:
        with self._lock:
            session = self._session
            return session is not None and self._clock() - session.last_ok <= HEALTHY_WINDOW_SECONDS

    def offer_heartbeat(self, payload: Mapping[str, Any]) -> None:
        """Hand the worker the heartbeat just written to the file; latest wins."""

        text = json.dumps(payload)
        with self._lock:
            self._heartbeat_offers += 1
            self._heartbeat = (self._heartbeat_offers, text)
        self._wake.set()

    def enqueue_delivery(self) -> None:
        """The main thread added an outbox item: look at the outbox now."""

        self._outbox_dirty = True
        self._wake.set()

    def take_delivery_notices(self) -> list[dict[str, Any]]:
        """Final answers for the main thread to record and, where asked, show.

        A notice with ``durable`` true (rejected, conflict, expired) keeps its
        item on disk until :meth:`acknowledge_delivery_notice`, so it is shown
        again after a restart if it never was.
        """

        with self._lock:
            notices, self._notices = self._notices, []
        return notices

    def acknowledge_delivery_notice(self, operation_id: str) -> None:
        """The user was told; the worker deletes the answered item."""

        self._acknowledged.put(str(operation_id))
        self._wake.set()

    def take_log_lines(self) -> list[str]:
        with self._lock:
            lines = list(self._log)
            self._log.clear()
        return lines

    def status(self) -> dict[str, Any]:
        """Diagnostics without secrets."""

        with self._lock:
            session = self._session
            live = session is not None and self._clock() - session.last_ok <= HEALTHY_WINDOW_SECONDS
            return {
                "mode": "live" if live else "file",
                "instanceId": session.endpoint.instance_id if session else None,
                "liveSessionId": session.live_session_id if session else None,
                "refreshes": self._refreshes,
                "lastCause": self._last_cause,
                "outboxWaiting": sum(1 for item in self._items or () if item.get("answer") is None),
            }

    # -- worker ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    delay = self.step()
                except Exception:  # noqa: BLE001 - never let the worker die silently
                    self._drop()
                    self._note("unexpected", "WGLink live session hit an unexpected error; using files.")
                    delay = STALE_RECHECK_SECONDS
                if self._stop.is_set():
                    break
                self._wake.wait(max(0.0, min(delay, IDLE_STEP_SECONDS)))
        finally:
            self.end()

    def end(self) -> None:
        """End the session with WG (best effort) and forget it."""

        session = self._drop()
        if session is None:
            return
        try:
            session.transport.request(
                "DELETE", "/sessions/current", headers=self._auth(session), timeout=END_TIMEOUT_SECONDS
            )
        except NetworkFailure:
            pass

    def step(self) -> float:
        """Do the next piece of live work; returns seconds until another is due."""

        now = self._clock()
        if not self._lease_ok():
            if self._session is not None:
                self._note("lease", "WGLink no longer owns the IPC lease; live session ended.")
                self.end()
            return IDLE_STEP_SECONDS
        ipc = self._ipc_folder()
        if ipc is None:
            if self._session is not None:
                self.end()
            return IDLE_STEP_SECONDS
        self._ipc = ipc
        self._keep_outbox(ipc, now)
        session = self._session
        if session is not None:
            if files_fingerprint(ipc) != session.fingerprint:
                self._drop()
                self._note("restart", "WG's endpoint changed; registering again.")
                self._recheck_at = now
            else:
                return self._maintain(session, now)
        if self._offline:
            self._settle_offline(ipc)
        return self._discover(ipc, now)

    # -- discovery and registration ----------------------------------------------

    def _discover(self, ipc: Path, now: float) -> float:
        fingerprint = files_fingerprint(ipc)
        if self._blocked is not None:
            if fingerprint == self._blocked:
                return IDLE_STEP_SECONDS
            self._blocked = None
            self._proof_reread = False
            self._recheck_at = now
        if fingerprint != self._stale_fingerprint:
            self._recheck_at = now
            self._network_backoff = 0.0
        if now < self._recheck_at:
            return self._recheck_at - now
        endpoint = read_endpoint(ipc, posix=self._posix)
        if isinstance(endpoint, Stale):
            return self._stale(fingerprint, now, endpoint.reason, STALE_RECHECK_SECONDS)
        if self._halted():
            return IDLE_STEP_SECONDS
        try:
            transport = self._transport_factory(endpoint.base_url)
            hello = transport.request("GET", "/endpoint", timeout=self._timeout())
        except (NetworkFailure, ValueError):
            self._network_backoff = min(max(1.0, self._network_backoff * 2), STALE_RECHECK_SECONDS)
            return self._stale(fingerprint, now, "WG does not answer at its endpoint", self._network_backoff)
        if hello.status == 503 and hello.code == "store_busy":
            return self._busy(fingerprint, now, "WG's live service is starting or stopping")
        if hello.status != 200 or not _hello_matches(hello.body, endpoint):
            return self._stale(fingerprint, now, "WG's endpoint answer does not match its file", STALE_RECHECK_SECONDS)
        self._network_backoff = 0.0
        installation = installation_id(ipc)
        if installation is None:
            return self._stale(fingerprint, now, "no WGLink installation id could be stored", STALE_RECHECK_SECONDS)
        return self._register(ipc, endpoint, transport, installation, fingerprint, now)

    def _register(
        self, ipc: Path, endpoint: Endpoint, transport: Any, installation: str, fingerprint: tuple, now: float
    ) -> float:
        nonce = encode(self._nonce(32))
        body = {
            "cadApplication": CAD_APPLICATION,
            "liveProtocol": LIVE_PROTOCOL,
            "deliveryVersion": DELIVERY_VERSION,
            "installationId": installation,
            "adapterSessionId": self._adapter_session_id,
            "adapterVersion": self._adapter_version,
            "clientNonce": nonce,
            "clientProof": client_proof(endpoint.secret, nonce, endpoint.instance_id, installation),
            "loadedIdentity": copy.deepcopy(self._loaded_identity),
        }
        headers = {"Content-Type": "application/json", INSTALLATION_HEADER: installation}
        if self._halted():
            # Stopped or lost the lease while the hello was in flight: a
            # promoted standby may own the installation's session by now.
            return IDLE_STEP_SECONDS
        try:
            answer = transport.request("POST", "/sessions", headers=headers, body=body, timeout=self._timeout())
        except NetworkFailure:
            self._network_backoff = min(max(1.0, self._network_backoff * 2), STALE_RECHECK_SECONDS)
            return self._stale(fingerprint, now, "WG did not answer the registration", self._network_backoff)
        if answer.status == 503 and answer.code == "store_busy":
            return self._busy(fingerprint, now, "WG's live service is starting or stopping")
        if answer.status == 401 and answer.code == "registration_proof_invalid" and not self._proof_reread:
            # The file may have been replaced between reading and registering.
            self._proof_reread = True
            self._stale_fingerprint = None
            self._recheck_at = now
            self._note("proof", "WG refused the registration proof; reading its endpoint file again.")
            return 0.0
        if answer.status != 201:
            reason = f"WG refused the live registration ({answer.status} {answer.code or 'no code'})"
            if (
                (answer.status, answer.code) in _REGISTRATION_REFUSALS_UNTIL_RESTART
                or answer.status == 400
                or (answer.status == 401 and answer.code == "registration_proof_invalid")
            ):
                return self._block(fingerprint, reason)
            return self._stale(fingerprint, now, reason, STALE_RECHECK_SECONDS)
        reply = answer.body
        if not isinstance(reply, Mapping):
            return self._stale(fingerprint, now, "WG's registration answer is not an object", STALE_RECHECK_SECONDS)
        # The server proof is checked before anything else in the answer is used.
        if not server_proof_matches(endpoint.secret, nonce, reply.get("serverProof"), endpoint.instance_id, installation):
            return self._block(fingerprint, "WG's registration proof did not verify; staying on files")
        token = reply.get("sessionToken")
        live_session_id = reply.get("liveSessionId")
        protocol = reply.get("liveProtocol")
        if (
            not isinstance(token, str)
            or _TOKEN.fullmatch(token) is None
            or not isinstance(live_session_id, str)
            or not live_session_id
            or reply.get("instanceId") != endpoint.instance_id
            or not _is_int(protocol)
            or protocol != LIVE_PROTOCOL
        ):
            return self._stale(fingerprint, now, "WG's registration answer is not the documented one", STALE_RECHECK_SECONDS)
        session = _Session(
            endpoint=endpoint,
            transport=transport,
            installation_id=installation,
            live_session_id=live_session_id,
            token=token,
            fingerprint=fingerprint,
            refresh_at=now + self._refresh_delay(reply),
            expires_at=now + SESSION_LIFETIME_SECONDS,
            registered_at=now,
            last_ok=now,
            recovering=self._recover_next,
        )
        if self._halted():
            # Never keep a session registered after stop or lease loss.
            self.__end_quietly(session)
            return IDLE_STEP_SECONDS
        with self._lock:
            self._session = session
        self._offline = False
        self._outbox_dirty = True
        self._recover_next = False
        self._proof_reread = False
        self._busy_retries = 0
        self._stale_fingerprint = None
        self._note("live", "WGLink is live with WG.")
        return 0.0

    def _stale(self, fingerprint: tuple, now: float, reason: str, delay: float) -> float:
        self._offline = True
        self._stale_fingerprint = fingerprint
        self._recheck_at = now + delay
        self._busy_retries = 0
        self._note(reason, f"WGLink uses the files: {reason}.")
        return delay

    def _busy(self, fingerprint: tuple, now: float, reason: str) -> float:
        self._busy_retries += 1
        if self._busy_retries > STORE_BUSY_RETRIES:
            return self._stale(fingerprint, now, reason, STALE_RECHECK_SECONDS)
        self._stale_fingerprint = fingerprint
        self._recheck_at = now + STORE_BUSY_RETRY_SECONDS
        return STORE_BUSY_RETRY_SECONDS

    def _block(self, fingerprint: tuple, reason: str) -> float:
        """File mode until WG rewrites a discovery file."""

        self._offline = True
        self._blocked = fingerprint
        self._busy_retries = 0
        self._note(reason, f"WGLink uses the files: {reason}.")
        return IDLE_STEP_SECONDS

    # -- session maintenance -----------------------------------------------------

    def _halted(self) -> bool:
        """Stopping, or no longer the lease owner: start nothing new."""

        return self._stop.is_set() or not self._lease_ok()

    def _timeout(self) -> float:
        return END_TIMEOUT_SECONDS if self._stop.is_set() else REQUEST_TIMEOUT_SECONDS

    def _refresh_delay(self, reply: Mapping[str, Any]) -> float:
        """Seconds until the refresh: WG's ``refreshAfter``, at most 600 s."""

        delay = float(REFRESH_AFTER_SECONDS)
        value = reply.get("refreshAfter")
        if isinstance(value, str):
            try:
                deadline = datetime.strptime(value, _UTC_SECONDS).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                return delay
            delay = min(delay, max(MIN_REFRESH_SECONDS, deadline - self._wall()))
        return delay

    def _auth(self, session: _Session) -> dict[str, str]:
        return {"Authorization": f"Bearer {session.token}", INSTALLATION_HEADER: session.installation_id}

    def _maintain(self, session: _Session, now: float) -> float:
        if now >= session.expires_at:
            # Never present a token past its lifetime: register afresh.
            self._drop()
            self._note("expired", "The live session token lapsed before its refresh; registering again.")
            self._recheck_at = now
            return 0.0
        if self._halted():
            return IDLE_STEP_SECONDS
        if now >= session.refresh_at:
            outcome = self._refresh(session, now)
            if outcome is not None:
                return outcome
        with self._lock:
            pending = self._heartbeat if self._heartbeat and self._heartbeat[0] > self._heartbeat_sent else None
            if pending is not None:
                self._heartbeat_sent = pending[0]
        if pending is not None and not self._halted():
            outcome = self._post_heartbeat(session, pending[1], now)
            if outcome is not None:
                return outcome
        outcome = self._deliver_next(session, now)
        if outcome is not None:
            return outcome
        return max(0.0, min(session.refresh_at - now, IDLE_STEP_SECONDS))

    def _refresh(self, session: _Session, now: float) -> float | None:
        try:
            answer = session.transport.request(
                "POST", "/sessions/refresh", headers=self._auth(session), timeout=self._timeout()
            )
        except NetworkFailure:
            return self._lost(now, "WG did not answer the token refresh")
        if answer.status == 200 and isinstance(answer.body, Mapping):
            token = answer.body.get("sessionToken")
            if (
                isinstance(token, str)
                and _TOKEN.fullmatch(token) is not None
                and answer.body.get("liveSessionId") == session.live_session_id
            ):
                with self._lock:
                    session.token = token
                    session.refresh_at = now + self._refresh_delay(answer.body)
                    session.expires_at = now + SESSION_LIFETIME_SECONDS
                    session.last_ok = now
                    self._refreshes += 1
                return None
        if answer.status == 503 and answer.code == "store_busy" and now + STORE_BUSY_RETRY_SECONDS < session.expires_at:
            session.refresh_at = now + STORE_BUSY_RETRY_SECONDS
            return None
        return self._refused(session, answer, now, "token refresh")

    def _post_heartbeat(self, session: _Session, payload: str, now: float) -> float | None:
        headers = {"Content-Type": "application/json", **self._auth(session)}
        try:
            answer = session.transport.request(
                "POST", "/heartbeat", headers=headers, body=json.loads(payload), timeout=self._timeout()
            )
        except NetworkFailure:
            return self._lost(now, "WG did not answer the heartbeat")
        if answer.status == 204:
            with self._lock:
                session.last_ok = now
            return None
        if answer.status == 409 and answer.code == "heartbeat_stale":
            self._note("heartbeat_stale", "WG found a live heartbeat already stale; the next one follows.")
            return None
        if answer.status == 409 and answer.code == "session_mismatch":
            return self._session_lost(session, now, "session_mismatch")
        if answer.status == 503 and answer.code == "store_busy":
            return None
        return self._refused(session, answer, now, "heartbeat")

    def _session_lost(self, session: _Session, now: float, code: str) -> float:
        """Register again once; a second loss soon after uses the files for a while."""

        self._drop()
        if session.recovering and now - session.registered_at <= REREGISTER_WINDOW_SECONDS:
            self._offline = True
            self._stale_fingerprint = self._current_fingerprint()
            self._recheck_at = now + STALE_RECHECK_SECONDS
            self._note("lost-again", f"WG refused the live session again ({code}); using the files.")
            return STALE_RECHECK_SECONDS
        self._recover_next = True
        self._recheck_at = now
        self._note(f"lost-{code}", f"WG ended the live session ({code}); registering again.")
        return 0.0

    def _refused(self, session: _Session, answer: Answer, now: float, what: str) -> float:
        if answer.status == 401:
            return self._session_lost(session, now, answer.code or "401")
        self._drop()
        self.__end_quietly(session)
        self._offline = True
        self._stale_fingerprint = self._current_fingerprint()
        self._recheck_at = now + STALE_RECHECK_SECONDS
        reason = f"WG refused the live {what} ({answer.status} {answer.code or 'no code'})"
        self._note(reason, f"WGLink uses the files: {reason}.")
        return STALE_RECHECK_SECONDS

    def __end_quietly(self, session: _Session) -> None:
        try:
            session.transport.request(
                "DELETE", "/sessions/current", headers=self._auth(session), timeout=END_TIMEOUT_SECONDS
            )
        except NetworkFailure:
            pass

    def _lost(self, now: float, reason: str) -> float:
        self._drop()
        self._offline = True
        self._stale_fingerprint = self._current_fingerprint()
        self._network_backoff = 1.0
        self._recheck_at = now + self._network_backoff
        self._note("network", f"WGLink uses the files: {reason}.")
        return self._network_backoff

    # -- the outbox (worker) ---------------------------------------------------------

    def _keep_outbox(self, ipc: Path, now: float) -> None:
        """Acknowledgements, rescans, notices of answered items, the age bound."""

        outbox = Outbox(ipc)
        while True:
            try:
                operation_id = self._acknowledged.get_nowait()
            except queue.Empty:
                break
            stored = outbox.read(operation_id)
            if stored is not None and stored.get("answer") is not None:
                self._forget(outbox, operation_id)
            self._noticed.discard(operation_id)
        for operation_id in sorted(self._settled):
            if outbox.delete(operation_id):
                self._settled.discard(operation_id)
        if self._items is None or self._outbox_dirty or now - self._scanned_at >= OUTBOX_RESCAN_SECONDS:
            self._outbox_dirty = False
            self._scanned_at = now
            items, foreign = outbox.scan()
            # An item whose file this session has already settled is not work,
            # however often it is still readable.
            items = [item for item in items if item["operationId"] not in self._settled]
            with self._lock:
                self._items = items
            present = {item["operationId"] for item in items}
            for operation_id in set(self._retries) - present:
                del self._retries[operation_id]
            for path in foreign:
                try:
                    if self._wall() - path.stat().st_mtime > OUTBOX_MAX_AGE_SECONDS:
                        path.unlink()
                except OSError:
                    pass
        wall = self._wall()
        for item in list(self._items or ()):
            operation_id = item["operationId"]
            if item.get("answer") is not None:
                if operation_id not in self._noticed:
                    self._notice(item, item["answer"], durable=True)
                continue
            created = _parse_utc(item["createdAt"])
            if created is None or wall - created <= OUTBOX_MAX_AGE_SECONDS:
                continue
            if item["kind"] == KIND_SOLVE and item.get("fileWritten"):
                # Its v3 file carries it; nothing is lost by forgetting the item.
                self._forget(outbox, operation_id)
            else:
                self._answer(outbox, item, {"outcome": "expired", "state": None, "reason": None, "message": None})

    def _settle_offline(self, ipc: Path) -> None:
        """No session: a solve goes out as its v3 file; a taken file settles its item."""

        if self._solve_files is None or self._halted():
            return
        outbox = Outbox(ipc)
        for item in list(self._items or ()):
            if item.get("answer") is not None or item["kind"] != KIND_SOLVE:
                continue
            if not item.get("fileWritten"):
                self._write_file(outbox, item)
            elif not self._solve_files.solve_request_path(ipc, item["operationId"]).exists():
                # WG took the file: the file transport has it now.
                self._forget(outbox, item["operationId"])

    def _write_file(self, outbox: Outbox, item: dict[str, Any]) -> None:
        if self._solve_files is None or item["kind"] != KIND_SOLVE or item.get("fileWritten"):
            return
        ipc = self._ipc
        if ipc is None:
            return
        try:
            _write_solve_file(self._solve_files, ipc, item)
        except Exception as exc:  # noqa: BLE001 - WgOutdatedError, a vanished folder: retried later
            self._note(f"outbox-file-{type(exc).__name__}",
                       f"WGLink could not write a solve request as a file ({type(exc).__name__}); it stays queued.")
            return
        self._replace(outbox, {**item, "fileWritten": True})
        self._note("outbox-file", "WGLink wrote a queued solve request as a file for WG to take.")

    def _replace(self, outbox: Outbox, item: dict[str, Any]) -> None:
        outbox.write(item)
        with self._lock:
            self._items = [item if other["operationId"] == item["operationId"] else other for other in self._items or ()]

    def _forget(self, outbox: Outbox, operation_id: str) -> None:
        if outbox.delete(operation_id):
            self._settled.discard(operation_id)
        else:
            # The item is settled with WG but its file is held (Windows). Keep
            # the id: the next step deletes it, and until then the rescan must
            # not read it back as unanswered work and deliver it again.
            self._settled.add(operation_id)
        self._retries.pop(operation_id, None)
        with self._lock:
            self._items = [item for item in self._items or () if item["operationId"] != operation_id]

    def _answer(self, outbox: Outbox, item: dict[str, Any], answer: dict[str, Any]) -> None:
        """A final answer the user must see: kept on disk until acknowledged."""

        self._replace(outbox, {**item, "answer": answer})
        self._retries.pop(item["operationId"], None)
        self._notice(item, answer, durable=True)

    def _notice(self, item: Mapping[str, Any], answer: Mapping[str, Any], *, durable: bool) -> None:
        notice = {
            "operationId": item["operationId"],
            "kind": item["kind"],
            "createdAt": item["createdAt"],
            "outcome": answer.get("outcome"),
            "state": answer.get("state"),
            "reason": answer.get("reason"),
            "message": answer.get("message"),
            "durable": durable,
        }
        with self._lock:
            self._notices.append(notice)
            if durable:
                self._noticed.add(item["operationId"])

    def _retry(self, operation_id: str) -> _Retry:
        return self._retries.setdefault(operation_id, _Retry())

    def _deliver_next(self, session: _Session, now: float) -> float | None:
        """POST the oldest due item, at most one per step."""

        if self._halted() or self._ipc is None:
            return None
        for item in list(self._items or ()):
            if item.get("answer") is None and self._retry(item["operationId"]).next_at <= now:
                return self._deliver(session, item, now)
        return None

    def _deliver(self, session: _Session, item: dict[str, Any], now: float) -> float | None:
        headers = {"Content-Type": "application/json", **self._auth(session)}
        try:
            answer = session.transport.request(
                "POST", "/deliveries", headers=headers, body=delivery_body(item),
                timeout=END_TIMEOUT_SECONDS if self._stop.is_set() else DELIVERY_TIMEOUT_SECONDS,
            )
        except NetworkFailure:
            # Maybe accepted, maybe not: the same id is delivered again, and a
            # solve also goes out as its v3 file (_settle_offline).
            return self._lost(now, "WG did not answer a delivery")
        if answer.status in (401, 403):
            return self._refused(session, answer, now, "delivery")
        with self._lock:
            session.last_ok = now
        outbox = Outbox(self._ipc) if self._ipc is not None else None
        if outbox is None:
            return None
        retry = self._retry(item["operationId"])
        code = answer.code
        if answer.status == 200:
            self._accepted(outbox, item, answer.body)
            return None
        if (answer.status, code) == (409, "operation_conflict"):
            self._note(f"conflict-{item['operationId']}",
                       f"WG refused delivery {item['operationId']}: its id already names a different request.")
            self._answer(outbox, item, {
                "outcome": "conflict", "state": None, "reason": "operation_conflict", "message": None,
            })
            return None
        hint = _retry_after(answer)
        if (answer.status, code) == (503, "snapshot_not_readable"):
            instance = session.endpoint.instance_id
            if retry.transient is None or retry.transient[0] != instance:
                retry.transient = (instance, now)
            retry.busy, retry.refused_since, retry.slow = 0, None, 0.0
            if now - retry.transient[1] >= TRANSIENT_GUARD_SECONDS:
                self._note("transient-guard", "WG kept answering that a return is not readable past its 30 s bound.")
                self._write_file(outbox, item)
                self._slow(retry, now)
            else:
                retry.next_at = now + max(1.0, hint)
            return None
        if (answer.status, code) == (503, "store_busy"):
            retry.busy += 1
            retry.refused_since = None
            if retry.busy >= STORE_BUSY_DELIVERY_ANSWERS:
                self._note("delivery-store-busy", "WG's operation store stayed busy; the delivery is retried later.")
                self._write_file(outbox, item)
                self._slow(retry, now)
            else:
                retry.next_at = now + max(hint, min(2.0 ** (retry.busy - 1), 8.0))
            return None
        if (answer.status, code) in _RETRYABLE_REFUSALS:
            retry.busy = 0
            if retry.refused_since is None:
                retry.refused_since, retry.refusals = now, 0
            retry.refusals += 1
            if now - retry.refused_since >= RETRYABLE_REFUSAL_SECONDS:
                self._note(f"delivery-{code}", f"WG is not taking deliveries yet ({code}); retrying slowly.")
                self._slow(retry, now)
            else:
                retry.next_at = now + max(hint, min(2.0 ** (retry.refusals - 1), FAST_RETRY_MAX_SECONDS))
            return None
        self._note(
            f"delivery-{answer.status}-{code}",
            f"WG answered a delivery unexpectedly ({answer.status} {code or 'no code'}); retrying later.",
        )
        retry.busy, retry.refused_since = 0, None
        self._write_file(outbox, item)
        self._slow(retry, now)
        return None

    def _accepted(self, outbox: Outbox, item: dict[str, Any], body: object) -> None:
        """A 200: final for the item whatever the operation's state."""

        operation = body.get("operation") if isinstance(body, Mapping) else None
        if not isinstance(operation, Mapping) or body.get("result") not in {"created", "recovered"}:
            self._note("delivery-body", "WG accepted a delivery with an answer WGLink does not recognise.")
            operation = {}
        state = operation.get("state") if isinstance(operation.get("state"), str) else None
        answer = {
            "outcome": "rejected" if state == "rejected" else "delivered",
            "state": state,
            "reason": operation.get("reason") if isinstance(operation.get("reason"), str) else None,
            "message": operation.get("message") if isinstance(operation.get("message"), str) else None,
        }
        if answer["outcome"] == "rejected":
            self._answer(outbox, item, answer)
            return
        self._forget(outbox, item["operationId"])
        self._notice(item, answer, durable=False)

    @staticmethod
    def _slow(retry: _Retry, now: float) -> None:
        retry.slow = SLOW_RETRY_START_SECONDS if retry.slow <= 0 else min(retry.slow * 2, SLOW_RETRY_MAX_SECONDS)
        retry.next_at = now + retry.slow

    def _current_fingerprint(self) -> tuple | None:
        ipc = self._ipc_folder()
        return files_fingerprint(ipc) if ipc is not None else None

    def _drop(self) -> _Session | None:
        with self._lock:
            session, self._session = self._session, None
        return session

    def _note(self, cause: str, line: str) -> None:
        """Queue a line for the main thread when the cause differs from the last one.

        A state that persists (the same stale reason every 30 s) is logged once;
        each change of state -- including back and forth -- is logged again.
        """

        with self._lock:
            if cause == self._last_cause:
                return
            self._last_cause = cause
            self._log.append(line)


__all__ = [
    "Answer",
    "Endpoint",
    "KIND_SNAPSHOT",
    "KIND_SOLVE",
    "LiveClient",
    "NetworkFailure",
    "Outbox",
    "OutboxFull",
    "Stale",
    "Transport",
    "UNPARSEABLE",
    "advertises_live",
    "client_proof",
    "decode_32",
    "delivery_body",
    "delivery_item",
    "encode",
    "files_fingerprint",
    "installation_id",
    "item_waiting_live",
    "loaded_identity",
    "produce_snapshot",
    "produce_solve",
    "read_endpoint",
    "server_proof",
    "server_proof_matches",
]
