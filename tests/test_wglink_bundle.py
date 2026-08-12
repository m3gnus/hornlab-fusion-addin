"""Exercise the Fusion-free WGLink boundary with hostile synthetic bundles.

The production fixtures are useful measurements but are deliberately optional;
the security and policy contract must stay reproducible from tiny bundles made
inside each test's temporary directory.
"""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import stat
import sys
import unicodedata
import zipfile

import numpy as np
import pytest

from hornlab_mesher.grid_resample import normalized_arc_positions


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
from wglink_bundle import (  # noqa: E402
    DEFAULT_LIMITS,
    IDENTITY_MATRIX,
    TAG_AREA_TOLERANCE,
    Limits,
    WgLinkError,
    attribute_payload,
    enclosure_plan,
    effective_parameters,
    format_expression,
    fusion_matrix_to_mm,
    format_measurement_mm,
    health_regressions,
    interface_parameters,
    instance_parameter_prefix,
    link_state,
    mm_to_internal,
    parameter_slug,
    parameters_by_suffix,
    plan_sections,
    read_bundle,
    refreshed_body_evidence,
    rollback_target,
    section_arc_positions,
    tag_verdict,
    throat_area_mm2,
    transform_points,
    validate_parameter_name,
)


def _grid(*, n_phi: int = 4, n_length: int = 3) -> dict:
    points = []
    for phi in range(n_phi):
        angle = 2.0 * math.pi * phi / n_phi
        ray = []
        for station in range(n_length):
            radius = 10.0 + station * 5.0
            ray.append(
                [
                    radius * math.cos(angle),
                    80.0 + radius * math.sin(angle),
                    station * station * 10.0,
                ]
            )
        points.append(ray)
    return {
        "all_rings_planar": True,
        "build_mode": "enclosure",
        "check_points": [],
        "closed": True,
        "frame": "link-local",
        "has_outer_points": False,
        "inner_points": points,
        "n_length": n_length,
        "n_phi": n_phi,
        "outer_points": None,
        "ring_planar": [True] * n_length,
        "ring_z_mm": [float(station * station * 10.0) for station in range(n_length)],
        "units": "mm",
        # Informational and already applied to every point above.
        "vertical_offset_mm": 80.0,
        "wall_thickness_mm": 0.0,
    }


def _parameters(*, include_placement: bool = True) -> list[dict]:
    values = {
        "throat_dia": 25.399764,
        "mouth_w": 40.0,
        "mouth_h": 40.0,
        "depth": 40.0,
        "wall_t": 0.0,
        "vertical_offset": 80.0,
        "enc_w": 344.0,
        "enc_h": 579.0,
        "enc_depth": 280.0,
        "enc_edge": 18.0,
    }
    if include_placement:
        values.update({"enc_x0": -172.0, "enc_y0": -347.0, "enc_z_front": 94.77})
    return [
        {
            "name": f"wg_tiny_{name}",
            "role": "interface",
            "unit": "mm",
            "value": value,
        }
        for name, value in values.items()
    ] + [
        {
            "name": "wg_tiny_coverage_h",
            "role": "informational",
            "value": 48.5,
        }
    ]


