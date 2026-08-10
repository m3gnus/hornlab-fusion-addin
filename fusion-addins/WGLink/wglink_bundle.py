"""Read WGLink bundles and decide CAD-link policy without importing Fusion.

Fusion's embedded Python has only the standard library.  Keeping validation,
topology selection, identity comparison, and rebuild policy here makes the
security boundary testable without launching Fusion and prevents the API layer
from growing a second, subtly different implementation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any
import unicodedata
import zipfile


SUPPORTED_FEATURES = frozenset(
    {"checksummed-files-v1", "link-local-frame-v1"}
)
TAG_AREA_TOLERANCE = 0.01
IDENTITY_MATRIX = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
_PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IDENTITY_ERROR = (
    "this bundle was not exported from Waveguide Generator; export it from WG "
    "so the link has an identity"
)


class WgLinkError(ValueError):
    """A refusal whose message is suitable for a Fusion dialog box."""


@dataclass(frozen=True)
class Limits:
    """Resource ceilings applied before untrusted bundle bytes are hashed."""

    max_file_bytes: int = 256 * 1024 * 1024
    max_bundle_bytes: int = 512 * 1024 * 1024
    max_files: int = 64
    max_depth: int = 32
    max_decompression_ratio: float = 200.0


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class LinkIdentity:
    """The stable design and monotonic export identity used by Update."""

    design_id: str
    lineage_id: str | None
    export_id: str
    export_sequence: int


@dataclass
class Bundle:
    """A validated bundle, materialized root, and original source path."""

    manifest: dict[str, Any]
    grid: dict[str, Any]
    root: Path
    source: Path
    identity: LinkIdentity
    owns_root: bool = False

    def close(self) -> None:
        """Release an extracted zip root owned by this bundle."""

        if getattr(self, "owns_root", False):
            cleanup = getattr(shutil, "rmtree", None)
            if cleanup is not None:
                cleanup(self.root, ignore_errors=True)
            self.owns_root = False

    def __enter__(self) -> Bundle:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Fusion callers should use the context-manager scope.  This is a last
        # line of defence for older callers so successful zip reads do not
        # accumulate extraction roots for the lifetime of the Fusion process.
        self.close()


@dataclass(frozen=True)
class Parameter:
    """A realized document-global interface parameter from Waveguide Generator."""

    name: str
    value: float
    unit: str
    role: str

    @property
    def expression(self) -> str:
        return format_expression(self.value)


@dataclass(frozen=True)
class SectionPlan:
    """The stable point and station topology materialized in Fusion."""

    phi_stride: int
    ring_stride: int
    points_per_ring: int
    ring_indices: tuple[int, ...]

    @property
    def sections(self) -> int:
        return len(self.ring_indices)


@dataclass(frozen=True)
class LinkState:
    """Comparison between stored document attributes and an incoming bundle."""

    verdict: str
    stored_design_id: str | None
    bundle_design_id: str
    stored_lineage_id: str | None
    bundle_lineage_id: str | None
    stored_sequence: int | None
    bundle_sequence: int
    stored_export_id: str | None
    bundle_export_id: str


@dataclass(frozen=True)
class TagVerdict:
    """How to repair one acoustic-role appearance after a rebuild."""

    keep: object | None
    strip: tuple[object, ...]
    repaint: bool
    candidates: tuple[object, ...]


@dataclass(frozen=True)
class EnclosurePlan:
    """Parameter-driven enclosure sketch/extrude expressions and mm checks."""

    front_extent_expression: str
    back_extent_expression: str
    edge_expression: str
    edge_type: int
    plan_type: int
    rectangle_mm: tuple[float, float, float, float]
    front_extent_mm: float
    back_extent_mm: float
    edge_mm: float


@dataclass
class _OpenBundleFile:
    """One directory member held open across stat, hash, and JSON parsing."""

    path: Path
    size: int
    stream: Any


def _dialog_error(context: str, exc: Exception) -> WgLinkError:
    return WgLinkError(f"{context}: {exc}")


def _reject_json_constant(value: str) -> None:
    raise WgLinkError(f"JSON contains non-finite value {value!r}")


def _reject_nonfinite_tree(value: object, *, location: str) -> None:
    """Catch overflowed JSON numbers such as ``1e999`` in unused metadata too."""

    if isinstance(value, float) and not math.isfinite(value):
        raise WgLinkError(f"JSON contains non-finite value at {location}: {value!r}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_nonfinite_tree(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonfinite_tree(child, location=f"{location}[{index}]")


def _load_json_stream(entry: _OpenBundleFile, label: str) -> dict[str, Any]:
    try:
        entry.stream.seek(0)
        payload = json.loads(
            entry.stream.read().decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except WgLinkError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _dialog_error(
            f"could not read {label} {entry.path.name!r}", exc
        ) from exc
    if not isinstance(payload, dict):
        raise WgLinkError(
            f"{label} {entry.path.name!r} must contain a JSON object"
        )
    _reject_nonfinite_tree(payload, location=entry.path.name)
    return payload


def _validate_limits(limits: Limits) -> None:
    values = {
        "max_file_bytes": limits.max_file_bytes,
        "max_bundle_bytes": limits.max_bundle_bytes,
        "max_files": limits.max_files,
        "max_depth": limits.max_depth,
        "max_decompression_ratio": limits.max_decompression_ratio,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise WgLinkError(f"bundle limit {name} must be positive, got {value!r}")


def _validate_member_name(name: object, *, label: str) -> str:
    if not isinstance(name, str) or not name:
        raise WgLinkError(f"{label} has an empty path {name!r}")
    if "\\" in name:
        raise WgLinkError(f"{label} path contains a backslash: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise WgLinkError(f"{label} path is absolute: {name!r}")
    segments = name.split("/")
    if any(not segment for segment in segments):
        raise WgLinkError(f"{label} path has an empty segment: {name!r}")
    if ".." in segments:
        raise WgLinkError(f"{label} path contains '..': {name!r}")
    return name


def _normalised_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _check_name_collisions(names: Sequence[str], *, label: str) -> None:
    seen: dict[str, str] = {}
    for name in names:
        key = _normalised_name(name)
        previous = seen.get(key)
        if previous is not None:
            raise WgLinkError(
                f"{label} paths collide after Unicode/case normalization: "
                f"{previous!r} and {name!r}"
            )
        seen[key] = name


def _open_regular_file(path: Path, relative: str) -> _OpenBundleFile:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise WgLinkError(f"bundle contains a symlink: {relative!r}") from exc
        raise _dialog_error(f"could not open bundle file {relative!r}", exc) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WgLinkError(f"bundle contains a non-file entry: {relative!r}")
        stream = os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise
    return _OpenBundleFile(path=path, size=metadata.st_size, stream=stream)


def _scan_directory(root: Path, limits: Limits) -> dict[str, _OpenBundleFile]:
    if root.is_symlink():
        raise WgLinkError(f"bundle root is a symlink: {root}")
    if not root.exists() or not root.is_dir():
        raise WgLinkError(f"bundle path is not a directory: {root}")

    files: dict[str, _OpenBundleFile] = {}
    total_size = 0
    entry_count = 0
    pending: list[tuple[Path, int]] = [(root, 0)]
    try:
        while pending:
            directory, depth = pending.pop()
            if depth > limits.max_depth:
                raise WgLinkError(
                    f"bundle directory depth {depth} exceeds limit {limits.max_depth}"
                )
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError as exc:
                raise _dialog_error(
                    f"could not inspect bundle directory {directory}", exc
                ) from exc
            child_directories: list[Path] = []
            for entry in entries:
                entry_count += 1
                if entry_count > limits.max_files:
                    raise WgLinkError(
                        f"bundle contains more than {limits.max_files} entries"
                    )
                candidate = Path(entry.path)
                relative = candidate.relative_to(root).as_posix()
                if entry.is_symlink():
                    raise WgLinkError(f"bundle contains a symlink: {relative!r}")
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 > limits.max_depth:
                        raise WgLinkError(
                            f"bundle directory {relative!r} exceeds depth limit "
                            f"{limits.max_depth}"
                        )
                    child_directories.append(candidate)
                    continue
                opened = _open_regular_file(candidate, relative)
                if opened.size > limits.max_file_bytes:
                    opened.stream.close()
                    raise WgLinkError(
                        f"bundle file {relative!r} is {opened.size} bytes; limit is "
                        f"{limits.max_file_bytes}"
                    )
                total_size += opened.size
                if total_size > limits.max_bundle_bytes:
                    opened.stream.close()
                    raise WgLinkError(
                        f"bundle files total {total_size} bytes; limit is "
                        f"{limits.max_bundle_bytes}"
                    )
                files[relative] = opened
            # Reverse push preserves the sorted depth-first order without recursion.
            pending.extend((child, depth + 1) for child in reversed(child_directories))
    except Exception:
        for opened in files.values():
            opened.stream.close()
        raise
    return files


def _validated_files_table(
    manifest: Mapping[str, Any],
    root: Path,
    actual: Mapping[str, _OpenBundleFile],
    limits: Limits,
) -> dict[str, Mapping[str, Any]]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, Mapping):
        raise WgLinkError("wglink manifest has no valid 'files' table")
    if len(raw_files) > limits.max_files:
        raise WgLinkError(
            f"manifest declares {len(raw_files)} files; limit is {limits.max_files}"
        )

    names: list[str] = []
    records: dict[str, Mapping[str, Any]] = {}
    declared_total = 0
    resolved_root = root.resolve()
    for raw_name, raw_record in raw_files.items():
        name = _validate_member_name(raw_name, label="manifest file")
        if name == "wglink.json":
            raise WgLinkError("manifest files table must not include 'wglink.json'")
        if not isinstance(raw_record, Mapping):
            raise WgLinkError(f"manifest file record for {name!r} is not an object")
        size = raw_record.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise WgLinkError(
                f"manifest size_bytes for {name!r} is invalid: {size!r}"
            )
        if size > limits.max_file_bytes:
            raise WgLinkError(
                f"manifest size_bytes for {name!r} is {size}; limit is "
                f"{limits.max_file_bytes}"
            )
        declared_total += size
        if declared_total > limits.max_bundle_bytes:
            raise WgLinkError(
                f"manifest file sizes total {declared_total} bytes; limit is "
                f"{limits.max_bundle_bytes}"
            )
        candidate = (root / name).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise WgLinkError(
                f"manifest file resolves outside the bundle root: {name!r}"
            ) from exc
        names.append(name)
        records[name] = raw_record

    _check_name_collisions(names, label="manifest file")
    actual_names = set(actual).difference({"wglink.json"})
    declared_names = set(records)
    extras = sorted(actual_names.difference(declared_names))
    if extras:
        raise WgLinkError(
            "bundle contains file(s) not listed in the files table: "
            + ", ".join(repr(name) for name in extras)
        )
    missing = sorted(declared_names.difference(actual_names))
    if missing:
        raise WgLinkError(
            "bundle is missing declared file(s): "
            + ", ".join(repr(name) for name in missing)
        )

    # Both sources of size information are checked before any hashing.  This
    # matters for a corrupt multi-gigabyte file whose manifest claims one byte.
    for name, record in records.items():
        actual_size = actual[name].size
        expected_size = record["size_bytes"]
        if actual_size != expected_size:
            raise WgLinkError(
                f"size mismatch for {name!r}: filesystem has {actual_size} bytes, "
                f"manifest names {expected_size}"
            )
    return records


def _sha256(entry: _OpenBundleFile) -> str:
    digest = hashlib.sha256()
    try:
        entry.stream.seek(0)
        for block in iter(lambda: entry.stream.read(1024 * 1024), b""):
            digest.update(block)
    except OSError as exc:
        raise _dialog_error(
            f"could not hash bundle file {entry.path.name!r}", exc
        ) from exc
    return "sha256:" + digest.hexdigest()


def _verify_hashes(
    records: Mapping[str, Mapping[str, Any]],
    actual: Mapping[str, _OpenBundleFile],
) -> None:
    for name in sorted(records):
        expected = records[name].get("sha256")
        observed = _sha256(actual[name])
        if not isinstance(expected, str) or observed != expected:
            raise WgLinkError(f"sha256 mismatch for bundle file {name!r}")


def _finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WgLinkError(f"{label} must be a finite number, got {value!r}")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise WgLinkError(f"{label} must be finite, got {value!r}") from exc
    if not math.isfinite(number):
        raise WgLinkError(f"{label} must be finite, got {value!r}")
    return number


def _validate_manifest(manifest: Mapping[str, Any]) -> LinkIdentity:
    version = manifest.get("wglink_version")
    match = re.fullmatch(r"(\d+)\.(\d+)", version) if isinstance(version, str) else None
    if match is None or int(match.group(1)) != 1:
        raise WgLinkError(f"unsupported wglink_version {version!r}; expected major 1")

    required = manifest.get("required_features", [])
    if not isinstance(required, list) or not all(
        isinstance(feature, str) for feature in required
    ):
        raise WgLinkError(f"required_features is invalid: {required!r}")
    unknown = sorted(set(required).difference(SUPPORTED_FEATURES))
    if unknown:
        raise WgLinkError(
            "unsupported required_features: " + ", ".join(repr(value) for value in unknown)
        )

    coordinate_system = manifest.get("coordinate_system")
    if not isinstance(coordinate_system, Mapping):
        raise WgLinkError("coordinate_system is missing or invalid")
    expected_fields = {
        "length_unit": "mm",
        "handedness": "right",
        "matrix_convention": "row-major-local-to-parent",
    }
    for name, expected in expected_fields.items():
        observed = coordinate_system.get(name)
        if observed != expected:
            raise WgLinkError(
                f"coordinate_system.{name} is {observed!r}; expected {expected!r}"
            )
    matrix = coordinate_system.get("step_from_design")
    if not isinstance(matrix, list) or len(matrix) != 4:
        raise WgLinkError(f"step_from_design must be a finite 4x4 matrix, got {matrix!r}")
    converted: list[tuple[float, ...]] = []
    for row_index, row in enumerate(matrix):
        if not isinstance(row, list) or len(row) != 4:
            raise WgLinkError(
                f"step_from_design row {row_index} must have 4 values, got {row!r}"
            )
        converted.append(
            tuple(
                _finite_number(value, label=f"step_from_design[{row_index}][{column}]")
                for column, value in enumerate(row)
            )
        )
    if tuple(converted) != IDENTITY_MATRIX:
        raise WgLinkError(
            f"step_from_design is {matrix!r}; Phase 1 supports only the identity matrix"
        )

    design = manifest.get("design")
    export = manifest.get("export")
    if not isinstance(design, Mapping) or not isinstance(export, Mapping):
        raise WgLinkError(_IDENTITY_ERROR)
    design_id = design.get("id")
    export_id = export.get("id")
    sequence = export.get("sequence")
    if (
        not isinstance(design_id, str)
        or not design_id
        or not isinstance(export_id, str)
        or not export_id
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
    ):
        raise WgLinkError(_IDENTITY_ERROR)
    build_mode = design.get("build_mode")
    if build_mode not in {"enclosure", "freestanding"}:
        raise WgLinkError(
            f"design.build_mode {build_mode!r} is unsupported; CAD-LINK-PLAN.md §5.1 "
            "permanently excludes 'bare' and 'infinite_baffle'"
        )
    lineage = design.get("lineage_id")
    if lineage is not None and not isinstance(lineage, str):
        raise WgLinkError(f"design.lineage_id is invalid: {lineage!r}")
    return LinkIdentity(design_id, lineage, export_id, sequence)


def _validate_points(
    value: object,
    *,
    name: str,
    n_phi: int,
    n_length: int,
) -> None:
    if not isinstance(value, list) or len(value) != n_phi:
        actual = len(value) if isinstance(value, list) else type(value).__name__
        raise WgLinkError(
            f"{name} must have shape {n_phi}x{n_length}x3; first dimension is {actual!r}"
        )
    for phi, ray in enumerate(value):
        if not isinstance(ray, list) or len(ray) != n_length:
            actual = len(ray) if isinstance(ray, list) else type(ray).__name__
            raise WgLinkError(
                f"{name}[{phi}] must contain {n_length} rings, got {actual!r}"
            )
        for ring, point in enumerate(ray):
            if not isinstance(point, list) or len(point) != 3:
                raise WgLinkError(
                    f"{name}[{phi}][{ring}] must be an xyz triple, got {point!r}"
                )
            for axis, coordinate in enumerate(point):
                _finite_number(
                    coordinate,
                    label=f"{name}[{phi}][{ring}][{axis}]",
                )


def _grid_dimension(grid: Mapping[str, Any], name: str, minimum: int) -> int:
    value = grid.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WgLinkError(f"point-grid {name} must be at least {minimum}, got {value!r}")
    return value


def _validate_grid(grid: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    expected = {"frame": "link-local", "units": "mm"}
    for name, required in expected.items():
        observed = grid.get(name)
        if observed != required:
            raise WgLinkError(f"point-grid {name} is {observed!r}; expected {required!r}")
    if grid.get("closed") is not True:
        raise WgLinkError(f"point-grid closed must be true, got {grid.get('closed')!r}")
    # Do not apply vertical_offset_mm here or in Task B.  It is informational
    # and already present in every coordinate; doing it twice caused a measured
    # 80 mm Tritonia misassembly before mesher commit 0dfb9c9.
    manifest_mode = manifest["design"]["build_mode"]
    grid_mode = grid.get("build_mode")
    if grid_mode != manifest_mode:
        raise WgLinkError(
            f"point-grid build_mode {grid_mode!r} does not match manifest "
            f"{manifest_mode!r}"
        )

    n_phi = _grid_dimension(grid, "n_phi", 3)
    n_length = _grid_dimension(grid, "n_length", 2)
    _validate_points(
        grid.get("inner_points"),
        name="inner_points",
        n_phi=n_phi,
        n_length=n_length,
    )
    ring_z = grid.get("ring_z_mm")
    if not isinstance(ring_z, list) or len(ring_z) != n_length:
        raise WgLinkError(
            f"ring_z_mm must contain {n_length} values, got {ring_z!r}"
        )
    for index, value in enumerate(ring_z):
        _finite_number(value, label=f"ring_z_mm[{index}]")
    ring_planar = grid.get("ring_planar")
    if (
        not isinstance(ring_planar, list)
        or len(ring_planar) != n_length
        or not all(isinstance(value, bool) for value in ring_planar)
    ):
        raise WgLinkError(
            f"ring_planar must contain {n_length} booleans, got {ring_planar!r}"
        )

    has_outer = grid.get("has_outer_points")
    if not isinstance(has_outer, bool):
        raise WgLinkError(f"has_outer_points must be boolean, got {has_outer!r}")
    outer = grid.get("outer_points")
    if has_outer:
        _validate_points(
            outer,
            name="outer_points",
            n_phi=n_phi,
            n_length=n_length,
        )
    elif outer is not None:
        raise WgLinkError(
            "outer_points is present while has_outer_points is false"
        )


def _read_directory(root: Path, limits: Limits, *, source: Path | None = None) -> Bundle:
    actual = _scan_directory(root, limits)
    try:
        manifest_entry = actual.get("wglink.json")
        if manifest_entry is None:
            raise WgLinkError(f"bundle has no wglink.json manifest: {root}")
        manifest = _load_json_stream(manifest_entry, "wglink manifest")
        identity = _validate_manifest(manifest)
        records = _validated_files_table(manifest, root, actual, limits)
        _verify_hashes(records, actual)
        grid_entry = actual.get("point-grid.json")
        if grid_entry is None or "point-grid.json" not in records:
            raise WgLinkError("bundle files table does not contain 'point-grid.json'")
        grid = _load_json_stream(grid_entry, "point grid")
        _validate_grid(grid, manifest)
        return Bundle(
            manifest=dict(manifest),
            grid=dict(grid),
            root=root,
            source=source or root,
            identity=identity,
        )
    finally:
        for entry in actual.values():
            entry.stream.close()


def _zip_member_kind(info: zipfile.ZipInfo) -> str:
    unix_mode = info.external_attr >> 16
    kind = stat.S_IFMT(unix_mode)
    if kind in (0, stat.S_IFREG):
        return "directory" if info.is_dir() else "file"
    if kind == stat.S_IFDIR and info.is_dir():
        return "directory"
    if kind == stat.S_IFLNK:
        return "symlink"
    return "device or special entry"


def _extract_zip(source: Path, destination_parent: Path, limits: Limits) -> Path:
    if destination_parent.is_symlink():
        raise WgLinkError(f"temporary extraction directory is a symlink: {destination_parent}")
    try:
        destination_parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _dialog_error(
            f"could not create temporary extraction directory {destination_parent}", exc
        ) from exc
    if not destination_parent.is_dir():
        raise WgLinkError(
            f"temporary extraction path is not a directory: {destination_parent}"
        )
    root = Path(tempfile.mkdtemp(prefix="wglink-", dir=destination_parent))
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > limits.max_files:
                raise WgLinkError(
                    f"zip contains {len(members)} members; limit is "
                    f"{limits.max_files}"
                )
            names: list[str] = []
            logical_entries: dict[str, str] = {}
            declared_total = 0
            validated: list[tuple[zipfile.ZipInfo, str, str]] = []
            for info in members:
                candidate_name = info.filename[:-1] if info.is_dir() else info.filename
                name = _validate_member_name(candidate_name, label="zip member")
                depth = len(name.split("/")) - (0 if info.is_dir() else 1)
                if depth > limits.max_depth:
                    raise WgLinkError(
                        f"zip member {name!r} exceeds depth limit {limits.max_depth}"
                    )
                kind = _zip_member_kind(info)
                if kind not in {"file", "directory"}:
                    raise WgLinkError(f"zip member {name!r} is a {kind}")
                if info.file_size > limits.max_file_bytes:
                    raise WgLinkError(
                        f"zip member {name!r} declares {info.file_size} bytes; limit is "
                        f"{limits.max_file_bytes}"
                    )
                declared_total += info.file_size
                if declared_total > limits.max_bundle_bytes:
                    raise WgLinkError(
                        f"zip members declare {declared_total} bytes; limit is "
                        f"{limits.max_bundle_bytes}"
                    )
                if info.file_size:
                    if info.compress_size == 0:
                        raise WgLinkError(
                            f"zip member {name!r} has infinite decompression ratio"
                        )
                    ratio = info.file_size / info.compress_size
                    if ratio > limits.max_decompression_ratio:
                        raise WgLinkError(
                            f"zip member {name!r} decompression ratio {ratio:.1f}x exceeds "
                            f"{limits.max_decompression_ratio:g}x"
                        )
                names.append(name)
                pieces = name.split("/")
                for count in range(1, len(pieces) + 1):
                    logical_name = "/".join(pieces[:count])
                    logical_kind = (
                        kind if count == len(pieces) else "directory"
                    )
                    previous_kind = logical_entries.get(logical_name)
                    if previous_kind is not None and previous_kind != logical_kind:
                        raise WgLinkError(
                            f"zip path {logical_name!r} is both a file and a directory"
                        )
                    logical_entries[logical_name] = logical_kind
                    if len(logical_entries) > limits.max_files:
                        raise WgLinkError(
                            f"zip contains more than {limits.max_files} logical entries"
                        )
                validated.append((info, name, kind))
            _check_name_collisions(names, label="zip member")

            actual_total = 0
            for info, name, kind in validated:
                target = root.joinpath(*name.split("/"))
                if kind == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                try:
                    with archive.open(info, "r") as incoming, target.open("xb") as outgoing:
                        while True:
                            block = incoming.read(1024 * 1024)
                            if not block:
                                break
                            written += len(block)
                            actual_total += len(block)
                            if written > info.file_size or written > limits.max_file_bytes:
                                raise WgLinkError(
                                    f"zip member {name!r} expanded beyond its declared or "
                                    "per-file size"
                                )
                            if actual_total > limits.max_bundle_bytes:
                                raise WgLinkError(
                                    f"zip expanded beyond bundle limit "
                                    f"{limits.max_bundle_bytes} bytes"
                                )
                            outgoing.write(block)
                except WgLinkError:
                    raise
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise _dialog_error(f"could not extract zip member {name!r}", exc) from exc
                if written != info.file_size:
                    raise WgLinkError(
                        f"zip member {name!r} expanded to {written} bytes, declared "
                        f"{info.file_size}"
                    )
    except WgLinkError:
        shutil.rmtree(root, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise _dialog_error(f"could not read zip bundle {source.name!r}", exc) from exc
    return root


def read_bundle(
    path: str | Path,
    *,
    limits: Limits = DEFAULT_LIMITS,
    temp_dir: str | Path | None = None,
) -> Bundle:
    """Read a bundle directory or safely extract and read a ``.wglink`` zip.

    ``temp_dir`` lets the Fusion caller own extracted-file lifetime.  When it
    is omitted a stable process-temporary directory is created; ``Bundle.root``
    identifies the exact directory and may be removed after Task B is done.
    """

    _validate_limits(limits)
    source = Path(path)
    if source.is_symlink():
        raise WgLinkError(f"bundle path is a symlink: {source}")
    if source.is_dir():
        return _read_directory(source, limits, source=source)
    if not source.exists() or not source.is_file():
        raise WgLinkError(f"bundle path does not exist or is not a file: {source}")
    try:
        is_zip = zipfile.is_zipfile(source)
    except OSError as exc:
        raise _dialog_error(f"could not inspect bundle {source}", exc) from exc
    if not is_zip:
        raise WgLinkError(f"bundle file is not a valid zip archive: {source.name!r}")
    parent = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())
    extracted = _extract_zip(source, parent, limits)
    try:
        bundle = _read_directory(extracted, limits, source=source)
        bundle.owns_root = True
        return bundle
    except Exception:
        shutil.rmtree(extracted, ignore_errors=True)
        raise


def interface_parameters(bundle: Bundle) -> list[Parameter]:
    """Return only realized parameters that form the user's CAD interface."""

    raw_parameters = bundle.manifest.get("parameters", [])
    if not isinstance(raw_parameters, list):
        raise WgLinkError(f"manifest parameters must be a list, got {raw_parameters!r}")
    result: list[Parameter] = []
    for index, raw in enumerate(raw_parameters):
        if not isinstance(raw, Mapping):
            raise WgLinkError(f"manifest parameter {index} is not an object: {raw!r}")
        if raw.get("role") != "interface":
            continue
        name = raw.get("name")
        if not isinstance(name, str):
            raise WgLinkError(f"interface parameter name is invalid: {name!r}")
        validate_parameter_name(name)
        unit = raw.get("unit")
        if unit != "mm":
            raise WgLinkError(
                f"interface parameter {name!r} has unit {unit!r}; expected 'mm'"
            )
        value = _finite_number(raw.get("value"), label=f"interface parameter {name!r}")
        result.append(Parameter(name=name, value=value, unit=unit, role="interface"))
    return result


