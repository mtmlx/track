# ETA observation pilot

This is a separate evidence collector for ONE and CMA CGM, not a new operational
tracker. It never calls a ClickUp field, status, comment or timestamp write API.
No normal schedule, Lightsail instance or production projection is changed.

## Why another record type

Event idempotency alone cannot preserve what was known at each poll. An event
may retain its identifier while the forecast changes; an unchanged forecast is
also useful evidence of coverage. Each explicit run therefore creates an
immutable observation per current task/booking/container. Retrying the same
run reuses stored observations without a second carrier lookup. A changed
identity requires a new run. New polls always use new run IDs.

The existing DCSA event store remains unchanged. Observation keys start with
`eta-observation#v1#`; carrier and task GSI keys start with `eta-capture#` so
existing event queries do not consume these records. The worker uses conditional
puts and consistent readback; it never updates or deletes evidence. Existing
DynamoDB table configuration and IAM policies are not modified by this command.

## Evidence and qualifications

- Retain sanitized carrier events, query identity, account list, current
  allocation/quantity/POD, contract label, UTC request/observation timestamps,
  cursor coverage, event IDs, original offsets, mode, locations and voyages.
- Compressed payloads carry a SHA-256 checksum; oversized records fail closed.
  Exceptions are retained as type-only failures, never response bodies or secrets.
- Original confirmed ETA and advertised transit remain null until independently
  supplied. Current carrier forecasts are not relabeled as original promises.
- Forecast ARRI and actual vessel DEPA/ARRI require positive identity, explicit
  timezone and mode. Actual timestamps must not be future. Final ARRI must match
  the authoritative POD. DISC, truck and rail never stand in for vessel arrival.
- ONE uses the existing official `/v2/events` client with strict booking identity
  and reviewed split allocations. Its carrier endpoint path is not a claim of
  TNT 2.2 or 2.3 conformance.
- CMA uses its established TNT 2.2 official paginated reader, never a browser.
  If CMA masks a BKG as an opaque 64-character hex reference, it is not guessed
  or treated as the visible booking. A separate full booking query must return
  exactly the recorded container set, the same opaque reference and at least
  one matching allocation witness for the queried unit (event ID/time/code/
  classifier/location/mode plus EQ). Shared IDs with conflicting signatures
  fail closed; every unit event must retain the same opaque BKG reference.
  The booking view may omit unit-only equipment history; that history remains
  in the separate unit response and is never dropped or claimed identical.
  Both query results remain in the evidence. Otherwise the capture is review-only.
- Failed lookups, empty responses, unknown classifiers and incomplete identity
  remain explicit gaps. They cannot produce an adherence score.
- Explicit reviewed holds, legal tags and configured terminal statuses skip
  lookup and persistence. Only the explicit task allowlist is loaded.

## Configuration and execution

Config-check is offline and needs no credentials:

```bash
eta-shadow-capture
```

A live pilot is limited to ten explicitly allowed shipments (default two):

```bash
ETA_SHADOW_ENABLED=true \
ETA_SHADOW_BACKEND=dynamodb \
ETA_SHADOW_TABLE=track-trace-dcsa-events \
AWS_REGION=us-east-2 \
eta-shadow-capture --run --run-id <unique-run-id> \
  --task-id <native-one-task-id> --task-id <native-cma-task-id>
```

Inventory list membership and carrier secrets must be injected through existing
approved bindings. Do not put credentials into CLI arguments, files or logs.
For local fixtures only, use `ETA_SHADOW_BACKEND=sqlite` and an explicit
`ETA_SHADOW_SQLITE_PATH`. ECS must use the durable backend; no implicit local
fallback exists. SQLite evidence files are created with mode 0600.

## Acceptance before scheduling or expansion

1. Inspect the current task and approved scope; exclude holds and legal cases.
2. Capture one ONE task and one CMA task; verify every unit and all cursor pages.
3. Read saved records consistently and verify checksums and identity evidence.
4. Read ClickUp independently before/after: ETA, status, tracking stamps, every
   other custom field and task update timestamp must remain unchanged.
5. Repeat the same run: zero new records and zero repeated carrier lookups.
6. Run fixture regressions for revised and unchanged forecasts, rebooking,
   ambiguous identity, missing POD, transshipment discharge, future ACT,
   estimates, truck/rail, errors, unknown classifiers and legal/terminal holds.
7. Only after this evidence passes, review an isolated digest-pinned ECS task
   and bounded pilot schedule. Do not merge through the normal all-carrier
   production deployment as a shortcut. New carrier adapters remain disabled.

The initial canary validates capture safety and evidence quality. It does not
establish historical ETA adherence, original commitments or comparable transit
performance. Prospective repeated observations and sufficiently mature,
lane/service-matched cohorts are still required.

## Report separation

Management exports use stable Carrier A/B/etc. labels and pseudonymous vessel
labels. Real carrier identities, credentials and raw payloads remain in private
operational evidence, not the management bundle. Numerical results and dates
must not change when labels are anonymized.