def _manifest(*, include_placement: bool = True) -> dict:
    return {
        "bundle": {"id": "wgb_test"},
        "coordinate_system": {
            "handedness": "right",
            "length_unit": "mm",
            "matrix_convention": "row-major-local-to-parent",
            "step_from_design": [
                [1, 0, 0, 0],
                [0, 1, 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 1],
            ],
        },
        "datums": {
            "WG_MOUTH_OUTLINE_INNER": {
                "points_mm": [ray[-1] for ray in _grid()["inner_points"]]
            },
            "WG_THROAT_PLANE": {
                "origin_mm": [0.0, 80.0, 0.0],
                "normal": [0.0, 0.0, 1.0],
            }
        },
        "enclosure": {"edge_type": 2, "plan_type": 1},
        "design": {
            "build_mode": "enclosure",
            "config": {"formula": "R-OSSE", "R": {"value": 360, "raw": "180*2"}},
            "formula": "r-osse",
            "id": "wgd_test",
            "lineage_id": "wgl_test",
        },
        "export": {
            "geometry_hash": "sha256:geometry",
            "id": "wge_test_3",
            "sequence": 3,
        },
        "files": {},
        "parameters": _parameters(include_placement=include_placement),
        "required_features": ["checksummed-files-v1", "link-local-frame-v1"],
        "wglink_version": "1.0",
    }


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_bundle(
    path: Path,
    *,
    grid: dict | None = None,
    manifest: dict | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    path.mkdir()
    grid_bytes = (
        json.dumps(grid or _grid(), allow_nan=True, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    files = {
        "point-grid.json": grid_bytes,
        "waveguide.step": b"ISO-10303-21;\nEND-ISO-10303-21;\n",
    }
    files.update(extra_files or {})
    payload = manifest or _manifest()
    payload["files"] = {
        name: {"sha256": _digest(data), "size_bytes": len(data)}
        for name, data in files.items()
    }
    for name, data in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (path / "wglink.json").write_text(
        json.dumps(payload, allow_nan=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _rewrite_manifest(path: Path, manifest: dict) -> None:
    (path / "wglink.json").write_text(
        json.dumps(manifest, allow_nan=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rewrite_grid(path: Path, grid: dict) -> None:
    data = json.dumps(grid, allow_nan=True, sort_keys=True).encode("utf-8") + b"\n"
    (path / "point-grid.json").write_bytes(data)
    manifest = json.loads((path / "wglink.json").read_text(encoding="utf-8"))
    manifest["files"]["point-grid.json"].update(
        {"sha256": _digest(data), "size_bytes": len(data)}
    )
    _rewrite_manifest(path, manifest)


def _zip_directory(source: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    return target


def test_bundle_module_imports_only_the_standard_library():
    source = ROOT / "fusion-addins" / "WGLink" / "wglink_bundle.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])

    assert imported <= sys.stdlib_module_names | {"__future__"}


def test_reads_valid_directory_and_resolves_identity(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))

    assert bundle.root == tmp_path / "tiny.wglink"
    assert bundle.grid["n_phi"] == 4
    assert bundle.identity.design_id == "wgd_test"
    assert bundle.identity.export_sequence == 3


def test_accepts_newer_minor_version(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["wglink_version"] = "1.91"
    _rewrite_manifest(path, manifest)

    assert read_bundle(path).manifest["wglink_version"] == "1.91"


def test_rejects_unknown_major_version(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["wglink_version"] = "2.0"
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match=r"wglink_version '2\.0'.*major 1"):
        read_bundle(path)


def test_rejects_unknown_required_feature(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["required_features"].extend(["future-z", "future-a"])
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match=r"future-a.*future-z"):
        read_bundle(path)


def test_rejects_nonfinite_manifest_json(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["body"] = {"volume_mm3": math.inf}
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match="Infinity"):
        read_bundle(path)


def test_rejects_overflowed_json_number_in_unused_metadata(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["body"] = {"volume_mm3": "OVERFLOW"}
    text = json.dumps(manifest, sort_keys=True).replace('"OVERFLOW"', "1e999")
    (path / "wglink.json").write_text(text + "\n", encoding="utf-8")

    with pytest.raises(WgLinkError, match=r"non-finite.*body\.volume_mm3"):
        read_bundle(path)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_rejects_each_nonfinite_point_grid_constant(tmp_path, value):
    path = _write_bundle(tmp_path / "tiny.wglink")
    grid = _grid()
    grid["check_points"] = [[value, 0.0, 0.0]]
    _rewrite_grid(path, grid)

    with pytest.raises(WgLinkError, match="non-finite"):
        read_bundle(path)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("/absolute.step", "absolute"),
        ("nested/../escape.step", r"contains '\.\.'"),
        (r"nested\evil.step", "backslash"),
        ("nested//empty.step", "empty segment"),
    ],
)
def test_rejects_unsafe_manifest_file_names(tmp_path, name, message):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["files"][name] = {"sha256": _digest(b""), "size_bytes": 0}
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match=message):
        read_bundle(path)


def test_rejects_manifest_name_that_resolves_outside_root(tmp_path, monkeypatch):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["files"]["escape.step"] = {"sha256": _digest(b""), "size_bytes": 0}
    _rewrite_manifest(path, manifest)
    original_resolve = Path.resolve

    def redirected_resolve(self, *args, **kwargs):
        if self == path / "escape.step":
            return tmp_path / "outside.step"
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    with pytest.raises(WgLinkError, match="resolves outside.*escape.step"):
        read_bundle(path)


def test_rejects_unicode_casefold_file_collision(tmp_path):
    composed = "caf\N{LATIN SMALL LETTER E WITH ACUTE}.bin"
    decomposed = unicodedata.normalize("NFD", composed).upper()
    path = _write_bundle(
        tmp_path / "tiny.wglink",
        extra_files={composed: b"one", decomposed: b"two"},
    )

    with pytest.raises(WgLinkError, match=r"collide.*caf"):
        read_bundle(path)


def test_rejects_real_file_missing_from_files_table(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    (path / "nested").mkdir()
    (path / "nested" / "swap.step").write_bytes(b"not declared")

    with pytest.raises(WgLinkError, match=r"not listed.*nested/swap\.step"):
        read_bundle(path)


def test_rejects_symlink_bundle_root(tmp_path):
    real = _write_bundle(tmp_path / "real.wglink")
    link = tmp_path / "linked.wglink"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(WgLinkError, match="bundle path is a symlink"):
        read_bundle(link)


def test_rejects_symlink_directory_inside_bundle(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    (tmp_path / "outside").mkdir()
    (path / "linked-dir").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(WgLinkError, match="symlink.*linked-dir"):
        read_bundle(path)


def test_rejects_symlink_file_inside_bundle(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    (path / "linked.step").symlink_to(path / "waveguide.step")

    with pytest.raises(WgLinkError, match="symlink.*linked.step"):
        read_bundle(path)


def test_rejects_sha256_mismatch_and_names_file(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    (path / "waveguide.step").write_bytes(b"same size but corrupt..............")
    manifest = json.loads((path / "wglink.json").read_text())
    size = (path / "waveguide.step").stat().st_size
    manifest["files"]["waveguide.step"]["size_bytes"] = size
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match=r"sha256 mismatch.*waveguide\.step"):
        read_bundle(path)


def test_rejects_manifest_per_file_size_cap_before_hashing(tmp_path, monkeypatch):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["files"]["point-grid.json"]["size_bytes"] = 5_001
    _rewrite_manifest(path, manifest)
    monkeypatch.setattr("wglink_bundle._sha256", lambda _path: pytest.fail("hashed"))

    with pytest.raises(WgLinkError, match=r"size_bytes.*point-grid\.json.*limit"):
        read_bundle(path, limits=replace(DEFAULT_LIMITS, max_file_bytes=5_000))


def test_rejects_filesystem_bundle_size_cap_before_hashing(tmp_path, monkeypatch):
    path = _write_bundle(tmp_path / "tiny.wglink")
    monkeypatch.setattr("wglink_bundle._sha256", lambda _path: pytest.fail("hashed"))
    limit = sum(item.stat().st_size for item in path.iterdir()) - 1

    with pytest.raises(WgLinkError, match="bundle files total.*limit"):
        read_bundle(
            path,
            limits=replace(
                DEFAULT_LIMITS,
                max_file_bytes=10_000,
                max_bundle_bytes=limit,
            ),
        )


def test_rejects_manifest_file_count_cap(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")

    with pytest.raises(WgLinkError, match="more than 1 entries"):
        read_bundle(path, limits=replace(DEFAULT_LIMITS, max_files=1))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("length_unit", "inch", "length_unit.*inch"),
        ("handedness", "left", "handedness.*left"),
        ("matrix_convention", "column-major", "matrix_convention.*column-major"),
    ],
)
def test_rejects_wrong_coordinate_system_contract(tmp_path, field, value, message):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["coordinate_system"][field] = value
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match=message):
        read_bundle(path)


def test_rejects_nonfinite_step_matrix(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["coordinate_system"]["step_from_design"][2][1] = math.nan
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match="NaN"):
        read_bundle(path)


def test_rejects_malformed_step_matrix(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["coordinate_system"]["step_from_design"][2] = [0, 0, 1]
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match="row 2.*4 values"):
        read_bundle(path)


def test_rejects_nonidentity_step_placement(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["coordinate_system"]["step_from_design"][1][3] = 80
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match="Phase 1.*identity"):
        read_bundle(path)


@pytest.mark.parametrize("mode", ["bare", "infinite_baffle", "surface"])
def test_rejects_out_of_scope_build_modes(tmp_path, mode):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    manifest["design"]["build_mode"] = mode
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match=rf"{mode}.*§5\.1.*bare.*infinite_baffle"):
        read_bundle(path)


@pytest.mark.parametrize(
    ("section", "field"),
    [("design", "id"), ("export", "id"), ("export", "sequence")],
)
def test_rejects_missing_link_identity(tmp_path, section, field):
    path = _write_bundle(tmp_path / "tiny.wglink")
    manifest = json.loads((path / "wglink.json").read_text())
    del manifest[section][field]
    _rewrite_manifest(path, manifest)

    with pytest.raises(WgLinkError, match="not exported from Waveguide Generator"):
        read_bundle(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("frame", "design-local", "frame.*design-local"),
        ("units", "cm", "units.*cm"),
        ("closed", False, "closed must be true"),
        ("build_mode", "freestanding", "does not match manifest"),
        ("n_phi", 2, "n_phi must be at least 3"),
        ("n_length", 1, "n_length must be at least 2"),
    ],
)
def test_rejects_invalid_grid_header_fields(tmp_path, field, value, message):
    path = _write_bundle(tmp_path / "tiny.wglink")
    grid = _grid()
    grid[field] = value
    _rewrite_grid(path, grid)

    with pytest.raises(WgLinkError, match=message):
        read_bundle(path)


def test_rejects_wrong_inner_point_grid_shape(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    grid = _grid()
    grid["inner_points"][1][1] = [1.0, 2.0]
    _rewrite_grid(path, grid)

    with pytest.raises(WgLinkError, match=r"inner_points\[1\]\[1\].*xyz triple"):
        read_bundle(path)


def test_rejects_nonfinite_inner_coordinate_even_if_decoder_never_sees_constant(
    tmp_path,
):
    path = _write_bundle(tmp_path / "tiny.wglink")
    grid = _grid()
    grid["inner_points"][0][0][0] = 10**1000
    _rewrite_grid(path, grid)

    with pytest.raises(WgLinkError, match=r"inner_points\[0\]\[0\]\[0\].*finite"):
        read_bundle(path)


@pytest.mark.parametrize("field", ["ring_z_mm", "ring_planar"])
def test_rejects_wrong_ring_metadata_length(tmp_path, field):
    path = _write_bundle(tmp_path / "tiny.wglink")
    grid = _grid()
    grid[field].pop()
    _rewrite_grid(path, grid)

    with pytest.raises(WgLinkError, match=rf"{field}.*3"):
        read_bundle(path)


def test_rejects_outer_points_present_when_flag_is_false(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    grid = _grid()
    grid["outer_points"] = grid["inner_points"]
    _rewrite_grid(path, grid)

    with pytest.raises(WgLinkError, match="has_outer_points is false"):
        read_bundle(path)


def test_rejects_missing_outer_points_when_flag_is_true(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    grid = _grid()
    grid["has_outer_points"] = True
    _rewrite_grid(path, grid)

    with pytest.raises(WgLinkError, match="outer_points must have shape"):
        read_bundle(path)


def test_rejects_wrong_outer_point_grid_shape(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    grid = _grid()
    grid["has_outer_points"] = True
    grid["outer_points"] = grid["inner_points"][:-1]
    _rewrite_grid(path, grid)

    with pytest.raises(WgLinkError, match="outer_points must have shape 4x3x3"):
        read_bundle(path)


def test_directory_and_zip_accept_identically(tmp_path):
    directory = _write_bundle(tmp_path / "tiny-dir.wglink")
    archive = _zip_directory(directory, tmp_path / "tiny-zip.wglink")

    from_directory = read_bundle(directory)
    from_zip = read_bundle(archive, temp_dir=tmp_path / "extract")

    assert from_zip.manifest == from_directory.manifest
    assert from_zip.grid == from_directory.grid
    assert from_zip.root != directory
    assert from_zip.source == archive
    from_zip.close()


def test_zip_context_cleans_owned_extraction_on_success_and_exception(tmp_path):
    directory = _write_bundle(tmp_path / "tiny-dir.wglink")
    archive = _zip_directory(directory, tmp_path / "tiny-zip.wglink")
    extraction_parent = tmp_path / "extract"

    with read_bundle(archive, temp_dir=extraction_parent) as bundle:
        extracted = bundle.root
        assert extracted.is_dir()
    assert not extracted.exists()

    with pytest.raises(RuntimeError, match="caller failed"):
        with read_bundle(archive, temp_dir=extraction_parent) as bundle:
            failed_root = bundle.root
            raise RuntimeError("caller failed")
    assert not failed_root.exists()


def test_directory_hash_and_grid_parse_use_the_same_open_descriptor(
    tmp_path, monkeypatch
):
    path = _write_bundle(tmp_path / "tiny.wglink")
    original_verify = sys.modules["wglink_bundle"]._verify_hashes

    def verify_then_replace(records, actual):
        original_verify(records, actual)
        replacement = path / "replacement.json"
        replacement.write_text("{}\n", encoding="utf-8")
        os.replace(replacement, path / "point-grid.json")

    monkeypatch.setattr("wglink_bundle._verify_hashes", verify_then_replace)

    bundle = read_bundle(path)
    assert bundle.grid["n_phi"] == 4


def test_directory_walk_counts_directories_and_bounds_depth(tmp_path):
    path = _write_bundle(tmp_path / "tiny.wglink")
    cursor = path
    for index in range(6):
        cursor = cursor / f"d{index}"
        cursor.mkdir()

    with pytest.raises(WgLinkError, match="more than 5 entries"):
        read_bundle(path, limits=replace(DEFAULT_LIMITS, max_files=5))
    with pytest.raises(WgLinkError, match="depth limit 3"):
        read_bundle(
            path,
            limits=replace(DEFAULT_LIMITS, max_files=20, max_depth=3),
        )


def test_directory_and_zip_reject_corruption_identically(tmp_path):
    directory = _write_bundle(tmp_path / "tiny-dir.wglink")
    (directory / "waveguide.step").write_bytes(b"corrupt")
    archive = _zip_directory(directory, tmp_path / "tiny-zip.wglink")

    for path in (directory, archive):
        with pytest.raises(WgLinkError, match="size mismatch.*waveguide.step"):
            read_bundle(path, temp_dir=tmp_path / "extract")


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("/absolute", "absolute"),
        ("../escape", r"contains '\.\.'"),
        (r"folder\evil", "backslash"),
    ],
)
def test_zip_rejects_unsafe_member_names_before_writing(tmp_path, name, message):
    archive = tmp_path / "bad.wglink"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"bad")

    with pytest.raises(WgLinkError, match=message):
        read_bundle(archive, temp_dir=tmp_path / "extract")
    assert not list((tmp_path / "extract").glob("wglink-*"))


def test_zip_rejects_empty_member_segment(tmp_path):
    archive = tmp_path / "bad.wglink"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("one//two", b"bad")

    with pytest.raises(WgLinkError, match="empty segment"):
        read_bundle(archive, temp_dir=tmp_path / "extract")


def test_zip_rejects_unicode_casefold_member_collision(tmp_path):
    archive = tmp_path / "bad.wglink"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Payload.bin", b"one")
        output.writestr("payload.BIN", b"two")

    with pytest.raises(WgLinkError, match="collide.*Payload.bin.*payload.BIN"):
        read_bundle(archive, temp_dir=tmp_path / "extract")


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        (stat.S_IFLNK | 0o777, "symlink"),
        (stat.S_IFCHR | 0o666, "device or special"),
    ],
)
def test_zip_rejects_symlink_and_device_members(tmp_path, mode, message):
    archive = tmp_path / "bad.wglink"
    info = zipfile.ZipInfo("bad-entry")
    info.create_system = 3
    info.external_attr = mode << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, b"target")

    with pytest.raises(WgLinkError, match=message):
        read_bundle(archive, temp_dir=tmp_path / "extract")


def test_zip_rejects_member_count_cap(tmp_path):
    archive = tmp_path / "bad.wglink"
    with zipfile.ZipFile(archive, "w") as output:
        for index in range(4):
            output.writestr(f"file-{index}", b"x")

    with pytest.raises(WgLinkError, match="4 members; limit is 2"):
        read_bundle(
            archive,
            temp_dir=tmp_path / "extract",
            limits=Limits(max_files=2),
        )


def test_zip_counts_implicit_directories_before_extraction(tmp_path):
    archive = tmp_path / "nested.wglink"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("one/two/three/payload.bin", b"x")
    extraction_parent = tmp_path / "extract"

    with pytest.raises(WgLinkError, match="more than 3 logical entries"):
        read_bundle(
            archive,
            temp_dir=extraction_parent,
            limits=replace(DEFAULT_LIMITS, max_files=3),
        )
    assert not list(extraction_parent.glob("wglink-*"))


def test_zip_rejects_declared_uncompressed_file_size_cap(tmp_path):
    archive = tmp_path / "bad.wglink"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("large.bin", b"0123456789")

    with pytest.raises(WgLinkError, match="large.bin.*10 bytes.*limit is 9"):
        read_bundle(
            archive,
            temp_dir=tmp_path / "extract",
            limits=Limits(max_file_bytes=9),
        )


def test_zip_rejects_declared_uncompressed_bundle_size_cap(tmp_path):
    archive = tmp_path / "bad.wglink"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("one.bin", b"123456")
        output.writestr("two.bin", b"123456")

    with pytest.raises(WgLinkError, match="declare 12 bytes; limit is 11"):
        read_bundle(
            archive,
            temp_dir=tmp_path / "extract",
            limits=Limits(max_file_bytes=10, max_bundle_bytes=11),
        )


def test_zip_rejects_decompression_ratio_above_200(tmp_path):
    archive = tmp_path / "bomb.wglink"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("bomb.bin", b"0" * 20_000)

    with pytest.raises(WgLinkError, match=r"bomb.bin.*ratio.*200x"):
        read_bundle(archive, temp_dir=tmp_path / "extract")


def test_zip_rejects_actual_expansion_past_declared_size(tmp_path, monkeypatch):
    archive = tmp_path / "lying.wglink"
    archive.write_bytes(b"not consulted by the fake archive")
    info = zipfile.ZipInfo("wglink.json")
    info.file_size = 1
    info.compress_size = 1

    class LyingArchive:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def infolist(self):
            return [info]

        def open(self, _info, _mode):
            return BytesIO(b"two bytes")

    monkeypatch.setattr("wglink_bundle.zipfile.is_zipfile", lambda _path: True)
    monkeypatch.setattr("wglink_bundle.zipfile.ZipFile", LyingArchive)

    with pytest.raises(WgLinkError, match="expanded beyond.*declared"):
        read_bundle(archive, temp_dir=tmp_path / "extract")


def test_format_expression_preserves_shortest_exact_decimal_without_exponents():
    assert [
        format_expression(value)
        for value in (0, 344.0, 25.399764, -347.0, 1e-7, 1e12)
    ] == [
        "0.0 mm",
        "344.0 mm",
        "25.399764 mm",
        "-347.0 mm",
        "0.0000001 mm",
        "1000000000000.0 mm",
    ]


@pytest.mark.parametrize("name", ["1_bad", "wg-bad", "wg bad", ""])
def test_validate_parameter_name_refuses_non_identifiers(name):
    with pytest.raises(WgLinkError, match=repr(name)):
        validate_parameter_name(name)


def test_interface_parameters_filters_and_formats(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))
    parameters = interface_parameters(bundle)

    assert len(parameters) == 13
    assert all(parameter.role == "interface" for parameter in parameters)
    assert parameters[0].expression == "25.399764 mm"


def test_plan_sections_reference_counts_caps_endpoints_and_determinism():
    asro = plan_sections(168, 108)
    tritonia = plan_sections(60, 65)

    assert (asro.phi_stride, asro.points_per_ring, asro.ring_stride, asro.sections) == (
        4,
        42,
        8,
        15,
    )
    assert (tritonia.phi_stride, tritonia.points_per_ring) == (1, 60)
    assert tritonia.sections == 17
    for n_phi, n_length in ((3, 2), (99, 3), (176, 125), (4097, 1000)):
        first = plan_sections(n_phi, n_length, max_points=64, max_sections=24)
        second = plan_sections(n_phi, n_length, max_points=64, max_sections=24)
        assert first == second
        assert first.ring_indices[0] == 0
        assert first.ring_indices[-1] == n_length - 1
        assert first.points_per_ring <= 64
        assert 2 <= first.sections <= 24


def test_curvature_section_plan_selects_a_sharp_knee_deterministically():
    n_phi = 12
    n_length = 9
    points = []
    for ray in range(n_phi):
        offset = float(ray)
        points.append(
            [
                [float(station), offset, 0.0]
                if station <= 4
                else [4.0, offset, float(station - 4)]
                for station in range(n_length)
            ]
        )

    first = plan_sections(
        n_phi,
        n_length,
        inner_points=points,
        max_sections=5,
        chord_tolerance_mm=0.02,
    )
    second = plan_sections(
        n_phi,
        n_length,
        inner_points=points,
        max_sections=5,
        chord_tolerance_mm=0.02,
    )

    assert first == second
    assert 4 in first.ring_indices
    assert first.sections <= 5


def test_optional_tritonia_plan_retains_dense_mouth_cluster():
    fixtures = os.environ.get("WGLINK_TEST_BUNDLES")
    if not fixtures:
        pytest.skip("set WGLINK_TEST_BUNDLES to exercise production bundles")
    bundle = read_bundle(Path(fixtures) / "tritonia-base.wglink")

    plan = plan_sections(
        bundle.grid["n_phi"],
        bundle.grid["n_length"],
        inner_points=bundle.grid["inner_points"],
    )

    assert plan.sections <= 40
    assert plan.ring_indices[-1] - plan.ring_indices[-2] < 4


def test_section_arc_positions_equal_mesher_implementation():
    points = _grid(n_phi=9, n_length=7)["inner_points"]
    indices = [0, 2, 5, 6]

    expected = normalized_arc_positions(np.asarray(points))[indices]
    assert section_arc_positions(points, indices) == pytest.approx(expected)


def test_attribute_payload_stores_document_topology(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))
    plan = plan_sections(bundle.grid["n_phi"], bundle.grid["n_length"])

    attributes = attribute_payload(bundle, instance_id="instance-1", plan=plan)
    topology = json.loads(attributes["topology"])

    assert attributes["design_id"] == "wgd_test"
    assert attributes["export_sequence"] == "3"
    assert attributes["schema"] == "1"
    assert attributes["build_mode"] == "enclosure"
    assert attributes["slug"] == "tiny"
    assert attributes["bundle_path"] == str(bundle.root)
    assert attributes["design_hash"] == ""
    assert attributes["edit_version"] == ""
    assert attributes["design_name"] == ""
    assert attributes["formula"] == "r-osse"
    assert json.loads(attributes["config_json"]) == {
        "R": {"raw": "180*2", "value": 360},
        "formula": "R-OSSE",
    }
    assert "thicken_sign" not in attributes
    assert topology["has_outer"] is False
    assert topology["overshoot_mm"] == 0.0
    assert topology["point_count"] == 4
    assert topology["section_arc_positions"] == pytest.approx(
        normalized_arc_positions(np.asarray(bundle.grid["inner_points"]))
    )
    assert topology["sections"] == len(plan.ring_indices)
    assert topology["walls"] == 1


def test_parameters_by_suffix_does_not_confuse_depth_with_enc_depth(tmp_path):
    """MEASURED in Fusion: a '*_depth' suffix search matched two parameters.

    ``wg_tritonia_depth`` (throat plane to baffle) and ``wg_tritonia_enc_depth``
    (the cabinet's front-to-back size) both end in ``_depth``, so an unanchored
    suffix lookup found two and refused to insert the reference design at all.
    """

    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))

    indexed = parameters_by_suffix(bundle)

    assert indexed["depth"].name == "wg_tiny_depth"
    assert indexed["enc_depth"].name == "wg_tiny_enc_depth"
    assert indexed["depth"].value != indexed["enc_depth"].value
    # A parameter belonging to a DIFFERENT link in the same document-global
    # table must not leak into this link's index.
    assert all(not name.startswith("wg_") for name in indexed)


