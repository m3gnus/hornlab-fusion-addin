# Per-driver LEM coupling (ROADMAP item 2)

Status: design accepted, implementation staged. Owner note: scope deliberately
keeps the fixed LF/MF/HF slots (no N-driver table — deferred by owner
decision 2026-07-02).

## Goal

Every solved driver (LF, MF, HF), not just the passive-cardioid MF, can
optionally carry a voltage-driven Thiele/Small model. When a driver has T/S
parameters:

1. its SPL curves and exported FRDs become **absolute SPL at the drive
   voltage** (default 2.83 V RMS) instead of unit-cone-velocity levels that
   need manual per-driver scaling;
2. the run writes a per-driver **`.zma` electrical impedance** file, copied
   into the VituixCAD export — with FRD (mag+phase, off-axis, shared timing)
   + ZMA per driver, VituixCAD can do full passive crossover design;
3. the run plots **cone excursion vs frequency** at the drive voltage (with
   an Xmax line when provided).

Passive crossover *network simulation* stays out of scope — VituixCAD owns
that. Drivers without T/S keep today's unit-velocity behavior.

## Pre-work landed 2026-07-02 (codex-review findings, fixed)

The codex design review surfaced two real bugs, both fixed and
regression-tested before this feature starts:

1. **Forwarding gap**: the add-in dialog emitted `--passive-cardioid-coupled`
   and the driver flags, but `fusion_step_to_wg_pipeline.py` neither parsed
   nor forwarded them — dialog-launched coupled runs died at argparse. Fixed
   with a `PASSIVE_CARDIOID_COUPLED_FORWARD_OPTIONS` table + parity test.
2. **Normalization**: direct source bases are solved at unit normal
   **ACCELERATION** (`SolveConfig.velocity_mode` default; the docs wrongly
   said unit velocity), while the aperture-matrix solves force VELOCITY mode
   (`hornlab_sim/radiation_impedance.py:138`). The coupled-cardioid scaling
   multiplied cone *velocity* onto an acceleration basis → −6 dB/oct tilt and
   −90° phase error. Fixed via `_voltage_drive_pressure` (scale by cone
   acceleration `j*omega*U/S`), unit-tested. New basis NPZs carry
   `source_normalization="unit_normal_acceleration"`; a missing key means the
   same (acceleration has always been the effective mode).

## Physics and data flow

- The BEM solves each driver as a unit-normal-ACCELERATION source and
  captures `surface_pressure_avg[tag]` per frequency: area-weighted complex
  average surface pressure on the source patch, "pascals per unit drive",
  solver convention `e^{-i omega t}`, NOT normalized
  (`hornlab_metal_bem/bie.py::compute_surface_pressure_avg`).
- Radiation self-impedance of driver d: convert in SOLVER space first using
  the solver's own drive relation (`bie.py`: `v_n = weight/(1j*omega)`, so
  per-unit-velocity pressure = `1j*omega * p_avg_per_unit_weight`), then
  conjugate to engineering convention, then divide by volume velocity:
  `z_dd_eng = conj(1j*omega*p_avg_solver) / S_d`. Do NOT re-derive with a
  different j-sign order; add a tested helper next to
  `hornlab_sim.radiation_impedance.termination_load_from_solver_matrix` so
  both paths share the conversion.
- **No extra BEM cost**: z_dd comes from the driver's own direct solve. We
  deliberately ignore driver↔driver mutual coupling (drivers occupy
  different bands; the cardioid path needed mutuals only because port and MF
  are co-located and in-band). The manifest should record this assumption.
- New `hornlab_sim.methods.driver_coupling.coupled_direct_radiator_response`:
  the existing `coupled_cardioid_response` algebra minus the port branch —
  driver branch `z_drv = ras + s*mas + 1/(s*cas) + z_em`, acoustic load
  `z_load = z_self + z_rear_chamber` where the optional sealed rear chamber
  adds `1/(s * C_rear)`, `C_rear = V_rear/(rho*c^2)` (per-driver optional; a
  real MEH cone driver usually has a sealed back volume that raises its
  in-box resonance — measured Cms is free-air, the box term is additive).
  Returns cone volume velocity, electrical input impedance, excursion,
  acoustic load, diagnostics — same result-shape philosophy as the cardioid
  result. Shares `_effective_mmd`: Mmd is preferred verbatim; when only Mms
  is given, the two-face free-air radiation-mass estimate is subtracted to
  *estimate* Mmd before the BEM load is applied (the BEM z_self supplies the
  actual radiation loading). The Mms path is an approximation — warn loudly
  in the log/manifest rather than presenting it as exact.
- Scaling a solved basis to voltage drive uses the (fixed)
  `_voltage_drive_pressure` helper:
  `p_V(f) = (j*omega*U_cone(f) / S_d) * p_basis(f)` — cone ACCELERATION
  times the per-unit-acceleration basis.
- `.zma` format: existing `freq_Hz |Z| phase_deg` writer
  (`solve_fusion_wg_metal.py` cardioid path) — reuse it.

## Interaction with existing features

- **Passive-cardioid coupled MF**: when `--passive-cardioid-coupled` is on,
  the cardioid path owns MF (port branch + mutuals). Per-driver coupling must
  NOT double-apply to MF in that case. Unify parameter sourcing: the cardioid
  path reads MF's T/S from the same per-driver parameter set; the 11
  dedicated `passive_cardioid_driver_*` dialog fields are removed
  (settings v12 stale keys; CLI flags kept as deprecated aliases that feed
  the MF driver spec, so existing command lines keep working).
