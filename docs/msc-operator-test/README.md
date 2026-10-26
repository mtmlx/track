# MSC browser handoff: one-shipment test

## What is ready

This kit lets a second operator with a browser-enabled Codex instance capture
one MSC shipment and create an offline-validated batch for the existing AWS
importer. It does not host a browser, provide browser tools, solve MSC blocking,
grant AWS permissions, or automatically update ClickUp. Repository access
alone does not transfer another operator's browser sessions or credentials.

The first test ends with a reviewed **read-only AWS preview**. Applying changes
is a separately authorized step, not part of this kit. No new AWS resources or
permissions are required for the offline test. An existing authorized maintainer
performs the AWS side; the new operator does not need production secrets.

## 1. Set up the operator's checkout

Use the reviewed `mtmlx/track` branch/commit containing this kit. From its root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install . pytest
.venv/bin/python -m pytest tests/test_msc_operator_pack.py -q
```

Python 3.11+ is required. Do not copy `.env`, SSH keys, browser profiles, AWS
keys or ClickUp tokens from another computer. Confirm this Codex instance has
working browser/computer-use tools and can open MSC before collecting data.

## 2. Prepare the approved manifest

An authorized reviewer reads the live ClickUp task and supplies a manifest
based on `manifest.example.json`, outside the checkout. Confirm the internal
task ID, custom ID/name, booking, list, MSC carrier, eligibility, destination
and complete current container list. Include `master_bl_no` when the task has
a recorded Master B/L. The live reader uses `CLICKUP_CF_MASTER_BL`, defaulting
to the existing production Master B/L field. Terminal/excluded tasks must not enter
this test. Stop if container count or identity is disputed. Do not reuse an
old shipment example without a new task review.

Record `task_reviewed_at` as an ISO timestamp with timezone. The operator fills
`operator` and `captured_at` after capture. Both timestamps must be within six
hours, and capture must follow review. These are operator assertions, not proof
of carrier authenticity. The reviewer must still verify freshness before import.

Use a private directory such as `$HOME/msc-test`; protect it with mode 700.
Create its `captures` subdirectory. Keep all live data outside Git.

## 3. Capture every container

MSC tasks tagged `legal case` or `legal-case-do-not-update` are legal holds. Do not import,
stamp, change status, modify tracking fields or post automated failure comments
for them. The marker is an explicit owner instruction, not a terminal workflow
status. It must not be removed automatically when carrier evidence changes.
For the owner's 2025-loading review, protect the whole task if any recorded
container has an actual 2025 vessel/barge load without verified subsequent
delivery evidence. Keep the evidence and uncertainty in the private report;
add only the authorized legal marker/initial explanatory note, not an operational
status change. Resume only after explicit owner authorization.

1. Open https://www.msc.com/en/track-a-shipment.
2. Query the booking first. If it omits a unit, query the exact recorded Master
   B/L as well as each manifest container separately. Container-only results
   are diagnostics, not proof of booking/B/L ownership.
3. Verify every container, the original booking/B/L header, ocean Port of
   Discharge and separate inland delivery. Stop on contradictory identities.
4. Expand available movements and scroll through the entire result.
5. Save the complete visible text, including header, POD, ETA, all event dates,
   locations, descriptions, vessel/voyage and carrier result timestamp, as
   `captures/CONTAINERNUMBER.txt` in UTF-8. Take screenshots as optional evidence.
6. If blocked or incomplete, stop the test and report which container failed.
   Do not solve by deleting that container from the manifest or importing a
   partial batch. Do not manufacture text from assumptions or prior runs.

A B/L-only fallback requires the task's exact recorded Master B/L, known complete
container list/quantity, consistent ocean POD/delivery, and every unit's history.
A conflicting booking header still rejects the result even if the B/L matches.
For multi-container pages, preserve the original header and timestamp while
isolating each contiguous container history. Never combine identity headers
from one query with movement histories from another.

When MSC displays no B/L number, a booking-anchored fallback may retain the
recorded Master B/L without independently validating it. The original result
must show the exact booking, explicitly one B/L allocation and a container
quantity equal to the task's known quantity. Every recorded container must be
captured exactly once, with no extra units, mixed B/L evidence or conflicting
identity. The authoritative task POD must match the ocean discharge port;
an inland delivery match alone does not qualify. A visible conflicting B/L
still blocks import. ClickUp comments flag this limited validation and request
the assignee's document-level confirmation. Repeated fallback-only notes are
deduplicated against recent task comments; operational changes remain logged.

The existing parser consumes text, not image-only input. Screenshots may help
a reviewer but are not a replacement for the complete capture. Never include
cookies, login pages, passwords or unrelated browser content in the handoff.

## 4. Package offline

From the checkout, with the approved manifest at `$HOME/msc-test/manifest.json`:

```sh
.venv/bin/python -m shipment_sync.msc_operator_pack \
  --manifest "$HOME/msc-test/manifest.json" \
  --captures-dir "$HOME/msc-test/captures" \
  --output-dir "$HOME/msc-test/handoff"