def test_parameter_slug_comes_from_the_canonical_interface_parameter(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))

    assert parameter_slug(bundle) == "tiny"


def test_instance_parameter_prefix_preserves_first_and_numbers_later_instances(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))

    first = instance_parameter_prefix("tiny", [])
    second = instance_parameter_prefix("tiny", [first])
    third = instance_parameter_prefix("tiny", [first, second])
    parameters = effective_parameters(bundle, second)

    assert (first, second, third) == ("wg_tiny_", "wg_tiny2_", "wg_tiny3_")
    assert parameters["depth"].name == "wg_tiny2_depth"
    assert parameters["enc_depth"].name == "wg_tiny2_enc_depth"


def test_freestanding_attribute_payload_requires_and_stores_thicken_sign(tmp_path):
    grid = _grid()
    grid["build_mode"] = "freestanding"
    grid["has_outer_points"] = True
    grid["outer_points"] = [
        [[coordinate * 1.1 for coordinate in point] for point in ray]
        for ray in grid["inner_points"]
    ]
    manifest = _manifest()
    manifest["design"].update(
        {
            "build_mode": "freestanding",
            "design_hash": "sha256:design",
            "edit_version": 42,
            "name": "Tiny Horn",
        }
    )
    bundle = read_bundle(
        _write_bundle(tmp_path / "free.wglink", grid=grid, manifest=manifest)
    )
    plan = plan_sections(bundle.grid["n_phi"], bundle.grid["n_length"])

    with pytest.raises(WgLinkError, match="requires thicken_sign"):
        attribute_payload(bundle, instance_id="instance-1", plan=plan)

    attributes = attribute_payload(
        bundle,
        instance_id="instance-1",
        plan=plan,
        thicken_sign=-1,
    )

    assert attributes["thicken_sign"] == "-1"
    assert attributes["design_hash"] == "sha256:design"
    assert attributes["edit_version"] == "42"
    assert attributes["design_name"] == "Tiny Horn"

    one_wall = attribute_payload(
        bundle,
        instance_id="instance-2",
        plan=plan,
        walls=1,
        thicken_sign=1,
    )
    assert json.loads(one_wall["topology"])["has_outer"] is False
    assert json.loads(one_wall["topology"])["walls"] == 1


