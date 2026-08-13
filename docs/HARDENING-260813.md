# WGLink hardening items from the 2026-08-13 review — need live Fusion to validate

These are design-level findings from the adversarial review of the five-commit stack
(`Auto-insert … handoffs` → `Record observed parameters in mm`). They are deliberately
NOT fixed blind: each changes duplicate-registration or IPC-ownership behavior that can
only be validated with Fusion running (and ideally a deliberately double-registered
add-in, per the JSLoadedScriptsinfo trap).

1. **Adopted registrations advertise unserviceable session IDs** (WGLink.py ~:992).
   The presence thread publishes fusion-status with the adopted instance's own
   `sessionId`, but only the owning registration's watch loop services return
   requests, and request markers are session-scoped. WG can therefore address a
   return request to a session that will never consume it. Direction: elect exactly
   one IPC owner per process — only that instance publishes status and consumes
   requests; the adopted instance's presence should either republish the owner's
   session or carry an explicit `adopted: true` that WG treats as non-serviceable.

2. **Owner loss / partial startup leaves broken or competing ownership**
   (WGLink.py ~:1155). If the owner's panel construction fails partway, or the owner
   stops while an adopter survives, neither instance cleanly owns the panel or the
   watch. Direction: transactional panel construction + lease-based adopter promotion.

3. **Only `wglink_workspace` is registration-local** (WGLink.py ~:59). The other
   `wglink_*` modules are still bare-imported and stale-cacheable across double
   registrations — the same class of bug the workspace loader fixed. Direction: load
   the whole WGLink package under a registration-unique namespace. Scoped deliberately
   small in bd34ddb; widen only with a live double-registration test.

4. **Return publication lacks exclusive filename reservation; overwrite is
   crash-nonatomic** (wglink_send.py ~:1122). Two Fusion processes (rare, but Fusion
   can run twice) publishing the same target race between exists-check and
   `os.replace`; the backup dance can strand a `.bak` on crash. Direction: interprocess
   lock file or fully immutable unique names; make backup cleanup best-effort on next
   publish.

5. **Idle watch tick does three full main-thread document surveys** (WGLink.py ~:948).
   Each ~4 s tick recomputes the document signature, body inventory, and source state
   even when nothing changed. Direction: snapshot once per tick; recompute only after a
   command/document event indicates mutation. Perf-only; measure in live Fusion first.

Test-fidelity notes from the same review worth acting on when live:
- Lifecycle tests never exercise owner-first stop, three registrations, or partial
  panel construction.
- Body fakes put attributes on bodies directly; real occurrence proxies expose zero
  attributes (the `nativeObject` fallback is correct but unguarded by a realistic test).
- `runOnStartup: true` in the manifest is overridable by Fusion's JSLoadedScriptsinfo —
  manifest-only assertions are insufficient (both current registrations are `true` on
  the Windows machine; the pre-dedupe backup had both `false`).
