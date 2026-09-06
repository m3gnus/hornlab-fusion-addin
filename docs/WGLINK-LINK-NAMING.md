# Naming a WGLink link

*Investigation and recommendation, 2026-09-06.*

## The report

> Why is it called `260308Tritonia-M`? I never named it that in Fusion 360. I
> think it should be called what the Fusion 360 document is called, or even
> better there should be an option to define a WG link name.

The reporter's Fusion document is named `waveguide v1`. The link inside it
reports `designName: 260308Tritonia-M`, its bundle is
`.../wglink/260308Tritonia-M.wglink`, and its Fusion user parameters are
`wg_260308tritonia_m_throat_dia`, `wg_260308tritonia_m_depth`, and so on. He
went looking in WG for a project called `waveguide v1`, did not find one, and
concluded something was misnamed.

Nothing is misnamed. Three different names exist, they are allowed to differ,
and no part of the product had ever said so. That is the actual defect.

## 1. Where the name comes from

`designName` originates entirely on the Waveguide Generator side and reaches
Fusion as inert data inside the bundle manifest.

```
document store designName          frontend/src/stores/document.ts
  -> designNameSlug(designName)    frontend/src/stores/designName.ts:48
  -> POST baseName                 frontend/src/api/designIo.ts:151
  -> _base_name(request.base_name) server/exports/api.py:72, :495
  -> manifest design.name          server/exports/api.py:547
  -> bundle wglink.json
  -> WGLink attribute design_name  fusion-addins/WGLink/wglink_bundle.py:1340
```

The WG design name itself comes from the design's `.cfg` file name
(`designNameForOpenedFile`, `frontend/src/stores/designName.ts:102`), which is
why it reads like an ATH config stem rather than anything typed in Fusion.

Two slug functions apply on the way: the browser's `designNameSlug`
(case-preserving, keeps `-` and `.`) and the server's `_base_name`
(filename-safe, case-preserving). For this design both are identity
transformations, so `design.name` in the manifest is exactly
`260308Tritonia-M`.

## 2. Everything `designName` is load-bearing for

Traced, not guessed. The surprise is how little it carries.

### 2a. It is display-only in the add-in

| Site | Use |
|---|---|
| `wglink_bundle.py:1340` | copies `design.name` into the stored attribute `design_name` |
| `wglink_core.py:~530` | the Fusion parameter *comment* ("WGLink interface parameter for …") |
| `wglink_core.py:~2754` | the timeline group's name |
| `wglink_core.py:3176` | rewritten from the bundle on every Update |
| `wglink_core.py:~3481` | `bundle_design_name` in the Audit report |
| `wglink_watch.py:260` | published as `designName` in `.fusion-status.json` |
| `wglink_workspace.py:53, :197` | the Insert dropdown's bundle label |
| `WGLink.py:444` | the managed-link chooser's label |

Nothing in that list is compared, matched, or keyed on. Searching the whole
add-in for a comparison against `design_name` finds none.

### 2b. It is display-only on the WG side too

WG publishes it back to its own UI at exactly one place: the link picker in
`frontend/src/design/ParamPanel.tsx:668`, rendered as
`` `${link.designName} · ${link.instanceId}` ``. Every reconciliation WG does —
which return belongs to which project, whether a link is stale, whether a
bundle folder may be reused — compares `design_id`, `lineage_id`,
`instance_id`, `design_hash`, `geometry_hash` or `export_id`.

### 2c. What actually *is* derived from the name — and frozen

Two artifacts are minted **from the design name at first export** and then
**frozen to the design's lineage forever**:

| Artifact | Rule | Source |
|---|---|---|
| Parameter namespace `wg_<slug>_` | `_slug(design_name)`, lowercased, `[^a-z0-9_] -> _` | `server/exports/api.py:80`, `:112` `_instance_slug` |
| Bundle folder `<stem>.wglink` | `_base_name(design_name)` | `server/exports/api.py:136` `_lineage_bundle_stem` |

Both are recorded in the `lineage_cad_names` table with first-writer-wins
`COALESCE` semantics (`server/cadlink/store.py:466`), keyed on `lineage_id`,
which survives both renames and forks. The archive folder stem
(`claim_archive_stem`) is frozen the same way.

`_instance_slug`'s own docstring states the reason:

> The Fusion parameter namespace: minted once per lineage, never renamed.
> Fusion's datum and enclosure expressions name `wg_<slug>_*` for the life of
> the document and no update can retarget them, so a namespace that followed
> the filename made Save As an unrecoverable link break.