def test_enclosure_attribute_payload_rejects_thicken_sign(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))
    plan = plan_sections(bundle.grid["n_phi"], bundle.grid["n_length"])

    with pytest.raises(WgLinkError, match="only valid for a freestanding"):
        attribute_payload(
            bundle,
            instance_id="instance-1",
            plan=plan,
            thicken_sign=1,
        )


def test_rollback_target_skips_assembly_occurrences_and_sketches():
    entries = [
        {"index": 0, "kind": "Occurrence"},
        {"index": 1, "kind": "Occurrence"},
        {"index": 2, "kind": "Sketch"},
        {"index": 3, "kind": "Sketch"},
        {"index": 4, "kind": "LoftFeature"},
        {"index": 5, "kind": "PatchFeature"},
    ]

    assert rollback_target(entries) == 4


def test_rollback_target_returns_none_without_a_dependent_feature():
    assert rollback_target(
        [{"index": 0, "kind": "Occurrence"}, {"index": 1, "kind": "Sketch"}]
    ) is None


def test_rollback_target_treats_unreadable_entry_as_a_safe_refusal_boundary():
    assert rollback_target(
        [{"index": 0, "kind": "Sketch"}, {"index": 1, "kind": None}]
    ) is None


def test_rollback_target_is_scoped_after_this_instances_last_sketch():
    entries = [
        {"index": 0, "kind": "Sketch"},
        {"index": 1, "kind": "LoftFeature"},
        {"index": 5, "kind": "Sketch"},
        {"index": 6, "kind": None},
        {"index": 7, "kind": "Sketch"},
        {"index": 8, "kind": "LoftFeature"},
    ]

    assert rollback_target(entries, after_index=5) == 8


