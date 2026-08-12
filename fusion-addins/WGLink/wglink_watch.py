"""Notice that WG has exported a newer bundle for a link in this document.

Deliberately free of ``adsk``: every Fusion API call has to happen on Fusion's
main thread, so the background thread that drives this may only touch the
filesystem. WGLink.py owns the thread, the custom event, and the prompt; this
module owns the question "is there anything new?" and is therefore testable
without Fusion.

Only ``wglink.json`` is read, never the whole bundle. Validating a bundle hashes
a couple of megabytes of STEP and point grid, which is the right thing to do
before mutating a document and the wrong thing to do every few seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


HANDOFF_FILENAME = ".fusion-handoff.json"


@dataclass(frozen=True)
class Announcement:
    """A link whose bundle on disk has moved past what the document holds."""

    instance_id: str
    bundle_path: str
    stored_export_id: str
    available_export_id: str
    available_sequence: str

    def describe(self) -> str:
        return (
            f"{self.instance_id} — export sequence {self.available_sequence or '?'}"
        )


@dataclass(frozen=True)
class PendingHandoff:
    """A completed WG export that the user explicitly sent to Fusion."""

    marker_path: Path
    bundle_path: str
    bundle_id: str
    export_id: str
    sequence: str


def read_pending_handoff(marker_path: Path) -> PendingHandoff | None:
    """Read a scoped one-shot handoff without trusting an arbitrary path."""

    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schemaVersion") != 1 or payload.get("target") != "fusion360":
        return None
    bundle_id = payload.get("bundleId")
    export_id = payload.get("exportId")
    bundle_value = payload.get("bundlePath")
    if not all(isinstance(value, str) and value for value in (bundle_id, export_id, bundle_value)):
        return None
    try:
        bundle_root = marker_path.parent.resolve()
        bundle_path = Path(str(bundle_value)).expanduser().resolve()
    except OSError:
        return None
    if bundle_path.parent != bundle_root:
        return None
    if bundle_path.is_symlink() or not bundle_path.is_dir():
        return None
    sequence = payload.get("sequence")
    return PendingHandoff(
        marker_path=marker_path,
        bundle_path=str(bundle_path),
        bundle_id=str(bundle_id),
        export_id=str(export_id),
        sequence="" if sequence is None else str(sequence),
    )


def acknowledge_handoff(handoff: PendingHandoff) -> bool:
    """Remove only the marker this insert consumed, never a newer send."""

    current = read_pending_handoff(handoff.marker_path)
    if current is None or current.export_id != handoff.export_id:
        return False
    try:
        handoff.marker_path.unlink()
    except OSError:
        return False
    return True


def _manifest_path(bundle_path: str) -> Path:
    return Path(bundle_path).expanduser() / "wglink.json"


def read_export_identity(bundle_path: str) -> tuple[str, str] | None:
    """Return ``(export_id, sequence)`` from a bundle manifest, or None."""

    try:
        manifest = json.loads(_manifest_path(bundle_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(manifest, Mapping):
        return None
    export = manifest.get("export")
    if not isinstance(export, Mapping):
        return None
    export_id = export.get("id")
    if not isinstance(export_id, str) or not export_id:
        return None
    sequence = export.get("sequence")
    return export_id, "" if sequence is None else str(sequence)


class ExportWatcher:
    """Tracks which bundles have moved on, and which the user has been told about."""

    def __init__(self) -> None:
        self._announced: dict[str, str] = {}
        self._stamps: dict[str, tuple[float, int]] = {}

    def forget(self, instance_id: str) -> None:
        """Drop the announcement record once a link has been updated."""

        self._announced.pop(instance_id, None)

    def reset(self) -> None:
        self._announced.clear()
        self._stamps.clear()

    def _changed_on_disk(self, bundle_path: str) -> bool:
        """Cheap gate so an idle workspace costs one stat per link per tick."""

        try:
            status = _manifest_path(bundle_path).stat()
        except OSError:
            self._stamps.pop(bundle_path, None)
            return False
        stamp = (status.st_mtime, status.st_size)
        if self._stamps.get(bundle_path) == stamp:
            return False
        self._stamps[bundle_path] = stamp
        return True

    def survey(self, links: Iterable[Mapping[str, Any]]) -> list[Announcement]:
        """Announcements for links whose bundle names an unseen newer export.

        ``links`` carries plain strings copied off the document on the main
        thread -- never live Fusion objects, which must not cross a thread.
        """

        found: list[Announcement] = []
        for link in links:
            instance_id = str(link.get("instance_id") or "")
            bundle_path = str(link.get("bundle_path") or "")
            stored_export_id = str(link.get("export_id") or "")
            if not instance_id or not bundle_path:
                continue
            # A link whose stored export is unknown has never been updated from
            # a manifest; announcing it would be guessing, so leave it alone.
            if not stored_export_id:
                continue
            if not self._changed_on_disk(bundle_path):
                continue
            identity = read_export_identity(bundle_path)
            if identity is None:
                continue
            available_export_id, sequence = identity
            if available_export_id == stored_export_id:
                continue
            if self._announced.get(instance_id) == available_export_id:
                continue
            self._announced[instance_id] = available_export_id
            found.append(
                Announcement(
                    instance_id=instance_id,
                    bundle_path=bundle_path,
                    stored_export_id=stored_export_id,
                    available_export_id=available_export_id,
                    available_sequence=sequence,
                )
            )
        return found


def prompt_text(announcements: list[Announcement]) -> str:
    if len(announcements) == 1:
        one = announcements[0]
        return (
            f"Waveguide Generator exported a newer bundle for {one.instance_id}.\n\n"
            f"Export sequence {one.available_sequence or '?'}\n{one.bundle_path}\n\n"
            "Update the link now? This rebuilds the managed geometry in place; "
            "it creates and deletes no features, and Undo reverses it."
        )
    listed = "\n".join(f"  • {item.describe()}" for item in announcements)
    return (
        "Waveguide Generator exported newer bundles for these links:\n\n"
        f"{listed}\n\nUpdate them now? This rebuilds the managed geometry in "
        "place; it creates and deletes no features, and Undo reverses it."
    )