- **Crossover alignment**: keep level-match trims for the DSP-aligned sum
  (they emulate DSP gains); when all summed drivers are voltage-driven,
  report trims as physically meaningful dB-at-amplifier values. The
  additional "un-trimmed voltage-true sum" plot is DEFERRED until the
  export semantics have settled (review recommendation) — FRD/ZMA export
  ships first. Mixed runs (some coupled, some not) keep today's level-match
  semantics.
- **VituixCAD export**: coupled drivers export voltage-driven FRDs plus
  `<name>_impedance.zma`; uncoupled drivers keep unit-velocity FRDs and the
  README note about manual scaling.

## Dialog / CLI design (keep the dialog small)

One new group `Driver LEM (optional)` replacing the cardioid driver fields:

- Per driver (LF/MF/HF), ONE string input: `LF driver T/S`. Accepts either
  a **path to a Hornresp driver file** or **pasted `Key=value` text**
  (Hornresp driver format: `Sd=320.0`, `Bl=11.6`, `Cms=252.0E-06`,
  `Rms=3.18`, `Mmd=26.2`, `Le=0.8`, `Re=5.2`, `Xmax=5.5`, …). Blank = not
  coupled. This makes "load Hornresp driver configs" a paste/path, no extra
  import UI.
  - Unit map (Hornresp → internal): Sd cm²→m², Bl T·m, Cms m/N (beware the
    `E-06` exponents — Hornresp stores m/N, the old dialog used mm/N), Rms
    kg/s (mechanical ohms), Mmd g→kg, Le mH, Re ohm, Xmax mm. Accept both
    `Mmd` and `Mms` keys; accept optional `Nd`/`n` driver count, `Vrc`-style
    keys ignored with a warning (system params, not driver params).
- Per driver, one optional `LF rear chamber L` value (sealed back volume).
- Shared `Drive voltage V RMS` and `Generator Rg ohm` (moved from the
  cardioid group; the cardioid keeps its chamber/port/foam/polarity fields).

CLI (pipeline + solve script):
- `--driver-lem "NAME:Sd=320,Bl=11.6,Re=5.2,Mmd=26.2,Cms=2.52e-4,Rms=3.18,Le=0.8,Xmax=5.5"`
  (repeatable, one per driver; keys case-insensitive, same unit conventions
  as the Hornresp file so a file's content can be passed through verbatim).
- `--driver-rear-volume-l NAME:VALUE` (repeatable).
- `--drive-voltage`, `--rg-ohm` shared (defaults 2.83 / 0).
- Deprecated aliases: `--passive-cardioid-driver-*` map onto the MF spec.

## New artifacts

Per coupled driver `<NAME>`:
- `<NAME>_impedance.zma` (+ copy in `vituixcad/` when export enabled)
- `<NAME>_excursion.png` (excursion vs f at drive voltage, Xmax line if given)
- manifest `driver_lem` block per source: parameter echo (with unit
  normalization + Mmd source), z_self provenance, excursion band max,
  Z_e min/max, coupling assumption note (self-impedance only).

## Implementation stages

- **Stage A (hornlab-sim, sibling HornLab repo)**:
  `coupled_direct_radiator_response` + unit tests (analytic sanity: at low f
  with z_self→0 and no rear chamber the electrical impedance shows the
  free-air resonance from Mmd+radiation-free Cms; adding V_rear raises f_res;
  excursion ~ V/(omega^2) mass-controlled asymptote; energy sanity vs the
  cardioid function with a degenerate port).
- **Stage B (solve script)**: driver-spec parsing; z_dd from
  `surface_pressure_avg` (verified: complex values survive the results JSON
  as `{"real":..,"imag":..}` objects via `_jsonable`, raw SOLVER convention
  — conjugate on load; postprocess-only reruns copy the previous JSON's
  averages with `{}` fallback). For NEW runs, also store
  `surface_pressure_avg` + patch area + `source_normalization` inside the
  per-source pressure-basis NPZ so postprocess never has to mine JSON
  (review recommendation, folded into the existing artifact instead of a
  new file); coupling application + basis scaling via
  `_voltage_drive_pressure`; zma + excursion artifacts; crossover/vituix
  integration; manifest blocks.
- **Stage C (launch helper + dialog)**: T/S string parser (text or file
  path), new dialog group, settings v12 migration (stale cardioid driver
  keys), command builder args, estimate unaffected.
- **Stage D**: addin README rewrite of the coupled section, ROADMAP check-off,
  full pytest suite, hornlab-sim suite, HornLab impact check (cross-repo).

## Open questions (RESOLVED by the 2026-07-02 codex review — answers below)

1. Symmetry-reduced patch area: CONSISTENT — the surface average and the
   tagged area come from the same (reduced) mesh, so the reduction cancels
   in `p_avg / S_d`; use the mesh tag area, never datasheet Sd, for S_d.
2. Basis normalization: sources are driven at unit normal ACCELERATION, not
   velocity (`SolveConfig` default; no `velocity_mode` override in the solve
   script). See "Pre-work landed" — scaling and z_dd formulas account for it.
3. Complex JSON: survives as `{"real":..,"imag":..}` objects (`_jsonable`),
   raw solver convention; postprocess-only copies the prior JSON with `{}`
   fallback. New runs also embed the averages in the basis NPZ (Stage B).
4. Mms vs Mmd: `_effective_mmd` uses Mmd verbatim when given; otherwise
   estimates Mmd = Mms − two-face free-air radiation mass before the BEM
   load is applied. Correct as an estimate; warn loudly on the Mms path.
5. Le model: `bandpass.Driver` supports simple `Le` and LR-2 (`le2_h` +
   `re2_ohm`, both required together). Hornresp semi-inductance forms
   (`Leb`/`Ke`/`Rss`) are not supported — warn and ignore.