@pytest.mark.parametrize(
    ("stored", "verdict"),
    [
        ({}, "no_link"),
        (
            {
                "design_id": "wgd_other",
                "lineage_id": "wgl_test",
                "export_id": "old",
                "export_sequence": "1",
            },
            "different_design",
        ),
        (
            {
                "design_id": "wgd_other",
                "lineage_id": "wgl_other",
                "export_id": "old",
                "export_sequence": "1",
            },
            "different_lineage",
        ),
        (
            {
                "design_id": "wgd_test",
                "lineage_id": "wgl_test",
                "export_id": "wge_test_3",
                "export_sequence": "3",
            },
            "same_export",
        ),
        (
            {
                "design_id": "wgd_test",
                "lineage_id": "wgl_test",
                "export_id": "old",
                "export_sequence": "2",
            },
            "newer_export",
        ),
        (
            {
                "design_id": "wgd_test",
                "lineage_id": "wgl_test",
                "export_id": "future",
                "export_sequence": "4",
            },
            "older_export",
        ),
    ],
)
def test_link_state_all_six_verdicts(tmp_path, stored, verdict):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))

    state = link_state(stored, bundle)

    assert state.verdict == verdict
    assert state.bundle_sequence == 3


def test_link_state_refuses_same_sequence_with_different_export_identity(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))

    state = link_state(
        {
            "design_id": "wgd_test",
            "lineage_id": "wgl_test",
            "export_id": "wge_other",
            "export_sequence": "3",
            "geometry_hash": "sha256:other",
        },
        bundle,
    )

    assert state.verdict == "corrupt_export"
    assert state.stored_export_id == "wge_other"
    assert state.bundle_export_id == "wge_test_3"


