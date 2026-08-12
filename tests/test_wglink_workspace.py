"""WGLink reads WG's workspace rather than asking for it a second time."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_workspace as ws  # noqa: E402


def _wg_data_dir(tmp_path: Path, workspace: Path | None) -> Path:
    data = tmp_path / "WaveguideGenerator"
    data.mkdir(parents=True, exist_ok=True)
    if workspace is not None:
        (data / ws.SETTINGS_NAME).write_text(
            json.dumps({"schemaVersion": 1, "workspacePath": str(workspace)}),
            encoding="utf-8",
        )
    return data


def _bundle(folder: Path, name: str, sequence: int, design: str | None = None) -> Path:
    bundle = folder / f"{name}.wglink"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "wglink.json").write_text(
        json.dumps({
            "design": {"name": design if design is not None else name},
            "export": {"id": f"wge_{sequence}", "sequence": sequence},
        }),
        encoding="utf-8",
    )
    return bundle


def test_the_data_dir_follows_the_platform(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac = ws.data_dir(system="Darwin", environ={}, home=home)
    assert mac == home / "Library" / "Application Support" / "WaveguideGenerator"

    windows = ws.data_dir(system="Windows", environ={"APPDATA": str(tmp_path / "roam")}, home=home)
    assert windows == tmp_path / "roam" / "WaveguideGenerator"

    linux = ws.data_dir(system="Linux", environ={}, home=home)
    assert linux == home / ".local" / "share" / "WaveguideGenerator"

    xdg = ws.data_dir(system="Linux", environ={"XDG_DATA_HOME": str(tmp_path / "xdg")}, home=home)
    assert xdg == tmp_path / "xdg" / "WaveguideGenerator"


def test_the_data_dir_override_wins(tmp_path: Path) -> None:
    """WG honours WG2_DATA_DIR, so a machine using it must not be split in two."""

    override = tmp_path / "elsewhere"
    assert ws.data_dir(
        system="Darwin", environ={ws.DATA_DIR_ENV: str(override)}, home=tmp_path
    ) == override


def test_windows_without_appdata_is_unknown_not_a_crash(tmp_path: Path) -> None:
    assert ws.data_dir(system="Windows", environ={}, home=tmp_path) is None


def test_the_selected_workspace_is_read_from_wg(tmp_path: Path) -> None:
    workspace = tmp_path / "Synergy Horns" / "fusion project"
    workspace.mkdir(parents=True)
    data = _wg_data_dir(tmp_path, workspace)
    assert ws.workspace_root(environ={ws.DATA_DIR_ENV: str(data)}) == workspace.resolve()
    assert ws.return_folder(environ={ws.DATA_DIR_ENV: str(data)}) == workspace.resolve() / "wgreturn"
    assert ws.ipc_folder(create=True, environ={ws.DATA_DIR_ENV: str(data)}) == data / "ipc" / "wglink"


def test_wg_default_workspace_is_used_when_none_was_selected(tmp_path: Path) -> None:
    data = _wg_data_dir(tmp_path, None)
    (data / "workspace").mkdir()
    assert ws.workspace_root(environ={ws.DATA_DIR_ENV: str(data)}) == (data / "workspace").resolve()


def test_a_workspace_that_has_gone_away_reads_as_unknown(tmp_path: Path) -> None:
    data = _wg_data_dir(tmp_path, tmp_path / "on-a-disconnected-drive")
    assert ws.workspace_root(environ={ws.DATA_DIR_ENV: str(data)}) is None


@pytest.mark.parametrize("payload", ["not json", "[]", '{"workspacePath": ""}', '{}'])
def test_an_unusable_settings_file_reads_as_unknown(tmp_path: Path, payload: str) -> None:
    data = tmp_path / "WaveguideGenerator"
    data.mkdir()
    (data / ws.SETTINGS_NAME).write_text(payload, encoding="utf-8")
    assert ws.workspace_root(environ={ws.DATA_DIR_ENV: str(data)}) is None


def test_bundles_are_discovered_newest_first(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    folder = workspace / ws.BUNDLE_SUBDIRECTORY
    folder.mkdir(parents=True)
    older = _bundle(folder, "old-horn", 2)
    newer = _bundle(folder, "new-horn", 7, design="hans-rosse")
    os.utime(older / "wglink.json", (1_000, 1_000))
    os.utime(newer / "wglink.json", (9_000, 9_000))
    data = _wg_data_dir(tmp_path, workspace)

    found = ws.discover_bundles(environ={ws.DATA_DIR_ENV: str(data)})
    assert [item.path for item in found] == [newer, older]
    assert found[0].label() == "hans-rosse · sequence 7"


def test_discovery_skips_what_is_not_a_readable_bundle(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    folder = workspace / ws.BUNDLE_SUBDIRECTORY
    folder.mkdir(parents=True)
    _bundle(folder, "good", 1)
    (folder / "notes.txt").write_text("ignore me", encoding="utf-8")
    (folder / "empty.wglink").mkdir()
    broken = folder / "broken.wglink"
    broken.mkdir()
    (broken / "wglink.json").write_text("{{{", encoding="utf-8")
    (folder / "link.wglink").symlink_to(folder / "good.wglink")
    data = _wg_data_dir(tmp_path, workspace)

    found = ws.discover_bundles(environ={ws.DATA_DIR_ENV: str(data)})
    assert [item.path.name for item in found] == ["good.wglink"]


def test_no_workspace_means_no_bundles_not_an_error(tmp_path: Path) -> None:
    data = tmp_path / "WaveguideGenerator"
    data.mkdir()
    assert ws.discover_bundles(environ={ws.DATA_DIR_ENV: str(data)}) == []
    assert ws.bundle_folder(environ={ws.DATA_DIR_ENV: str(data)}) is None


def test_a_bundle_without_a_design_name_still_offers_a_label(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    folder = workspace / ws.BUNDLE_SUBDIRECTORY
    folder.mkdir(parents=True)
    bundle = folder / "unnamed.wglink"
    bundle.mkdir()
    (bundle / "wglink.json").write_text(json.dumps({"export": {"id": "wge_1"}}), encoding="utf-8")
    data = _wg_data_dir(tmp_path, workspace)

    found = ws.discover_bundles(environ={ws.DATA_DIR_ENV: str(data)})
    assert found[0].label() == "unnamed"
