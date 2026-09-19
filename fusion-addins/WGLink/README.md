# WGLink for Fusion

WGLink inserts a Waveguide Generator `.wglink` bundle as native, managed
Fusion history. It supports the two solid WG export modes:

- `enclosure`: the realized enclosure block with one all-edge treatment, minus
  the waveguide cavity;
- `freestanding`: the fitted-spline surface, throat patch, stitch, and outward
  parameter-driven wall thickness that form the freestanding waveguide solid.

The add-in is licensed under the repository's AGPL-3.0 license. Its manifest
author is `m3gnus`.

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

Send reads each link's `WG_THROAT_PLANE` and `WG_AXIS` by ownership, not by
name. Insert stamps every datum with the link that owns it and records its
entity token; Send resolves that token and requires it to name the stamped
datum. A renamed datum therefore still sends, and two links in one component —
the root-fallback links of a Part Design document — each send their own
throat frame. A recorded datum that has since been deleted sends no throat
contract for its link, never a neighbour's. Where the two records disagree, or
two datums claim one link, Send refuses and names the link. A link with no
such record is read by datum name only while it is the only link in its
component; beside another link it is refused. Re-insert it from WG to record
its datums.

Send also refuses a link whose wrapper placement is mirrored or not rigid. The
return contract's only chirality, `original`, is true of a rotation plus a
translation alone (determinant +1 within 1e-6, WG's rigid-placement
tolerance), so Send measures each placement and names the link instead of
labelling it `original`.

## Commands

The panel promotes the three everyday commands, **Set WG Source…**, **Solve in
WG** and **Send to WG**. **Manage WG Link…** contains **Declare Body…**,
**Insert**, **Update**, and **Detach**.
Insert and Update are ordinarily automatic — WG's *Send to CAD* publishes a
handoff the add-in applies on its own — so the dropdown is maintenance,
authoring-remedy, and recovery UI rather than the normal workflow. Audit and
Relink remain full head-less APIs but are not panel commands.

- **Set WG Source…** marks the selected faces as the `LF`, `MF`, `HF` or
  `PASSIVE_CARDIOID` drive source by creating (or reusing) an appearance named
  exactly after the role and painting it on. (`PASSIVE_CARDIOID` was named
  `PORT_EXIT` before this rename; a face already painted `PORT_EXIT` is still
  recognised as a source and keeps exporting under that name — only a newly
  painted face gets the new name.) That appearance name *is* the convention
  the export reads, and it is the one thing a model built from scratch in Fusion
  cannot be sent without; before this command it had to be authored by renaming
  a Fusion appearance by hand. The same dialog clears a role, which strips only
  faces that actually carry one of the four roles — a face painted with your own
  material is left alone.

  The command also stamps each selected face with the source's identity (a
  `source_identity` attribute on the native face: the id, the role, a per-face
  nonce and the number of faces the source was marked on). When WG advertises
  `"sourceIdentity": 1` in `wg-capabilities.json`, Send, Solve, a return WG
  asks for, the pre-flight and the heartbeat all declare `source-identity-v1`,
  and each `sources[].id` becomes that stable identity (`wgs-` and 20 base32
  characters) instead of the role-derived `source-hf`. A linked throat's identity
  is derived from its link's `instance_id` and needs no stamp. A WG that does not
  advertise the capability gets the legacy manifest, byte for byte, stamps or
  not. Fusion entity tokens and stamps never enter the manifest.

  Export refuses a painted role, never guessing, when a face carries no stamp,
  the faces carry two identities, a face was split or copied, or the source no
  longer adds up to the faces it was marked on. What re-running
  **Set WG Source…** does depends on the source:

  - **The source no longer resolves** — a member face's paint was removed or
    changed by hand, a face was removed, split or copied, or two identities
    share the role. Selecting every face that should drive the role and running
    the command gives it a **new** identity, removes the stale stamps from every
    other face, and WG asks for that source's setup again.
  - **The source still resolves** — the selected faces are added to it and it
    **keeps** its identity; this is also the remedy for faces painted by hand or
    before identities existed.
  - **Part of the source is outside what is being sent** (another component, or
    a body the selection leaves out) — the refusal says so. Include those faces
    in the export, or select them and **Clear** their WG source; running the
    command again on the faces in scope changes nothing.

  Clear removes the stamp from every selected face, including one whose paint
  was already removed by hand. A source whose faces were all deleted is simply
  absent from the next return. The command's stamp writes succeed together or
  are rolled back together (a read-only referenced component refuses a write);
  the appearance it already painted or cleared is kept, and the error says so —
  Send then names the remedy, or undo the command. A face whose stamp Fusion
  would not restore is reported, never called restored.
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
  the normal in-place Update instead of inserting a duplicate. For assemblies
  containing repeated placements of one design, WG includes the chosen
  `expectedInstanceId` beside the expected document id. WGLink updates only the
  uniquely matching managed instance. A missing selection for repeated links,
  a stale id, a duplicated id, or an instance target without its document target
  leaves the handoff unacknowledged and changes no geometry. Legacy and current
  single-instance handoffs still resolve automatically.
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
  `.fusion-status.json`, so Audit has no panel command. Each live link also
  publishes the exact body entity tokens, assembly-transform hash, source ids,
  and drive-channel ids that its next validated return would use. Those fields
  are optional: if Fusion cannot resolve a stable entity token, strict
  transform, or complete source contract, WGLink omits that identity instead
  of inventing a name or defaulting a transform.
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