def test_health_regressions_include_unhealthy_and_disappeared_features():
    before = [
        {"name": "Loft", "type": "LoftFeature", "health": "Healthy"},
        {"name": "Cut", "type": "CombineFeature", "health": "Healthy"},
        {"name": "Already bad", "type": "Sketch", "health": "Error"},
    ]
    after = [
        {"name": "Loft", "type": "LoftFeature", "health": "Warning"},
        {"name": "Already bad", "type": "Sketch", "health": "Healthy"},
    ]

    assert health_regressions(before, after) == [
        {
            "name": "Loft",
            "type": "LoftFeature",
            "health": "Warning",
            "before_health": "Healthy",
        },
        {
            "name": "Cut",
            "type": "CombineFeature",
            "health": "Missing",
            "before_health": "Healthy",
        },
    ]


def test_tag_verdict_strips_u3b_appearance_spread():
    verdict = tag_verdict(
        [
            {
                "id": "throat",
                "planar": True,
                "at_throat_plane": True,
                "area_mm2": 506.707,
            },
            {
                "id": "rear-cap",
                "planar": True,
                "at_throat_plane": False,
                "area_mm2": 926.631,
            },
        ],
        expected_area_mm2=506.707,
        tolerance=0.01,
    )

    assert verdict.keep == "throat"
    assert verdict.strip == ("rear-cap",)
    assert verdict.repaint is False


