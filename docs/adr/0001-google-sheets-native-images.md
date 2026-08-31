# ADR 0001: Google Sheets native image mutation is unsupported

Status: closed / not prioritized, accepted gap for v0.27.1, reviewed 2026-08-30

## Decision

The Gateway does not expose Google Sheets image insert or replace operations in
v0.27.1. It also rejects the architectural fallback of writing an `IMAGE()`
formula backed by a transient signed URL. No release containing this increment
claims native visual-asset support for Sheets. This accepted gap does not block
v0.27.1 production because Docs visual assets are independently validated and
existing structured Sheets operations are unchanged.

## Evidence

- The current Sheets API v4 discovery document has no add-image, insert-image,
  replace-image, image resource, or image readback schema. It exposes only
  generic delete/position operations for an already-existing embedded object.
- Apps Script `Sheet.insertImage(blob, ...)` and `OverGridImage.replace(blob)`
  do create native over-grid images from bytes, but remote execution requires
  `scripts.run` and Google explicitly documents that the Apps Script API does
  not work with service accounts.
- This Gateway authenticates keylessly as a service account and uses domain-wide
  delegation. Introducing a human refresh token or service-account key would
  violate the runtime identity and secret-management model.

Primary references:

- https://sheets.googleapis.com/$discovery/rest?version=v4
- https://developers.google.com/apps-script/reference/spreadsheet/sheet#insertimageblobsource,-column,-row
- https://developers.google.com/apps-script/reference/spreadsheet/over-grid-image
- https://developers.google.com/apps-script/api/how-tos/execute

## Rejected alternatives

- `IMAGE(short-lived-url)`: the cell retains a formula that can refetch the URL;
  persistence after expiry/deletion is not guaranteed, so it fails the durable
  native-image requirement.
- Private or undocumented Sheets RPCs: unsupported and unauditable.
- Browser/UI automation: not a production API contract and cannot provide the
  required concurrency, readback, or failure semantics.
- Human OAuth refresh tokens or Apps Script web apps: materially expand secrets,
  identity, and attack surface and were not authorized by this increment.
- XLSX export/re-import: does not perform a safe in-place mutation of the same
  native Sheet and risks loss of native semantics.

## Revisit condition

Revisit when Google adds a supported Sheets API request that accepts image bytes
or a one-time fetch and exposes stable readback, or when Apps Script API
executables support the Gateway's keyless delegated service-account identity.
The production test must insert and replace an image, wait beyond URL expiry,
delete staging, and then verify native readback on the same spreadsheet.
This is not an active v0.27.1 workstream and requires a new prioritized increment.
