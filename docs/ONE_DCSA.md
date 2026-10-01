# ONE Track & Trace production domain migration

Set `ONE_DCSA_ENABLED=true` and bind `ONE_DCSA_API_KEY` / `ONE_DCSA_API_SECRET` from AWS Secrets Manager on the ONE worker only. The endpoint is fixed to `https://apix.one-line.com`. Credentials and OAuth tokens must never appear in source, logs, or release evidence.

The worker obtains and caches a DCSA TNT_PROD token, reads `/v2/events`, follows bounded cursor pagination, deduplicates event IDs, and validates booking/container references before mapping. Estimates cannot become actual milestones. Missing destination evidence cannot produce destination ETA or final-vessel updates. Empty returns require actual EMPTY gate-in evidence after a verified destination discharge.

The existing `Vessel and Voyage/` remains the final destination leg. The latest actual event carries its own vessel/port. No new ClickUp fields or native status policy are introduced.

An API error, empty response, conflicting identity, unsupported pagination, or ambiguous booking-wide container population holds the record and preserves existing fields. Such holds must be reviewed after the first production run. The existing EDH implementation remains available with `ONE_DCSA_ENABLED=false` for rollback; it is not silently used after DCSA failures.

Release through protected main and required checks. Run a read-only ONE ECS pilot with the candidate digest and explicit transport guard against ClickUp writes before changing the ONE schedule. Preserve its exact prior task definition and schedule settings. Verify the next scheduled production run before communicating completion to ONE.
