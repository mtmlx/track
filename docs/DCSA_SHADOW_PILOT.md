# DCSA Shadow Pilot

## Purpose

The DCSA shadow lane captures and validates official carrier event payloads
without changing operational Track & Trace behavior. It is separate from the
normal `shipment-sync` command and has no ClickUp update, comment, field-write,
or native-status-write path.

It is an evidence lane, not a second production tracker.

## Scope

- Pilot carriers: CMA CGM and Maersk.
- CMA CGM is pinned to its approved TNT 2.2 `GET /events` contract.
- Maersk is admitted only after a captured payload establishes its TNT version.
- A browser, scraped page, public tracking response, or `API-Version` header is
  not evidence of a DCSA TNT version.
- Existing carrier ECS schedules stay unchanged.

## Command and safety boundary

```bash
# Parses configuration only. It makes no external request.
dcsa-tnt-shadow

# Reads ClickUp inventory and official carrier events, then records validated
# evidence in the configured ledger. It never writes to ClickUp.
dcsa-tnt-shadow --run
```

`--run` is enabled only with all required explicit configuration. A disabled or
partially configured shadow lane exits without accessing ClickUp or a carrier.

## Required configuration

```dotenv
DCSA_TNT_SHADOW_ENABLED=true
DCSA_TNT_SHADOW_CARRIERS=cma cgm
DCSA_TNT_SHADOW_CMA_CGM_VERSION=2.2
DCSA_TNT_SHADOW_MAX_SHIPMENTS=25

# Local validation only
DCSA_TNT_LEDGER_BACKEND=sqlite
DCSA_TNT_LEDGER_DB_PATH=/tmp/dcsa-events.sqlite3

# ECS production shadow worker only
# DCSA_TNT_LEDGER_BACKEND=dynamodb
# DCSA_TNT_LEDGER_TABLE=track-trace-dcsa-events
```

For Maersk, add `maersk` to `DCSA_TNT_SHADOW_CARRIERS` only after an approved
captured response establishes the contract, then set
`DCSA_TNT_SHADOW_MAERSK_VERSION=2.2` or `2.3`. The source requires
`MAERSK_API_MODE=events` and `MAERSK_TRACKING_API_URL`; it rejects automatic
mode and all website fallbacks.

## Evidence ledger

Production evidence belongs in DynamoDB, never an ECS task filesystem. The
ledger stores a redacted raw payload plus a normalized record and uses a
conditional event-key insert for idempotency.

The infrastructure template creates:

- `track-trace-dcsa-events`, a pay-per-request DynamoDB table with point-in-time
  recovery, server-side encryption, service-level deletion protection, and
  carrier/task indexes;
- `TrackTraceDcsaShadowTaskRole`, a dedicated role that can query, read, write,
  and update only that ledger; it has no ClickUp or scheduler permissions;
- a dedicated CloudWatch log group with 30-day retention.

Provision it only from an authorized AWS session:

```bash
scripts/provision_dcsa_shadow_ledger.sh
```

The template retains the ledger and log group on stack deletion. It grants no
delete or scan permissions.

## Run acceptance gates

Before a scheduled shadow task is created, demonstrate all of the following:

1. A carrier response is validated against the declared TNT version with no
   malformed-event leakage into the ledger.
2. Re-running the same response creates no duplicate event evidence.
3. The ledger contains the correct carrier, task identity, source endpoint,
   event time, event code, and redacted payload.
4. The summary confirms zero ClickUp writes; a direct ClickUp task readback
   shows no field, native-status, or comment change.
5. CMA and Maersk deltas are reviewed against the existing carrier path before
   any projection design is implemented.

`PENC` remains `requires_review`; `CANC` remains `halted`. Neither condition
activates a ClickUp change during this pilot.

### CMA TNT 2.2 compatibility

