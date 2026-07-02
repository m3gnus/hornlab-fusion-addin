# WG Metal Pipeline Fusion Add-In

Exports the active Fusion design to STEP and to a native `.f3d` archive, then
starts a background pipeline process so Fusion can be used while meshing,
diagnostics, and direct solves run. The Fusion exports themselves are
synchronous because they use the Fusion API command context.

The background process runs:

1. `scripts/prepare_step_for_wg_metal.py`
2. `scripts/diagnose_wg_metal_orientation.py`
3. `scripts/solve_fusion_wg_metal.py` (unless "Mesh only" is checked)

## Automation

The add-in is designed to not fail on configuration drift:

- **Sources are matched automatically.** The dialog declares mesh resolutions
  for `LF`, `MF`, `HF`, and an optional `PORT_EXIT` interface patch group
  (blank skips that source entirely). Apply the same `PORT_EXIT` Fusion
  appearance/name to both side-port mouth faces when they should be driven as
  one symmetric reduced-model aperture. Sources whose appearance/shell
  name is absent from the STEP are skipped and recorded in the manifest under
  `skipped_sources` instead of failing the run. The run fails only when none
  of the declared sources are found.
- **Port exits produce a termination matrix.** When `PORT_EXIT` or any
  `PORT_EXIT*` source is present, the direct solve step also writes
  `port_exit_radiation_impedance_matrix.npz` and a `.summary.json`. The NPZ
  contains both `solver_impedance_matrix` and
  `engineering_impedance_matrix = conj(Z_solver)`, matching the validated
  2026-06-11 LEM/TMM insertion convention for Helmholtz chamber
  back-loading.
- **Passive-cardioid MF combine is optional.** The `Passive cardioid MF`
  group can combine solved `MF` and `PORT_EXIT` pressure bases after the
  direct solve. The network is: rear chamber compliance `C = V/(rho*c^2)` in
  shunt, series path of foam resistance `R`, port inertance, and the BEM
  radiation self-impedance `Z_pp`; the port is driven by the MF rear wave
  AND by the exterior MF front wave pressurizing the port exit (mutual
  `Z(port<-MF)` from the aperture matrix, solved with the MF diaphragm
  included): `Q_port = ratio * (rear_sign - j*w*C*Z_pm) * Q_mf` with
  `ratio = 1/(1 + j*w*C*(R + j*w*M + Z_pp))`. Outputs are
  `MF_passive_cardioid_results.npz`, `MF_passive_cardioid_summary.json`,
  `MF_passive_cardioid_frequency_response.png`, and
  `MF_passive_cardioid_directivity_heatmap.png`. Blank port area uses the
  tagged `PORT_EXIT` mesh area. `Rear-wave polarity` defaults on; flip it only
  if the null appears on the wrong side. Use a `-180..180 deg` polar sweep when
  you want the rear null visible; narrower requested windows are preserved.
  **Coupled mode:** `Couple driver LEM` makes the passive-cardioid branch use
  the `MF driver T/S` entry from the separate `Driver LEM (optional)` group.
  MF is then owned by the MF+port coupled network, so the generic per-driver
  MF coupling is skipped rather than applied twice. Coupled mode also writes
  `MF_passive_cardioid_coupled_results.npz`,
  `MF_passive_cardioid_coupled_frequency_response.png`, and
  `MF_passive_cardioid_impedance.zma`, and the summary JSON gains a
  `coupled` block with the driver echo, excursion, impedance, and area
  diagnostics. The `.zma` is VituixCAD-compatible (`freq_Hz |Z| phase_deg`);
  when VituixCAD export is enabled it is copied for the `MF_cardioid` driver.
  **Design guidance:** the chamber/foam pair is a low-pass on port flow with
  corner `f_RC = 1/(2*pi*R*C)` — above it the rear wave compresses the
  chamber instead of flowing through the foam and the cardioid collapses
  (e.g. 10 L + 10 kPa*s/m^3 gives ~225 Hz, far below a 1 kHz band top; 2 L +
  4 kPa*s/m^3 gives ~2.8 kHz). The summary's `diagnostics` block reports
  `rc_corner_hz` and the in-band `|Q_port/Q_mf|` range, and the solve log
  warns when the port is too weak to form a null. **Model limits:** with
  coupling off, the MF cone is a fixed acceleration source (the rear load does
  not react back on it), so the radiation *pattern* per frequency is
  trustworthy but the absolute response shape is approximate; with coupling
  on, the response and ZMA are voltage-driven by the coupled driver/LEM/BEM
  load. The chamber is still a lumped, undamped compliance, valid below its
  first internal mode (~c/(2*max_dim), often mid-band), which real stuffing
  softens.
