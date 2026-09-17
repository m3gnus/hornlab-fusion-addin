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
