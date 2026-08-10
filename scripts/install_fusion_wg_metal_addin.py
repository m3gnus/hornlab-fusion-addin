#!/usr/bin/env python3
"""Install HornLab Fusion add-ins into Fusion's AddIns folder."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


REPO_ROOT = Path(__file__).resolve().parents[1]
ADDIN_NAMES = ("WGMetalPipeline", "WGLink")
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
    parser.add_argument(
        "--addin",
        action="append",
        choices=(*ADDIN_NAMES, "all"),
        default=[],
        help=(
            "Add-in to install; may be repeated. Defaults to WGMetalPipeline for "
            "backwards compatibility. Use '--addin all' for both add-ins."
        ),
    )
    parser.add_argument("--symlink", action="store_true", help="Symlink instead of copying")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing selected add-in install",
    )
    return parser.parse_args()


def _default_addins_dirs() -> list[Path]:
    if DEFAULT_LEGACY_ADDINS_DIR.exists():
        return [DEFAULT_LEGACY_ADDINS_DIR]
    if DEFAULT_ADDINS_DIR.exists():
        return [DEFAULT_ADDINS_DIR]
    return [DEFAULT_ADDINS_DIR]


def _install_one(
    source_dir: Path,
    addins_dir: Path,
    *,
    replace: bool,
    symlink: bool,
) -> tuple[str, Path]:
    addins_dir = addins_dir.expanduser().resolve()
    target = addins_dir / source_dir.name
    addins_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if not replace:
            raise SystemExit(f"{target} already exists; rerun with --replace")
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    if symlink:
        target.symlink_to(source_dir, target_is_directory=True)
        return "symlinked", target
    shutil.copytree(source_dir, target, ignore=shutil.ignore_patterns("__pycache__"))
    return "copied", target


def main() -> int:
    args = parse_args()
    addins_dirs = args.addins_dir or _default_addins_dirs()
    requested = args.addin or ["WGMetalPipeline"]
    addin_names = ADDIN_NAMES if "all" in requested else tuple(dict.fromkeys(requested))
    for addins_dir in addins_dirs:
        for addin_name in addin_names:
            source_dir = REPO_ROOT / "fusion-addins" / addin_name
            mode, target = _install_one(
                source_dir,
                addins_dir,
                replace=args.replace,
                symlink=args.symlink,
            )
            print(f"{mode} {source_dir} -> {target}")
    joined = ", ".join(addin_names)
    print(f"Restart Fusion, then open Utilities > Add-Ins > Add-Ins > {joined}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