- **Driver LEM coupling is optional per direct source.** The `Driver LEM
  (optional)` group accepts one `LF/MF/HF driver T/S` string per source. Each
  string may be pasted Hornresp-style `Key=Value` text or a path to a Hornresp
  driver `.txt` file. Units follow Hornresp: `Sd` cm2, `Mmd`/`Mms` g, `Cms`
  m/N, `Rms` kg/s, `Le`/`Le2` mH, `Re`/`Re2` ohm, `Xmax` mm, optional `N`
  driver count. `Leb`, `Ke`, `Rss`, and `Vrc*` system keys are ignored with a
  warning. Shared `Drive voltage V RMS` and `Generator Rg ohm` set the voltage
  drive. For each coupled direct source the solver derives the BEM radiation
  self-load from that source's surface-average pressure and patch area, runs
  `hornlab_sim.methods.driver_coupling.coupled_direct_radiator_response`, and
  scales the pressure basis for response plots, crossover input, and VituixCAD
  export. Outputs include `<NAME>_impedance.zma`, `<NAME>_excursion.png`, and a
  manifest `driver_lem` block with normalized parameter echo, Mmd/Mms provenance,
  self-impedance provenance, excursion maximum, impedance range, and the explicit
  note that driver-driver mutual coupling is neglected.
- **Elements use explicit millimetre sizing.** Source patches use their source
  mesh mm values, rigid/shadow surfaces use `Rigid body mesh mm`, and the
  near-field baffle grades from each source's own size out to the background
  over `Transition mm`. The run still validates and reports the mesh-valid
  solve band from the prepared mesh. Painted appearances can override per face
  with `Refine overrides` (`NAME:<num>mm`), kept physically rigid.
- **Mesh size and solve cost are predicted before meshing.** The dialog's
  `Estimate` readout shows the predicted triangle count (per role), the dense
  BEM matrix RAM (`N^2 * 16` bytes) with a feasibility flag, the solve time,
  and the radiating valid band as the dials change, from
  `N ~= 2.3 * sum(area / size^2)` over the model's BRep face areas. The prepare
  manifest records the authoritative prediction under `mesh_size_prediction`,
  including the actual-vs-predicted error after meshing (~5% on the validation
  model). Fine OCC meshes are also de-slivered (5 um vertex weld + needle
  removal) so the dense solve cannot go singular.
- **Symmetry planes are auto-detected.** The prepare step classifies mesh free
  edges against the `x=0`/`y=0`/`z=0` planes (3+ edges on a plane = cut plane)
  and the pipeline derives mirror axes, quadrants, and the native Metal
  symmetry mode from the detection. The `Mirror plane` dropdown under
  Advanced can still force an explicit plane.
