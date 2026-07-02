#!/usr/bin/env python3
"""Install the HornLab WG Metal Fusion add-in into Fusion's AddIns folder."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "fusion-addins" / "WGMetalPipeline"
DEFAULT_LEGACY_ADDINS_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Autodesk"
    / "Autodesk Fusion 360"
    / "API"
    / "AddIns"
)
DEFAULT_ADDINS_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Autodesk"
    / "Autodesk Fusion"
    / "API"
    / "AddIns"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--addins-dir",
        action="append",
        type=Path,
        default=[],
        help=(
            "Fusion AddIns folder. May be repeated for explicit multi-install. "
            "By default, uses the legacy macOS path and falls back to the current path."
        ),
    )
    parser.add_argument("--symlink", action="store_true", help="Symlink instead of copying")
    parser.add_argument("--replace", action="store_true", help="Replace an existing WGMetalPipeline install")
    return parser.parse_args()


def _default_addins_dirs() -> list[Path]:
    if DEFAULT_LEGACY_ADDINS_DIR.exists():
        return [DEFAULT_LEGACY_ADDINS_DIR]
    if DEFAULT_ADDINS_DIR.exists():
        return [DEFAULT_ADDINS_DIR]
    return [DEFAULT_ADDINS_DIR]


def _install_one(addins_dir: Path, *, replace: bool, symlink: bool) -> tuple[str, Path]:
    addins_dir = addins_dir.expanduser().resolve()
    target = addins_dir / "WGMetalPipeline"
    addins_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if not replace:
            raise SystemExit(f"{target} already exists; rerun with --replace")
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    if symlink:
        target.symlink_to(SOURCE_DIR, target_is_directory=True)
        return "symlinked", target
    shutil.copytree(SOURCE_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    return "copied", target


def main() -> int:
    args = parse_args()
    addins_dirs = args.addins_dir or _default_addins_dirs()
    for addins_dir in addins_dirs:
        mode, target = _install_one(addins_dir, replace=args.replace, symlink=args.symlink)
        print(f"{mode} {SOURCE_DIR} -> {target}")
    print("Restart Fusion, then open Utilities > Add-Ins > Add-Ins > WGMetalPipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