def test_tag_verdict_requests_repaint_when_tag_is_missing():
    verdict = tag_verdict([], expected_area_mm2=506.707, tolerance=0.01)

    assert verdict.keep is None
    assert verdict.strip == ()
    assert verdict.repaint is True


def test_tag_verdict_strips_wrong_area_single_face_and_repaints():
    verdict = tag_verdict(
        [
            {
                "id": "spread-cap",
                "planar": True,
                "at_throat_plane": True,
                "area_mm2": 926.631,
            }
        ],
        expected_area_mm2=506.707,
        tolerance=0.01,
    )

    assert verdict.keep is None
    assert verdict.strip == ("spread-cap",)
    assert verdict.repaint is True


def test_tag_verdict_exposes_the_one_shared_tolerance_candidate_set():
    verdict = tag_verdict(
        [
            {"id": "inside", "planar": True, "at_throat_plane": True, "area_mm2": 99.01},
            {"id": "outside", "planar": True, "at_throat_plane": True, "area_mm2": 98.99},
        ],
        expected_area_mm2=100.0,
        tolerance=TAG_AREA_TOLERANCE,
    )

    assert verdict.candidates == ("inside",)


def test_enclosure_plan_carries_parameter_expressions_and_numeric_checks(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))

    plan = enclosure_plan(bundle)

    assert plan is not None
    assert plan.front_extent_expression == "wg_tiny_enc_z_front"
    assert plan.back_extent_expression == (
        "wg_tiny_enc_depth - wg_tiny_enc_z_front"
    )
    assert plan.edge_expression == "wg_tiny_enc_edge"
    assert plan.edge_type == 2
    assert plan.plan_type == 1
    assert plan.rectangle_mm == pytest.approx((-172.0, -347.0, 172.0, 232.0))
    assert plan.front_extent_mm == pytest.approx(94.77)
    assert plan.back_extent_mm == pytest.approx(185.23)


def test_enclosure_plan_refuses_missing_absolute_placement_parameter(tmp_path):
    path = _write_bundle(
        tmp_path / "tiny.wglink",
        manifest=_manifest(include_placement=False),
    )

    with pytest.raises(WgLinkError, match=r"wg_tiny_enc_x0.*re-export it from WG"):
        enclosure_plan(read_bundle(path))


