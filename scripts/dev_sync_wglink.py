#!/usr/bin/env python3
"""Push this checkout's WGLink into Fusion's installed add-in, in place.

WGLink ships as a copy that Waveguide Generator installs from a pinned commit,
so proving a one-line change means push, pin, package, install, restart -- a
round trip through GitHub for an edit that takes a second to make. This script
is the short way round for development: it overwrites the *contents* of the
add-in Fusion already has registered, leaving the managed install's own markers
alone, so Fusion's registry still names exactly one WGLink at exactly one path.
Registering the repository as a second add-in is the thing to avoid; two
registrations of one add-in are two Python modules with separate globals, and
the second deletes the first's panel.

    python scripts/dev_sync_wglink.py            # sync, then restart the add-in
    python scripts/dev_sync_wglink.py --status   # what is installed, and its last tick
    python scripts/dev_sync_wglink.py --watch    # re-sync whenever a source file changes

After a sync, restart the add-in in Fusion (Utilities -> Add-Ins -> WGLink ->
Stop, then Run) and read the heartbeat back with ``--status``: it prints the
``sourceCommit`` the running add-in reports, which is what proves the restart
took, and the wall clock of its last watch tick.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "fusion-addins" / "WGLink"
DEV_MARKER = "wglink_dev.json"
# Written by WG's managed installer and by Update's resampler handoff. A dev
# sync replaces code, never the installation's identity.
PRESERVED = ("wglink_install.json", "wglink_runtime.json")
ADDIN_DIRS = (
    Path.home() / "Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns",
    Path.home() / "Library/Application Support/Autodesk/Autodesk Fusion/API/AddIns",
    Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser()
    / "Autodesk/Autodesk Fusion 360/API/AddIns",
)
STATUS_FILE = (
    Path.home()
    / "Library/Application Support/WaveguideGenerator/ipc/wglink/.fusion-status.json"
)


def installed_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    for parent in ADDIN_DIRS:
        candidate = parent.expanduser() / "WGLink"
        if (candidate / "WGLink.py").is_file():
            return candidate.resolve()
    raise SystemExit(
        "No installed WGLink found. Install it once from Waveguide Generator "
        "(Help -> Install Fusion add-in), then run this again."
    )


def source_files() -> list[Path]:
    """Every file the add-in loads, repo-relative to the add-in folder."""

    return sorted(
        path
        for path in SOURCE_DIR.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not path.name.startswith(".")
    )


def tree_hash(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(SOURCE_DIR)).encode())
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def head_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    dirty = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", str(SOURCE_DIR)],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return result.stdout.strip() + ("+dirty" if dirty else "")


def sync(target: Path, *, quiet: bool = False) -> int:
    files = source_files()
    changed = 0
    for path in files:
        relative = path.relative_to(SOURCE_DIR)
        destination = target / relative
        if destination.exists() and destination.read_bytes() == path.read_bytes():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        changed += 1
        if not quiet:
            print(f"  updated {relative}")
    # A file deleted from the checkout has to leave the install too, or the
    # add-in keeps importing a module the source no longer has.
    keep = {str(path.relative_to(SOURCE_DIR)) for path in files}
    keep.update(PRESERVED)
    keep.add(DEV_MARKER)
    for path in sorted(target.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(target))
        if relative not in keep:
            path.unlink()
            changed += 1
            if not quiet:
                print(f"  removed {relative}")
    (target / DEV_MARKER).write_text(
        json.dumps(
            {
                "sourceRoot": str(SOURCE_DIR),
                "sourceCommit": head_commit(),
                "treeHash": tree_hash(files),
                "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return changed


def status(target: Path) -> None:
    files = source_files()
    print(f"installed : {target}")
    marker = target / DEV_MARKER
    if marker.is_file():
        payload = json.loads(marker.read_text(encoding="utf-8"))
        matches = payload.get("treeHash") == tree_hash(files)
        print(f"dev sync  : {payload.get('sourceCommit')} at {payload.get('syncedAt')}")
        print(f"            {'matches this checkout' if matches else 'STALE — run a sync'}")
    else:
        managed = target / "wglink_install.json"
        if managed.is_file():
            payload = json.loads(managed.read_text(encoding="utf-8"))
            print(f"dev sync  : none (WG-managed copy of {payload.get('sourceCommit')})")
        else:
            print("dev sync  : none")
    if not STATUS_FILE.is_file():
        print("heartbeat : no status file — the add-in has not run")
        return
    payload = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    age = time.time() - STATUS_FILE.stat().st_mtime
    print(f"heartbeat : {payload.get('updatedAt')} ({age:.0f}s ago)")
    document = payload.get("document") or {}
    print(f"document  : {document.get('name')} — {len(document.get('links') or [])} link(s)")
    diagnostics = payload.get("diagnostics") or {}
    running = (diagnostics.get("source") or {}).get("sourceCommit")
    if running:
        print(f"running   : {running} (from the add-in itself)")
    else:
        print("running   : unreported — restart the add-in to pick up a synced build")
    tick = diagnostics.get("lastTickMs")
    if tick:
        parts = ", ".join(f"{name}={value}" for name, value in sorted(tick.items()))
        print(f"last tick : {parts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addin-dir", type=Path, help="installed WGLink folder")
    parser.add_argument("--status", action="store_true", help="report and exit")
    parser.add_argument("--watch", action="store_true", help="re-sync on every change")
    args = parser.parse_args()

    target = installed_dir(args.addin_dir)
    if args.status:
        status(target)
        return 0
    if args.watch:
        print(f"watching {SOURCE_DIR} -> {target}; Ctrl-C to stop")
        seen = ""
        while True:
            current = tree_hash(source_files())
            if current != seen:
                seen = current
                changed = sync(target)
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] synced {changed} file(s) — restart the add-in in Fusion")
            time.sleep(1.0)
    changed = sync(target)
    print(f"synced {changed} file(s) into {target}")
    print("Restart WGLink in Fusion (Utilities -> Add-Ins -> WGLink -> Stop, then Run),")
    print("then: python scripts/dev_sync_wglink.py --status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
