# WGLink for Fusion

WGLink inserts a Waveguide Generator `.wglink` bundle as native, managed
Fusion history. It supports the two solid WG export modes:

- `enclosure`: the realized enclosure block with one all-edge treatment, minus
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

The enclosure starts as a rectangular prism. Its four longitudinal edges and
both four-edge end perimeters are passed together to one parametric treatment:
`edge_type=1` makes one fillet and `edge_type=2` makes one chamfer, with the
distance/radius still driven by the link's `enc_edge` expression. Giving Fusion
all twelve original edges at once lets it construct the three-way corner
mitres; no feature runs over faces made by an earlier chamfer.

Every browser object created by Insert has a deliberate user-facing name.
Bodies use `WGLink enclosure`, `WGLink freestanding waveguide`, and `WGLink
waveguide cut tool`; the freestanding construction bodies are named `WGLink
waveguide surface`, `WGLink throat patch body`, and `WGLink stitched waveguide
body` while they exist. These names are presentation only. WGLink continues to
resolve ownership and topology exclusively through attributes and entity
tokens, never names.

Fusion Part Design documents reject a second component. Insert therefore has
an explicit root-component fallback, enabled by default. Its report warns that
a root link cannot be moved or jointed as a unit. Start with an Assembly when
that behavior matters, or disable `allow_root_fallback` in a head-less call to
make Insert refuse instead.

WGLink pushes manifest parameters whose role is `interface`. Enclosure links
also own `<parameter_prefix>mouth_overshoot` (5 mm by default), which drives the
join extrude that carries the cavity through the baffle. Existing parameters
are updated by assigning their expression; they are never deleted and
recreated. Informational parameters remain JSON metadata, and unrelated
`wg_*` parameters are left alone.

The supported reference layer is:

- `WGI_THROAT_SKETCH` and `WGI_MOUTH_SKETCH`;
- WG datum planes and `WG_AXIS`;
- the managed enclosure or waveguide body, with the documented update limits
  below.

## Commands

The panel promotes the three everyday commands, **Set WG Source…**, **Solve in
WG** and **Send to WG**. **Manage WG Link…** contains **Declare Body…**,
**Insert**, **Update**, and **Detach**.
Insert and Update are ordinarily automatic — WG's *Send to CAD* publishes a
handoff the add-in applies on its own — so the dropdown is maintenance,
authoring-remedy, and recovery UI rather than the normal workflow. Audit and
Relink remain full head-less APIs but are not panel commands.

- **Set WG Source…** marks the selected faces as the `LF`, `MF`, `HF` or
  `PORT_EXIT` drive source by creating (or reusing) an appearance named exactly
  after the role and painting it on. That appearance name *is* the convention
  the export reads, and it is the one thing a model built from scratch in Fusion
  cannot be sent without; before this command it had to be authored by renaming
  a Fusion appearance by hand. The same dialog clears a role, which strips only
  faces that actually carry one of the four roles — a face painted with your own
  material is left alone.
- **Solve in WG** writes the same validated `.wgreturn` bundle as Send to WG,
  then asks Waveguide Generator to prepare that exact bundle and start the
  solve, so WG is already solving when you switch to it. The request is a
  one-shot marker carrying its own command id, deliberately separate from
  `wgreturn.json`: WG re-reads returns whenever it re-lists the workspace, so
  intent recorded inside the immutable geometry evidence would be re-observed
  and re-solved. WG records each command id as spent, refuses a bundle whose
  manifest hash changed after the request, and stops at its own ingestion
  gates — unacknowledged blocking findings never solve automatically. There is
  no remembered "solve next time" preference: an expensive solve should not be
  a side effect of a later plain send.
- **Insert** offers the bundles already sitting in Waveguide Generator's
  workspace, validates the chosen one before Fusion mutation, and builds the
  full WG viewport model. **Send to CAD** in Waveguide Generator also publishes
  a one-shot handoff beside the completed bundle: WGLink inserts a new link into
  the active Fusion design automatically, including after a cold start. If that
  bundle is already linked in the active document, the existing watcher offers
  the normal in-place Update instead of inserting a duplicate.
- **Update** reads the stored bundle path, resamples the new grid outside
  Fusion, validates the existing sketch topology, rolls the timeline back, and
  moves fit points in place. Before its first mutation it also verifies that
  the tagged throat face remains in the component-local link frame. It creates
  and deletes no document features. If the stored path is missing, Update
  searches WG's current workspace for the same design id, selects its highest
  export sequence, records that path using the same identity guard as Relink,
  and continues. A manually moved bundle outside the workspace can still be
  selected through the head-less `wglink_core.relink` API.
