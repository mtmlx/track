# ONE split bookings

ONE can assign the containers of one ClickUp shipment to different bookings or
master BLs. A shared original reference is not sufficient proof for all units.

Reviewed allocations live in `src/shipment_sync/allocations/one_split_bookings.json` and
ship with the installed package. Add an entry through the normal reviewed release
process only after checking each booking/container against the carrier and source
documents. Never infer a mapping from a failed lookup, an adjacent booking number,
an email request, or an unrelated container's history.

Each entry is bound to the native ClickUp task ID, original booking, exact complete
container set and count, and destination port. A changed task requires a new review;
the adapter fails closed rather than reusing a stale allocation. Every event in a
split lookup must explicitly identify only that container's approved booking.
Container identity, pagination, timestamp, destination and terminal-status checks
remain unchanged. All lookups finish before any shipment writeback.

Shipment completion ETA remains the latest destination ETA across all units, only
when all units have an ETA. Physical milestones still require actual evidence for
every unit. A transshipment discharge is not final arrival. Different destination
vessels are not collapsed into a guessed shared vessel; the tracking comment lists
each unit's booking, destination vessel, ETA and source. Existing protected fields
and manual status safeguards remain in effect.

## MTMLXGT-31624

Reviewed October 16, 2026 against ONE's result pages and the two carrier MBL
attachments on task `86e2bkqh7`:

| Container | Booking | Destination |
| --- | --- | --- |
| ONEU4454604 | NB6BI3181300 | GTPRQ |
| ONEU4413040 | NB6BI3181301 | GTPRQ |

The ClickUp parent booking and both valid container identifiers are retained.
Revalidate the allocation whenever the booking, container population or POD changes.
