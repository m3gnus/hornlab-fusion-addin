#!/usr/bin/env python3
"""Render static HTML reports for Fusion WG Metal run folders."""

from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
import os
from pathlib import Path
from typing import Any


RUN_MANIFESTS_DIR_NAME = "manifests"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_manifest_path(run_dir: Path, name: str) -> Path:
    preferred = run_dir / RUN_MANIFESTS_DIR_NAME / name
    if preferred.exists():
        return preferred
    return run_dir / name


def _has_run_manifest(run_dir: Path, name: str) -> bool:
    return (run_dir / RUN_MANIFESTS_DIR_NAME / name).exists() or (run_dir / name).exists()


def _run_manifests(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    final_manifest = _read_json(_run_manifest_path(run_dir, "final_summary_manifest.json"))
    direct_manifest = _read_json(_run_manifest_path(run_dir, "direct_solve_manifest.json"))
    if not direct_manifest:
        direct = final_manifest.get("direct_solve")
        if isinstance(direct, dict):
            direct_manifest = direct
    return final_manifest, direct_manifest


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _html(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _rel(run_dir: Path, value: Any) -> str | None:
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = run_dir / path
    try:
        return os.path.relpath(path, run_dir)
    except ValueError:
        return str(path)


def _link(run_dir: Path, value: Any, label: str | None = None) -> str:
    rel = _rel(run_dir, value)
    if not rel:
        return ""
    return f'<a href="{_html(rel)}">{_html(label or Path(rel).name)}</a>'


def _image(run_dir: Path, value: Any, caption: str) -> str:
    rel = _rel(run_dir, value)
    if not rel:
        return ""
    return (
        '<figure>'
        f'<img src="{_html(rel)}" alt="{_html(caption)}">'
        f'<figcaption>{_html(caption)}</figcaption>'
        '</figure>'
    )


def _section(title: str, body: str) -> str:
    if not body.strip():
        body = '<p class="muted">No artifacts recorded.</p>'
    return f'<section><h2>{_html(title)}</h2>{body}</section>'


def _summary_table(final_manifest: dict[str, Any], direct_manifest: dict[str, Any]) -> str:
    config = _as_dict(direct_manifest.get("config"))
    crossover = _as_dict(config.get("crossover"))
    crossover_summary = f"LF/MF {crossover.get('lf_mf_hz')} Hz, MF/HF {crossover.get('mf_hf_hz')} Hz"
    if crossover.get("lf_hf_hz") is not None:
        crossover_summary += f", LF/HF {crossover.get('lf_hf_hz')} Hz"
    rows = [
        ("Status", direct_manifest.get("status") or final_manifest.get("status") or "unknown"),
        ("Started", direct_manifest.get("started_at") or final_manifest.get("started_at") or ""),
        ("Finished", direct_manifest.get("finished_at") or final_manifest.get("finished_at") or ""),
        ("Layout", direct_manifest.get("layout_version", 1)),
        ("Frequency", f"{config.get('freq_min_hz', '')} - {config.get('freq_max_hz', '')} Hz"),
        ("Count/spacing", f"{config.get('freq_count', '')} / {config.get('freq_spacing', '')}"),
        ("Crossover", crossover_summary),
        ("Mesh", direct_manifest.get("mesh") or final_manifest.get("solve_mesh") or ""),
    ]
    return (
        "<table>"
        + "".join(
            f"<tr><th>{_html(label)}</th><td>{_html(value)}</td></tr>"
            for label, value in rows
            if value not in (None, "")
        )
        + "</table>"
    )


def _per_driver_section(run_dir: Path, direct_manifest: dict[str, Any]) -> str:
    outputs = _as_dict(direct_manifest.get("outputs"))
    response_by_source = _as_dict(outputs.get("source_frequency_response_pngs"))
    basis_by_source = _as_dict(outputs.get("source_pressure_basis_npzs"))
    result_by_source = _as_dict(outputs.get("source_results_jsons"))
    heatmap_by_source = _as_dict(outputs.get("source_directivity_heatmap_pngs"))
    chunks: list[str] = []
    for source in _as_list(direct_manifest.get("sources")):
        if not isinstance(source, dict):
            continue
        name = str(source.get("name", "source"))
        figures = [
            _image(run_dir, response_by_source.get(name) or source.get("frequency_response_png"), f"{name} frequency response"),
            _image(run_dir, heatmap_by_source.get(name) or source.get("directivity_heatmap_png"), f"{name} directivity heatmap"),
        ]
        links = []
        if basis_by_source.get(name) or source.get("pressure_basis_npz"):
            links.append(_link(run_dir, basis_by_source.get(name) or source.get("pressure_basis_npz"), "pressure basis"))
        if result_by_source.get(name) or source.get("results_json"):
            links.append(_link(run_dir, result_by_source.get(name) or source.get("results_json"), "results JSON"))
        body = "".join(item for item in figures if item)
        if links:
            body += '<p class="links">' + " ".join(links) + "</p>"
        chunks.append(f"<h3>{_html(name)}</h3>{body}")
    return "".join(chunks)


def _combined_section(run_dir: Path, direct_manifest: dict[str, Any]) -> str:
    outputs = _as_dict(direct_manifest.get("outputs"))
    banner = ""
    alignment = _as_dict(direct_manifest.get("crossover_alignment"))
    if alignment.get("status") == "skipped":
        banner = (
            '<p style="background:#fff3cd;border:1px solid #ffe08a;border-radius:6px;'
            'padding:10px 12px;color:#7a5b00;margin:0 0 14px">'
            "<strong>Time-aligned combine skipped &mdash; no combined directivity "
            f"heatmap.</strong> {_html(str(alignment.get('reason', '')))}</p>"
        )
    keys = [
        ("combined_frequency_response_png", "Combined frequency response"),
        ("combined_time_aligned_frequency_response_png", "Time-aligned frequency response"),
        ("combined_time_aligned_directivity_heatmap_png", "Time-aligned directivity"),
        ("combined_interference_heatmap_png", "Interference heatmap"),
    ]
    body = banner + "".join(_image(run_dir, outputs.get(key), label) for key, label in keys)
    off_axis = _as_dict(outputs.get("combined_off_axis_frequency_response_pngs"))
    body += "".join(
        _image(run_dir, value, f"Off-axis response {plane}")
        for plane, value in sorted(off_axis.items())
    )
    if outputs.get("driver_time_alignment_txt"):
        body += '<p class="links">' + _link(run_dir, outputs["driver_time_alignment_txt"], "driver time alignment") + "</p>"
    return body


def _derived_section(run_dir: Path, direct_manifest: dict[str, Any]) -> str:
    outputs = _as_dict(direct_manifest.get("outputs"))
    body = ""
    for title, key in (
        ("Directivity index / power", "source_directivity_power_pngs"),
        ("Beamwidth", "source_beamwidth_pngs"),
        ("Group delay", "source_group_delay_pngs"),
    ):
        values = _as_dict(outputs.get(key))
        if values:
            body += f"<h3>{_html(title)}</h3>"
            body += "".join(
                _image(run_dir, value, f"{name} {title.lower()}")
                for name, value in sorted(values.items())
            )
    for key, label in (
        ("combined_time_aligned_directivity_power_png", "Combined directivity index / power"),
        ("combined_time_aligned_beamwidth_png", "Combined beamwidth"),
        ("combined_time_aligned_group_delay_png", "Combined group delay"),
    ):
        body += _image(run_dir, outputs.get(key), label)
    return body


def _driver_lem_section(run_dir: Path, direct_manifest: dict[str, Any]) -> str:
    outputs = _as_dict(direct_manifest.get("outputs"))
    chunks: list[str] = []
    for name, zma in sorted(_as_dict(outputs.get("driver_lem_impedance_zmas")).items()):
        impedance = _as_dict(outputs.get("driver_lem_impedance_pngs")).get(name)
        excursion = _as_dict(outputs.get("driver_lem_excursion_pngs")).get(name)
        links = [_link(run_dir, zma, f"{name} ZMA")]
        results = _as_dict(outputs.get("driver_lem_results_npzs")).get(name)
        if results:
            links.append(_link(run_dir, results, "results NPZ"))
        body = _image(run_dir, impedance, f"{name} impedance")
        body += _image(run_dir, excursion, f"{name} excursion")
        body += '<p class="links">' + " ".join(links) + "</p>"
        chunks.append(f"<h3>{_html(name)}</h3>{body}")
    return "".join(chunks)


def _cardioid_section(run_dir: Path, direct_manifest: dict[str, Any]) -> str:
    outputs = _as_dict(direct_manifest.get("outputs"))
    body = ""
    for key, label in (
        ("passive_cardioid_frequency_response_png", "Passive-cardioid frequency response"),
        ("passive_cardioid_directivity_heatmap_png", "Passive-cardioid directivity"),
        ("passive_cardioid_coupled_frequency_response_png", "Coupled passive-cardioid response"),
        ("passive_cardioid_impedance_png", "Passive-cardioid impedance"),
    ):
        body += _image(run_dir, outputs.get(key), label)
    links = [
        _link(run_dir, outputs.get("passive_cardioid_results_npz"), "results NPZ"),
        _link(run_dir, outputs.get("passive_cardioid_summary_json"), "summary JSON"),
        _link(run_dir, outputs.get("passive_cardioid_coupled_results_npz"), "coupled results NPZ"),
        _link(run_dir, outputs.get("passive_cardioid_impedance_zma"), "impedance ZMA"),
    ]
    links = [item for item in links if item]
    if links:
        body += '<p class="links">' + " ".join(links) + "</p>"
    return body


def _radiation_section(run_dir: Path, direct_manifest: dict[str, Any]) -> str:
    outputs = _as_dict(direct_manifest.get("outputs"))
    links = [
        _link(
            run_dir,
            outputs.get("port_exit_radiation_impedance_npz"),
            "radiation impedance matrix NPZ",
        ),
        _link(
            run_dir,
            outputs.get("port_exit_radiation_impedance_summary_json"),
            "radiation impedance summary JSON",
        ),
    ]
    links = [item for item in links if item]
    return '<p class="links">' + " ".join(links) + "</p>" if links else ""


def _vituixcad_section(run_dir: Path, direct_manifest: dict[str, Any]) -> str:
    outputs = _as_dict(direct_manifest.get("outputs"))
    links = [
        _link(run_dir, outputs.get("vituixcad_export_dir"), "export folder"),
        _link(run_dir, outputs.get("vituixcad_readme_txt"), "README"),
        _link(run_dir, outputs.get("vituixcad_active_lr4_vxp"), "active LR4 project"),
    ]
    shown_zmas: set[str] = set()
    for name, zma in sorted(_as_dict(outputs.get("vituixcad_driver_zmas")).items()):
        links.append(_link(run_dir, zma, f"{name} ZMA"))
        shown_zmas.add(str(zma))
    mf_cardioid_zma = outputs.get("vituixcad_mf_cardioid_zma")
    if mf_cardioid_zma and str(mf_cardioid_zma) not in shown_zmas:
        links.append(
            _link(
                run_dir,
                mf_cardioid_zma,
                "MF cardioid ZMA",
            )
        )
    links = [item for item in links if item]
    return '<p class="links">' + " ".join(links) + "</p>" if links else ""


def _logs_section(run_dir: Path, final_manifest: dict[str, Any]) -> str:
    links: list[str] = []
    for value in _as_dict(final_manifest.get("logs")).values():
        link = _link(run_dir, value)
        if link:
            links.append(link)
    logs_dir = run_dir / "logs"
    if logs_dir.is_dir():
        for path in sorted(logs_dir.glob("*.log")):
            link = _link(run_dir, path)
            if link not in links:
                links.append(link)
    return '<p class="links">' + " ".join(links) + "</p>" if links else ""


def render_run(run_dir: Path) -> Path:
    run_dir = run_dir.expanduser().resolve()
    final_manifest, direct_manifest = _run_manifests(run_dir)
    if not final_manifest and not direct_manifest:
        raise SystemExit(f"no run manifest found in {run_dir}")
    title = run_dir.name
    status = direct_manifest.get("status") or final_manifest.get("status") or "unknown"
    html_text = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f"<title>{_html(title)} - WG Metal Report</title>",
            "<style>",
            "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:0;background:#f6f7f9;color:#1d252c;line-height:1.45}",
            "header{padding:28px 32px;background:#17202a;color:white}",
            "main{max-width:1180px;margin:0 auto;padding:24px 20px 48px}",
            "section{margin:0 0 24px;padding:20px;background:white;border:1px solid #d9e0e6;border-radius:8px}",
            "h1{margin:0 0 6px;font-size:28px}h2{margin:0 0 14px;font-size:20px}h3{margin:18px 0 10px;font-size:15px}",
            "table{border-collapse:collapse;width:100%;font-size:14px}th{text-align:left;width:180px;color:#5b6670}td,th{border-bottom:1px solid #edf0f2;padding:7px 0}",
            "figure{display:inline-block;vertical-align:top;width:min(520px,100%);margin:0 16px 16px 0}img{max-width:100%;height:auto;border:1px solid #dfe5ea;background:white}figcaption{font-size:13px;color:#5b6670;margin-top:5px}",
            "a{color:#0f5f8f;text-decoration:none}.links a{display:inline-block;margin:0 12px 8px 0}.muted{color:#6b7680}",
            "</style></head><body>",
            f"<header><h1>{_html(title)}</h1><div>Status: {_html(status)}</div></header>",
            "<main>",
            _section("Run Config", _summary_table(final_manifest, direct_manifest)),
            _section("Per-Driver Plots", _per_driver_section(run_dir, direct_manifest)),
            _section("Combined / Crossover", _combined_section(run_dir, direct_manifest)),
            _section("Derived Acoustics", _derived_section(run_dir, direct_manifest)),
            _section("Radiation Impedance", _radiation_section(run_dir, direct_manifest)),
            _section("Driver LEM", _driver_lem_section(run_dir, direct_manifest)),
            _section("Passive Cardioid", _cardioid_section(run_dir, direct_manifest)),
            _section("VituixCAD", _vituixcad_section(run_dir, direct_manifest)),
            _section("Logs", _logs_section(run_dir, final_manifest)),
            "</main></body></html>",
        ]
    )
    out = run_dir / "report.html"
    out.write_text(html_text, encoding="utf-8")
    return out


