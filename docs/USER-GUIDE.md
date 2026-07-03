# WG Metal Pipeline User Guide

## 1. Install

From this repository:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 scripts/install_fusion_wg_metal_addin.py --symlink --replace
```

Restart Fusion 360, then enable `WGMetalPipeline` under Utilities > Add-Ins.
Use the symlink install. A copied install cannot reach the repository `scripts/`
folder, and the add-in warns with the reinstall command when that happens.

The dialog's Advanced > Python field defaults to `<repo>/.venv/bin/python` when
that interpreter exists. Direct solves also need the HornLab solver packages:
`hornlab-metal-bem` from `requirements.txt`, plus `hornlab_sim` and
`hornlab_plots`. Discovery order is top-level workspace siblings next to this
repo (`../hornlab-sim`, `../hornlab-plots`, `../hornlab-metal-bem`), then
legacy `../HornLab/*` checkouts, then packages installed in the active
environment.

## 2. Preparing Your Fusion Design

Mark source faces with Fusion appearances or named surface bodies matching the
source names you will solve: `LF`, `MF`, `HF`, and optional `PORT_EXIT`.
The prepare step matches named STEP shells/surfaces first, then appearance/style
labels, with a case-insensitive fallback. Missing requested sources are skipped;
the run fails only if none of the requested sources are found.

Use `PORT_EXIT` for the port mouth faces used by the passive-cardioid or port
radiation-impedance path. Multiple faces with the same `PORT_EXIT` label are
treated as one in-phase aperture group.

Fusion STEP exports are treated as millimetres by default
(`unit-scale-to-m = 0.001`). Dialog mesh fields are in mm. Acoustic distances
and volumes use the units printed in the field labels: polar distance in m,
rear chamber in L, port length in mm, port area in cm2.

Symmetry is detected from free edges on the `x=0`, `y=0`, and `z=0` planes.
Leave Advanced > Mirror plane on Auto detect unless you need to force a half,
quarter, or full model.

## 3. The Dialog

Presets: the top row loads and saves named full-dialog configurations. Enter a
name and click Save preset, or choose an existing preset and click Load preset.
Preset JSON files are stored under
`~/Library/Application Support/HornLab/WGMetalPipeline/presets/` and can also
be used by the headless `--preset` CLI flag.

Sources and mesh: enter source patch mesh sizes for `LF`, `MF`, `HF`, and
optionally `PORT_EXIT`. Blank means do not request that source. `Rigid body
mesh mm` controls the background mesh, and `Transition mm` controls grading
from source size to background.

Mesh sizing: `Refine overrides` accepts comma-separated `NAME:<num>mm` entries
for painted rigid faces, such as `Rim:8mm`. The Estimate box predicts triangle
count, dense BEM memory, solve time, and mesh-valid frequency.

Solve: set frequency range, count, spacing, polar distance, and polar angle
window. Crossover fields generate LR4 aligned combined outputs: two solved
drivers need one crossover field; three drivers need both LF/MF and MF/HF.
`Clamp solves to mesh-valid band` limits each source to its conservative valid
frequency. `Show mesh-valid markers on plots` only controls plot markers and
shading, not the solve itself.

Passive cardioid MF: `Combine MF + port exit` postprocesses solved `MF` and
`PORT_EXIT` bases through the rear chamber, port, and foam model. `Couple driver
LEM` uses the `MF driver T/S` entry from Driver LEM and makes the MF+port branch
voltage-driven; generic per-driver MF coupling is skipped to avoid double
coupling.

Driver LEM: each `LF/MF/HF driver T/S` field accepts pasted Hornresp-style
`Key=Value` text or a path to a readable driver file. Supported Hornresp units
are `Sd` cm2, `Mmd`/`Mms` g, `Cms` m/N, `Rms` kg/s, `Le`/`Le2` mH,
`Re`/`Re2` ohm, `Xmax` mm, and optional `N`/`Nd` count. `Leb`, `Ke`, `Rss`,
and `Vrc*` keys are warned and ignored. Coupling changes that driver's pressure
basis from unit acceleration to SPL at the chosen drive voltage.

Outputs: choose the output root, whether to open the folder, whether to open
the generated report, and which artifact categories to write. The category
checkboxes cover per-driver plots, combined/crossover outputs, passive
cardioid outputs, Driver LEM sidecars, derived acoustics, VituixCAD export,
radiation-impedance matrices, pressure bases, and the HTML report. All
categories default on except VituixCAD, preserving earlier run behavior.

Advanced: choose Python, optional Waveguide Generator folder launch, and mirror
plane override.

## 4. Reading The Outputs

New runs use solver layout version 2. Manifests stay at the run root, while
solver artifacts are grouped by category:

- `sources/`: per-source result JSON, pressure bases, direct source plots, and
  port-exit radiation-impedance matrices
- `combined/`: combined response plots, crossover/alignment plots, off-axis
  plots, interference heatmaps, and `driver_time_alignment.txt`
- `derived/`: DI/power-response, beamwidth, and group-delay PNG/CSV/JSON files
- `driver-lem/`: Driver LEM active pressure, impedance ZMA/PNG, excursion,
  and sidecar files
- `cardioid/`: passive-cardioid MF artifacts
- `vituixcad/`: VituixCAD FRD/ZMA/project export when enabled
- `logs/`: pipeline command logs

Use `direct_solve_manifest.json` as the authoritative map from logical output
names to concrete paths; old layout-1 flat runs remain readable by
postprocess-only regeneration.

Start with per-driver files in `sources/`:

- `<SOURCE>_frequency_response.png`: on-axis SPL for that source. Coupled
  Driver LEM sources are at the run voltage; uncoupled sources are unit-source
  levels. Dashed overlays show wrapped on-axis phase on the right axis.
- `<SOURCE>_directivity_heatmap.png`: normalized directivity over the solved
  polar grid.
- `derived/<SOURCE>_directivity_index_power_response.*`: DI and power-response
  PNG, CSV, and JSON from solid-angle-weighted polar-cut intensity integration.
  The report text notes that this is an approximation because the stored cuts
  are not full-sphere samples.
- `derived/<SOURCE>_beamwidth.*`: per-plane -6 dB beamwidth vs frequency as
  PNG, CSV, and JSON.
- `derived/<SOURCE>_group_delay.*`: on-axis group delay vs frequency as PNG,
  CSV, and JSON, computed from unwrapped engineering-convention pressure phase.
- `sources/<SOURCE>_pressure_basis.npz`: saved complex pressure basis used for
  postprocess reruns.
- `driver-lem/<SOURCE>_impedance.zma`,
  `driver-lem/<SOURCE>_impedance.png`, and
  `driver-lem/<SOURCE>_excursion.png`: written for direct sources with Driver
  LEM specs. The excursion plot includes an Xmax line when `Xmax` was supplied.

Then read combined outputs in `combined/`:

- `combined/combined_frequency_response.png`: direct source responses together, with
  wrapped phase overlays.
- `combined/combined_frequency_response_time_aligned.png`: LR4 filtered, level-matched,
  delay-aligned sum when crossover fields are set, with wrapped phase overlays.
- `combined/combined_directivity_heatmap_time_aligned.png`: directivity of
  that aligned sum.
- `derived/combined_time_aligned_directivity_index_power_response.*`,
  `derived/combined_time_aligned_beamwidth.*`, and
  `derived/combined_time_aligned_group_delay.*`: the same DI/power,
  beamwidth, and group-delay sidecars for the aligned crossover sum.
- `combined/combined_interference_heatmap_time_aligned.png`: coherent vs incoherent
  interaction between drivers.
- `combined/combined_frequency_response_off_axis_<plane>.png`: off-axis aligned sum
  plots with wrapped phase overlays.
- `combined/driver_time_alignment.txt`: crossover frequencies, level trims, delays,
  arrival offsets, and any mesh-band alignment warnings.

Passive cardioid outputs, when enabled, are in `cardioid/` as
`MF_passive_cardioid_*`. Coupled mode also writes
`MF_passive_cardioid_coupled_results.npz`,
`MF_passive_cardioid_coupled_frequency_response.png`, and
`MF_passive_cardioid_impedance.zma` plus `MF_passive_cardioid_impedance.png`.

Manifests and logs are the audit trail: `manifest.json` from mesh prep,
`direct_solve_manifest.json`, `fusion_wg_pipeline_manifest.json`,
`final_summary_manifest.json`, `fusion_addin_launch.json`, and `logs/*.log`.
Open `report.html` for a static gallery and run summary. To build or refresh
an index over an output root:

```bash
.venv/bin/python scripts/render_run_report.py --index runs/fusion360
```

To compare two run folders:

```bash
.venv/bin/python scripts/compare_runs.py \
  runs/fusion360/run-a \
  runs/fusion360/run-b \
  --out runs/fusion360/compare-run-a-run-b \
  --plot-theme hornlab
```

The compare report writes `ab_compare.html` plus overlay PNGs for on-axis
response, directivity index, beamwidth, and group delay. It reads both flat
layout-1 runs and structured layout-2 runs through their manifests.

## 5. VituixCAD Workflow

Enable Outputs > VituixCAD export before running. Open `vituixcad/README.txt`
first; it restates the import assumptions for that run.

Load the FRD angle sets from `vituixcad/hor/` and `vituixcad/ver/` for each
driver. Keep every VituixCAD driver X/Y/Z offset and delay at 0: the BEM export
already contains one shared mic grid, one timing reference, and the relative
phase between drivers. The exporter removes only the common time of flight to
reduce phase wrap.

Coupled Driver LEM sources export voltage-driven FRDs and their calculated
`<SOURCE>_impedance.zma`. Uncoupled sources export unit-source FRDs and no ZMA;
scale their SPL and import measured or datasheet impedance before passive
crossover work.

When `HornLab_active_lr4.vxp` exists, open it as a starting project. It contains
the computed active LR4 filters, level gains, and delays from
`driver_time_alignment.txt`. Treat `MF_cardioid` as the MF driver when passive
cardioid output is present.

## 6. Re-running Without Re-solving

For complete headless reruns, preset-driven sweeps, report regeneration, and
A/B compare examples, see [HEADLESS.md](HEADLESS.md).

To regenerate derived artifacts from an existing run folder with the recorded
solve command:

```bash
.venv/bin/python scripts/regenerate_fusion_derived_artifacts.py runs/fusion360/<run-folder>
```

That wrapper recovers `solve_fusion_wg_metal.py` from the launch or summary
manifest and appends `--postprocess-only`; it does not itself accept a
`--postprocess-only` flag.

To change crossover frequencies, call the solve script directly with the same
run folder, sources, and `--postprocess-only`, overriding the crossover fields:

```bash
.venv/bin/python scripts/solve_fusion_wg_metal.py \
  --mesh runs/fusion360/<run-folder>/tagged_sources.msh \
  --out runs/fusion360/<run-folder> \
  --source LF:2 --source MF:3 --source HF:4 \
  --crossover-lf-mf-hz 160 \
  --crossover-mf-hf-hz 1200 \
  --postprocess-only
```

Postprocess-only reuses saved pressure bases and existing radiation matrix
artifacts. Driver LEM reruns use stored surface averages when available; old
runs with empty averages skip that Driver LEM source with a warning.
Layout-2 runs regenerate into their category folders; old flat layout-1 runs
regenerate beside the original flat artifacts so archived runs stay usable.

## 7. Troubleshooting

Copied-install warning: reinstall with
`python3 scripts/install_fusion_wg_metal_addin.py --symlink --replace`, then
restart Fusion.

Missing sources skipped: check the source names in `manifest.json` and the
available shell/appearance names reported in errors. Paint or name faces as
`LF`, `MF`, `HF`, or `PORT_EXIT`, or leave that source blank in the dialog.

Mesh-valid band: a solid marker is the conservative fully resolved limit; a
dashed marker is the radiating-aperture limit. Use clamp only when you want
each source solved no higher than the conservative band.

Coupled-run requirements: direct Driver LEM needs a solved source and stored
surface-average pressure. Passive-cardioid coupled mode needs solved `MF` and
`PORT_EXIT`, passive-cardioid chamber/port settings, and `MF driver T/S`.
