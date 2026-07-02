# Roadmap

Prioritized improvement plan for the WGMetalPipeline add-in and pipeline.
Ordered by leverage-per-complexity; the add-in should stay small, so each
item states what it *removes* or reuses, not just what it adds.

Status 2026-07-02: items 1, 2, and 4 DONE. Item 2 followed the accepted plan in
[plans/per-driver-lem-coupling.md](plans/per-driver-lem-coupling.md).
Item 3's N-driver table is DEFERRED by owner decision (three fixed slots are
enough for now); its T/S + Hornresp-import half moved into item 2.

## 1. Remove the `frequency-role` sizing mode (simplification) — DONE

`manual-mm` has been the default and the recommended mode since settings
v10; `frequency-role` sizes every radiating surface for the single band top
(20 kHz → ~2.9 mm everywhere), which is usually far denser than needed.

Remove the mode entirely:

- dialog: `mesh_sizing_mode` dropdown, the three elements/wave dials,
  `mesh_mode_summary`
- `fusion_pipeline_launch.py`: mode normalization/aliases, EPW plumbing
- `scripts/wg_mesh_sizing.py` + `scripts/prepare_step_for_wg_metal.py`:
  the `f_max_hz` branch of `role_size_mm` and the role-EPW dials
- tests covering the frequency path

**Keep** the mesh-valid band *reporting* (`valid_f_max_hz`, clamp policy,
plot markers, the live Estimate readout): manual-mm depends on it. Only the
frequency-driven *sizing* goes. Estimated net: ~150–200 lines deleted.

## 2. Per-driver LEM coupling: T/S parameters, ZMA, and true levels — DONE

The highest-leverage feature. Before this work only the passive-cardioid MF
path had a voltage-driven Thiele/Small driver model; direct BEM drivers were
unit-normal-acceleration sources, so their exported FRDs needed manual
per-driver level scaling and had no impedance — exactly what blocked passive
crossover design in VituixCAD.

Implemented by generalizing the existing `Couple driver LEM` machinery
(`hornlab_sim.methods.driver_coupling`) to direct drivers:

- per-driver T/S parameters (Sd, Bl, Re, Le, Mmd/Mms, Cms/Vas/Fs, Qms/Rms)
- voltage-driven SPL at the chosen drive level → correct *relative* driver
  levels in the crossover sum and absolute SPL/2.83 V curves
- per-driver electrical impedance → one `.zma` per driver in the VituixCAD
  export (the cardioid MF already does this; extend to LF/MF/HF)
- per-driver excursion curves with Xmax markers where supplied

With FRDs (magnitude + phase, off-axis, shared timing reference — already
exported) plus per-driver ZMA, VituixCAD can do full **passive** crossover
design natively. Simulating the passive network inside the add-in stays out
of scope: VituixCAD is the better network simulator; the add-in's job is to
feed it complete data.

Completed in stages:

- per-source `--driver-lem` specs and Hornresp driver-file parsing
- per-source rear chamber values plus shared drive voltage and generator Rg
- voltage-driven direct-source FRDs, crossover input, excursion PNGs, and ZMAs
- VituixCAD export copies for coupled direct drivers and coupled cardioid MF
- settings v12 dialog migration with a compact `Driver LEM (optional)` group

## 3. N drivers instead of fixed LF/MF/HF slots — DEFERRED (owner decision)

Replace the three fixed source rows with a driver table: add/remove/duplicate
entries, each with a name (`LF`, `MF`, `MF2`, `HF`…), mesh resolution, a
stable mesh tag, an ordered position in the crossover chain, and optional
T/S parameters (from item 2). Known touch points, all mechanical:

- role/priority maps and `_source_role` plot styling in
  `solve_fusion_wg_metal.py`
- `_crossover_chain` generalized to an ordered N-way cascade (N−1 crossover
  frequencies); a 2-way must remain the trivially working case
- VituixCAD export member list and the generated `.vxp` filter chains
- canonical tag registry in `fusion_step_to_wg_pipeline.py` (today
  LF=2, MF=3, HF=4, PORT_EXIT=10) — allocate new stable tags
- dialog: table-style group instead of three string rows

