"""Two Fusion processes on one WG data folder: the file path (A8 brief 4).

Two real Python processes, each importing the add-in's own writer modules, each
publish returns into one return folder and write a request for each into one
request inbox, at the same time. What must hold:

* every request id is unique, and every request file is whole and names the
  return its writer published;
* no return bundle is overwritten: each name is reserved exclusively
  (``wglink_send._reserve_target``), across processes;
* nothing is left behind -- no staging file, reservation or temporary bundle.

Known limit, documented rather than fixed here: ``.fusion-status.json`` is one
file, last writer wins. Two Fusion processes on one folder overwrite each
other's heartbeat. The cross-process live lease waits for the step-6 decision.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGLink"


# The add-in's modules, as a second Fusion process would load them: its own
# interpreter, no shared import state. adsk is stubbed; nothing here calls it.
_WORKER = textwrap.dedent(
    """
    import json, sys, tempfile, time, types, uuid
    from pathlib import Path

    addin, output, ipc, workspace, label, count, go = sys.argv[1:8]
    for name in ("adsk", "adsk.core", "adsk.fusion"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["adsk"].__path__ = []
    sys.modules["adsk"].core = sys.modules["adsk.core"]
    sys.modules["adsk"].fusion = sys.modules["adsk.fusion"]
    sys.path.insert(0, addin)
    import wglink_send, wglink_watch

    output, ipc, workspace = Path(output), Path(ipc), Path(workspace)
    deadline = time.monotonic() + 60
    while not Path(go).exists():
        if time.monotonic() > deadline:
            sys.exit("no start signal")
        time.sleep(0.001)

    records = []
    for index in range(int(count)):
        # The order send() publishes in: reserve, stage, publish, release.
        target, reservation = wglink_send._reserve_target(output / "speaker.wgreturn", overwrite=False)
        try:
            temp = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=output))
            (temp / "wgreturn.json").write_text(json.dumps({"writer": label, "index": index}))
            wglink_send._publish(temp, target, False)
        finally:
            reservation.unlink(missing_ok=True)
        relative, manifest = wglink_watch.return_reference(target, workspace)
        kind = wglink_watch.WG_REQUEST_KINDS[index % 2]
        command_id = str(uuid.uuid4())
        wglink_watch.write_wg_request(
            ipc,
            kind=kind,
            command_id=command_id,
            return_id="wgr_" + label,
            bundle_relative=relative,
            manifest_sha256=manifest,
            requested_at=wglink_watch.utc_timestamp(),
        )
        records.append({
            "bundle": target.name, "id": command_id, "kind": kind,
            "manifest": manifest, "relative": relative, "index": index,
        })
    print(json.dumps(records))
    """
)


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    output = workspace / "wgreturn"
    ipc = tmp_path / "ipc"
    output.mkdir(parents=True)
    ipc.mkdir()
    (ipc / "wg-capabilities.json").write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 4, "fusionRequestDelivery": 3,
    }))
    return workspace, output, ipc


def test_two_processes_publishing_and_requesting_at_once_never_collide(tmp_path: Path) -> None:
    workspace, output, ipc = _setup(tmp_path)
    script = tmp_path / "worker.py"
    script.write_text(_WORKER)
    go = tmp_path / "go"
    count = 25
    workers = {
        label: subprocess.Popen(
            [sys.executable, str(script), str(ADDIN), str(output), str(ipc), str(workspace), label, str(count), str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for label in ("fusion-a", "fusion-b")
    }
    time.sleep(0.5)  # both interpreters have imported and are spinning on the signal
    go.write_text("go")
    records: dict[str, list[dict]] = {}
    for label, process in workers.items():
        stdout, stderr = process.communicate(timeout=180)
        assert process.returncode == 0, stderr
        records[label] = json.loads(stdout)

    everything = [(label, record) for label, items in records.items() for record in items]
    assert len(everything) == 2 * count

    # Unique ids, and one whole request file for each, naming its writer's return.
    ids = [record["id"] for _label, record in everything]
    assert len(set(ids)) == len(ids)
    inbox = ipc / ".wg-solve-requests"
    assert sorted(path.name for path in inbox.iterdir()) == sorted(f"{i}.json" for i in ids)
    for label, record in everything:
        payload = json.loads((inbox / f"{record['id']}.json").read_text())
        assert payload["schemaVersion"] == 4
        assert payload["kind"] == record["kind"]
        assert payload["operationId"] == payload["commandId"] == record["id"]
        assert payload["bundlePath"] == record["relative"]
        assert payload["manifestSha256"] == record["manifest"]
        assert ("returnId" in payload) is (record["kind"] == "prepare_and_solve")

    # Every bundle name was used once, and still holds what its reserver wrote:
    # nobody published over anybody.
    bundles = [record["bundle"] for _label, record in everything]
    assert len(set(bundles)) == len(bundles)
    for label, record in everything:
        manifest = json.loads((output / record["bundle"] / "wgreturn.json").read_text())
        assert manifest == {"writer": label, "index": record["index"]}
    assert sorted(path.name for path in output.iterdir()) == sorted(bundles)


# -- the reservation itself, deterministically ---------------------------------------


@pytest.fixture
def send_module(monkeypatch):
    for name in ("adsk", "adsk.core", "adsk.fusion"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["adsk"].__path__ = []
    sys.modules["adsk"].core = sys.modules["adsk.core"]
    sys.modules["adsk"].fusion = sys.modules["adsk.fusion"]
    monkeypatch.syspath_prepend(str(ADDIN))
    for helper in ("wglink_core", "wglink_send"):
        spec = importlib.util.spec_from_file_location(helper, ADDIN / f"{helper}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, helper, module)
        spec.loader.exec_module(module)
    return sys.modules["wglink_send"]


_RESERVE_ONCE = textwrap.dedent(
    """
    import json, sys, types
    from pathlib import Path
    for name in ("adsk", "adsk.core", "adsk.fusion"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["adsk"].__path__ = []
    sys.modules["adsk"].core = sys.modules["adsk.core"]
    sys.modules["adsk"].fusion = sys.modules["adsk.fusion"]
    sys.path.insert(0, sys.argv[1])
    import wglink_core, wglink_send
    try:
        target, reservation = wglink_send._reserve_target(Path(sys.argv[2]), overwrite=sys.argv[3] == "overwrite")
    except wglink_core.WgLinkError as exc:
        print(json.dumps({"refused": str(exc)}))
    else:
        print(json.dumps({"target": target.name, "reservation": reservation.name}))
    """
)


def _reserve_in_another_process(tmp_path: Path, target: Path, mode: str = "new") -> dict:
    script = tmp_path / "reserve.py"
    script.write_text(_RESERVE_ONCE)
    completed = subprocess.run(
        [sys.executable, str(script), str(ADDIN), str(target), mode],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_a_name_reserved_in_one_process_is_not_given_to_another(send_module, tmp_path: Path) -> None:
    target = tmp_path / "speaker.wgreturn"
    mine, reservation = send_module._reserve_target(target, overwrite=False)
    assert mine == target and reservation.exists()

    theirs = _reserve_in_another_process(tmp_path, target)

    assert theirs == {"target": "speaker-2.wgreturn", "reservation": ".speaker-2.wgreturn.reserve"}
    assert reservation.exists()


def test_an_overwrite_of_a_name_another_process_is_publishing_is_refused(
    send_module, tmp_path: Path
) -> None:
    target = tmp_path / "speaker.wgreturn"
    send_module._reserve_target(target, overwrite=False)

    theirs = _reserve_in_another_process(tmp_path, target, "overwrite")

    assert "Another WGLink export is already publishing speaker.wgreturn" in theirs["refused"]


def test_control_a_released_name_is_given_to_the_next_process(send_module, tmp_path: Path) -> None:
    """The same measurement, with the reservation gone: the name is free again."""

    target = tmp_path / "speaker.wgreturn"
    _mine, reservation = send_module._reserve_target(target, overwrite=False)
    reservation.unlink()

    theirs = _reserve_in_another_process(tmp_path, target)

    assert theirs == {"target": "speaker.wgreturn", "reservation": ".speaker.wgreturn.reserve"}


def test_a_published_name_is_skipped_and_its_reservation_released(send_module, tmp_path: Path) -> None:
    target = tmp_path / "speaker.wgreturn"
    target.mkdir()  # another process published it and released its reservation

    theirs = _reserve_in_another_process(tmp_path, target)

    assert theirs["target"] == "speaker-2.wgreturn"
    assert not (tmp_path / ".speaker.wgreturn.reserve").exists()
