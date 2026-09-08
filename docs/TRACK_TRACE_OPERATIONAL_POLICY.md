# Track & Trace Operational Policy

Status: Approved operating policy and implementation contract.

This document defines the cross-carrier operational behavior for Track & Trace. It applies regardless of whether data comes from a carrier API, DCSA API, website, or approved manual capture. It does not activate new production behavior by itself.

## 1. Principles

- Preserve evidence-backed carrier facts with their source and event time.
- Move workflow statuses forward only, except for an approved correction or cancellation.
- Never replace an operations-owned value without an approved override policy.
- Stop carrier polling and Track & Trace projection for terminal shipments.
- Record enough evidence to explain every field or workflow change.

## 2. Source of truth and field ownership

| Data or decision | Owner | Policy |
| --- | --- | --- |
| Carrier events, vessel, ETD, ETA, gate, discharge, and containers | Carrier source | Automation may write only evidence-backed values and must retain source time and source URL or reference. |
| Native ClickUp task status | Operations workflow | This is the actionable workflow state. Automation may advance it only through the approved sequence. |
| `Estatus DB/` or carrier-status custom field | Carrier-data projection | It is explanatory, not an independent workflow authority, and must not silently disagree with native status. |
| Manual operational correction | Authorized operator | It wins until the operator clears it or controlled reconciliation approves replacement. |
| Last T&T Update | Automation audit | It proves a completed processing attempt, not that every intended write succeeded. |
| Canonical event ledger | System of record for provenance | It retains sanitized raw data, normalized event, source version, idempotency key, and projection result. |

The approved behavior for a manual correction is a shipment-level or field-level automation lock. Until that lock exists, a manual correction that might be overwritten is an exception requiring review before automation resumes for that shipment.

## 3. Eligibility and terminal conditions

Track & Trace runs only for open shipments with a supported carrier and at least one usable identifier: carrier booking, master bill, transport document, or container number. A blank booking number alone does not make a shipment ineligible when the carrier supports a verified alternative identifier.

The following native ClickUp statuses are terminal and must be prefiltered before any carrier call or ClickUp write:

- `blocked`
- `cancelado`
- `booking canceled`
- `booking cancelled`
- `canceled`
- `cancelled`
- `vacío devuelto`
- `vacio devuelto`
- `empty returned`
- `embarque cerrado`
- `closed`
- `completo en wf pagado`

A validated DCSA `CANC` event is also terminal. Retain the cancellation event, mark the shipment Track & Trace ineligible, transition the native ClickUp status through the approved `Cancelado` mapping, and stop future polling and projection. If identity, event validation, or status write/readback fails, halt tracking and create a one-time exception for Operations rather than treating the event as generic.

## 4. Booking confirmation

`Pendiente de booking` may move to `BK confirmado` only when all five conditions hold:

1. Carrier is set.
2. A verified booking reference is set.
3. ETD is set.
4. ETA is set.
5. The carrier explicitly reports the booking as confirmed.

`PENC`, `processing`, `data processing`, `pending`, `booking processing`, a booking number alone, and an estimated itinerary are not confirmation evidence. TNT 2.3 uses positive confirmation semantics for every carrier; it must not infer confirmation because a status is merely not recognized as pending.

## 5. Primary workflow state machine

| Target native status | Required evidence | Notes |
| --- | --- | --- |
| `BK confirmado` | All section 4 conditions | Do not infer confirmation from itinerary data. |
| `Recolectado` | Actual gate-out-empty and no actual gate-in-full | Do not regress if a later stage exists. |
| `En puerto Origen` | Actual gate-out-empty, actual gate-in-full, ETD in future | Before departure from the first departure port. |
| `Tránsito` | Actual/current-date ETD after gate-in-full and future ETA | Explicit origin feeder-barge load is also transit. |
| `Por arribar` | ETA is five to ten days ahead | Warning stage, not proof of arrival. |
| `arribado en puerto` | Actual destination discharge, or ETA today/past without conflicting later evidence | Actual discharge is stronger than an estimate. |
| `en ruta a almacén` | Actual gate-out delivery after destination arrival | Only for MTM-managed inland or an approved list mapping. |
| `en almacén` | Verified warehouse receipt or approved operations confirmation | Gate-out alone is not warehouse receipt. |
| `Vacío devuelto` | Actual gate-in-empty on/before today after delivery or warehouse stage | Terminal; no further Track & Trace activity. |

