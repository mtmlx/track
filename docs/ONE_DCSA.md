# ONE Track & Trace production domain migration

Set `ONE_DCSA_ENABLED=true` and bind `ONE_DCSA_API_KEY` / `ONE_DCSA_API_SECRET` from AWS Secrets Manager on the ONE worker only. The endpoint is fixed to `https://apix.one-line.com`. Credentials and OAuth tokens must never appear in source, logs, or release evidence.

The worker obtains and caches a DCSA TNT_PROD token, reads `/v2/events`, follows bounded cursor pagination, deduplicates event IDs, and validates booking/container references before mapping. Estimates cannot become actual milestones. Missing destination evidence cannot produce destination ETA or final-vessel updates. Empty returns require actual EMPTY gate-in evidence after a verified destination discharge.

The DCSA empty/laden indicator distinguishes empty pickup, full gate-in, laden destination discharge and delivery, and empty return. A different or missing load state cannot validate the corresponding date field or origin milestone.

The existing `Vessel and Voyage/` remains the final destination leg. The latest actual event carries its own vessel/port. No new ClickUp fields or native status policy are introduced.

For multiple containers, retrieve and validate every container separately. A shipment-wide actual milestone requires matching actual evidence at the same port across all containers, dated when the last container completes it. Destination gate-out and empty-return evidence must follow each container's own actual discharge. ETA uses the last destination arrival across every container; write the final vessel only when all containers agree. The existing native status policy remains in force. Booking discovery can fill missing containers only when it includes every listed container and matches the declared count where present. Requests are bounded to twenty containers per record.

An API error, empty response for any container, conflicting identity, unsupported pagination, or a mismatched container population holds the entire record and preserves existing fields. Missing destination ETA or differing final vessels preserve their corresponding existing fields. Such holds must be reviewed after the first production run. The existing EDH implementation remains available with `ONE_DCSA_ENABLED=false` for rollback; it is not silently used after DCSA failures.

Release through protected main and required checks. Run a read-only ONE ECS pilot with the candidate digest and explicit transport guard against ClickUp writes before changing the ONE schedule. Preserve its exact prior task definition and schedule settings. Verify the next scheduled production run before communicating completion to ONE.