def format_expression(value_mm: float) -> str:
    """Format an exact, locale-independent Fusion expression without exponents."""

    value = _finite_number(value_mm, label="millimetre expression value")
    shortest = repr(value)
    if "e" in shortest.lower():
        shortest = format(Decimal(shortest), "f")
    return f"{shortest} mm"


def validate_parameter_name(name: str) -> None:
    """Refuse names Fusion cannot install as document-global identifiers."""

    if not isinstance(name, str) or _PARAMETER_NAME.fullmatch(name) is None:
        raise WgLinkError(
            f"Fusion parameter name {name!r} is invalid; expected "
            "[A-Za-z_][A-Za-z0-9_]*"
        )


def parameter_slug(bundle: Bundle) -> str:
    """Return the one parameter namespace owned by this link.

    ``throat_dia`` is part of the interface contract in every supported solid
    build.  Deriving the namespace here keeps Audit from growing its own guess
    about which document-global ``wg_*`` parameters belong to the link.
    """

    suffix = "_throat_dia"
    matches = [
        parameter.name[: -len(suffix)]
        for parameter in interface_parameters(bundle)
        if parameter.name.endswith(suffix)
    ]
    if len(matches) != 1 or not matches[0].startswith("wg_"):
        raise WgLinkError(
            "link requires exactly one 'wg_<slug>_throat_dia' interface "
            f"parameter; found {len(matches)}"
        )
    slug = matches[0][len("wg_") :]
    if not slug:
        raise WgLinkError("link parameter slug must not be empty")
    return slug