- **Solves run the requested band by default.** The dialog launches with
  `--underresolved-solve-policy warn`: mesh-valid ceilings remain visible in
  the launch dialog and pipeline manifest
  (`solve_frequency_adjustment.mesh_valid_freq_max_hz`), but a 20 kHz request
  still solves to 20 kHz. Each source's directivity heatmap and response plot
  overlay two markers: a solid `mesh-valid` line at the conservative
  fully-resolved frequency (patch and near-walls) and a dashed `aperture-valid`
  line at the radiating-aperture frequency (patch-only, undragged by
  intentionally coarse shadow walls). The shaded band between them is where
  only the radiating aperture is resolved, so the trustworthy band stays
  obvious. Unticking `Show mesh-valid markers on plots` hides both markers and
  the shaded band (the solve and its recorded mesh-valid limits are
  unaffected). Ticking `Clamp solves to mesh-valid band` switches
  to `clamp-per-source`: each source solves up to
  `min(requested max, its effective mesh-valid limit)`, and sources whose
  limit falls below the requested minimum are skipped. The effective limit is
  the lower of the source patch limit and the rigid-wall limit within the
  transition distance of the patch, because the wave a source launches travels
  along the surrounding horn walls: a 4 mm HF patch inside 40 mm walls is only
  valid to the walls' ~1.5 kHz, not the patch's ~14 kHz. To push the
  trustworthy band higher, refine `Rigid body mesh mm` or reduce the amount
  of coarse wall geometry included in `Transition mm`, not just the source
  patch.
- **The observation frame follows the horn.** With `--frame-axis auto`
  (default), the pipeline snaps the diagnosed source forward axis to the
  nearest principal axis allowed by the cut planes (the radiation axis must
  lie in every symmetry plane), picks the matching mouth centroid as polar
  origin (mirrored coordinates zeroed onto the cut planes), and derives the
  horizontal/vertical sweep vectors. A top/bottom half model firing along +X
  is observed along +X, not the legacy hardcoded +Z. Explicit
  `--frame-axis/--frame-origin/--frame-u/--frame-v` still override.
- **Completion is announced.** The pipeline posts a macOS notification with
  the final status and the solved/skipped sources, and updates
  `fusion_addin_launch.json` (`status`, `returncode`, `finished_at`, `error`)
  so a run can never silently end as "running".
- **Stale installs warn.** If the add-in runs from a copied install instead
  of a repo symlink, the launch dialog shows a warning with the reinstall
  command. Install with:

  ```bash
  python3 scripts/install_fusion_wg_metal_addin.py --symlink --replace
  ```

  The installer registers one add-in by default: the legacy
  `Autodesk Fusion 360` add-in path when present, otherwise the current
  macOS Fusion path.
  Use repeated `--addins-dir` arguments only when you intentionally want
  multiple installs.

Strict behavior remains available on the lower-level CLI with
`--underresolved-solve-policy fail` (refuse underresolved solves),
`warn` (solve the requested band with mesh-valid ceilings recorded),
`clamp-per-source` (per-source clamped bands, the dialog checkbox), or
`clamp` (shared band clamped to the lowest source limit).

## Outputs

The output folder contains:

- exported `.step`
- exported native `.f3d` Fusion archive of the model handed to the pipeline
- `tagged_sources.msh`
- one full-domain metre-unit `<source>_source_tag2_m.msh` per source
- `orientation_report.json`
- `expanded_*q_*.msh` + preview PNG
- one `<source>_results.json` per solved source
- one `<source>_pressure_basis.npz` per solved source — the complex pressure
  grid reused by the crossover sum, passive-cardioid combine, and VituixCAD
  export. Stored in the engineering `e^{+j omega t}` phase convention (a
  delay is a falling phase) and tagged with a `phase_convention` key; the
  native solver's raw `e^{-i omega t}` output is conjugated once at this
  write boundary. Legacy bases without the key hold raw solver-convention
  data and are conjugated on load, so old runs stay loadable — but
  crossover/alignment, passive-cardioid, and FRD artifacts generated before
  the 2026-07-02 convention fix were computed conjugate-wrong; re-run the
  pipeline to regenerate them (the raw bases themselves were always valid).
  New bases also embed `surface_pressure_avg`, the source patch area, and
  `source_normalization=unit_normal_acceleration` for postprocess-only
  per-driver coupling.