def _manifest_sort_key(run_dir: Path) -> tuple[str, float]:
    final_manifest, direct_manifest = _run_manifests(run_dir)
    raw = (
        direct_manifest.get("finished_at")
        or final_manifest.get("finished_at")
        or direct_manifest.get("started_at")
        or final_manifest.get("started_at")
    )
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw))
            return dt.isoformat(), run_dir.stat().st_mtime
        except ValueError:
            pass
    return "", run_dir.stat().st_mtime


def render_index(output_root: Path) -> Path:
    output_root = output_root.expanduser().resolve()
    runs = [
        path
        for path in output_root.iterdir()
        if path.is_dir()
        and (
            _has_run_manifest(path, "direct_solve_manifest.json")
            or _has_run_manifest(path, "final_summary_manifest.json")
        )
    ]
    runs.sort(key=_manifest_sort_key, reverse=True)
    rows = []
    for run_dir in runs:
        final_manifest, direct_manifest = _run_manifests(run_dir)
        status = direct_manifest.get("status") or final_manifest.get("status") or "unknown"
        date = (
            direct_manifest.get("finished_at")
            or final_manifest.get("finished_at")
            or direct_manifest.get("started_at")
            or final_manifest.get("started_at")
            or ""
        )
        step = final_manifest.get("step") or direct_manifest.get("mesh") or ""
        design = Path(str(step)).stem if step else run_dir.name
        report = run_dir / "report.html"
        href = f"{run_dir.name}/report.html"
        if not report.exists():
            try:
                render_run(run_dir)
            except SystemExit:
                href = f"{run_dir.name}/"
        rows.append(
            "<tr>"
            f'<td><a href="{_html(href)}">{_html(run_dir.name)}</a></td>'
            f"<td>{_html(design)}</td>"
            f"<td>{_html(date)}</td>"
            f"<td>{_html(status)}</td>"
            "</tr>"
        )
    html_text = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            "<title>WG Metal Runs</title>",
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:32px;background:#f6f7f9;color:#1d252c}main{max-width:980px;margin:auto}table{width:100%;border-collapse:collapse;background:white;border:1px solid #d9e0e6}td,th{padding:10px 12px;border-bottom:1px solid #edf0f2;text-align:left}a{color:#0f5f8f;text-decoration:none}</style>",
            "</head><body><main>",
            "<h1>WG Metal Runs</h1>",
            "<table><thead><tr><th>Run</th><th>Design</th><th>Date</th><th>Status</th></tr></thead><tbody>",
            *rows,
            "</tbody></table></main></body></html>",
        ]
    )
    out = output_root / "index.html"
    out.write_text(html_text, encoding="utf-8")
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--index", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.index:
        print(render_index(args.path))
    else:
        print(render_run(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
