Acknowledgement files exactly as WG writes them.

Each `*.json` was produced by calling WG's real `write_acknowledgement()`
(`server/cadlink/solve_command.py`) at WG commit a37609bc (branch
`feature/cad-request-acceptance-file`), with the refusal reasons taken from
WG's own constants and helpers (`OUTDATED_ADDIN_REASON`, `inbox_refusal`,
`_delivery_conflict`). Do not edit by hand; regenerate from WG.

- `accepted.json`: WG holds a Solve.
- `refused-old-addin.json`: a request from an older WGLink.
- `refused-invalid.json`: a request that is not valid (a Send that names a returnId).
- `refused-conflict.json`: the command id was used for a different request (`digest` null).