- The head-less **`audit` API** reports bundle/link state, pushed-parameter
  drift, source tag state, feature health, the measured link-frame offset, and
  evidence that the managed body is unmodified, modified, missing, or unknown.
  The add-in continuously publishes the same document/link inventory to WG in
  `.fusion-status.json`, so Audit has no panel command.
- **Send to WG** observes the root or one occurrence subtree without changing
  the document, applies the explicit return-scope policy, and writes an atomic,
  checksummed `.wgreturn` bundle into WG's own workspace. The destination is
  not a dialog choice: WG only ingests from `<workspace>/wgreturn`, so a
  return written anywhere else is invisible to it. Names are collision-safe
  rather than overwriting, because a return WG has not ingested yet is not in
  content-addressed storage. A body can carry the `WGLink` attribute
  `return_declaration=exterior-shell` or `return_declaration=exclude` when its
  surface/exclusion intent cannot be inferred safely; **Declare Body…** writes
  and clears it.
- **Declare Body…** classifies the selected bodies as `exterior-shell` or
  `exclude`, or clears the declaration. A visible surface body with no
  declaration refuses the export outright, so this is the in-product remedy for
  modelling a horn as a loft surface rather than a solid.
- The head-less **`relink` API** records a manually moved or renamed bundle
  path outside the current WG workspace. The design id must match unless the
  caller explicitly forces the operation.
- **Detach** removes `WGLink` attributes only. Bodies, sketches, features, and
  appearances remain in the document. The panel command requires confirmation:
  identity removal is permanent, and inserting a fresh WG copy is the only way
  to obtain a managed link again.

## The workspace is WG's setting

The WGLink folder is chosen once, under **Settings → CAD Link** in Waveguide
Generator. It is separate from WG's run-output folder. WGLink reads it from
WG's own `cadlink_settings.json` — resolved the way WG resolves it,
`WG2_DATA_DIR` included — and lists the bundles in `<workspace>/wglink` in the
Insert dropdown, newest first, labelled by design name and export sequence.
Update also searches those bundles by design identity when a stored path no
longer exists. Nothing is ever written back to WG's settings file.

There is deliberately no second copy of the folder in Fusion. Storing one made the
first insert a two-place setup and let the two settings disagree, which inserts
a bundle WG is no longer writing to. When the workspace cannot be read — WG
never ran, the folder is on a disconnected drive, the bundle came from another
machine — the Insert dropdown falls back to the browse entries and manual picker
behaves as it always did.

## Duplicate-registration ownership and recovery

Fusion may load several registered WGLink paths into one Python process. Each
entry point now loads **all** of its executable `wglink_*` helpers beneath a
registration-unique package name; no registration borrows a helper module that
another checkout put in Python's ordinary import cache.

One deliberately shared, data-only broker elects the registration allowed to
own WGLink's machine-local IPC in that Fusion process. The model is a renewable
lease with two phases:

1. A registration atomically claims an absent or expired lease as
   `constructing`. It replaces any stale panel, creates every command/control,
   registers its private watch event, and only then commits the lease as
   `active`. Any exception rolls back the entire panel/watch transaction and
   releases the claim.
2. Only the active lease owner publishes `.fusion-status.json`, consumes
   handoff/return-request markers, or owns the command panel. Its background
   worker renews the lease without reading Fusion objects; all Fusion API work
   remains in the custom-event handler on Fusion's main thread.
3. Other registrations register only a private candidate event. They publish no
   session id, because they cannot service session-scoped requests. An orderly
   owner removes its panel/status before releasing. If its worker disappears,
   the lease expires after three watch intervals; exactly one candidate claims
   it, transactionally rebuilds the panel/watcher, and becomes the IPC owner.
   A delayed stop from the former owner sees that its lease is gone and cannot
   delete its successor's UI or event.

The broker is process-local by design: two Fusion processes have independent
sessions, while duplicate registrations inside either process must expose one
serviceable session. Fake-backed lifecycle tests cover rollback, two- and
three-registration promotion, orderly owner-first stop, expired-owner takeover,
and realistic occurrence-proxy attribute behavior. The same cases still need
verification in a live Fusion process before this recovery path is considered
field-validated.

## The Send and Solve pre-flight

Both export dialogs state what is about to happen before OK: bodies included,
WG links in scope, and each source with its role and measured area. A model
with no drivable source is named there rather than at the dead end after OK.

A return with no WG links in scope is legal — a model built from scratch in
Fusion is exported as an *unlinked* return — but it carries no throat frame, so
WG solves it in the assembly frame exactly as modelled: radiation along +Z,
throat at z = 0, bounding box centred on x = 0 and y = 0. Nothing enforces that
convention on either side, and a mis-framed model simply yields wrong
directivity, so the pre-flight measures the model against it and says which of
the three assumptions the model breaks. It never refuses.