CMA's published TNT 2.2 contract documents `LOAD` and `ERT` as CMA reference
extensions. The canonical parser admits them only when `carrier` is CMA CGM
and the declared contract is TNT 2.2; other carrier/version combinations stay
on the DCSA reference-code set. Shadow summaries aggregate validation failures
into stable non-payload categories so an unsupported carrier extension can be
diagnosed without logging event bodies.

The live CMA endpoint also emits opaque, non-UUID `eventID` values despite the
published UUID format. Those are accepted only for CMA TNT 2.2, bounded to 256
characters, used as opaque idempotency input, and recorded with the
`carrier-event-id-not-uuid` conformance warning. Other carriers and versions
remain UUID-strict.

## CMA legacy-versus-DCSA comparison

The comparison lane is a separate, one-off test of the current CMA API
projection against the complete validated DCSA TNT 2.2 event stream. It does
not use the public CMA website or Playwright.

```bash
# Configuration only; no network call.
cma-cgm-dcsa-compare

# Explicitly enabled, bounded read-only comparison.
CMA_CGM_COMPARISON_ENABLED=true \
CMA_CGM_COMPARISON_MAX_SHIPMENTS=25 \
SHIPMENT_ALLOWED_LINES='cma cgm' \
cma-cgm-dcsa-compare --run
```

For every selected non-terminal CMA shipment, the lane makes one official API
cursor traversal. The existing adapter's legacy interpretation is generated
from the first response page, while the DCSA interpretation validates every
page linked by CMA's `Next-Page` cursor header. The result reports only counts,
event codes/timestamps, ETA, vessel/voyage, and named differences; it does not
log raw payloads, container identifiers, credentials, or raw ClickUp field
values.

`CMA_CGM_DCSA_MAX_PAGES` defaults to 25 and is capped at 250. If CMA supplies
another page after the limit, the comparison fails that shipment rather than
claiming complete coverage. Cursor links are restricted to the configured CMA
API origin, and repeated pages fail closed.

The command has no ClickUp field/status/comment write path, no ledger-write
path, and no scheduler creation/update path. It is not a replacement for the
normal CMA worker and must remain unscheduled until its comparison evidence is
reviewed. It requires the exact single-carrier scope
`SHIPMENT_ALLOWED_LINES=cma cgm`; if ClickUp cannot apply that carrier filter,
the comparison fails closed rather than loading the entire shipment inventory.

### CMA comparison ECS canary

The ECS comparison task is separate from both the normal CMA worker and the
DCSA event-ledger canary. It reuses only the existing isolated task role and
log group, writes no DynamoDB record, and is never scheduled by its helper
scripts.

```bash
CMA_CGM_COMPARISON_IMAGE_URI=525753067477.dkr.ecr.us-east-2.amazonaws.com/track-trace@sha256:<digest> \
scripts/register_cma_cgm_comparison_task.sh

scripts/run_cma_cgm_comparison_canary.sh
```

Use `--dry-run` on either helper to inspect its task/network configuration
without registering or starting anything. The runtime is constrained to the
existing CMA worker's Fargate network configuration and an immutable
Linux/amd64 image.

## CMA ECS canary

The CMA canary is an explicitly separate task family. It preserves the normal
CMA worker's `X86_64` Fargate runtime and VPC network configuration but uses
the DCSA-only task role and log group. It receives only the inventory and CMA
OAuth secret bindings required by the shadow command; normal status/comment
settings are not injected.

Build and push an immutable `linux/amd64` image, then register the task by
digest:

```bash
DCSA_CMA_SHADOW_IMAGE_URI=525753067477.dkr.ecr.us-east-2.amazonaws.com/track-trace@sha256:<digest> \
scripts/register_dcsa_cma_shadow_task.sh
```

The task is fixed to CMA CGM TNT 2.2, `DCSA_TNT_SHADOW_MAX_SHIPMENTS=25`, and
the DynamoDB ledger. Launch one canary only after task-definition readback:

```bash
scripts/run_dcsa_cma_shadow_canary.sh
```

Neither command creates an EventBridge Scheduler schedule. Scheduling remains
disabled until the ledger and log evidence have been reviewed and accepted.
