# ONE container movement register

The ONE DCSA adapter retains each validated container's movements separately
from the existing shipment-wide milestone projection. Actual events retain
their source ID, container reference, UTC timestamp and carrier-local timestamp.
Split bookings retain the validated booking assigned to each container.

ClickUp tracking comments include an actual port-exit count and actual empty-return
count, individual timestamps with timezone, outstanding estimates clearly marked
as estimates, and carrier links/event IDs. The snapshot includes the register, so
partial progress and corrected times publish a new report even when the shipment
status does not change. Identical subsequent evidence does not republish it.

Counts require an exact container reference, an unambiguous destination match,
actual laden destination discharge at or before the gate event, a timezone-aware
timestamp not in the future, and the correct laden/empty state. Estimates never
complete a milestone. Incomplete container populations cannot establish full
completion. Existing operational date-only fields and workflow rules remain
unchanged; the register is published in comments, not new custom date fields.

The existing 33793 hold, identity validation, legal holds and terminal exclusions
remain in force. No additional carrier queries or infrastructure are required.
