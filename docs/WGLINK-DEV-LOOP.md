# Validating a WGLink change without a release

WGLink reaches Fusion as a copy that Waveguide Generator installs from a pinned
commit. That is right for users and wrong for development: proving a one-line
change means push the add-in, bump WG's pin, build the package, install it and
restart Fusion — minutes of round trip for an edit that took a second, and
nothing on the way back states which build Fusion actually loaded.

The short way round replaces the *contents* of the add-in Fusion already has
registered.

```
python scripts/dev_sync_wglink.py     # copy this checkout over the install
```

Then restart the add-in in Fusion — **Utilities → Add-Ins → WGLink → Stop, then
Run**. A full Fusion restart is not needed.

```
python scripts/dev_sync_wglink.py --status
```

`--status` prints the commit the *running* add-in reports through its own
heartbeat, which is what proves the restart picked the edit up, and the wall
clock of its last watch tick. `--watch` re-syncs on every save.

## Do not register the repository as a second add-in

Fusion's registry, not `WGLink.manifest`, decides what loads. Adding the
checkout under **Scripts and Add-Ins** leaves two registrations of one add-in,
which are two Python modules with separate globals: the second deletes the
first's panel and command definitions, and the symptoms (an empty panel, or
buttons that vanish on stop) look nothing like the cause. Syncing in place keeps
exactly one registration at exactly one path.

## Reading the heartbeat

The add-in republishes `.fusion-status.json` in WG's IPC folder on every watch
tick — every four seconds, on Fusion's main thread. Under `diagnostics` it
carries what that tick cost:

```json
"diagnostics": {
  "watchIntervalSeconds": 4.0,
  "lastTickMs": {
    "resolve_links_ms": 12.4,
    "geometry_state_ms": 0.3,
    "geometry_state": "cached",
    "geometry_state_age_s": 37.2,
    "per_link_ms": 3.1,
    "snapshot_ms": 16.4
  },
  "source": {"sourceCommit": "abc1234+dirty", "syncedAt": "..."}
}
```

`geometry_state_ms` is the export-scope walk and body fingerprint — the work
Send does short of writing STEP, and the only part of a tick that evaluates
geometry rather than reading a property. `geometry_state` says which of three
things the tick did:

| | |
|---|---|
| `measured` | the document moved, so it was measured; this is the real cost |
| `cached` | nothing moved — the tick was free |
| `deferred` | it moved, but the last measurement was expensive enough that another one is not due yet |

A healthy idle document reads `cached` with `geometry_state_ms` near zero. A
run of `measured` ticks at hundreds of milliseconds on a document nobody is
editing is the bug this instrumentation exists to catch. `source` appears only
after a dev sync; a managed install stays silent.

## Going back to the managed install

Reinstall from Waveguide Generator (Help → Install Fusion add-in), or run WG's
`scripts/install_wglink.py`. Either restores the pinned copy and removes the dev
marker's claim by overwriting it.