**Hornresp driver import**: Hornresp driver files are trivially parseable
`Key=value` text (Sd, Bl, Cms, Rms, Mmd, Le, Re, …). A "Load Hornresp
driver" button that fills a driver's T/S fields (with unit conversion —
Hornresp stores Cms in m/N, Mmd in g, Sd in cm²) is a small, high-comfort
add-on once the driver table exists. Note Hornresp provides **Mmd** (dialog
currently exposes Mms with the radiation-mass correction; the CLI already
accepts Mmd — expose it in the dialog with the import).

## 4. Cheap new outputs from data the pipeline already has — DONE

Per-driver complex pressure over the full angle grid is already stored in
`<source>_pressure_basis.npz`; these now fall out of it in normal runs and
`--postprocess-only` reruns:

- **Directivity index + power response** (integrate intensity over the
  polar grid) and **beamwidth vs frequency** (-6 dB width), written per source
  and for the aligned crossover sum as PNG, CSV, and JSON.
- **Group delay** from `-d(unwrap(angle(p_engineering)))/d(omega)`, per-driver
  on axis and for the aligned sum. The implementation uses the stored
  engineering `e^{+j omega t}` pressure; a synthetic `e^{-j omega tau}` delay
  reports `+tau`.
- **Phase overlay** on frequency-response PNGs using wrapped engineering phase.

The DI/power response is a solid-angle-weighted polar-cut approximation:
stored horizontal/vertical cuts are not a full sphere, so the plot caption and
JSON report state that intensity is plane-averaged at each polar angle and
extrapolated to `4*pi`.

Deliberately later: impulse/step responses (needs a dense linear frequency
grid — a solve-cost decision, not a post-processing one).

## 5. Output panel and structured run folders

A run folder currently collects 20+ files flat at the root. Two changes:

- **Dialog**: replace the single `Export VituixCAD FRDs` toggle with an
  "Outputs" group listing every artifact category with a checkbox —
  per-driver plots, combined/crossover set, cardioid set, VituixCAD export,
  radiation-impedance matrix, pressure bases — so everything the pipeline
  *can* produce is visible in one place.
- **Folder layout**: subfolders per category (`sources/`, `combined/`,
  `cardioid/`, `vituixcad/`, `logs/`), manifests at the root.
  `direct_solve_manifest.json` already maps logical names → paths, so
  readers should go through the manifest; add a `layout_version` key and
  keep `regenerate_fusion_derived_artifacts.py` able to read both layouts
  (old runs must stay postprocess-able).

## 6. Post-hoc exploration ("side viewer app") — deliberately minimal

The pressure bases make any crossover change a cheap linear recombination —
no re-solve needed — and `regenerate_fusion_derived_artifacts.py` already
re-runs the postprocess (new crossover frequencies, plots, exports) against
stored bases. Rather than a new GUI application:

- rely on **VituixCAD** for interactive filter experimentation (item 2
  completes its inputs; its live six-pack is better than anything a custom
  viewer would reach soon)
- add a **static HTML report** per run (PNG gallery + manifest summary) and
  a small index page over the output root for flicking through runs without
  Fusion
- revisit a real viewer only if these prove insufficient — a custom app is
  a permanent maintenance surface and mostly duplicates the two above

## 7. Smaller ideas

- **Named presets**: save/load full dialog configurations as JSON (the
  settings file only remembers last-used values); presets travel with a
  design and double as headless-rerun configs.
- **Headless batch**: document running `fusion_step_to_wg_pipeline.py`
  against an already-exported STEP for parameter sweeps without Fusion.
- **Half-space / ground-plane mode**: the Metal solver has an `xy` symmetry
  kernel; a "rigid floor" toggle would emulate ground-plane measurement
  conditions (needs observation-frame care on the mirrored axis).
- **A/B compare**: overlay on-axis + DI curves of two run folders into one
  report page.

## Sequencing

1. Item 1 (delete frequency-role) — negative complexity, do first
2. Item 2 (per-driver LEM/ZMA) — flagship; unlocks passive XO work
3. Item 3 (N-driver table + Hornresp import) — builds on 2
4. Item 4 (DI/power/group delay/excursion) — cheap, parallelizable
5. Items 5–6 (outputs panel, folders, HTML report) — UX consolidation
6. Item 7 as opportunity allows