def parameters_by_suffix(bundle: Bundle) -> dict[str, Parameter]:
    """Index this link's interface parameters by suffix, anchored on the slug.

    Matching on ``name.endswith("_" + suffix)`` is wrong and was measured wrong
    in Fusion: ``wg_tritonia_depth`` and ``wg_tritonia_enc_depth`` BOTH end in
    ``_depth``, so a suffix lookup for the throat-to-baffle depth found two
    parameters and refused to insert at all.  Anchoring on ``wg_<slug>_`` makes
    every suffix exact, and drops any stray ``wg_*`` parameter that belongs to
    a different link in the same document-global table.
    """

    prefix = f"wg_{parameter_slug(bundle)}_"
    indexed: dict[str, Parameter] = {}
    for parameter in interface_parameters(bundle):
        if not parameter.name.startswith(prefix):
            continue
        suffix = parameter.name[len(prefix) :]
        if suffix in indexed:
            raise WgLinkError(
                f"link declares {parameter.name!r} twice in its interface table"
            )
        indexed[suffix] = parameter
    return indexed


def instance_parameter_prefix(slug: str, existing: Sequence[str]) -> str:
    """Choose the first unused document-global namespace for one insertion."""

    if not isinstance(slug, str) or not slug:
        raise WgLinkError(f"link parameter slug is invalid: {slug!r}")
    base = f"wg_{slug}"
    occupied = {str(value) for value in existing}
    first = f"{base}_"
    if first not in occupied:
        return first
    index = 2
    while f"{base}{index}_" in occupied:
        index += 1
    return f"{base}{index}_"