- one `<source>_directivity_heatmap.png` per solved source
- one `<source>_frequency_response.png` per solved source
- `<NAME>_driver_lem_results.npz`, `<NAME>_driver_lem_pressure.npz`,
  `<NAME>_impedance.zma`, and `<NAME>_excursion.png` for each direct source
  with a Driver LEM T/S spec. The manifest's per-source `driver_lem` block
  records normalized parameters, self-impedance provenance, excursion maximum,
  electrical impedance range, and the self-coupling-only assumption.
- `port_exit_radiation_impedance_matrix.npz` and
  `port_exit_radiation_impedance_matrix.summary.json` when port-exit source
  tags are solved
- `MF_passive_cardioid_results.npz`, `MF_passive_cardioid_summary.json`,
  `MF_passive_cardioid_frequency_response.png`, and
  `MF_passive_cardioid_directivity_heatmap.png` when passive-cardioid MF
  combine is enabled
- `MF_passive_cardioid_coupled_results.npz`,
  `MF_passive_cardioid_coupled_frequency_response.png`, and
  `MF_passive_cardioid_impedance.zma` when passive-cardioid coupled driver LEM
  is enabled; the shared summary JSON includes the `coupled` block
- `combined_frequency_response.png`
- crossover-sum outputs when crossover frequency fields are filled (one field
  for a two-way with two of LF/MF/HF solved, both fields for a three-way):
  - `combined_frequency_response_time_aligned.png` — per-driver LR4-filtered
    curves plus the level-matched, delay-aligned complex sum (the DSP "best
    result"), and the sum before delay for comparison
  - `combined_directivity_heatmap_time_aligned.png` — directivity of the
    aligned sum
  - `combined_interference_heatmap_time_aligned.png` — coherent-vs-incoherent
    sum ratio per angle/frequency; 0 dB means the drivers add fully in phase,
    deep negatives mark driver-spacing cancellation (watch the crossover
    bands, where two drivers carry comparable level)
  - `combined_frequency_response_off_axis_<plane>.png` — the aligned sum at
    0/15/30/45/60 deg; on-axis delay alignment cannot fix off-axis path
    differences, so crossover lobing shows here
  - `driver_time_alignment.txt` — applied delays, implied arrival offsets,
    level trims, and crossover phase checks. When passive-cardioid MF combine
    is enabled, the crossover sum uses the combined MF+port response.
    Per-source clamped solves are interpolated onto the widest solved grid
    and contribute nothing above their own band.
- `vituixcad/` when `Export VituixCAD FRDs` is checked: per-driver per-angle
  `.frd` sets under `hor/` and `ver/` plus a `README.txt`. When the LR4
  crossover alignment completes, the folder also contains
  `HornLab_active_lr4.vxp` with the generated driver entries, active LR4
  high/low-pass blocks, buffer gain trims, and buffer delays. All drivers
  share one mesh, mic grid, and timing reference, so the exported phase
  already carries every inter-driver path/delay difference; keep driver
  X/Y/Z offsets at 0. Phase follows the measurement convention (a later
  arrival falls with frequency), with the common time of flight removed.
  Uncoupled direct BEM drivers remain unit-source-drive SPL (arbitrary
  per-driver scale; the solver drives sources at unit normal acceleration) and
  have no electrical side. Coupled direct drivers export voltage-driven FRDs
  and their `<NAME>_impedance.zma` is copied into the VituixCAD folder. The
  passive-cardioid combined MF exports as `MF_cardioid`; with coupled mode
  enabled its FRDs use the voltage-driven MF+port field and
  `MF_passive_cardioid_impedance.zma` is copied into the VituixCAD folder for
  that driver. Raise `Number of frequencies` (120-200) for crossover-design
  resolution.
- `manifest.json`
- `fusion_wg_pipeline_manifest.json`
- `final_summary_manifest.json`
- `fusion_addin_launch.json` with the command, PID, output folder, expected
  log/manifest paths, and final status
- command logs under `logs/`

## Dialog

Inputs are grouped: **Sources and mesh** (per-source resolutions, rigid body
resolution, transition distance), **Mesh sizing** (refine overrides and the live
Estimate readout), **Solve** (frequency band and polar grid, mesh-only toggle,
mesh-valid clamp, mesh-valid plot-marker toggle), **Passive cardioid MF**
(optional MF plus `PORT_EXIT` postprocess combine), **Driver LEM (optional)**
(per-source T/S text or Hornresp driver-file path, rear volumes, shared drive
voltage and source resistance), **Output** (output root, open folder), and
**Advanced** (mirror plane override, Python interpreter, Waveguide Generator
folder, launch WG). Typical 3-way values:

```text
LF source mesh mm: 20
MF source mesh mm: 10
HF source mesh mm: 5
Port exit mesh mm:
Rigid body mesh mm: 20
Refine overrides:
Passive cardioid MF:
  Combine MF + port exit: off
  Rear chamber L:
  Port length mm: 0
  Port area cm2:
  Foam resistance Pa s/m3: 0
  Rear-wave polarity: on
  Couple driver LEM: off
Driver LEM (optional):
  LF driver T/S:
  LF rear chamber L:
  MF driver T/S:
  MF rear chamber L:
  HF driver T/S:
  HF rear chamber L:
  Drive voltage V RMS: 2.83
  Generator Rg ohm: 0
```

The pipeline uses canonical tags: `LF=2`, `MF=3`, `HF=4`,
and `PORT_EXIT=10`. `PORT_EXIT` is a combined in-phase aperture group: if two
side-port mouth faces both carry that appearance/name, they are solved as one
source basis. For independent left/right termination studies, command-line
source specs may still use separate names and tags such as `PORT_EXIT_L=10`
and `PORT_EXIT_R=11`. The passive-cardioid MF combine expects a solved
`PORT_EXIT` basis and combines it with the solved `MF` basis. The port-exit
tags are for reduced LEM/TMM radiation-impedance termination studies; leave
them blank for normal direct source solves. The matrix artifact stores the
solver convention and the engineering convention separately; use
`engineering_impedance_matrix` or the summary's `in_phase_termination_load`
for the LEM/TMM side.

Mirror plane override values map to cut planes as before:

| Fusion mirror plane | Cut plane | Pipeline behavior |
|---|---|---|
| `Auto detect` (default) | from mesh free edges | native solve when supported |
| `Left/Right + Front/Back` | `x=0` and `y=0` | Native quarter-domain solve (`yz+xz`) |
| `Left/Right` | `x=0` | Native half-domain solve (`yz`) |
| `Front/Back` | `y=0` | Native half-domain solve (`xz`) |
| `Top/Bottom` | `z=0` | Native half-domain solve (`xy`) |
| `Full model` | none | Full-domain solve |

Source resolution is the per-source mesh-size knob. `Rigid body mesh mm` is the
background size for rigid surfaces away from source refinement. If `Rigid body
mesh mm` is blank, the pipeline falls back to the coarsest declared source
resolution. The STEP mesher pins source patches at their chosen size and
applies source-local Distance/Threshold fields so the near-field grades from
the source size to the background over the transition distance. `Refine
overrides` paint specific appearance/shell names at a chosen explicit
millimetre size without changing their physical (rigid) tag.

The dialog remembers the last values used after a run. Settings are stored in:

```text
~/Library/Application Support/HornLab/WGMetalPipeline/settings.json
```

Direct solves use the canonical `hornlab_metal_bem` package — from the
`hornlab-metal-bem` sibling checkout when present, otherwise from the active
Python environment (native Metal dense assembly with an Accelerate `cgesv`
direct solve) — not the Waveguide Generator server and not any legacy
in-tree solver copy. The observation frame is auto-derived (see
above); the explicit fallback frame remains:

```text
axis=+Z
origin=0,0,0.31 m
u=+X
v=+Y
```

Launching the sibling `Waveguide Generator` checkout remains optional and is
off by default.
