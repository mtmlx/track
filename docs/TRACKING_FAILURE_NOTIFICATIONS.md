# Tracking failure notifications

This implementation adds assignee-facing comments for identified shipment failures
across registered carriers and the MSC browser capture/import workflow. Deployment
is a separate approval step; this document does not activate a release.

## Content and ownership

Every notice names the carrier, booking/container references, expected quantity,
failure stage and official carrier website. It separates the issue, impact, reason
tracking cannot proceed, and corrective actions. It mentions the task's current
assignees. An unassigned task receives an explicit request to assign an owner;
the automation never invents or changes an assignee. ClickUp delivery preferences
still determine email/push notification delivery.

| Failure | Required response |
| --- | --- |
| Booking/container identity conflict | Confirm allocation, including splits/reassignments; correct references only with evidence. |
| Missing/invalid references or incomplete container captures | Supply the full assigned identifier set and all per-container captures; verify quantity. |
| Missing/ambiguous destination evidence | Verify authoritative POD and event locations; never guess destination arrival/discharge. |
| No usable or incomplete carrier data | Obtain carrier confirmation; if website and API differ, request integration review. No invalid-booking inference. |
| Authentication, configuration or preflight failure | Integration support checks endpoints, network and authorized access; never paste credentials into ClickUp. |
| Browser challenge/access block | Use the official authorized browser/manual recovery path or contact carrier/support. Do not alter valid shipment data. |
| Timeout/service limit | Review availability and backoff; avoid repeated retries. |
| ClickUp writeback failure | Read back fields/activity before retrying; partial writes are possible. |

Wan Hai access-block notices retain the website link, complete-screen copy/paste
instruction, and TracyAI manual extraction handoff. A blank container field can
be legitimate for a supported, explicitly confirmed booking-only lookup.
`Not Assigned` is a placeholder, not a container identifier. The comments do not
declare a booking cancelled or invalid solely because a lookup failed.

## Guards

- Fresh task identity, carrier, booking, containers, quantity, POD and terminal-status
  checks prevent comments about replaced references or ineligible shipments.
- A newer successful Last T&T Update supersedes a lookup failure.
- The same category/references/assignees are suppressed for 24 hours across API
  and browser paths. An owner or reference change can create a new notice.
  Distinct failed container references have separate cooldown keys so none are
  hidden behind another container's notice.
- Comment history is paginated with a bounded safety limit. Unavailable or
  malformed history fails closed; the worker logs delivery failure.
- Comment POSTs are single-attempt. An ambiguous response is not blindly retried.
- The task is re-read just before posting. Any intervening field/status/owner
  change suppresses the stale notice. Existing ONE conflict markers are honored
  during their 24-hour cooldown when upgrading to the shared notifier.
- Three notification failures pause comment delivery for the remainder of that
  batch, preventing a ClickUp outage from delaying every shipment indefinitely.
  Carrier processing continues and delivery failures remain in the run logs.
- The notice does not change shipment fields, status, assignees or Last T&T
  Update. Writeback notices explicitly warn that preceding writes may be partial.
- Read-only projection failures are not labeled uncertain writes. Browser planning
  exceptions remain in the private run report/logs. Mixed accepted/failed browser
  evidence names the failed reference without implying an accepted write failed.
- Browser preview mode never posts comments. Only approved `--apply` imports do.
- Excluded, recent, terminal and deliberately unsupported shipments are not
  failures and do not receive notifications.
- Carrier circuit-breaker and preflight notices explicitly say the task was not
  queried; they do not imply a shipment-specific carrier rejection.
- Raw exceptions, response bodies, credential values and token-bearing URLs are
  not copied into task comments. Operators consult restricted runtime logs.

## Scope limits

A task comment requires an identified, eligible shipment and working ClickUp
access. Discovery/startup failures that prevent task identification remain global
operational errors. A service outage affecting ClickUp can also prevent comment
delivery; a task comment is not a substitute for infrastructure monitoring.

The history check and POST are not an atomic distributed lock. Avoid overlapping
API/browser writers; simultaneous workers can race. This implementation does not
change ECS schedules, browser execution infrastructure, or carrier access policy.

## Validation

`tests/test_tracking_failure_comments.py` covers all registered carrier families,
failure categorization, native owner tags, same-day suppression, owner changes,
concurrent corrections, terminal exclusions, secret-safe wording, preflight and
circuit-breaker skips, notification delivery failure, and browser preview/apply.
Existing ONE cooldown/pagination and MSC multi-container regression tests remain
part of the release gates.