So `design.name` in the manifest **does** follow a WG rename, while the
namespace and folder do not. WG's own regression test asserts exactly this
divergence (`server/tests/test_wglink_export.py:375`).

The add-in never sees a `parameter_prefix` field in the manifest. It recovers
the namespace by finding the single interface parameter ending in
`_throat_dia` and stripping `wg_` (`wglink_bundle.py:983` `parameter_slug`).
WG recovers it the same way when the registry has no record
(`server/exports/api.py:86` `_slug_from_manifest`).

At Insert, a second placement of the same design takes `wg_<slug>2_`
(`instance_parameter_prefix`, `wglink_bundle.py`), so the namespace is
per-*instance* while the slug is per-lineage. Allocation reads both the live
link records and the document's actual parameter table, because they disagree:
Detach removes a link's attributes and leaves its parameters and the geometry
they drive, so a namespace with no record can still be fully occupied — and a
user may have authored `wg_<slug>_*` names of their own. Insert takes the next
free namespace instead, and refuses by name if it cannot find one. Update never
allocates: it writes the namespace its own record was minted with, for the life
of the document.

### 2d. One more name-derived artifact, purely cosmetic

The wrapper component is named `WGLink_<slug>_<n>` (`_next_wrapper_name`,
`wglink_core.py:2364`) — so the user's Fusion browser reads
`WGLink_260308tritonia_m_1`. Links are resolved by the stored `instance_id`
attribute (`_link_records` / `_resolve_link`), never by component or timeline
name, so the user may rename it freely and nothing notices.

## 3. What a rename would break

### 3a. Changing the parameter prefix is a hard break, and the code says so

`update()` refuses outright when the incoming bundle's slug differs from the
one the document stored (`wglink_core.py:~2932`):

> WGLink parameter namespace mismatch: the document owns `…`, while the bundle
> owns `…`. Force cannot retarget the existing datum and enclosure expressions
> to another namespace. To recover: Detach this link and delete its component,
> then Insert the bundle …

There is no `force` past it. The document's datum planes, enclosure sketch
expressions and every user-authored feature that references
`wg_260308tritonia_m_depth` name that parameter as a string. Fusion has no API
to repoint an expression at a renamed parameter, and renaming a user parameter
in place does not rewrite the expressions that reference it. So a prefix change
means:

- the old parameters are **orphaned**, not migrated: they stay in the document,
  still driving the existing geometry;
- the new export's parameters are **created alongside** them, driving nothing;
- Update refuses before any of that, which is the correct outcome and the only
  reason this has never been an incident.

The recovery cost is real: Detach, delete the component, Insert, then repoint
every one of the user's own features by hand.

### 3b. Changing the bundle folder name is cheap

`bundle_path` is stored, but a missing path is repaired automatically:
`_bundle_for_update` (`wglink_core.py:2797`) rescans WG's workspace and matches
on `design_id`, then relinks the record. The filename is not identity-bearing
on either side.

### 3c. Changing the displayed design name is free

It is rewritten from the bundle on every Update already
(`wglink_core.py:3176`). Nothing reads it back.

## 4. Name-as-identity, found while looking

Two latent issues, neither caused by this report, both worth recording.

**(1) `newestReturnForProject` matches on a name.**
`waveguide-generator/frontend/src/api/cadProjects.ts:235` reconciles a return
bundle to a project by string-matching `wgreturn.json`'s `document.name`
against the project's `documentName`/`archiveStem`:

```ts
const names = new Set([project.documentName, project.archiveStem].filter(Boolean));
return [...bundles]
  .filter((bundle) => bundle.readable && bundle.documentName !== null && names.has(bundle.documentName))
```

Its caller (`CadLinkCoordinator.tsx:1296`) finds the project by `lineageId` —
by id — and then finds its return by name. Rename the Fusion document and
session restore silently finds nothing. It fails closed rather than adopting
the wrong bundle, and the returns carry `designIds` already, which is what the
sibling helper `returnBelongsToProject` uses. This one should key on
`design_id` too.

**(2) `instances[].parameter_prefix` is validated and then discarded.**
`server/cadlink/wgreturn.py:320` requires the field and no code ever reads it.
The one failure the whole freeze-the-namespace design exists to prevent — a
returned instance whose Fusion parameters live under a different namespace than
the lineage's recorded `parameter_slug` — is therefore undetectable on the WG
side, even though `store.get_lineage_cad_names(lineage_id)["parameter_slug"]`
is sitting right there to compare against.

Neither is in scope here. Both are filed as observations.

## 5. The two options as asked, and the recommendation

