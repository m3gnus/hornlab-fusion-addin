# WGLink User Guide

WGLink connects Fusion 360 to the Waveguide Generator app (WG). It inserts a
WG design as native, managed Fusion history, keeps it updated as the design
changes in WG, and sends Fusion geometry back to WG to be solved — including
models drawn from scratch in Fusion. This guide is for using the add-in; the
architecture and update limits are documented in
[`fusion-addins/WGLink/README.md`](../fusion-addins/WGLink/README.md), and the
WG side of the workflow in WG's own user guide.

## 1. Install

The ordinary Waveguide Generator platform installer installs or updates WGLink
on macOS and Windows. It also connects Update to WG's existing pinned Python
environment, so the Fusion add-in needs neither this source checkout nor a
second virtual environment. Restart Fusion after installing WG and confirm
**Run on Startup** is ticked for WGLink under **Utilities → Scripts and
Add-Ins**.

For add-in development, install from this repository's checkout instead:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/install_fusion_wg_metal_addin.py --addin WGLink --symlink
```

Use the symlink install for development. Update resamples spline profiles by invoking this
repository's `.venv` and `scripts/wglink_resample.py`; Fusion's embedded Python
does not have the scientific stack. Those packages come from the add-in
checkout's own active environment — the installer never probes sibling
checkouts for them.

Fusion's own record of the Run on Startup toggle
overrides the add-in manifest, so a copy once started by hand stays manual
until the box is ticked. Install from exactly one location: a second copy
loads a second module instance. WGLink elects one active copy and keeps the
others as recovery standbys, but duplicate installations still make upgrades
and support needlessly ambiguous.

One-time setup on the WG side: choose a **WGLink folder** in WG under
**Settings → CAD Link**. The add-in reads the same setting, so Insert and
Send need no folder dialogs afterwards.

## 2. The panel

Three everyday commands sit directly on the WGLink panel. **Manage WG Link…**
contains only **Declare Body…**, **Insert**, **Update**, and **Detach**.

| Command | What it does |
|---|---|
| **Set WG Source…** | Mark the selected faces as the `LF`, `MF`, `HF`, or `PASSIVE_CARDIOID` drive source (applies an appearance with that exact name and stamps the source's identity; Clear removes both). A face already painted `PORT_EXIT`, the role's old name, is still recognised. |
| **Solve in WG** | Export the assembly and ask WG to prepare and solve it, so WG is already solving when you switch windows. |
| **Send to WG** | Export the assembly as a validated `.wgreturn` bundle without asking for a solve. |
| Declare Body… | Classify a body for the return: `exterior-shell` includes an open surface body, `exclude` leaves a body out, Clear restores automatic scoping. |
| Insert | Insert a WG `.wglink` bundle as a managed link. Optionally give the link a **Link name** of your own; leave it empty to use WG's design name. Ordinarily unneeded: WG's Send to CAD offers the insert automatically. |
| Update | Rebuild a managed link in place from its current bundle. If its stored bundle moved, Update finds the newest export of the same design in WG's current workspace and repairs the path automatically. |
| Detach | Permanently remove WGLink identity without changing geometry. Fusion asks for confirmation because the only way back is to insert a fresh copy from WG. |

Audit, Relink and renaming a link remain available through the headless
`wglink_core.audit`, `wglink_core.relink` and `wglink_core.set_link_name`
APIs. Audit's document/link data is also published
continuously to WG in `.fusion-status.json`, so it does not need a panel
command. Newer heartbeats add optional `linkName`, `bodyObjectIds`,
`transformHash`, `sourceIds`, and `driveChannelIds` members to each managed
link. They come from
the same strict observation used by Send to WG; an unavailable or fallback
identity is omitted, while older WG clients can continue ignoring the added
members under heartbeat schema 1.

When WG advertises the live protocol (`liveProtocol` in `wg-capabilities.json`
and a `wg-endpoint.json` beside it), the active WGLink also registers a live
session with WG over loopback HTTP and posts the same heartbeat object there.
This is additive: `.fusion-status.json` is still written on every tick, and
every request still travels as the v3 files, so WG started later, a WG without
live support, or any refusal simply leaves WGLink on the files. The session
runs on its own background thread, never through a proxy, and only in the
WGLink registration that owns the panel. When its state changes, it writes a
line to Fusion's Text Commands palette (for example "WGLink is live with WG."
or "WGLink uses the files: …"); a state that persists is not repeated, but
each new change, including a return to an earlier state, is written again. A
refusal that only a new WG start can change (an add-in or protocol WG does not
accept, or a registration proof that does not verify) waits for WG to restart;
anything else is tried again within 30 seconds.

With such a WG, **Solve in WG** and **Send to WG** also put the return in
WGLink's outbox (`ipc/wglink/.wglink-outbox/`, one private file per return,
which WG never reads). Each item keeps one operation id for good; WG accepts an
id once, so delivering it again — after a lost answer, a WG restart or a Fusion
restart — never makes a second operation. While the live session is healthy the
background thread delivers the item over HTTP. Without one (WG closed, starting,
or refusing the session), Solve in WG writes the v3 solve file under the same id
at once, and a queued solve gets its file when the live session is lost;
WG takes whichever arrives first and recovers the other. A Send to WG item never
becomes a file: it waits for WG's next live session (the return itself is in the
WGLink folder either way). WG's answer is final for the item: an accepted
delivery is removed silently; a return WG rejected (for example one it could not
read for 24 hours) or an id WG says names a different request is shown once in a
message box and then removed. WGLink never resends either by itself: send the
return again from Fusion, which is a new request. The outbox holds at most 100
items; an item not delivered within 7 days is removed with a message. A WG
without the live protocol gets exactly the v3 files, as before.

## 3. The linked round trip

1. In WG, **Send to CAD** writes the bundle and raises Fusion; the add-in
   offers the Insert (first time) or Update (afterwards) — one click.
   If the Fusion document contains multiple managed placements of the same WG
   design, choose the instance in WG first. The handoff names that exact
   instance; WGLink refuses missing, stale, or ambiguous placement identity
   rather than updating whichever copy happens to appear first.
2. Edit in Fusion: move and joint the **wrapper occurrence**, not the managed
   bodies inside it. WG parameters appear as `wg_<name>_*` user parameters.
3. **Solve in WG** sends the geometry back and starts the solve. WG switches
   itself to CAD mode and shows progress; if the ingestion reports blocking
   findings, WG parks the request and shows what it is waiting on.

Renaming the WG design is safe: the parameter namespace and bundle folder are
fixed the first time a design is exported and never change afterwards.

### The three names a link has

A linked document carries three separate names, and they are allowed to differ.
Nothing is wrong when they do.

| Name | Where it comes from | What it controls |
|---|---|---|
| **Fusion document name** | You, in Fusion (`waveguide v1`) | The Fusion document and its `.f3d`. WGLink publishes it to WG, which titles the CAD project with it. |
| **WG design name** | WG, from the design you exported (`260308Tritonia-M`) | Nothing, on its own. It is a label WG stamps into the bundle, and it follows a rename in WG. |
| **Link name** | You, optionally, in the Insert dialog | The link's label in WGLink's menus, in the timeline group, and in what WG shows for the placement. Nothing else. |

The `wg_<name>_*` user parameters are a **fourth** thing, and no name above
moves them. That namespace and the `.wglink` folder name are minted from the
design's name the first time it is exported, and then frozen to the design's
lineage for good — your Fusion datums, enclosure expressions and your own
features all reference it by name, and Fusion cannot retarget an expression to
a different parameter. So a design first exported as `260308Tritonia-M` keeps
`wg_260308tritonia_m_*` and `260308Tritonia-M.wglink` even after it is renamed
in WG, and even in a document you called something else. That is deliberate:
the alternative is that renaming a design breaks every document already linked
to it.

Rename a link at any time — it changes a label and nothing a rebuild or a
return reads:

```python
wglink_core.set_link_name(app, "Left waveguide")            # one link
wglink_core.set_link_name(app, "Left waveguide", {"instance_id": "..."})
wglink_core.set_link_name(app, "")                          # back to WG's name
```

The wrapper component (`WGLink_<name>_1`) is an ordinary Fusion component and
you can rename it in the browser yourself; WGLink finds its links by stored
identity, never by a component or timeline name.

## 4. Starting from a model drawn in Fusion

A from-scratch model — no WG design behind it — is a legal return. Three
requirements:

1. **A drive source.** Mark the throat or diaphragm face with
   **Set WG Source…**. Hand-painting an appearance named exactly `LF`, `MF`,
   `HF`, or `PASSIVE_CARDIOID` onto the face does the same thing.
2. **Closed solids.** An open surface body must be classified with
   **Declare Body…** (`exterior-shell` or `exclude`), or the export refuses
   it as unclassified.
3. **The solver frame.** With no link to anchor the model, WG assumes it
   radiates along **+Z**, throat at the **origin**, centred on x = 0 and
   y = 0 so mirror symmetry can be found. The Send/Solve dialogs show a
   pre-flight summary — scope, sources with areas, and bold warnings when the
   frame looks wrong. Fix placement before sending.

In WG the return arrives marked `unlinked`; acknowledge that one finding, set
mesh sizing and drive channels in the CAD Link panel, and solve.

### Source identity

A Waveguide Generator that reads source identities keeps a painted source's
settings from one return to the next by the identity **Set WG Source…** stamped
on its faces. Once WG reads them, Send and Solve refuse a painted role, and the
message names it, when:

- faces were painted by hand or before identities existed — select them and run
  **Set WG Source…** with that role; faces added to a source that still resolves
  keep its identity;
- a face's paint was removed or changed by hand, a face was removed, split or
  copied, or the role carries two identities — select **every** face that should
  drive the role and run **Set WG Source…** again. That gives the source a new
  identity, and WG asks for its setup once;
- some of the source's faces are outside what you are sending — include them,
  or select them and **Clear** their WG source. Clear also takes the identity off
  a face whose paint you already removed by hand.

A linked waveguide's throat needs nothing: its identity follows the link.

**Upgrade note.** The first time WG advertises source identities, every
document's status token changes once, because source ids are part of it. WG
therefore shows models it imported earlier as changed; send them again. Painted
sources marked before this release are refused until you re-run
**Set WG Source…** on them.

## 5. Returning a model you already cut in half

WG normally finds a model's mirror symmetry and cuts it down itself. A model
that arrives **already** cut is different: there is nothing left to remove, and
nothing in the geometry distinguishes a deliberate half from an open shell. Left
undeclared, the half is solved as a full model with a large hole in it — a wrong
answer rather than an error.

So say it. **Model domain** on the Send/Solve dialog offers the full model
(the default), a half cut on x = 0 or on y = 0, and a quarter cut on both.

Three requirements, and WGLink checks the first two before it exports:

1. **Keep the positive side.** WG keeps x ≥ 0 and y ≥ 0, so a half that lives
   on the negative side is refused with the remedy: mirror it first.
2. **Do not straddle the plane.** A declaration that the exported bodies
   contradict is refused, and the refusal states the measurement it was
   refused on.
3. **Leave the cut face open.** The plane is where the solver's mirror goes;
   capping it turns the mirror into a rigid wall. WG re-derives this from the
   mesh it builds and refuses a declaration the mesh denies, so a capped or
   leaking half never solves silently.

The declaration travels in the return manifest as `assembly.domain`, gated by
the `reduced-domain-v1` required feature: a WG that predates the feature refuses
the bundle instead of solving it whole.

Sources need no adjustment. A half model's drive face is already half its full
area, which is exactly what WG's own cutter would have produced, and the solver
scales the mirrored domain the same way either way.

## 6. Assembly scope

**Leave Assembly scope empty** to return the root component, or select exactly
one occurrence.

A selected occurrence is exported as its **component**, in that component's own
frame. Fusion's STEP export takes a Component and writes it in its own
coordinates; it offers no way to export an occurrence in its assembly
placement. So the bundle states which frame it is in
(`coordinate_system.export_frame`), and every coordinate in it — the bounding
box, each instance placement, the declared-domain measurement — is read from
the native bodies, which Fusion defines as the bodies "outside the context of
an assembly". A moved or jointed wrapper occurrence therefore returns
correctly, which is the ordinary WGLink round trip: Insert places the wrapper,
you move and joint it, and Solve in WG returns it.

The one shape this cannot do is a **placed occurrence that itself contains
sub-assemblies**. Its children's bodies are native to their own components, and
getting from there to the exported frame means composing the placement chain —
arithmetic WGLink will not do unverified. That case is refused, and there are
three real ways forward: leave Assembly scope empty and send the whole root
assembly, select one of the sub-assemblies on its own, or move the occurrence
back onto the assembly origin by editing or deleting the joint or Move feature
that placed it. (Fusion's **Ground** is not one of them: it freezes an
occurrence where it already is and never moves it back to the origin.)

## 7. Troubleshooting

- **"Why is this link called something I never typed?"** — it is WG's name for
  the design, not a name for your Fusion document. See *The three names a link
  has* in section 3. Give the link your own **Link name** at Insert, or rename
  it afterwards with `wglink_core.set_link_name`. The `wg_<name>_*` parameters
  keep the bundle's namespace either way, on purpose.
- **"Waveguide Generator has no selected CAD Link workspace"** — choose the
  WGLink folder in WG under Settings → CAD Link, then send again.
- **"The CAD Link folder Waveguide Generator is set to no longer exists"** — the
  folder WG remembers was moved, renamed or deleted. The message names it.
  Choose the folder again in WG under Settings → CAD Link, then send again.
- **"WGLink cannot find the component that holds the WG waveguide …"** — Send
  could not find the component the waveguide was inserted into, so it cannot
  say where the waveguide sits, and it will not guess. If you deleted or
  replaced that component, undo it and send again. If it is still there,
  insert the waveguide from WG again and send that copy. What makes the
  component unfindable in the remaining cases is not yet known.
- **The panel is missing after a Fusion restart** — tick Run on Startup for
  WGLink (see Install); Fusion's toggle overrides the manifest.
- **"WGLink helper body … is still visible"** on Send — a freestanding
  insertion leaves a stitched surface shell behind, and Fusion's STEP export
  writes every visible body of the exported component, so that shell would
  reach the solver as a second radiating surface. Insert now hides it, but a
  document inserted by an earlier WGLink still has it visible. Hide **the body
  itself** in the browser — the refusal names it — and send again, or run
  Insert again. Hiding a folder that contains the body will not work: Autodesk
  exports a body that is invisible only because its group is hidden as if it
  were visible.
- **A surface sits on top of the waveguide in the browser, and I selected and
  simulated it by mistake** — a freestanding insertion leaves a stitched
  surface shell behind. It is zero-thickness and coincident with the final
  solid, so in the browser and in the canvas it is easy to pick instead of the
  body you meant, and a solve started from it is a solve of the wrong
  geometry. Insert hides it, and so does **Update** — run Update on the
  document and WGLink hides every leftover helper body it still shows,
  reporting them under `helpers`. Audit names them without touching anything,
  so you can see which document is affected before you change it. A document
  built before this change keeps the shell visible until one of those runs.
  Failing that, hide **the body itself** in the browser. Hiding a folder or
  group that contains it does **not** work: Autodesk exports a body that is
  invisible only because its group is hidden as if it were visible, so it
  would still reach the STEP file and the solver. Do not delete the shell —
  WGLink reads its managed bodies by role for tag repair and anchoring, and a
  deleted one cannot be restored without recreating the link.
- **"WGLink parameter namespace mismatch"** on Update — the document's link
  predates the stable-namespace fix and its bundle was renamed. The refusal
  names the recovery: Detach and delete the component, then Insert; the
  rebuild restores the managed bodies, datums and parameters, and only
  user-authored features on the old parameters need repointing.
- **"WGLink Insert creates parameters; it never takes over ones it did not
  create"** — the namespace this Insert would have used is already occupied by
  parameters WGLink does not own, and it could not allocate a free one either.
  Insert normally steps to the next free namespace on its own: Detach leaves a
  link's `wg_<name>_*` parameters and the geometry they drive behind, so a
  fresh Insert of that design takes `wg_<name>2_` rather than reassigning the
  detached model's expressions. The refusal names the colliding parameters;
  rename or delete them, or keep them and let the new link have its own
  namespace. Only Update rewrites parameters, and only for the namespace a
  link this document still records was minted with.
- **"The linked bundle could not be found in the current WG workspace"** —
  Update already searched the selected WG workspace by design identity. Put the
  bundle back under that workspace and run Update again. If it was deliberately
  moved elsewhere, re-point it with the headless `wglink_core.relink` API.
- **Nothing arrives in WG after Solve in WG** — WG must be running; the
  request survives until it next runs, and the WG window still has to be
  brought forward by hand.
- **"WGLink delivery to WG" message** — WG gave a final answer that needs you:
  the return was rejected (the message carries WG's reason) or its id already
  named a different request. Nothing is retried; send the return again from
  Fusion if you still want it.
- **Text Commands says "WGLink uses the files: …"** — nothing is broken:
  the live session is optional and the files carry everything. The line names
  why. On macOS and Linux WGLink refuses a `wg-endpoint.json` that other users
  could read or change, an `ipc/wglink` folder that group or other can write
  to, and an `ipc/wglink` folder or endpoint file that is a symlink; a
  `WG2_DATA_DIR` on a shared location therefore stays on the files.
- **WG says a request is running and nothing happens in Fusion** — a WGLink
  command is open, or no design is. WGLink starts nothing over its own command:
  the request waits. Finish or cancel the WGLink command, and it runs on the
  next pass. If no design is ready, or no WG workspace is selected, the reason
  travels to WG in the heartbeat; while a WGLink command is open it does not,
  so WG shows the request as simply still running.
- **WG shows an update as still running after Fusion restarted** — WGLink
  settles what it can against the open document, read-only, and never runs an
  interrupted update again. If the update was for a document you do not have
  open, WGLink keeps it rather than reporting it as never started; open that
  document and it is settled. An update that had started changing the model
  reports **Update interrupted — recovery required**: undo the partial change
  or repair the link, then send it from WG again.
- **You cancelled in WG and Fusion still changed the model** — a Fusion call
  that has started cannot be interrupted. WGLink stops at the last point before
  a write; past that it finishes the call and reports what actually happened
  rather than claiming a clean cancellation.
- **Text Commands alternates between "live" and "ended the live session"** —
  two Fusion processes are running against the same WG data folder. They share
  one WGLink installation id, so each registration replaces the other's. WGLink
  bounds this: a second loss within 30 seconds of registering again keeps that
  process on the files for 30 seconds. Close one of the Fusion processes.
- **Duplicate WGLink installations** — remove the extra registration and
  restart Fusion. Current copies elect one active panel/watcher and promote a
  surviving standby if the owner stops, but a mixed-version duplicate remains
  an unsupported installation to diagnose.