def test_enclosure_plan_refuses_legacy_bundle_without_edge_treatment(tmp_path):
    manifest = _manifest()
    del manifest["enclosure"]
    path = _write_bundle(tmp_path / "legacy.wglink", manifest=manifest)

    with pytest.raises(WgLinkError, match="re-export.*fillet versus chamfer"):
        enclosure_plan(read_bundle(path))


def test_enclosure_plan_reads_default_fillet_treatment(tmp_path):
    manifest = _manifest()
    manifest["enclosure"]["edge_type"] = 1
    path = _write_bundle(tmp_path / "fillet.wglink", manifest=manifest)

    assert enclosure_plan(read_bundle(path)).edge_type == 1


def test_enclosure_plan_is_none_for_freestanding(tmp_path):
    grid = _grid()
    grid["build_mode"] = "freestanding"
    grid["has_outer_points"] = True
    grid["outer_points"] = [
        [[[coordinate * 1.1 for coordinate in point] for point in ray]][0]
        for ray in grid["inner_points"]
    ]
    manifest = _manifest()
    manifest["design"]["build_mode"] = "freestanding"
    bundle = read_bundle(
        _write_bundle(tmp_path / "tiny.wglink", grid=grid, manifest=manifest)
    )

    assert enclosure_plan(bundle) is None


def test_mm_to_internal_is_the_single_mm_to_fusion_cm_boundary():
    assert mm_to_internal(25.399764) == pytest.approx(2.5399764)
    assert mm_to_internal(-347.0) == pytest.approx(-34.7)


def test_missing_deviation_formats_as_not_measured_instead_of_zero():
    assert format_measurement_mm(None) == "not measured"
    assert format_measurement_mm(0.125) == "0.1250 mm"


def test_link_local_check_points_transform_to_root_assembly_frame():
    matrix = [
        [1.0, 0.0, 0.0, 300.0],
        [0.0, 1.0, 0.0, -20.0],
        [0.0, 0.0, 1.0, 5.0],
        [0.0, 0.0, 0.0, 1.0],
    ]

    assert transform_points([[1.0, 2.0, 3.0]], matrix) == [[301.0, -18.0, 8.0]]


def test_body_evidence_preserves_modified_state_and_advances_clean_baseline():
    assert refreshed_body_evidence("modified", "old", "rebuilt") == (
        "modified",
        "old",
    )
    assert refreshed_body_evidence("unmodified", "old", "rebuilt") == (
        "unmodified",
        "rebuilt",
    )


def test_throat_area_uses_the_realized_interface_diameter(tmp_path):
    bundle = read_bundle(_write_bundle(tmp_path / "tiny.wglink"))

    assert throat_area_mm2(bundle) == pytest.approx(math.pi * (25.399764 / 2.0) ** 2)


def test_optional_real_fixtures_share_one_placed_link_local_frame():
    fixtures = os.environ.get("WGLINK_TEST_BUNDLES")
    if not fixtures:
        pytest.skip("set WGLINK_TEST_BUNDLES to exercise production bundles")
    root = Path(fixtures)
    for name in ("tritonia-base.wglink", "tritonia-changed.wglink"):
        bundle = read_bundle(root / name)
        throat_y = bundle.manifest["datums"]["WG_THROAT_PLANE"]["origin_mm"][1]
        throat_ring_y = [ray[0][1] for ray in bundle.grid["inner_points"]]
        # The grid already contains the 80 mm placement: its throat-ring centre
        # and datum agree. Adding vertical_offset_mm again would move it to 160.
        assert 0.5 * (min(throat_ring_y) + max(throat_ring_y)) == pytest.approx(
            throat_y
        )
        assert throat_y == pytest.approx(80.0)


def test_fusion_matrix_translation_is_converted_from_centimetres():
    """The one place a Fusion matrix crosses into the contract's frame.

    `Matrix3D.asArray()` is row-major with a CENTIMETRE translation column,
    while D1 says every outbound coordinate is millimetres. Emitting the raw
    array mixes both in one matrix and is silent while the wrapper sits at the
    origin.
    """

    rotation_30_deg = [
        0.8660254, -0.5, 0.0, 30.0,   # 30 cm  -> 300 mm
        0.5, 0.8660254, 0.0, -20.0,   # -20 cm -> -200 mm
        0.0, 0.0, 1.0, 1.0,           # 1 cm   -> 10 mm
        0.0, 0.0, 0.0, 1.0,
    ]

    rows = fusion_matrix_to_mm(rotation_30_deg)

    assert [row[3] for row in rows] == pytest.approx([300.0, -200.0, 10.0, 1.0])
    # The rotation block is dimensionless and must pass through untouched.
    assert rows[0][:3] == pytest.approx([0.8660254, -0.5, 0.0])
    assert rows[1][:3] == pytest.approx([0.5, 0.8660254, 0.0])
    assert rows[3] == [0.0, 0.0, 0.0, 1.0]


def test_fusion_matrix_identity_round_trips_unchanged():
    assert fusion_matrix_to_mm(
        [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    ) == [list(row) for row in IDENTITY_MATRIX]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([1.0] * 15, "16 values"),
        ([1.0] * 16, r"last row"),
    ],
)
def test_fusion_matrix_refuses_malformed_transforms(values, message):
    with pytest.raises(WgLinkError, match=message):
        fusion_matrix_to_mm(values)
