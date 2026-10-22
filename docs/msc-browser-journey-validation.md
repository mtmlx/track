# MSC booking-anchored browser batches

Container tracking can return a subsequent journey after equipment reuse.
Reloading or clearing cache does not establish shipment identity.

1. Export a fresh eligible queue. Search the current task booking/BL first.
2. Capture visible booking/BL identity, Port of Discharge, Shipped To and one
   complete container history per capture. For multi-container bookings,
   retain these headers with each isolated history; never combine timelines.
3. Import a preview with `--import-batch PATH --report REPORT.jsonl`.
4. Review rejected tasks separately. Missing booking identity, conflicting
   routes, ambiguous destinations and incomplete container coverage fail closed.
   Bare `KANSAS, USA` does not establish Kansas City. Correct authoritative task
   data before recapture; do not weaken the matcher to make a batch pass.
5. Apply the reviewed batch with `--apply --report REPORT.jsonl`. Re-read
   changed ClickUp fields and run another preview before declaring completion.

The destination may match the discharge port OR the inland Shipped To place.
Destination-arrival classification still uses the existing event-port rules.
The gate applies before all field and status planning, including the successful
T&T timestamp. A container-only fallback without an exact visible booking/BL
anchor is intentionally rejected, even when its destination happens to match.

JSONL reports are appended and flushed after each task outcome. `write_accepted`
means requests returned, not that independent readback succeeded. `write_started`
without acceptance, or `write_uncertain`, requires a fresh ClickUp read before
retrying. Resume with a batch of remaining/reconciled tasks, not by blindly
replaying accepted or uncertain writes. Reports contain evidence hashes rather
than raw captures or credentials. Existing source captures remain separate.

No historical data is reset or repaired by enabling this validation. Existing
container-only saved captures must be recaptured through the booking workflow.
