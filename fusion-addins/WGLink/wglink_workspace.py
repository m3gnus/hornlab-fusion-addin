"""Find the workspace folder the user already chose in Waveguide Generator.

The workspace is WG's setting. Asking for it a second time in Fusion made every
first insert a two-place setup, and left the two free to disagree -- pick the
wrong folder here and WGLink inserts a bundle WG is no longer writing to.

So this reads WG's own ``workspace_settings.json`` rather than storing a second
copy. That is a deliberate coupling to another repository's file, kept honest by
three rules: the file is versioned (``schemaVersion``), nothing is ever written
back to it, and every failure degrades to ``None`` so the manual picker still
works for bundles from another machine.

Path resolution mirrors ``server/platform/paths.resolve_data_dir``, including
the ``WG2_DATA_DIR`` override; keep the two in step.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
from typing import Any, Mapping


DATA_DIR_ENV = "WG2_DATA_DIR"
APP_DIRECTORY = "WaveguideGenerator"
SETTINGS_NAME = "workspace_settings.json"
BUNDLE_SUBDIRECTORY = "wglink"
BUNDLE_SUFFIX = ".wglink"
RETURN_SUBDIRECTORY = "wgreturn"
IPC_SUBDIRECTORY = Path("ipc") / "wglink"


@dataclass(frozen=True)
class DiscoveredBundle:
    """A bundle sitting in WG's workspace, described well enough to choose from."""

    path: Path
    design_name: str
    export_sequence: str
    modified_at: float

    def label(self) -> str:
        sequence = f" · sequence {self.export_sequence}" if self.export_sequence else ""
        return f"{self.design_name or self.path.stem}{sequence}"


def data_dir(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path | None:
    env = os.environ if environ is None else environ
    configured = env.get(DATA_DIR_ENV)
    if configured:
        return Path(configured).expanduser().absolute()

    os_name = platform.system() if system is None else system
    home_dir = Path.home() if home is None else Path(home)
    if os_name == "Darwin":
        root = home_dir / "Library" / "Application Support"
    elif os_name == "Windows":
        appdata = env.get("APPDATA")
        if not appdata:
            return None
        root = Path(appdata)
    else:
        xdg = env.get("XDG_DATA_HOME")
        root = Path(xdg) if xdg else home_dir / ".local" / "share"
    return (root / APP_DIRECTORY).expanduser().absolute()


def workspace_root(**kwargs: Any) -> Path | None:
    """The folder WG is writing bundles into, or None if it cannot be read.

    Falls back to WG's default workspace when no folder has been selected, which
    is where WG itself writes until the user picks one.
    """

    root = data_dir(**kwargs)
    if root is None:
        return None
    try:
        payload = json.loads((root / SETTINGS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = None
    if isinstance(payload, Mapping):
        raw = str(payload.get("workspacePath") or "").strip()
        if raw:
            selected = Path(raw).expanduser()
            if selected.is_dir():
                return selected.resolve()
    default = root / "workspace"
    return default.resolve() if default.is_dir() else None


def bundle_folder(**kwargs: Any) -> Path | None:
    root = workspace_root(**kwargs)
    if root is None:
        return None
    folder = root / BUNDLE_SUBDIRECTORY
    return folder if folder.is_dir() else None


def return_folder(**kwargs: Any) -> Path | None:
    """The folder WG reads CAD-authored returns from.

    Unlike ``bundle_folder``, the folder does not have to exist yet: the Send
    command creates it atomically with the first return.  Resolving this from
    WG's selected workspace keeps the two applications on the same transport
    path even when an old Fusion preference points somewhere else.
    """

    root = workspace_root(**kwargs)
    return root / RETURN_SUBDIRECTORY if root is not None else None


def ipc_folder(*, create: bool = False, **kwargs: Any) -> Path | None:
    """Machine-local command/status transport; never place it in Dropbox."""

    root = data_dir(**kwargs)
    if root is None:
        return None
    folder = root / IPC_SUBDIRECTORY
    if create:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
    return folder if folder.is_dir() else None


def _describe(bundle: Path) -> DiscoveredBundle | None:
    manifest_path = bundle / "wglink.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        modified_at = manifest_path.stat().st_mtime
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(manifest, Mapping):
        return None
    design = manifest.get("design")
    export = manifest.get("export")
    name = ""
    if isinstance(design, Mapping):
        name = str(design.get("name") or "")
    sequence = ""
    if isinstance(export, Mapping) and export.get("sequence") is not None:
        sequence = str(export.get("sequence"))
    return DiscoveredBundle(
        path=bundle,
        design_name=name or bundle.stem,
        export_sequence=sequence,
        modified_at=modified_at,
    )


def discover_bundles(**kwargs: Any) -> list[DiscoveredBundle]:
    """Readable bundles in WG's workspace, newest first."""

    folder = bundle_folder(**kwargs)
    if folder is None:
        return []
    try:
        candidates = sorted(folder.iterdir())
    except OSError:
        return []
    found = []
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if candidate.suffix != BUNDLE_SUFFIX:
            continue
        described = _describe(candidate)
        if described is not None:
            found.append(described)
    return sorted(found, key=lambda item: item.modified_at, reverse=True)