```

Use a new output directory for every attempt. The command checks identity,
complete container coverage, POD agreement, parseable events and the existing
cross-container ETA/vessel consistency rules. It writes private `batch.json`
and `report.json` files with capture and batch hashes. It makes no network
requests, reads no runtime secrets and cannot apply changes.

Send the manifest, batch, report and any screenshots through the approved
private company channel. Do not attach live captures to a GitHub issue/PR or
publish an S3 object. SHA-256 detects changed bytes; it is not authentication.

## 5. Authorized maintainer: AWS preview only

Re-read the live task, verify the manifest and batch hashes, and compare all
container captures to the approved identity, current eligibility and POD.
Stop if the task changed or evidence is stale. Keep production credentials in
the existing AWS secret configuration, not on the new operator's machine.

Use the existing private batch transport and the pinned MSC task image with
the command below in the established controlled ECS import procedure:

```text
msc-browser-assisted --import-batch-url <short-lived-private-batch-url>
```

Do **not** append `--apply`. Do not put the URL into a public ticket, repository
or shared log. This kit intentionally does not grant access or provision an
ECS launcher. If the established upload/launch procedure is unavailable, stop
and request maintainer setup rather than distributing AWS administrator keys.

Important: `--task-id` does not narrow `--import-batch` mode. The batch itself
must contain only the one approved internal task ID. Importer success/exit 0
alone is not acceptance: it can log a rejected shipment and continue. Inspect
the complete output for rejections and every proposed field/status change.
Preview performs live reads but must end with `No ClickUp write was made.`

## Acceptance and next step

- All approved containers captured, correct POD and no missing page sections.
- Offline validation succeeds and reviewer verifies the private batch hash.
- AWS preview resolves only the approved eligible MSC task, with no rejection.
- Proposed dates, vessel and status match the carrier evidence and agreed rules.
- ClickUp is unchanged by the test; reviewer records commit/image digest and
  preview output, including any failure. No success claim based only on upload.

Only after explicit approval may the maintainer perform an import, re-read
the affected ClickUp fields and run another preview for recurrence. Historical
data repairs, including MTMLXGT-27395, retain their separate approval gate.

MSC near-arrival catch-up covers 1-10 days before ocean ETA after qualifying
actual vessel loading/departure. Forecasts, truck/rail movement and ETA alone
do not prove transit or arrival. Ocean arrival remains separate from discharge
and inland delivery; every unit must have a validated empty return before
closing a multi-container shipment. Terminal tasks remain excluded.

Failure comments identify the reference/POD/allocation problem and required
correction, mention current assignees, and deduplicate the same issue for 24
hours. Raw exceptions/credentials stay out of comments. A changed Master B/L
invalidates the old review. Successful T&T stamping occurs only after field,
status and audit-comment writes succeed; an uncertain write requires readback,
not automatic replay.
