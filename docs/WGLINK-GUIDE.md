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
| **Set WG Source…** | Mark the selected faces as the `LF`, `MF`, `HF`, or `PASSIVE_CARDIOID` drive source (applies an appearance with that exact name; Clear removes it). A face already painted `PORT_EXIT`, the role's old name, is still recognised. |
| **Solve in WG** | Export the assembly and ask WG to prepare and solve it, so WG is already solving when you switch windows. |
| **Send to WG** | Export the assembly as a validated `.wgreturn` bundle without asking for a solve. |
| Declare Body… | Classify a body for the return: `exterior-shell` includes an open surface body, `exclude` leaves a body out, Clear restores automatic scoping. |
| Insert | Insert a WG `.wglink` bundle as a managed link. Ordinarily unneeded: WG's Send to CAD offers the insert automatically. |
| Update | Rebuild a managed link in place from its current bundle. If its stored bundle moved, Update finds the newest export of the same design in WG's current workspace and repairs the path automatically. |
| Detach | Permanently remove WGLink identity without changing geometry. Fusion asks for confirmation because the only way back is to insert a fresh copy from WG. |

Audit and Relink remain available through the headless `wglink_core.audit` and
`wglink_core.relink` APIs. Audit's document/link data is also published
continuously to WG in `.fusion-status.json`, so it does not need a panel
command. Newer heartbeats add optional `bodyObjectIds`, `transformHash`,
`sourceIds`, and `driveChannelIds` members to each managed link. They come from
the same strict observation used by Send to WG; an unavailable or fallback
identity is omitted, while older WG clients can continue ignoring the added
members under heartbeat schema 1.

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
one occurrence. An occurrence is exported as its component, in the component's
own frame — Fusion offers no way to export one in its assembly placement — so an
occurrence that is moved or rotated away from the assembly origin is refused
rather than exported into a frame the manifest does not describe. Return the
root instead, or ground the occurrence at the origin.

## 7. Troubleshooting

- **"Waveguide Generator has no selected CAD Link workspace"** — choose the
  WGLink folder in WG under Settings → CAD Link, then send again.
- **The panel is missing after a Fusion restart** — tick Run on Startup for
  WGLink (see Install); Fusion's toggle overrides the manifest.
- **"WGLink parameter namespace mismatch"** on Update — the document's link
  predates the stable-namespace fix and its bundle was renamed. The refusal
  names the recovery: Detach and delete the component, then Insert; the
  rebuild restores the managed bodies, datums and parameters, and only
  user-authored features on the old parameters need repointing.
- **"The linked bundle could not be found in the current WG workspace"** —
  Update already searched the selected WG workspace by design identity. Put the
  bundle back under that workspace and run Update again. If it was deliberately
  moved elsewhere, re-point it with the headless `wglink_core.relink` API.
- **Nothing arrives in WG after Solve in WG** — WG must be running; the
  request survives until it next runs, and the WG window still has to be
  brought forward by hand.
- **Duplicate WGLink installations** — remove the extra registration and
  restart Fusion. Current copies elect one active panel/watcher and promote a
  surviving standby if the owner stops, but a mixed-version duplicate remains
  an unsupported installation to diagnose.