Insert remembers the last bundle folder. When a document has several managed
links, Update and Detach show a **Managed link** dropdown listing them by design
name; a single-link document is not asked. Head-less Audit and Relink callers
choose with `options['instance_id']`. Send instead exposes an anchor choice only
when its selected scope contains several linked instances.

## Update atomicity and recovery

Fusion offers no transaction covering parameter edits and sketch fit-point
moves. WGLink validates identity, build mode, bundle content, resampler output,
ring counts, points per ring, interface sketches, and rollback availability
before the first mutation. It also compares the tagged throat face centre with
`(0, vertical_offset)` and its plane with the stored throat z. If a body Move
has carried the body away from its own datums, Update reports the measured
x/y/plane-z offset and refuses. Undo the body Move, or use **Detach** if the
geometry is now genuinely user-owned. Head-less callers can pass `force=True`
when proceeding is intentional. It then performs one rolled-back pass and
restores the timeline marker for a single recompute. A progress JSON file is
written after every ring.

The frame guard is deliberately component-local. Moving the whole wrapper
component moves its body and managed datums together, is a legitimate assembly
placement, and still passes. The guard only detects a body moved relative to
those datums. The head-less Audit API never refuses: it reports the offset and
marks the local body state `modified` when the invariant fails.

That is the strongest honest boundary Fusion exposes; it is not a database
transaction. If a rebuild fails after mutation begins, use **Undo** to recover
the document. Do not continue modelling on a partially failed rebuild.

## What the head-less Audit API can and cannot prove

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

Fusion grounds the first component created in a design to its parent. On Insert,
WGLink clears that flag from its wrapper so the component can be placed and
jointed. For a document built before this change, right-click the wrapper in the
browser and choose **Unground From Parent**, or re-run Insert.

## Measured behaviour, Fusion 2704.1.53

Tritonia-V enclosure and asro68 freestanding, throwaway documents:

| | measured |
|---|---|
| enclosure volume vs the mesher's own solid | **−0.0047 %** (53,558,269 mm³), one all-edge treatment |
| enclosure face inventory | 3 planar-z, 8 at 45°, **8 at 54.7°** (three-way mitres), 8 vertical, 1 cavity |
| enclosure bounding box | exact on all six faces, before and after |
| cavity surface vs the mesher's grid | mean **0.00016 mm**, max **0.0012 mm** |
| update | 2572 fit points moved, **0 features regressed**, including a user feature built on a managed datum |
| source tag | one face, 506.70 mm², 0 strays, before and after |
| freestanding solid vs the mesher's | −0.75 % (the mesher exports an opened throat; the native build has a driver plate) |

The pre-change deviation maximum was confined to the last three stations at
the mouth. It persisted as section density increased because the former axial
duplicate mouth ring forced the fitted loft's end tangent. The loft now ends at
the real mouth ring and a separate face extrude provides the punch-through;
Fusion-side deviation and volume measurements for that change are pending.

The one-feature enclosure measurement above is from the owner's hand-corrected
document. The implementation enforces a twelve-edge input before creating the
single treatment, but the add-in-built corner face inventory and resulting
volume still require the Fusion verification fixture.

**Not verified:** §5.6's moved-wrapper case. Setting `occurrence.transform2`
from a script reads the new matrix straight back, but after
`design.snapshots.add()` the occurrence is at identity again and nothing has
moved — reproduced on a plain component with no WGLink involvement, so it is
Fusion's scripted-move behaviour rather than the add-in's. Dragging the wrapper
in the UI and pressing Update is therefore still an unchecked path.

## Install

Waveguide Generator's macOS and Windows platform installers install a packaged
copy and point it at WG's own pinned scientific runtime. That is the end-user
path: it needs no hornlab-fusion-addin checkout and no second virtualenv.

For development, run from this repository checkout:

```sh
.venv/bin/python scripts/install_fusion_wg_metal_addin.py \
  --addin WGLink --symlink
```

The symlinked install is recommended because Update must invoke the repository
`.venv` and `scripts/wglink_resample.py`; Fusion's embedded Python does not have
the scientific interpolation stack. A copied install can still work when
`HORNLAB_FUSION_ADDIN_REPO` points to the checkout, or when a head-less caller
passes `repo_root` and `python_path` options.

Restart Fusion and use the WGLink toolbar panel under **Utilities**. The
manifest sets `runOnStartup` to `true`: start-up only registers a panel and its
command definitions, and every piece of geometry work happens when a command is
executed, so there is nothing heavy to defer. Leaving it `false` meant the panel
was gone after each Fusion restart until it was started by hand, which also kept
the export watcher from ever running.

Icons live in `resources/<operation>/{16x16,32x32,64x64}.png` and are generated
by `scripts/make_wglink_icons.py`. A checkout without them still works; Fusion
falls back to unadorned buttons.