def effective_parameters(bundle: Bundle, prefix: str) -> dict[str, Parameter]:
    """Rename manifest parameter suffixes into this instance's namespace."""

    if not isinstance(prefix, str) or not prefix.endswith("_"):
        raise WgLinkError(f"instance parameter prefix is invalid: {prefix!r}")
    validate_parameter_name(prefix[:-1])
    return {
        suffix: Parameter(
            name=f"{prefix}{suffix}",
            value=parameter.value,
            unit=parameter.unit,
            role=parameter.role,
        )
        for suffix, parameter in parameters_by_suffix(bundle).items()
    }


def _power_of_two_stride(minimum: int) -> int:
    stride = 1
    while stride < minimum:
        stride *= 2
    return stride


def _ring_indices(n_length: int, stride: int) -> tuple[int, ...]:
    indices = list(range(0, n_length, stride))
    if indices[-1] != n_length - 1:
        indices.append(n_length - 1)
    return tuple(indices)


def _sample_ray_indices(n_phi: int, maximum: int = 12) -> tuple[int, ...]:
    count = min(n_phi, maximum)
    return tuple(sorted({index * n_phi // count for index in range(count)}))


def _point_segment_distance(
    point: Sequence[float],
    start: Sequence[float],
    end: Sequence[float],
) -> float:
    direction = [float(end[axis]) - float(start[axis]) for axis in range(3)]
    offset = [float(point[axis]) - float(start[axis]) for axis in range(3)]
    squared_length = sum(value * value for value in direction)
    if squared_length <= 0.0:
        return math.sqrt(sum(value * value for value in offset))
    fraction = max(
        0.0,
        min(1.0, sum(offset[axis] * direction[axis] for axis in range(3)) / squared_length),
    )
    return math.sqrt(
        sum(
            (offset[axis] - fraction * direction[axis]) ** 2
            for axis in range(3)
        )
    )


def _interval_chord_error(
    points: Sequence[Sequence[Sequence[float]]],
    rays: Sequence[int],
    left: int,
    right: int,
) -> tuple[float, int | None]:
    worst_error = 0.0
    worst_station = None
    for station in range(left + 1, right):
        station_error = max(
            _point_segment_distance(
                points[ray][station], points[ray][left], points[ray][right]
            )
            for ray in rays
        )
        if station_error > worst_error:
            worst_error = station_error
            worst_station = station
    return worst_error, worst_station


def _curvature_ring_indices(
    points: Sequence[Sequence[Sequence[float]]],
    n_phi: int,
    n_length: int,
    max_sections: int,
    tolerance_mm: float,
) -> tuple[int, ...]:
    _validate_points(
        list(points), name="inner_points", n_phi=n_phi, n_length=n_length
    )
    rays = _sample_ray_indices(n_phi)
    selected = {0, n_length - 1}
    while len(selected) < max_sections:
        ordered = sorted(selected)
        candidates = []
        for left, right in zip(ordered, ordered[1:]):
            error, station = _interval_chord_error(points, rays, left, right)
            if station is not None:
                candidates.append((error, -left, -right, station))
        if not candidates:
            break
        error, _left_key, _right_key, station = max(candidates)
        if error < tolerance_mm:
            break
        selected.add(station)
    return tuple(sorted(selected))


def plan_sections(
    n_phi: int,
    n_length: int,
    *,
    max_points: int = 64,
    max_sections: int = 40,
    inner_points: Sequence[Sequence[Sequence[float]]] | None = None,
    chord_tolerance_mm: float = 0.02,
) -> SectionPlan:
    """Choose deterministic curvature-aware stations within Fusion's build caps."""

    if isinstance(n_phi, bool) or not isinstance(n_phi, int) or n_phi < 3:
        raise WgLinkError(f"n_phi must be at least 3, got {n_phi!r}")
    if isinstance(n_length, bool) or not isinstance(n_length, int) or n_length < 2:
        raise WgLinkError(f"n_length must be at least 2, got {n_length!r}")
    if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points < 3:
        raise WgLinkError(f"max_points must be at least 3, got {max_points!r}")
    if (
        isinstance(max_sections, bool)
        or not isinstance(max_sections, int)
        or max_sections < 2
    ):
        raise WgLinkError(f"max_sections must be at least 2, got {max_sections!r}")
    tolerance = _finite_number(chord_tolerance_mm, label="chord_tolerance_mm")
    if tolerance < 0.0:
        raise WgLinkError(
            f"chord_tolerance_mm must not be negative, got {chord_tolerance_mm!r}"
        )

    # Power-of-two thinning records the proven spike topology, not merely the
    # smallest arithmetic stride: 168 rays use stride 4 (42 points), and 108
    # rings use stride 8 (15 explicit sections including the final ring).
    minimum_phi_stride = math.ceil(n_phi / max_points)
    phi_stride = _power_of_two_stride(minimum_phi_stride)
    points_per_ring = len(range(0, n_phi, phi_stride))
    ring_stride = 1
    indices = _ring_indices(n_length, ring_stride)
    # Preserve the established count-only topology cap for callers without a
    # grid. Curvature-aware callers may spend the larger section budget needed
    # to retain dense terminations.
    uniform_budget = min(max_sections, 24)
    while len(indices) > uniform_budget:
        ring_stride *= 2
        indices = _ring_indices(n_length, ring_stride)
    if inner_points is not None:
        indices = _curvature_ring_indices(
            inner_points,
            n_phi,
            n_length,
            max_sections,
            tolerance,
        )
    return SectionPlan(phi_stride, ring_stride, points_per_ring, indices)


def section_arc_positions(
    inner_points: Sequence[Sequence[Sequence[float]]],
    indices: Sequence[int],
) -> list[float]:
    """Match the mesher's averaged normalized cumulative ray arc lengths."""

    if not isinstance(inner_points, Sequence) or isinstance(inner_points, (str, bytes)):
        raise WgLinkError("inner_points must be a point-grid sequence")
    n_phi = len(inner_points)
    if n_phi < 3:
        raise WgLinkError(f"inner_points must contain at least 3 rays, got {n_phi}")
    first = inner_points[0]
    n_length = len(first)
    if n_length < 2:
        raise WgLinkError(
            f"inner_points must contain at least 2 sections, got {n_length}"
        )
    _validate_points(
        list(inner_points),
        name="inner_points",
        n_phi=n_phi,
        n_length=n_length,
    )
    if (
        not isinstance(indices, Sequence)
        or isinstance(indices, (str, bytes))
        or len(indices) < 2
    ):
        raise WgLinkError(f"section indices must contain at least 2 entries: {indices!r}")
    selected: list[int] = []
    for value in indices:
        if isinstance(value, bool) or not isinstance(value, int):
            raise WgLinkError(f"section index is invalid: {value!r}")
        if value < 0 or value >= n_length:
            raise WgLinkError(
                f"section index {value} is outside 0..{n_length - 1}"
            )
        selected.append(value)
    if any(right <= left for left, right in zip(selected, selected[1:])):
        raise WgLinkError(f"section indices must be strictly increasing: {selected!r}")

    sums = [0.0] * n_length
    for ray_index, ray in enumerate(inner_points):
        cumulative = [0.0]
        for left, right in zip(ray, ray[1:]):
            step = math.sqrt(
                sum((float(right[axis]) - float(left[axis])) ** 2 for axis in range(3))
            )
            cumulative.append(cumulative[-1] + step)
        length = cumulative[-1]
        if length <= 0.0:
            raise WgLinkError(f"inner_points ray {ray_index} has zero arc length")
        for station, value in enumerate(cumulative):
            sums[station] += value / length
    positions = [value / n_phi for value in sums]
    if any(right <= left for left, right in zip(positions, positions[1:])):
        raise WgLinkError("averaged inner_points arc positions are not increasing")
    return [positions[index] for index in selected]


def attribute_payload(
    bundle: Bundle,
    *,
    instance_id: str,
    plan: SectionPlan,
    overshoot_mm: float = 0.0,
    walls: int | None = None,
    assembly_from_link: Sequence[Sequence[float]] = IDENTITY_MATRIX,
    chirality: str = "original",
    local_body_state: str = "unmodified",
    thicken_sign: int | None = None,
    parameter_prefix: str | None = None,
) -> dict[str, str]:
    """Serialize the complete deterministic attribute set Task B commits."""

    if not isinstance(instance_id, str) or not instance_id:
        raise WgLinkError(f"instance_id is invalid: {instance_id!r}")
    overshoot = _finite_number(overshoot_mm, label="overshoot_mm")
    if overshoot < 0.0:
        raise WgLinkError(f"overshoot_mm must not be negative, got {overshoot_mm!r}")
    outer_available = bool(bundle.grid.get("has_outer_points"))
    resolved_walls = 2 if outer_available else 1
    if walls is not None:
        if walls not in (1, 2):
            raise WgLinkError(f"walls must be 1 or 2, got {walls!r}")
        resolved_walls = walls
    if resolved_walls == 2 and not outer_available:
        raise WgLinkError(
            "walls=2 requires bundle outer_points, but has_outer_points is false"
        )
    matrix = [list(row) for row in assembly_from_link]
    if len(matrix) != 4 or any(len(row) != 4 for row in matrix):
        raise WgLinkError(f"assembly_from_link must be 4x4, got {matrix!r}")
    for row_index, row in enumerate(matrix):
        for column, value in enumerate(row):
            _finite_number(value, label=f"assembly_from_link[{row_index}][{column}]")

    positions = section_arc_positions(bundle.grid["inner_points"], plan.ring_indices)
    topology = {
        # This describes the document topology, not every wall available in
        # the bundle.  The proven patch/stitch/thicken path materializes one
        # fitted-spline wall even when WG also exports sampled outer points.
        "has_outer": resolved_walls == 2,
        "overshoot_mm": overshoot,
        "point_count": plan.points_per_ring,
        "section_arc_positions": positions,
        "walls": resolved_walls,
    }
    identity = bundle.identity
    design = bundle.manifest.get("design", {})
    if not isinstance(design, Mapping):
        design = {}
    build_mode = str(design.get("build_mode", ""))
    if build_mode == "freestanding":
        if isinstance(thicken_sign, bool) or thicken_sign not in (-1, 1):
            raise WgLinkError(
                "freestanding attribute payload requires thicken_sign -1 or 1"
            )
    elif thicken_sign is not None:
        raise WgLinkError("thicken_sign is only valid for a freestanding link")
    payload = {
        "assembly_from_link": json.dumps(matrix, separators=(",", ":")),
        "build_mode": build_mode,
        "bundle_id": str(bundle.manifest.get("bundle", {}).get("id", "")),
        "bundle_path": str(bundle.source),
        "chirality": str(chirality),
        "design_hash": str(design.get("design_hash", "")),
        "design_id": identity.design_id,
        "design_name": str(design.get("name", "")),
        "edit_version": str(design.get("edit_version", "")),
        "export_id": identity.export_id,
        "export_sequence": str(identity.export_sequence),
        "geometry_hash": str(bundle.manifest.get("export", {}).get("geometry_hash", "")),
        "instance_id": instance_id,
        "lineage_id": identity.lineage_id or "",
        "local_body_state": str(local_body_state),
        "parameter_prefix": parameter_prefix or f"wg_{parameter_slug(bundle)}_",
        "schema": "1",
        "slug": parameter_slug(bundle),
        "topology": json.dumps(topology, sort_keys=True, separators=(",", ":")),
    }
    if thicken_sign is not None:
        payload["thicken_sign"] = str(thicken_sign)
    return payload


def rollback_target(
    entries: Sequence[Mapping[str, object]],
    *,
    after_index: int = -1,
) -> int | None:
    """Choose the first dependent timeline entry, preserving assembly rings.

    Occurrences can precede the managed sketches in an assembly timeline.  A
    rollback to one of them suppresses the sketches and invalidates captured
    fit-point references, so both kinds are deliberately skipped.
    """

    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise WgLinkError(f"timeline entry {position} is not an object")
        index = entry.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise WgLinkError(
                f"timeline entry {position} has invalid index {index!r}"
            )
        if index <= after_index:
            continue
        kind = entry.get("kind")
        if kind is not None and not isinstance(kind, str):
            raise WgLinkError(
                f"timeline entry {index} has invalid kind {kind!r}"
            )
        if kind is None:
            continue
        if kind not in {"Sketch", "Occurrence"}:
            return index
    return None


def _stored_text(stored: Mapping[str, object], name: str) -> str | None:
    value = stored.get(name)
    if value is None:
        return None
    text = str(value)
    return text or None


def link_state(stored: Mapping[str, object], bundle: Bundle) -> LinkState:
    """Classify freshness while preserving every identity value that decided it."""

    identity = bundle.identity
    stored_design = _stored_text(stored, "design_id")
    stored_lineage = _stored_text(stored, "lineage_id")
    stored_export = _stored_text(stored, "export_id")
    stored_geometry = _stored_text(stored, "geometry_hash")
    bundle_geometry = _stored_text(bundle.manifest.get("export", {}), "geometry_hash")
    raw_sequence = stored.get("export_sequence")
    try:
        stored_sequence = int(raw_sequence) if raw_sequence is not None else None
    except (TypeError, ValueError):
        stored_sequence = None

    if stored_design is None or stored_export is None or stored_sequence is None:
        verdict = "no_link"
    elif (
        stored_lineage is not None
        and identity.lineage_id is not None
        and stored_lineage != identity.lineage_id
    ):
        verdict = "different_lineage"
    elif stored_design != identity.design_id:
        verdict = "different_design"
    elif identity.export_sequence == stored_sequence:
        export_disagrees = stored_export != identity.export_id
        geometry_disagrees = (
            stored_geometry is not None
            and bundle_geometry is not None
            and stored_geometry != bundle_geometry
        )
        verdict = "corrupt_export" if export_disagrees or geometry_disagrees else "same_export"
    elif identity.export_sequence > stored_sequence:
        verdict = "newer_export"
    else:
        verdict = "older_export"
    return LinkState(
        verdict=verdict,
        stored_design_id=stored_design,
        bundle_design_id=identity.design_id,
        stored_lineage_id=stored_lineage,
        bundle_lineage_id=identity.lineage_id,
        stored_sequence=stored_sequence,
        bundle_sequence=identity.export_sequence,
        stored_export_id=stored_export,
        bundle_export_id=identity.export_id,
    )


def health_regressions(
    before: Sequence[Mapping[str, object]],
    after: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Return Healthy-to-unhealthy transitions, treating disappearance as failure."""

    after_by_key = {
        (str(row.get("name")), str(row.get("type"))): row for row in after
    }
    regressions: list[dict[str, object]] = []
    for row in before:
        if row.get("health") != "Healthy":
            continue
        key = (str(row.get("name")), str(row.get("type")))
        observed = after_by_key.get(key)
        if observed is None:
            regressions.append(
                {
                    "name": row.get("name"),
                    "type": row.get("type"),
                    "health": "Missing",
                    "before_health": "Healthy",
                }
            )
        elif observed.get("health") != "Healthy":
            result = dict(observed)
            result["before_health"] = "Healthy"
            regressions.append(result)
    return regressions


def _face_value(face: object, name: str, default: object = None) -> object:
    if isinstance(face, Mapping):
        return face.get(name, default)
    return getattr(face, name, default)


def _face_identifier(face: object, index: int) -> object:
    for name in ("id", "token", "index"):
        value = _face_value(face, name)
        if value is not None:
            return value
    return index


def tag_verdict(
    faces: Sequence[object],
    *,
    expected_area_mm2: float,
    tolerance: float,
) -> TagVerdict:
    """Choose the tagged throat face geometrically and strip appearance spread.

    ``faces`` describes the faces currently carrying one role.  Each descriptor
    supplies ``planar``, ``at_throat_plane``, ``area_mm2`` and preferably an
    ``id`` or ``token``.  Paint is only an input inventory; it is never the
    evidence that a face is the throat disc.
    """

    expected = _finite_number(expected_area_mm2, label="expected_area_mm2")
    relative_tolerance = _finite_number(tolerance, label="tag area tolerance")
    if expected <= 0.0:
        raise WgLinkError(f"expected_area_mm2 must be positive, got {expected_area_mm2!r}")
    if relative_tolerance < 0.0:
        raise WgLinkError(f"tag area tolerance must not be negative: {tolerance!r}")

    described: list[tuple[object, float, bool]] = []
    for index, face in enumerate(faces):
        identifier = _face_identifier(face, index)
        area = _finite_number(
            _face_value(face, "area_mm2"),
            label=f"tagged face {identifier!r} area_mm2",
        )
        matches = (
            _face_value(face, "planar") is True
            and _face_value(face, "at_throat_plane") is True
            and math.isclose(area, expected, rel_tol=relative_tolerance, abs_tol=0.0)
        )
        described.append((identifier, abs(area - expected), matches))

    candidates = [entry for entry in described if entry[2]]
    # U3b, measured 2026-08-10: HF spread from the 506.707 mm² throat disc to
    # a 926.631 mm² derived rear cap.  Keeping every painted face would silently
    # create a second acoustic source in the STEP preparation path.
    keep = min(candidates, key=lambda entry: entry[1])[0] if candidates else None
    strip = tuple(identifier for identifier, _difference, _matches in described if identifier != keep)
    return TagVerdict(
        keep=keep,
        strip=strip,
        repaint=keep is None,
        candidates=tuple(identifier for identifier, _difference, matches in described if matches),
    )


def _enclosure_parameter_values(bundle: Bundle) -> dict[str, Parameter]:
    required = (
        "enc_x0",
        "enc_y0",
        "enc_w",
        "enc_h",
        "enc_z_front",
        "enc_depth",
        "enc_edge",
    )
    parameters = parameters_by_suffix(bundle)
    missing = [suffix for suffix in required if suffix not in parameters]
    if missing:
        slug = parameter_slug(bundle)
        raise WgLinkError(
            "enclosure link requires the realized placement parameters "
            + ", ".join(f"wg_{slug}_{suffix}" for suffix in missing)
            + "; this bundle predates wglink 1.1, so re-export it from WG "
            "rather than letting CAD guess where the cabinet sits"
        )
    return {suffix: parameters[suffix] for suffix in required}


def enclosure_plan(
    bundle: Bundle,
    parameter_prefix: str | None = None,
) -> EnclosurePlan | None:
    """Return the parameter-driven cabinet plan, never guessed placement."""

    if bundle.manifest["design"]["build_mode"] == "freestanding":
        return None
    treatment = bundle.manifest.get("enclosure")
    if not isinstance(treatment, Mapping):
        raise WgLinkError(
            "enclosure bundle has no edge treatment metadata; re-export it from WG "
            "rather than letting CAD guess fillet versus chamfer"
        )
    edge_type = treatment.get("edge_type")
    plan_type = treatment.get("plan_type")
    if isinstance(edge_type, bool) or edge_type not in (1, 2):
        raise WgLinkError(f"enclosure.edge_type must be 1 or 2, got {edge_type!r}")
    if isinstance(plan_type, bool) or plan_type != 1:
        raise WgLinkError(f"enclosure.plan_type must be 1, got {plan_type!r}")
    parameters = _enclosure_parameter_values(bundle)
    if parameter_prefix is not None:
        parameters = effective_parameters(bundle, parameter_prefix)
        missing = [
            suffix
            for suffix in ("enc_x0", "enc_y0", "enc_w", "enc_h", "enc_z_front", "enc_depth", "enc_edge")
            if suffix not in parameters
        ]
        if missing:
            raise WgLinkError(
                "enclosure link is missing placement parameter suffixes: "
                + ", ".join(missing)
            )
    x0 = parameters["enc_x0"]
    y0 = parameters["enc_y0"]
    width = parameters["enc_w"]
    height = parameters["enc_h"]
    front = parameters["enc_z_front"]
    depth = parameters["enc_depth"]
    edge = parameters["enc_edge"]
    back_extent = depth.value - front.value
    return EnclosurePlan(
        front_extent_expression=front.name,
        back_extent_expression=f"{depth.name} - {front.name}",
        edge_expression=edge.name,
        edge_type=edge_type,
        plan_type=plan_type,
        rectangle_mm=(
            x0.value,
            y0.value,
            x0.value + width.value,
            y0.value + height.value,
        ),
        front_extent_mm=front.value,
        back_extent_mm=back_extent,
        edge_mm=edge.value,
    )


def format_measurement_mm(value: object, *, digits: int = 4) -> str:
    """Format a measured distance without turning missing evidence into zero."""

    if value is None:
        return "not measured"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "not measured"
    if not math.isfinite(number):
        return "not measured"
    return f"{number:.{digits}f} mm"


def transform_points(
    points: Sequence[Sequence[float]],
    matrix: Sequence[Sequence[float]],
) -> list[list[float]]:
    """Transform link-local millimetres into the root assembly frame."""

    rows = [list(row) for row in matrix]
    if len(rows) != 4 or any(len(row) != 4 for row in rows):
        raise WgLinkError(f"assembly transform must be 4x4, got {matrix!r}")
    result = []
    for index, point in enumerate(points):
        if len(point) != 3:
            raise WgLinkError(f"check point {index} must have 3 coordinates")
        xyz = [_finite_number(value, label=f"check point {index}") for value in point]
        result.append(
            [
                sum(float(rows[axis][column]) * xyz[column] for column in range(3))
                + float(rows[axis][3])
                for axis in range(3)
            ]
        )
    return result


def refreshed_body_evidence(
    local_state: str,
    stored_fingerprint: str | None,
    rebuilt_fingerprint: str,
) -> tuple[str, str | None]:
    """Advance the baseline only when CAD says the managed body was untouched."""

    state = str(local_state)
    if state == "unmodified":
        return state, str(rebuilt_fingerprint)
    return state, stored_fingerprint


def throat_area_mm2(bundle: Bundle) -> float:
    """Return the realized circular source-disc area from its interface API."""

    matches = [
        parameter
        for parameter in interface_parameters(bundle)
        if parameter.name.endswith("_throat_dia")
    ]
    if len(matches) != 1:
        raise WgLinkError(
            "link requires exactly one '*_throat_dia' interface parameter; "
            f"found {len(matches)}"
        )
    diameter = matches[0].value
    if diameter <= 0.0:
        raise WgLinkError(
            f"interface parameter {matches[0].name!r} must be positive, got {diameter}"
        )
    return math.pi * (diameter / 2.0) ** 2


def mm_to_internal(value_mm: float) -> float:
    """Convert link-local millimetres to Fusion's centimetre internal unit."""

    return _finite_number(value_mm, label="millimetre coordinate") / 10.0


__all__ = [
    "Bundle",
    "DEFAULT_LIMITS",
    "EnclosurePlan",
    "Limits",
    "LinkIdentity",
    "LinkState",
    "Parameter",
    "SectionPlan",
    "TAG_AREA_TOLERANCE",
    "TagVerdict",
    "WgLinkError",
    "attribute_payload",
    "effective_parameters",
    "enclosure_plan",
    "format_expression",
    "format_measurement_mm",
    "health_regressions",
    "interface_parameters",
    "instance_parameter_prefix",
    "link_state",
    "mm_to_internal",
    "parameter_slug",
    "plan_sections",
    "read_bundle",
    "refreshed_body_evidence",
    "rollback_target",
    "section_arc_positions",
    "tag_verdict",
    "throat_area_mm2",
    "transform_points",
    "validate_parameter_name",
]
