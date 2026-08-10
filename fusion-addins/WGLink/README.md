# WGLink for Fusion

WGLink inserts a Waveguide Generator `.wglink` bundle as native, managed
Fusion history. It supports the two solid WG export modes:

- `enclosure`: the realized enclosure block with both edge treatments, minus
  the waveguide cavity;
- `freestanding`: the fitted-spline surface, throat patch, stitch, and outward
  parameter-driven wall thickness that form the freestanding waveguide solid.

The add-in is licensed under the repository's AGPL-3.0 license. Its manifest
author is `m3gnus <megamaggi@gmail.com>`.

## What WGLink manages

Each Assembly insertion owns one movable wrapper occurrence. The wrapper holds
the WG parameters, datum planes and axis, stable throat and mouth interface
sketches, ring sketches, native features, final body, source-role face, and the
attributes/entity tokens that identify them. Move and joint the wrapper, not an
individual managed body.

Fusion Part Design documents reject a second component. Insert therefore has
an explicit root-component fallback, enabled by default. Its report warns that
a root link cannot be moved or jointed as a unit. Start with an Assembly when
that behavior matters, or disable `allow_root_fallback` in a head-less call to
make Insert refuse instead.

WGLink pushes only manifest parameters whose role is `interface`. Existing
parameters are updated by assigning their expression; they are never deleted
and recreated. Informational parameters remain JSON metadata, and unrelated
`wg_*` parameters are left alone.

The supported reference layer is:

- `WGI_THROAT_SKETCH` and `WGI_MOUTH_SKETCH`;
- WG datum planes and `WG_AXIS`;
- the managed enclosure or waveguide body, with the documented update limits
  below.

## Commands

- **Insert** selects a bundle folder, validates it before Fusion mutation, and
  builds the full WG viewport model.
- **Update** reads the stored bundle path, resamples the new grid outside
  Fusion, validates the existing sketch topology, rolls the timeline back, and
  moves fit points in place. It creates and deletes no document features.
- **Audit** reports bundle/link state, pushed-parameter drift, source tag state,
  feature health, and evidence that the managed body is unmodified, modified,
  missing, or unknown.
- **Relink** records a moved or renamed bundle path. The design id must match
  unless the caller explicitly forces the operation.
- **Detach** removes `WGLink` attributes only. Bodies, sketches, features, and
  appearances remain in the document.

Insert and Relink remember the last bundle folder. When a document has multiple
links, enter the instance id in the command dialog so the command does not have
to guess.

## Update atomicity and recovery

Fusion offers no transaction covering parameter edits and sketch fit-point
moves. WGLink validates identity, build mode, bundle content, resampler output,
ring counts, points per ring, interface sketches, and rollback availability
before the first mutation. It then performs one rolled-back pass and restores
the timeline marker for a single recompute. A progress JSON file is written
after every ring.

That is the strongest honest boundary Fusion exposes; it is not a database
transaction. If a rebuild fails after mutation begins, use **Undo** to recover
the document. Do not continue modelling on a partially failed rebuild.

## What Audit can and cannot prove

Audit is evidence, not a freshness authority. CAD cannot author WG freshness.
It can observe a missing or changed body, parameter drift, the geometric source
tag, and unhealthy timeline features. Fusion has no face-to-feature reverse
index, so Audit cannot enumerate every direct B-rep reference and cannot detect
a reference that silently rebound to the wrong face. A green Audit therefore
does not prove semantic reference correctness. Direct face/edge references are
unsupported where a stable interface sketch or datum can be used instead.

The source-role appearance is reasserted geometrically after every Update.
This removes appearances that spread to derived faces during recompute and
repaints the one planar throat disc at the expected realized area.

## Referencing managed artifacts from your own component

The managed datums, sketches and body live inside the wrapper component, so a
sketch or feature in the root cannot reference them directly — Fusion answers
`planarEntity is not in the assembly context of this component`. Reach them
through the occurrence, which is ordinary Fusion practice:

```python
plane = occurrence.component.constructionPlanes.itemByName("WG_BAFFLE_PLANE")
host = plane.createForAssemblyContext(occurrence)   # now usable from the root
```

The same applies to faces of the managed body. In the UI this is automatic —
clicking the datum inside the occurrence picks the proxy for you.

## Measured behaviour, Fusion 2704.1.53

Tritonia-V enclosure and asro68 freestanding, throwaway documents:

| | measured |
|---|---|
| enclosure volume vs the mesher's own solid | **−0.013 %** on insert, **−0.015 %** after an update |
| enclosure bounding box | exact on all six faces, before and after |
| cavity surface vs the mesher's grid | mean **0.009 mm**, p95 **0.001 mm**, max 0.23 mm |
| update | 2572 fit points moved, **0 features regressed**, including a user feature built on a managed datum |
| source tag | one face, 506.70 mm², 0 strays, before and after |
| freestanding solid vs the mesher's | −0.75 % (the mesher exports an opened throat; the native build has a driver plate) |

The deviation maximum is a handful of points at the mouth lip, where the mesher
clusters five stations into the last 1.5 mm of z. Curvature-aware sectioning
took it from 0.865 mm to 0.23 mm; closing the rest costs minutes of rebuild
time per update for a tenth of a millimetre on one lip. Raise `max_sections`
(default 40) in a head-less call if a project needs it.

**Not verified:** §5.6's moved-wrapper case. Setting `occurrence.transform2`
from a script reads the new matrix straight back, but after
`design.snapshots.add()` the occurrence is at identity again and nothing has
moved — reproduced on a plain component with no WGLink involvement, so it is
Fusion's scripted-move behaviour rather than the add-in's. Dragging the wrapper
in the UI and pressing Update is therefore still an unchecked path.

## Install

Run from the repository checkout:

```sh
.venv/bin/python scripts/install_fusion_wg_metal_addin.py \
  --addin WGLink --symlink
```

The symlinked install is recommended because Update must invoke the repository
`.venv` and `scripts/wglink_resample.py`; Fusion's embedded Python does not have
the scientific interpolation stack. A copied install can still work when
`HORNLAB_FUSION_ADDIN_REPO` points to the checkout, or when a head-less caller
passes `repo_root` and `python_path` options.

Restart Fusion, open **Utilities > Add-Ins**, start WGLink, and use the WGLink
toolbar panel. The manifest deliberately sets `runOnStartup` to `false`, so no
heavy geometry work runs while Fusion is starting.
