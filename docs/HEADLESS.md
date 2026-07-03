# Headless WG Metal Runs

Use this when Fusion has already exported a STEP file and you want to rerun the
pipeline, sweep parameters, or refresh reports without opening Fusion.

## Re-run An Exported STEP

Save a preset from the Fusion dialog when you want the headless run to reuse
the same mesh, solve, output, Driver LEM, and postprocess settings. Presets live
under:

```text
~/Library/Application Support/HornLab/WGMetalPipeline/presets/<name>.json
```

Then run the pipeline directly:

```bash
.venv/bin/python scripts/fusion_step_to_wg_pipeline.py \
  --step runs/fusion360/<old-run>/<design>.step \
  --out runs/fusion360/headless-rerun \
  --preset <name-or-path> \
  --run-solves \
  --skip-missing-sources
```

`--preset` accepts either a saved preset name or a JSON path. Explicit CLI flags
override preset values, so keep `--step`, `--out`, and `--run-solves` in the
command even when the rest comes from the preset.

Without a preset, provide the source mesh settings explicitly:

```bash
.venv/bin/python scripts/fusion_step_to_wg_pipeline.py \
  --step exports/design.step \
  --out runs/fusion360/manual-headless \
  --sources LF:20,MF:10,HF:5 \
  --transition-mm 200 \
  --rigid-res-mm 20 \
  --freq-min-hz 50 \
  --freq-max-hz 20000 \
  --freq-count 60 \
  --freq-spacing log \
  --run-solves \
  --skip-missing-sources
```

## Sweep A Parameter

When overriding sources, provide the full source set because `--sources`
replaces the preset source fields:

```bash
for hf_mm in 4 5 6; do
  .venv/bin/python scripts/fusion_step_to_wg_pipeline.py \
    --step exports/design.step \
    --out "runs/fusion360/sweep-hf-${hf_mm}mm" \
    --preset baseline \
    --sources "LF:20,MF:10,HF:${hf_mm}" \
    --run-solves \
    --skip-missing-sources
done
```

The same pattern works for solve settings, for example overriding
`--freq-count`, `--crossover-lf-mf-hz`, `--crossover-mf-hf-hz`, or
`--plot-theme`.

## Regenerate Derived Artifacts And Reports

To refresh postprocess artifacts from an existing run folder without re-solving:

```bash
.venv/bin/python scripts/regenerate_fusion_derived_artifacts.py \
  runs/fusion360/<run-folder>
```

Then rebuild the per-run report and the output-root index:

```bash
.venv/bin/python scripts/render_run_report.py runs/fusion360/<run-folder>
.venv/bin/python scripts/render_run_report.py --index runs/fusion360
```

## Compare Two Runs

Generate an A/B report with on-axis response, DI, beamwidth, group delay, and a
config diff table:

```bash
.venv/bin/python scripts/compare_runs.py \
  runs/fusion360/run-a \
  runs/fusion360/run-b \
  --out runs/fusion360/compare-run-a-run-b \
  --plot-theme hornlab
```

The compare script reads layout-1 and layout-2 run folders through their
manifests. It uses the aligned crossover sum when the manifest and pressure
bases can reconstruct it; otherwise it overlays per-driver curves.