### Option 1 — name the link after the Fusion document

**Cost, if it means the parameter prefix.** A hard break of every existing
linked document, per §3a. Also impossible in the general case: the add-in
receives the Fusion document name, but the *bundle* is written by WG before
Fusion is ever consulted, and an unsaved Fusion document has no name at all
(`_fusion_snapshot` falls back to `"Untitled"`). WG would be naming a namespace
after a string it cannot see and that can change under it. **Reject.**

**Cost, if it means only the display.** Nearly free — but it is also close to
already true. WG's project naming already puts the Fusion document name first
(`cadProjects.ts:99`: `documentName || archiveStem || filename || 'Untitled
project'`), and shows it in the workflow headline, the status bar and the CAD
project panel. The one place still showing the WG design name is the per-link
picker, `ParamPanel.tsx:668` — and that is the *right* place for it, because a
document can hold several links from different WG designs and the document name
cannot tell them apart.

### Option 2 — let the user name the link, with a sensible default

**Cost: low, if and only if the name stays a label.** A new optional attribute,
absent on every existing link, with display falling back to the design name
exactly as today. No identifier, no namespace, no path, no wire-format break.

The default must be the WG design name, not the Fusion document name: a
document with two links needs two distinguishable labels, and the document name
gives both the same one.

### Recommendation

**Do option 2 as a display-name-only change, and say out loud that the four
names are four different things.** That is what was implemented — see §6.

The reporter's literal request ("call it what the Fusion document is called")
would not have fixed his problem. He went looking in WG for a project called
`waveguide v1`; WG *does* title that project `waveguide v1`. What he was
missing is that `260308Tritonia-M` names the *design* he exported, and that the
`wg_260308tritonia_m_*` parameters are frozen to it deliberately so that
renaming never breaks a linked document. A label he chooses himself gives him
the recognition he asked for, and a documented explanation gives him the
reason — at no risk to anything already on disk.

### Backward compatibility

- **Existing linked documents:** unchanged. No attribute is rewritten, no
  parameter is touched, no identifier moves. A link with no `link_name`
  displays its `design_name`, byte for byte as before.
- **Older WG clients reading a newer heartbeat:** unaffected. `linkName` is an
  added optional member under heartbeat `schemaVersion: 1`, the same additive
  route already taken by `bodyObjectIds`, `transformHash`, `sourceIds` and
  `driveChannelIds`. It is `null` when unset — never back-filled from
  `designName`, so a client showing both never shows one name twice.
- **Newer add-in, older bundle:** unaffected. The label lives only in the Fusion
  document; nothing is asked of the bundle.
- **Older add-in, document containing a label:** the extra attribute is ignored
  and the old add-in shows the design name.
- **`.wgreturn`:** untouched. The label is deliberately *not* exported — WG has
  no field for it, and adding one would need a schema version.

## 6. What was implemented, and what was left alone

Implemented in `hornlab-fusion-addin`:

- `normalize_link_name` / `link_display_name` in `wglink_core.py`: one
  single-line label, capped at 120 characters, control characters refused; an
  empty or absent label means "show the WG design name".
- An optional **Link name** field on the Insert dialog, with help text that
  states what it does *not* change.
- The label stored as the `link_name` attribute, only when non-empty, and added
  to `_PAYLOAD_KEYS` so it survives a read.
- `wglink_core.set_link_name(app, name, options)` — a headless rename for links
  that already exist, alongside `audit` and `relink`. It writes one attribute.
- The label used for the timeline group at Insert, the managed-link chooser
  label, the Insert summary, and the Audit report.
- `linkName` published in `.fusion-status.json`, additive under schema 1.
- Update preserves the label: `_update_payload_attributes` merges, and the
  refresh dictionary does not name `link_name`.
- This document, plus a *three names a link has* section and a troubleshooting
  entry in `docs/WGLINK-GUIDE.md` — the missing explanation is at least half the
  defect.

Deliberately left alone:

- **The parameter prefix `wg_<slug>_`.** Changing it is a breaking change to
  every existing linked document (§3a). It is the user's call, not one to make
  while fixing a label.
- **The `.wglink` bundle folder name and `archive_stem`.** Frozen per lineage
  by WG. Cheap to change but pointless without a reason.
- **The wrapper component name `WGLink_<slug>_<n>`.** Renameable by the user in
  Fusion already, and nothing keys on it.
- **The WG side.** Reading `linkName` in `ParamPanel.tsx:668` is a one-line
  follow-up in `waveguide-generator`, out of scope for a single branch here.
- **The two name-as-identity findings in §4.**