Rail milestones such as `At rail`, `Container arrived at ramp`, and `Released to consignee` are post-discharge facts. They may be retained and shown to operations but must not replace the primary ocean workflow without an approved status mapping for the list.

## 6. Vessel, location, and dates

- ETD is the first carrier-supported departure from the origin chain. An origin barge departure counts when it is the first actual or planned departure and affects pricing.
- ETA is arrival at the final port of discharge or final delivery port, not a transshipment arrival.
- Vessel/Voyage is the vessel expected to discharge at the final port once evidence exists. A later actual vessel-bearing event supersedes an earlier planned vessel.
- `BARGE` is an origin-stage temporary marker only. It requires explicit feeder evidence and must never overwrite a named ocean vessel after origin processing.
- Transport-call location, UN/LOCODE, terminal, and facility type are separate facts. A port must not be guessed from a vessel or copied from an unrelated movement.

## 7. Containers

- Number of Containers is the hard maximum for automatic projection.
- Automation may add a container only when the carrier source is authoritative for the shipment identity and the result stays within that maximum.
- A carrier response above the expected quantity creates an exception; it must not append stale or split-booking containers.
- A manually corrected container list is protected until controlled reconciliation provides carrier evidence for each replacement.

## 8. DCSA TNT 2.3 event policy

| Event or data element | Required handling |
| --- | --- |
| `PENC` | Retain as Pending Confirmation; do not confirm booking or advance workflow. |
| `CANC` | Retain as Cancelled; halt tracking and create a one-time exception or approved `Cancelado` transition. |
| `documentReferences` and reference types | Use for identity reconciliation and audit; do not discard. |
| `facilityTypeCode` and transport-call location | Preserve as normalized location facts. |
| `eventID` | Use as the primary idempotency key with carrier and specification version. |
| Unknown critical code or unsupported version | Quarantine with an actionable exception; do not project a generic event. |

## 9. Projection and write controls

Every projection is planned before it is written and read back after it is written. A partial ClickUp update is not a successful projection.

Each update must record shipment and carrier identity, source event ID/time/version, proposed fields and native-status changes, API response and readback, and whether an override or exception prevented a write.

The native task status and `Estatus DB/` must be reconciled after every workflow transition. A comment alone is not a successful workflow change.

## 10. Current implementation gap register

| Policy requirement | Current state | Required follow-up |
| --- | --- | --- |
| Native terminal-status prefilter | Implemented | Retain and test for every carrier path. |
| Forward-only primary workflow | Implemented | Keep as a hard invariant. |
| Positive confirmation for all carriers | Partial | Replace non-ONE negative-pending fallback. |
| Carrier `CANC` stops work | Shadow lane records it as `halted`; no ClickUp projection exists | Add the approved cancellation projection only after carrier payload review. |
| `PENC` prevents confirmation | Shadow lane records it as `requires_review`; no ClickUp projection exists | Add the approved pending-booking projection only after carrier payload review. |
| Manual field ownership | Not implemented | Add override metadata and reconciliation workflow. |
| Durable event evidence and idempotency | Implemented for the DCSA shadow lane | Retain DynamoDB evidence, conditional event identity, and non-projecting review gates. |
| ClickUp readback and resumable status writes | Partial | Make workflow writes durable and verified. |

## 11. Approved pilot scope

CMA and Maersk are the shadow-mode pilots, subject to endpoint and version validation. Existing carrier schedules remain unchanged. No ClickUp field, status, comment, or customer-facing output is written from the DCSA lane until real carrier events validate the policy.

## 12. DCSA shadow implementation

The `dcsa-tnt-shadow` command is an isolated ingestion lane. It reads ClickUp
inventory, accepts only official API event payloads, validates the declared TNT
contract, redacts sensitive payload values, and records evidence without a
ClickUp write path. Its DynamoDB ledger is append-only from the worker's
perspective: conditional puts, reads, indexed queries, and projection-state
updates only. It has no delete or scan permission.

The CMA CGM pilot is fixed to the carrier's published TNT 2.2 contract. The
Maersk pilot requires an explicitly reviewed TNT version and `MAERSK_API_MODE`
set to `events`; automatic mode and public-site fallback are rejected. The
implementation and production acceptance gates are detailed in
[`docs/DCSA_SHADOW_PILOT.md`](DCSA_SHADOW_PILOT.md).
