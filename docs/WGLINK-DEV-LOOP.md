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
    "return_state_ms": 812.4,
    "per_link_ms": 41.9,
    "snapshot_ms": 870.2
  },
  "source": {"sourceCommit": "abc1234+dirty", "syncedAt": "..."}
}
```

`return_state_ms` is the export-scope walk and fingerprint — the same work Send
does short of writing STEP. It is recomputed from scratch on every tick, so it
is the first number to read when Fusion feels heavy while a managed link is
open. `source` appears only after a dev sync; a managed install stays silent.

## Going back to the managed install

Reinstall from Waveguide Generator (Help → Install Fusion add-in), or run WG's
`scripts/install_wglink.py`. Either restores the pinned copy and removes the dev
marker's claim by overwriting it.