## Delivery with Waveguide Generator

Commands and requests cross through WG's machine-local IPC folder,
`<WG data folder>/ipc/wglink`. The contract is WG's
`docs/architecture/CAD-OPERATIONS.md`; WGLink implements the add-in's half.

- **Delivery version 3, and nothing older.** Every request, in either
  direction, is its own file with `schemaVersion` 3. There is no single-slot
  marker and no twin. The heartbeat reports `deliveryVersion: 3`, and WG
  refuses an add-in that reports less, asking for the add-in it installs. WGLink
  in turn needs WG to advertise version 3 in `wg-capabilities.json`; an older WG
  gets no solve command, its requests are never run, and WGLink says once per
  session that WG needs updating. WG installs and updates its own managed
  WGLink, so the two are always the pair that shipped together.
- **Solve commands.** **Solve in WG** writes `.wg-solve-requests/<commandId>.json`.
  A second command never replaces one WG has not read yet.
- **WG's requests.** Return requests and handoffs arrive as
  `.fusion-return-requests/<id>.json` and `.fusion-handoffs/<id>.json`. WGLink
  takes them in `deliverySequence` order, claims each by renaming it to a
  hidden name, runs it at most once and deletes the claim, also when the
  request is refused.
- **Exact targets.** An update names the Fusion document, the exact instance
  and the model state WG measured. A handoff that names no instance is an
  insert, and it is refused if the active document already links that design:
  WGLink never picks "the one matching link" for WG. A return request names
  its document, instance and baseline too.
- **The target, immediately before mutating.** Update and Insert take a
  precondition and run it after their last read and before their first write:
  for an update, the live model state against WG's baseline; for an insert, the
  active document and "no link of this design yet". A document that moved in
  between is a conflict, never an overwrite.
- **Reconciliation, then interruption.** Update and Insert stamp WG's operation
  id beside the export identity, as their last write. A handoff whose operation
  id is already on a link is acknowledged without mutating, and before the
  baseline check, so a lost acknowledgement never reads as a conflict. Before
  its first write that changes the model, Update saves the user's exact timeline
  marker and journals `prepared`, `applying`, `applied`, then `verified`, with one
  stable `startedAt`. The marker is restored to its saved position on every exit;
  Update never moves it blindly to the end. Export identity and operation evidence
  are stamped only after verification, then the journal is cleared. An apply
  failure leaves `applying`; a verification failure leaves `applied` and reports
  **Update applied but not verified — recovery required**. That operation is never
  run again, and the heartbeat publishes `phase` and `startedAt` additively in
  `document.applyingOperation` so WG can say where recovery stopped.
- **Insert destination and expiry.** A new WG can bind an insert to the active
  document with `destination: {kind: "document", value: <document id>}`, or to a
  new document with `{kind: "new_document", value: <request id>}` when none is
  active. WGLink consumes and refuses a different destination, and consumes an
  insert more than 30 minutes after `requestedAt` as `expired`, without touching
  the document. A request with no `destination` keeps the version-3 behaviour for
  a pinned older WG.
- **Supersession.** An update that has not started yet is dropped when a newer
  one for the same document and instance arrives; only the newest runs. WG
  withdraws the older file itself; WGLink covers a file WG could not remove.
  The heartbeat's `diagnostics.recentOutcomes` names the dropped request with
  the outcome `superseded`.
- **Leftover claims.** The first tick with an active document settles every
  claim an interrupted session left behind, read-only, and lists each in
  `diagnostics.recentOutcomes`: evidence on a link means it
  applied, the applying mark means recovery is required, and neither means it
  never started. None is run again; each claim is removed.
- **Correlation.** The heartbeat's `diagnostics.lastRequest` names the last
  round trip (`channel`, `correlationId`, `attemptId`, `delivery`, `outcome`),
  and each link publishes the `operationId` stamped on it.

### The heartbeat reads cached state only

