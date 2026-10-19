# ONE forecast reconciliation and departure evidence

## Contract

- An identity-matched ONE forecast can refresh ETD at the first origin port.
  It cannot populate actual gate/discharge fields or prove transit.
- A known first-origin load/gate location prevents a later transshipment
  departure from being substituted when origin departure is missing.
- An actual departure at the first origin takes precedence over its forecasts.
  Conflicting forecast departures at that origin hold the update for review.
- A confirmed booking without assigned containers can carry an origin forecast
  and destination vessel, but never physical milestones. Pending, cancelled,
  rejected and unknown booking lifecycles cannot publish these forecasts.
- A destination import voyage alone is not the documented sailing voyage.
  A unique matching export-voyage forecast can resolve it; actual export
  evidence retains precedence. Existing vessel/rotation conflict guards remain.
- ONE truck/rail departures cannot establish ocean ETD or transit. Estimated
  events cannot establish transit. Existing actual-event and barge guards remain.
- Routine tracking never rolls a human/native-automation status backward.
  Historical incorrect statuses require separately approved repair.
- ONE completion timestamps are written after successful fields, status and
  comment writes. A partial write is not recorded as a completed check.

## Evidence and external automations

CloudWatch receives `one_tracking_validated` before writeback, with source,
booking-only flag, discovered containers, origin schedule classifier,
destination vessel, ETA and normalized movement evidence. The separate
`one_tracking_writeback` event identifies updated/unchanged completion. A
validation event alone does not prove successful writeback.

Native ClickUp automations are outside this repository. An automation that
moves a shipment to transit or sends a departure notice from ETD alone remains
unsafe: ETD is a forecast. Require a dedicated actual-departure validation
signal (or reviewed actual movement evidence), tied to the current booking,
before both the transition and customer notice. Do not treat booking
confirmation, the clock reaching ETD, or Last T&T Update as that signal.
No native ClickUp automation was modified by this release.

## Reviewed hold: MTMLXGT-33793 / 86e3etkkm

`allocations/one_write_holds.json` protects this exact task pending repair
approval. Scheduled runs skip it before preflight and notices. The writeback
entry point also refuses all writes, including comments and timestamps.
Read-only planning remains available. Do not remove the hold until an approved
repair has been applied and read back; then release it in a reviewed commit.

Evidence available on October 18, 2026:

- CNS confirmation in task comments: booking SH6AAN844400, RDO ACE 2642E,
  ETD October 26, cutoff October 22. Comment 90170260895096 flags the conflict.
- Saved task: ONE SERENITY 2639E, ETD October 18, native transit status.
- Current carrier evidence: TRHU4409892, RDO ACE 2642E, Shanghai departure
  October 21 ESTIMATE, destination arrival November 25 ESTIMATE. No actual
  loading/departure. These dates conflict with CNS; do not silently call either
  departure date confirmed or actual.
- ClickUp status history: transit at 2026-10-18T10:00:07.607Z; ClickBot notice
  comment 90170263795783 at 10:00:10.249Z. This precedes the later T&T check.

Repair preview must refresh the task and carrier evidence after deployment,
display exact before/after values, identify any forecast/date conflict, propose
only supported fields and the evidence-supported operational status, and leave
booking, ETA, cutoff and unrelated fields unchanged unless explicitly justified.
No guessed destination dates or broad resets. Approval is required before any
repair, then verify saved fields and a repeated read-only plan for recurrence.

## Regression coverage

`tests/test_one_rebooking.py` covers forecast ETD/local dates, first-origin
selection, confirmed containerless bookings, unconfirmed safeguards, forecast
vessel/export direction, actual/inland departures, ambiguous schedules,
idempotence, completion-write ordering, and the protected repair task.

## Inventory recovery: depot, split references and missing references

- Empty-pickup GTOT is not an ocean-origin anchor. Bind departure to the first
  laden gate-in/load location; absent those anchors, use the first eligible
  departure. Preserve the feeder/barge anchor when its own departure is missing:
  never substitute a later mother-vessel departure. No new port aliases are
  inferred from depot geography. Forecasts remain non-physical evidence.
  A gate-out before first loading invalidates the same depot's earlier gate-in.
  When explicit loading exists, forecast gate-ins cannot displace that origin;
  actual feeder gate-in evidence still prevents borrowing a mother departure.
- The reviewed 31624 mapping accepts the parent booking alone or the exact full
  reviewed booking set (comma/semicolon/whitespace separated). Duplicate,
  unknown, child-only and partial combinations are rejected. The saved booking
  field is never rewritten; every container response still requires its own
  positive BKG identity, correct container population, destination and count.
- ONE tasks without both references remain discoverable for notification only.
  After terminal/hold/exclusion checks and before freshness/preflight/lookup,
  report missing information to the current assignees through the existing
  concurrency-checked, cooldown-deduplicated comment path. Do not query a carrier,
  stamp Last T&T Update, update dates/vessel or advance status. A pending booking
  is not a carrier failure; owners must supply confirmed references when issued.
- Regression tests: `tests/test_one_inventory_recovery.py`. Native ClickUp
  departure automations remain outside this release, and 33793 stays held.

## Confirmed booking-only EDH forecasts

The EDH fallback maps E061 estimated departure only after exact booking identity,
complete population, explicit confirmation and matching shipment POD checks.
Departure location must exactly match the carrier POR name/code; do not infer
port aliases from geography or country fields. Preserve the UTC instant and
ONE's port-local wall-clock rendering separately. Conflicting origin schedules
and timezone-less departure dates fail closed.

This forecast is not added to physical movement history. Booking-wide actual
events cannot establish pickup, loading, transit, discharge or return without
container evidence. The booking header's vessel/voyage is not proof of a final
destination leg, so it never overwrites a verified shipment vessel. Existing
ETA behavior and the 33793 write hold remain unchanged.
