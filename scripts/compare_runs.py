#!/usr/bin/env python3
"""Generate an A/B comparison report for two Fusion WG Metal run folders."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import html
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
P_REF = 2.0e-5
PRESSURE_NPZ_PHASE_CONVENTION = "engineering_exp_plus_jwt"

for package_dir in reversed(
    (
        REPO_ROOT.parent / "hornlab-plots",
        REPO_ROOT.parent / "HornLab" / "hornlab-plots",
    )
):
    if package_dir.is_dir() and str(package_dir) not in sys.path:
        sys.path.insert(0, str(package_dir))

try:
    from hornlab_plots import get_theme, set_theme  # type: ignore
except Exception:  # pragma: no cover - exercised only without HornLab packages
    get_theme = None
    set_theme = None


@dataclass
class Curve:
    frequencies_hz: np.ndarray
    values: np.ndarray
    label: str


@dataclass
class PressureBasis:
    frequencies_hz: np.ndarray
    angles_deg: np.ndarray
    planes: np.ndarray
    pressure_complex: np.ndarray


class _FallbackTheme:
    figure_bg = "#f6f7f9"
    axes_bg = "#ffffff"
    text_color = "#1d252c"
    tick_color = "#2f3b45"
    spine_color = "#cfd7df"
    grid_color = "#b7c0c8"
    primary_grid_alpha = 0.45
    secondary_grid_alpha = 0.28
    response_colors = {
        "combined": "#0f5f8f",
        "lf": "#9a3412",
        "mf": "#247a4b",
        "hf": "#7c3aed",
        "raw": "#5b6670",
        "other": "#c2410c",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _array(value: Any) -> np.ndarray:
    return np.asarray(value, dtype=np.float64)


def _spl_db(pressure: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(pressure), 1.0e-30) / P_REF)


def _lr4_lowpass(freqs: np.ndarray, fc_hz: float) -> np.ndarray:
    s = 1j * np.asarray(freqs, dtype=np.float64) / float(fc_hz)
    return 1.0 / (s * s + np.sqrt(2.0) * s + 1.0) ** 2


def _lr4_highpass(freqs: np.ndarray, fc_hz: float) -> np.ndarray:
    s = 1j * np.asarray(freqs, dtype=np.float64) / float(fc_hz)
    return (s * s) ** 2 / (s * s + np.sqrt(2.0) * s + 1.0) ** 2


def _crossover_weights(
    freqs: np.ndarray,
    members: list[str],
    crossovers_hz: list[float],
) -> dict[str, np.ndarray]:
    weights: dict[str, np.ndarray] = {}
    for index, name in enumerate(members):
        weight = np.ones(np.asarray(freqs).shape, dtype=np.complex128)
        if index > 0:
            weight = weight * _lr4_highpass(freqs, crossovers_hz[index - 1])
        if index < len(crossovers_hz):
            weight = weight * _lr4_lowpass(freqs, crossovers_hz[index])
        weights[name] = weight
    return weights


class RunData:
    def __init__(self, run_dir: Path, *, label: str | None = None) -> None:
        self.run_dir = run_dir.expanduser().resolve()
        self.label = label or self.run_dir.name
        self.final_manifest = _read_json(self.run_dir / "final_summary_manifest.json")
        self.direct_manifest = _read_json(self.run_dir / "direct_solve_manifest.json")
        if not self.direct_manifest:
            direct = self.final_manifest.get("direct_solve")
            if isinstance(direct, dict):
                self.direct_manifest = direct
        if not self.direct_manifest and not self.final_manifest:
            raise SystemExit(f"no run manifest found in {self.run_dir}")

    @property
    def outputs(self) -> dict[str, Any]:
        return _as_dict(self.direct_manifest.get("outputs"))

    @property
    def config(self) -> dict[str, Any]:
        return _as_dict(self.direct_manifest.get("config"))

    def resolve(self, value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = self.run_dir / path
        if path.exists():
            return path
        local = self.run_dir / Path(str(value)).name
        if local.exists():
            return local
        return path

    def first_existing(self, *values: Any) -> Path | None:
        for value in values:
            path = self.resolve(value)
            if path is not None and path.exists():
                return path
        return None

    def source_names(self) -> list[str]:
        names = [
            str(entry.get("name"))
            for entry in _as_list(self.direct_manifest.get("sources"))
            if isinstance(entry, dict) and entry.get("name")
        ]
        for key in (
            "source_results_jsons",
            "source_pressure_basis_npzs",
            "source_active_pressure_basis_npzs",
        ):
            for name in _as_dict(self.outputs.get(key)):
                if str(name) not in names:
                    names.append(str(name))
        return names

    def source_entry(self, name: str) -> dict[str, Any]:
        for entry in _as_list(self.direct_manifest.get("sources")):
            if isinstance(entry, dict) and str(entry.get("name")) == name:
                return entry
        return {}

    def output_map_path(self, key: str, name: str) -> Path | None:
        return self.resolve(_as_dict(self.outputs.get(key)).get(name))

    def source_result_json(self, name: str) -> Path | None:
        entry = self.source_entry(name)
        return self.first_existing(
            self.output_map_path("source_results_jsons", name),
            entry.get("results_json"),
            self.run_dir / "sources" / f"{name}_results.json",
            self.run_dir / f"{name}_results.json",
        )

    def source_basis_npz(self, name: str) -> Path | None:
        entry = self.source_entry(name)
        return self.first_existing(
            self.output_map_path("source_active_pressure_basis_npzs", name),
            entry.get("active_pressure_basis_npz"),
            self.output_map_path("source_pressure_basis_npzs", name),
            entry.get("pressure_basis_npz"),
            self.run_dir / "sources" / f"{name}_pressure_basis.npz",
            self.run_dir / f"{name}_pressure_basis.npz",
        )

    def source_derived_json(
        self,
        map_key: str,
        entry_key: str,
        fallback_filename: str,
        name: str,
    ) -> Path | None:
        entry = self.source_entry(name)
        return self.first_existing(
            self.output_map_path(map_key, name),
            entry.get(entry_key),
            self.run_dir / "derived" / fallback_filename,
            self.run_dir / fallback_filename,
        )

    def combined_json(self, key: str, filename: str) -> Path | None:
        return self.first_existing(
            self.outputs.get(key),
            self.run_dir / "derived" / filename,
            self.run_dir / filename,
        )


def _load_pressure_basis(path: Path) -> PressureBasis:
    with np.load(path, allow_pickle=False) as data:
        pressure = np.asarray(data["pressure_complex"], dtype=np.complex128)
        if "phase_convention" not in data:
            pressure = np.conjugate(pressure)
        else:
            convention = str(np.asarray(data["phase_convention"]).item())
            if convention != PRESSURE_NPZ_PHASE_CONVENTION:
                raise ValueError(
                    f"{path} stores pressure_complex with unsupported phase convention "
                    f"{convention!r}"
                )
        return PressureBasis(
            frequencies_hz=np.asarray(data["frequencies_hz"], dtype=np.float64),
            angles_deg=np.asarray(data["observation_angles_deg"], dtype=np.float64),
            planes=np.asarray(data["observation_planes"], dtype=str),
            pressure_complex=pressure,
        )


def _result_curve(run: RunData, source_name: str) -> Curve | None:
    result_path = run.source_result_json(source_name)
    if result_path and result_path.exists():
        payload = _read_json(result_path)
        if "frequencies_hz" in payload and "on_axis_spl_db" in payload:
            return Curve(
                _array(payload["frequencies_hz"]),
                _array(payload["on_axis_spl_db"]),
                f"{run.label} {source_name}",
            )
    basis_path = run.source_basis_npz(source_name)
    if basis_path is None or not basis_path.exists():
        return None
    basis = _load_pressure_basis(basis_path)
    on_axis = int(np.argmin(np.abs(basis.angles_deg)))
    return Curve(
        basis.frequencies_hz,
        _spl_db(basis.pressure_complex[:, 0, on_axis]),
        f"{run.label} {source_name}",
    )


def _aligned_sum_curve(run: RunData) -> Curve | None:
    payload = _as_dict(run.direct_manifest.get("crossover_alignment"))
    if not payload or payload.get("status") not in (None, "complete"):
        return None
    members = [str(item) for item in _as_list(payload.get("members")) if str(item)]
    crossovers_hz = [float(value) for value in _as_list(payload.get("crossovers_hz"))]
    if len(members) < 2 or len(crossovers_hz) != len(members) - 1:
        return None
    bases: dict[str, PressureBasis] = {}
    for member in members:
        path = run.source_basis_npz(member)
        if path is None or not path.exists():
            return None
        bases[member] = _load_pressure_basis(path)
    first = bases[members[0]]
    for member, basis in bases.items():
        if basis.pressure_complex.shape != first.pressure_complex.shape:
            raise ValueError(f"{run.run_dir}: pressure grid mismatch for {member}")
        if not np.allclose(basis.frequencies_hz, first.frequencies_hz):
            raise ValueError(f"{run.run_dir}: frequency grid mismatch for {member}")
    freqs = first.frequencies_hz
    weights = _crossover_weights(freqs, members, crossovers_hz)
    gains_db = _as_dict(_as_dict(payload.get("level_match")).get("gains_db"))
    delays_ms = _as_dict(payload.get("delays_ms"))
    combined = np.zeros(first.pressure_complex.shape, dtype=np.complex128)
    for member in members:
        gain = 10.0 ** (float(gains_db.get(member, 0.0)) / 20.0)
        delay_s = float(delays_ms.get(member, 0.0)) / 1000.0
        phase = np.exp(-1j * 2.0 * np.pi * freqs * delay_s)
        factor = weights[member] * gain * phase
        combined += bases[member].pressure_complex * factor[:, None, None]
    on_axis = int(np.argmin(np.abs(first.angles_deg)))
    return Curve(
        freqs,
        _spl_db(combined[:, 0, on_axis]),
        f"{run.label} aligned sum",
    )


def on_axis_curves(run: RunData) -> list[Curve]:
    aligned = _aligned_sum_curve(run)
    if aligned is not None:
        return [aligned]
    curves = []
    for name in run.source_names():
        curve = _result_curve(run, name)
        if curve is not None:
            curves.append(curve)
    return curves


def _json_curve(path: Path | None, key: str, label: str) -> Curve | None:
    if path is None or not path.exists():
        return None
    payload = _read_json(path)
    if "frequencies_hz" not in payload or key not in payload:
        return None
    return Curve(_array(payload["frequencies_hz"]), _array(payload[key]), label)


def directivity_curves(run: RunData) -> list[Curve]:
    combined = _json_curve(
        run.combined_json(
            "combined_time_aligned_directivity_power_json",
            "combined_time_aligned_directivity_index_power_response.json",
        ),
        "directivity_index_db",
        f"{run.label} aligned sum",
    )
    if combined is not None:
        return [combined]
    curves = []
    for name in run.source_names():
        curve = _json_curve(
            run.source_derived_json(
                "source_directivity_power_jsons",
                "directivity_power_json",
                f"{name}_directivity_index_power_response.json",
                name,
            ),
            "directivity_index_db",
            f"{run.label} {name}",
        )
        if curve is not None:
            curves.append(curve)
    return curves


def beamwidth_curves(run: RunData) -> list[Curve]:
    combined_path = run.combined_json(
        "combined_time_aligned_beamwidth_json",
        "combined_time_aligned_beamwidth.json",
    )
    if combined_path and combined_path.exists():
        return _beamwidth_curves_from_json(combined_path, f"{run.label} aligned sum")
    curves = []
    for name in run.source_names():
        curves.extend(
            _beamwidth_curves_from_json(
                run.source_derived_json(
                    "source_beamwidth_jsons",
                    "beamwidth_json",
                    f"{name}_beamwidth.json",
                    name,
                ),
                f"{run.label} {name}",
            )
        )
    return curves


def _beamwidth_curves_from_json(path: Path | None, label: str) -> list[Curve]:
    if path is None or not path.exists():
        return []
    payload = _read_json(path)
    freqs = _array(payload.get("frequencies_hz", []))
    widths = _as_dict(payload.get("beamwidth_deg"))
    curves = []
    for plane, values in sorted(widths.items()):
        curves.append(Curve(freqs, _array(values), f"{label} {plane}"))
    return curves


def group_delay_curves(run: RunData) -> list[Curve]:
    combined = _group_delay_curve(
        run.combined_json(
            "combined_time_aligned_group_delay_json",
            "combined_time_aligned_group_delay.json",
        ),
        f"{run.label} aligned sum",
    )
    if combined is not None:
        return [combined]
    curves = []
    for name in run.source_names():
        curve = _group_delay_curve(
            run.source_derived_json(
                "source_group_delay_jsons",
                "group_delay_json",
                f"{name}_group_delay.json",
                name,
            ),
            f"{run.label} {name}",
        )
        if curve is not None:
            curves.append(curve)
    return curves


def _group_delay_curve(path: Path | None, label: str) -> Curve | None:
    if path is None or not path.exists():
        return None
    payload = _read_json(path)
    if "frequencies_hz" not in payload:
        return None
    if "group_delay_ms" in payload:
        values = _array(payload["group_delay_ms"])
    elif "group_delay_s" in payload:
        values = _array(payload["group_delay_s"]) * 1000.0
    else:
        return None
    return Curve(_array(payload["frequencies_hz"]), values, label)


def _activate_theme(name: str):
    if set_theme is not None and get_theme is not None:
        try:
            set_theme(name)
            return get_theme()
        except Exception:
            pass
    return _FallbackTheme()


def _theme_colors(theme) -> list[str]:
    colors = getattr(theme, "response_colors", {})
    if isinstance(colors, dict) and colors:
        return [str(value) for value in colors.values()]
    return list(_FallbackTheme.response_colors.values())


def _plot_curves(path: Path, curves: list[Curve], *, title: str, ylabel: str, theme) -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig, ax = plt.subplots(figsize=(10.0, 4.8))
    fig.patch.set_facecolor(getattr(theme, "figure_bg", "#f6f7f9"))
    ax.set_facecolor(getattr(theme, "axes_bg", "#ffffff"))
    ax.set_title(title, color=getattr(theme, "text_color", "#1d252c"), fontsize=13, fontweight="600")
    ax.set_xlabel("Frequency [Hz]", color=getattr(theme, "text_color", "#1d252c"))
    ax.set_ylabel(ylabel, color=getattr(theme, "text_color", "#1d252c"))
    ax.tick_params(colors=getattr(theme, "tick_color", "#2f3b45"), labelsize=9)
    for spine in ax.spines.values():
        spine.set_color(getattr(theme, "spine_color", "#cfd7df"))
    colors = _theme_colors(theme)
    plotted = 0
    x_min = np.inf
    x_max = 0.0
    for index, curve in enumerate(curves):
        freqs = np.asarray(curve.frequencies_hz, dtype=np.float64)
        values = np.asarray(curve.values, dtype=np.float64)
        mask = np.isfinite(freqs) & (freqs > 0.0) & np.isfinite(values)
        if not np.any(mask):
            continue
        ax.semilogx(
            freqs[mask],
            values[mask],
            linewidth=2.0,
            color=colors[index % len(colors)],
            label=curve.label,
        )
        plotted += 1
        x_min = min(x_min, float(np.min(freqs[mask])))
        x_max = max(x_max, float(np.max(freqs[mask])))
    if plotted:
        ax.set_xlim(x_min, x_max)
        legend = ax.legend(
            loc="best",
            fontsize=9,
            facecolor=getattr(theme, "axes_bg", "#ffffff"),
            edgecolor=getattr(theme, "spine_color", "#cfd7df"),
            labelcolor=getattr(theme, "text_color", "#1d252c"),
        )
        legend.get_frame().set_alpha(0.92)
    else:
        ax.text(
            0.5,
            0.5,
            "No comparable artifact found",
            ha="center",
            va="center",
            transform=ax.transAxes,
            color=getattr(theme, "text_color", "#1d252c"),
        )
    ax.grid(
        True,
        which="both",
        color=getattr(theme, "grid_color", "#b7c0c8"),
        alpha=getattr(theme, "secondary_grid_alpha", 0.28),
        linewidth=0.6,
    )
    fig.tight_layout(pad=1.4)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(path), dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)


def _flatten_config(value: Any, *, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        flattened: dict[str, str] = {}
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten_config(child, prefix=child_prefix))
        return flattened
    return {prefix: json.dumps(value, sort_keys=True)}


def config_diffs(run_a: RunData, run_b: RunData) -> list[tuple[str, str, str]]:
    left = _flatten_config(run_a.config)
    right = _flatten_config(run_b.config)
    rows = []
    for key in sorted(set(left) | set(right)):
        a_value = left.get(key, "")
        b_value = right.get(key, "")
        if a_value != b_value:
            rows.append((key, a_value, b_value))
    return rows


def _html(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _write_html(
    out_dir: Path,
    run_a: RunData,
    run_b: RunData,
    pngs: dict[str, Path],
    diff_rows: list[tuple[str, str, str]],
) -> Path:
    rows = "".join(
        "<tr>"
        f"<td>{_html(key)}</td>"
        f"<td>{_html(a_value)}</td>"
        f"<td>{_html(b_value)}</td>"
        "</tr>"
        for key, a_value, b_value in diff_rows
    )
    if not rows:
        rows = '<tr><td colspan="3" class="muted">No differing config keys.</td></tr>'
    figures = "".join(
        "<figure>"
        f'<img src="{_html(path.name)}" alt="{_html(title)}">'
        f"<figcaption>{_html(title)}</figcaption>"
        "</figure>"
        for title, path in pngs.items()
    )
    html_text = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f"<title>{_html(run_a.label)} vs {_html(run_b.label)} - WG Metal A/B</title>",
            "<style>",
            "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:0;background:#f6f7f9;color:#1d252c;line-height:1.45}",
            "header{padding:28px 32px;background:#17202a;color:white}",
            "main{max-width:1180px;margin:0 auto;padding:24px 20px 48px}",
            "section{margin:0 0 24px;padding:20px;background:white;border:1px solid #d9e0e6;border-radius:8px}",
            "h1{margin:0 0 6px;font-size:28px}h2{margin:0 0 14px;font-size:20px}",
            "figure{display:inline-block;vertical-align:top;width:min(540px,100%);margin:0 16px 16px 0}img{max-width:100%;height:auto;border:1px solid #dfe5ea;background:white}figcaption{font-size:13px;color:#5b6670;margin-top:5px}",
            "table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #edf0f2;padding:7px 8px;text-align:left;vertical-align:top}th{color:#5b6670}.muted{color:#6b7680}",
            "</style></head><body>",
            f"<header><h1>{_html(run_a.label)} vs {_html(run_b.label)}</h1><div>A/B comparison</div></header>",
            "<main>",
            "<section><h2>Overlays</h2>"
            '<p class="muted">On-axis response uses an aligned sum when the run '
            "manifest and pressure bases can reconstruct it; otherwise it shows "
            "per-driver curves.</p>"
            f"{figures}</section>",
            "<section><h2>Config Differences</h2>",
            "<table><thead><tr>"
            f"<th>Key</th><th>{_html(run_a.label)}</th><th>{_html(run_b.label)}</th>"
            "</tr></thead><tbody>",
            rows,
            "</tbody></table></section>",
            "</main></body></html>",
        ]
    )
    out = out_dir / "ab_compare.html"
    out.write_text(html_text, encoding="utf-8")
    return out


def compare_runs(
    run_a_dir: Path,
    run_b_dir: Path,
    *,
    out_dir: Path,
    plot_theme: str,
    name_a: str | None = None,
    name_b: str | None = None,
) -> Path:
    run_a = RunData(run_a_dir, label=name_a)
    run_b = RunData(run_b_dir, label=name_b)
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    theme = _activate_theme(plot_theme)
    pngs = {
        "On-axis Frequency Response": out_dir / "on_axis_frequency_response.png",
        "Directivity Index": out_dir / "directivity_index.png",
        "Beamwidth": out_dir / "beamwidth.png",
        "Group Delay": out_dir / "group_delay.png",
    }
    _plot_curves(
        pngs["On-axis Frequency Response"],
        [*on_axis_curves(run_a), *on_axis_curves(run_b)],
        title="A/B On-Axis Frequency Response",
        ylabel="On-axis SPL [dB]",
        theme=theme,
    )
    _plot_curves(
        pngs["Directivity Index"],
        [*directivity_curves(run_a), *directivity_curves(run_b)],
        title="A/B Directivity Index",
        ylabel="Directivity index [dB]",
        theme=theme,
    )
    _plot_curves(
        pngs["Beamwidth"],
        [*beamwidth_curves(run_a), *beamwidth_curves(run_b)],
        title="A/B -6 dB Beamwidth",
        ylabel="-6 dB beamwidth [deg]",
        theme=theme,
    )
    _plot_curves(
        pngs["Group Delay"],
        [*group_delay_curves(run_a), *group_delay_curves(run_b)],
        title="A/B On-Axis Group Delay",
        ylabel="Group delay [ms]",
        theme=theme,
    )
    return _write_html(out_dir, run_a, run_b, pngs, config_diffs(run_a, run_b))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_a", type=Path)
    parser.add_argument("run_b", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--plot-theme", default="hornlab")
    parser.add_argument("--name-a", default=None)
    parser.add_argument("--name-b", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    html_path = compare_runs(
        args.run_a,
        args.run_b,
        out_dir=args.out,
        plot_theme=args.plot_theme,
        name_a=args.name_a,
        name_b=args.name_b,
    )
    print(html_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