The four-second heartbeat inspects no geometry. Measuring a link's state means
walking the root export scope and evaluating every included face and body on
Fusion's main thread, which on a dense linked document is a permanent load on
the application the user is modelling in. So the tick publishes identity —
stored attributes, which cost nothing — and, for the measured half, whatever a
previous measurement left in the cache.

Each link therefore carries two extra tokens beside its measured state, both
property reads and both additive under heartbeat schema 1:

- `geometryRevisionToken` — the revision the document is at **now**. It moves
  with the timeline count, each managed body's revision and visibility, the
  stored export id and edit version, and the source-identity stamping
  generation.
- `measuredRevisionToken` — the revision the published measurement was taken
  at, or `null` when there is no measurement to offer.

Equal tokens mean `documentSignatureHash`, `documentBodyCount`,
`sourceStateHash`, `bodyFingerprintHash` and `localBodyState` describe the
document as it stands. Anything else — unequal tokens, or a null
`geometryRevisionToken` meaning the revision could not be computed at all — means
they are an observation of an earlier revision and must not be read as current.
Read the hashes, not the tokens, for whether there is an observation to use: an
empty `documentSignatureHash` with `localBodyState: "unknown"` is "cannot tell",
which is what a restart or a switch to a document nothing has measured yet
publishes. It is never another document's measurement and never a claim of a
fresh one.

**An observation is never withdrawn for being old.** There is no age ceiling on
what the heartbeat publishes, because nothing renews a cache the tick may not
measure into: a ceiling would only decide how long a linked idle document took
to lose its baseline for good, and WG refuses to publish a return request or an
exact-target handoff without one. The tokens are what an age ceiling used to
approximate, and they are exact.

A measurement is asked for only when a linked document has no observation worth
publishing, or when a WGLink command the user completed may have moved one —
and by every guarded operation WG asks for, which measures inline. "Has no
observation" rather than "has just changed" is deliberate: a request can be
spent without answering anything (the document stopped being nameable, its
links went, the measurement threw), and a rule that asks once per change never
learns the answer never arrived. `GEOMETRY_REFRESH_RETRY_SECONDS` is the rate
at which such a document may be asked about again, so this is a rate rather
than a retry loop — and rather than a fixed allowance, which would give up on a
document that was merely still loading and leave it with no baseline for the
rest of the session. An observation stops the asking entirely. A document whose
observation is merely *stale* asks for nothing.

Each request names the document it is about, and is dropped without measuring
if that document is no longer active, cannot be named, or has no WGLink links.
At most one refresh is pending per add-in instance — a second request for the
same document is dropped, and one for a different document replaces it, so the
live question is never stuck behind a dead one. It is paid for on the next
tick, the only main thread the Fusion API allows.

A guarded update or return measures inline and refuses a document that has
moved or that WGLink cannot read, so a stale published token can cost a refusal
but never an overwrite; that refusal refreshes the cache, so the next heartbeat
carries the current state.

An unsaved document is identified by its root component's `entityToken`, not by
any Python-side identity: Fusion's bindings mint a fresh proxy per property
read, so neither `id(document)` nor holding the object says anything durable
about the document. A document that cannot be named at all is cached under
nothing and reports "cannot tell".

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

### Source identity: live-Fusion verification still owed

Fake-backed tests cover the resolution, refusal, reassignment, rollback and
capability gating above. These depend on Fusion behaviour a fake cannot prove,
and need a live check on macOS and Windows before the feature is field-validated:

- A face split, copied, pasted or patterned carries its original's attribute
  onto every result (the split/copy refusal relies on it).
- Stamps survive an upstream timeline edit and recompute. A lost stamp on a
  face that kept its paint reads as "carries no identity" — a false refusal.
- Painting a face through an occurrence proxy, and whether the native face then
  reads that paint; decision 4 (one face placed twice is one source) assumes the
  proxies of one native face share its stamp.
- `Design.findAttributes` reaches faces in externally referenced components, and
  a read-only referenced component refuses the write so the rollback runs.
- Fusion `==` identifies an attribute's `parent` with the same face reached from
  a body's `faces` (the fakes use object identity or the token fallback).
- Undo of Set WG Source… restores the stamps. Undo moves nothing the heartbeat's
  change key watches, so the published token can stay stale for up to the 60 s
  age ceiling, then reads "cannot tell" until the next measurement (at most the
  120 s duty-cycle wait).
- The plan's exit rows: a source face removed, split or made ambiguous triggers
  reassignment, not a silent remap.

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
tag, and unhealthy timeline features. Parameter drift is measured numerically,
not textually: Fusion does not re-emit a parsed expression bit-for-bit, so two
spellings of one length within `EXPRESSION_REL_TOLERANCE` (1e-9 relative,
1e-9 mm absolute) are the same value. That band is far below the finest change
a user can enter, and far above the float round-trip it exists to absorb. Fusion has no face-to-feature reverse
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
