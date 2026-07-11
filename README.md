# hornlab-fusion-addin

A Fusion 360 add-in plus background pipeline for simulating loudspeaker
designs directly from CAD: export the active design to STEP, mesh it with
acoustic source tags (gmsh), and run native Apple Metal BEM solves with
crossover summing, passive-cardioid post-processing, and VituixCAD export.
An opt-in MF chamber path exports a separate watertight air volume, solves its
3D pressure field with tetrahedral FEM, and couples its individual entry flows
to the existing exterior Metal BEM bases.

The add-in lives in `fusion-addins/WGMetalPipeline/`; it launches the
pipeline in `scripts/` as a background process so Fusion stays usable while
meshing and solves run:

1. `scripts/prepare_step_for_wg_metal.py` — STEP → tagged, role-sized meshes
2. `scripts/diagnose_wg_metal_orientation.py` — orientation/symmetry checks
3. `scripts/solve_fusion_wg_metal.py` — per-source Metal BEM solves, crossover
   alignment, passive-cardioid combine, plots, VituixCAD FRD/ZMA export

## Docs

- [User guide](docs/USER-GUIDE.md)
- [Headless reruns, sweeps, and A/B compare](docs/HEADLESS.md)
- [Pipeline, dialog, and output reference](fusion-addins/WGMetalPipeline/README.md)

## Install

```bash
git clone https://github.com/m3gnus/hornlab-fusion-addin.git
cd hornlab-fusion-addin
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python3 scripts/install_fusion_wg_metal_addin.py --symlink --replace
```

Restart Fusion, then enable it under Utilities > Add-Ins > WGMetalPipeline.
The add-in runs the pipeline with the interpreter configured in the dialog
under Advanced > Python (default: `<repo>/.venv/bin/python` when present).

## Dependencies

- Meshing/diagnostics and chamber FEM need `numpy`, `scipy`, `gmsh`, `meshio`.
- Direct solves need [hornlab-metal-bem](https://github.com/m3gnus/hornlab-metal-bem)
  (Apple Silicon) plus the `hornlab_sim` and `hornlab_plots` packages.
  `requirements.txt` installs all three from their standalone GitHub
  repositories. Discovery prefers top-level workspace siblings next to this
  repo (`../hornlab-sim`, `../hornlab-plots`, `../hornlab-metal-bem`) when
  present, then packages installed in the active environment; no legacy
  monorepo checkout is probed. Without them the mesh-only path (`Mesh only` in
  the dialog) still works.

## Tests

```bash
.venv/bin/python -m pytest tests/
```

Some solve tests exercise a real smoke mesh and skip automatically when
`runs/scratch/260609-fusion-addin-normalized-sources-smoke/` is absent
(run artifacts under `runs/` are never tracked).

## License

AGPL-3.0-or-later
