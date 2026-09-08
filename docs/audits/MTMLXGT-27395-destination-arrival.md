# MTMLXGT-27395: destination arrival fix and separate repair preview

## Scope and evidence

Reviewed on 2026-09-08, against main `eb54ae8fe3dc408b1aab9456cb455cfdd5fb3d8a`.
Task: https://app.clickup.com/t/86e207qur (MTMLXGT-27395).
Booking: 177WJVJVJ400165T. Containers: MSMU8716298, MSCU5291332,
MSNU7949391, MSDU7230448.

The read-only ClickUp read returned Port Of Discharge `PUERTO CORTES`
(field `303d68fa-f250-43ac-abe8-17b70e07b46d`, option index 47).
The four saved MSC browser captures from September 8 explicitly identify
`Port of Discharge: Puerto Cortes, HN` and `Transhipment: Colon, PA`.
Source: https://www.msc.com/en/track-a-shipment, queried by each container.
The operator capture states results were provided on 08.09.2026 at 16:00
Central Europe Standard Time. These are saved carrier observations, not a new
live carrier request made during this code change.

Each capture includes:

| Date | Location | Event |
| --- | --- | --- |
| 2026-09-11 | Puerto Cortes, HN | Estimated Time of Arrival |
| 2026-09-04 | Colon, PA | Full Transshipment Loaded |
| 2026-09-01 | Colon, PA | Full Transshipment Discharged |
| 2026-07-19 | Shanghai, CN | Export Discharged from Barge |

No actual Puerto Cortes discharge is present. September 11 is an estimate,
not a substitute for an actual discharge date.

Task comments record automatic arrival transitions at
2026-09-04T12:17:40Z (comment 90170248708732) and
2026-09-07T18:12:04Z (comment 90170249321074).
Comment 90170249321080 contains the subsequent customer arrival announcement.
The exact original write request that populated the discharge date has not
been recovered; the persisted date matches the transshipment event, and the
code defect explains how it can drive repeated arrival decisions.

## Root cause and fix

`msc_browser_assisted.status_from_browser_capture` discarded the explicit POD.
`clickup_client._find_destination_discharge_index` classified discharge by
event order and later movement presence, without comparing destination ports.
`_build_direct_event_field_updates` projected that result into Discharge date.
`_derive_operational_status_step` then treated any persisted discharge date as
arrival evidence, even if the latest tracking had no destination discharge.

MSC ingestion now retains explicit POD and opts into destination validation.
The planner cross-checks the configured ClickUp POD when present. An actual,
dated, nonfuture discharge must match the destination to project discharge or
promote arrival. Unknown or conflicting evidence cannot fall back to the old
event-order heuristic or stored arrival status for MSC rail classification.
Historical status corrections are not made automatically.

Port matching normalizes accents/case/punctuation, explicitly maps Puerto
Cortes/HNPCR and Colon/PACON, and otherwise requires an exact country-qualified
location. It does not fuzzy-match cities or infer unrecognized codes. New
aliases require evidence and tests. Other carriers retain their existing
classification behavior; this is not a global port registry migration.
MSC API POD key extraction is conservative; unknown response shapes remain
unconfirmed, and no live MSC API entitlement or payload schema was verified.

## Repair preview: no writes performed

The latest task read is already `por arribar`; do not reverse that correction.

| Field | Current verified value | Proposed action |
| --- | --- | --- |
| Task status | por arribar | Preserve |
| Discharge date (`605b0679-9be8-4c92-a817-7474f1712881`) | `1788246000000` = 2026-09-01T07:00:00Z | Clear only this unsupported date after approval |
| ETA (`736ddd1d-33da-4ff8-a128-f7f3f738987d`) | `1789110000000` = 2026-09-11T07:00:00Z | Preserve |
| Vessel, containers, gates, schedule ETA and all other fields | Outside this repair | Preserve |

An offline planner preview using all four saved captures, the current status,
POD and the affected date values returned no status write and no discharge
write. Repeating with the proposed cleared discharge also returned no status
write and no discharge write. This is a narrowly scoped offline preview, not
a production-configured run or an applied repair.

After explicit merge approval:
1. Verify the GitHub deployment run for the merge SHA and its release evidence.
2. Read ECS task-definition image digests and schedule targets; reconcile them
   with that release's image digest. Do not start a broad shipment run.
3. Read the task again and obtain current carrier evidence for all containers.
   Stop if identity, POD, status, date or destination-arrival evidence changed.
4. Run a read-only production-configured preview for this task. Present the
   exact single-field repair again for explicit approval.
5. Only after that approval, clear the named discharge custom field. Do not
   guess a replacement date or reset status, ETA, containers, gates or vessel.
6. Re-read the field, verify preservation of unrelated fields, and run another
   read-only preview to confirm the bad discharge/arrival does not recur.

## Validation and deployment review

`PYTHONPATH=src /opt/anaconda3/bin/python -m pytest -q --tb=short`:
321 tests passed. Includes transshipment versus final discharge, aliases,
missing/ambiguous ports, estimates/future dates, conflicting shipment POD,
historical stored dates/status and repeated runs after a proposed repair.
`git diff --check` passed.

Existing `.github/workflows/deploy-track.yml` validates PRs and deploys main
through `deploy/aws-ecs/release/release.py`, using the existing OIDC role.
The release script builds the commit, checks image scanning, uses an immutable
ECR digest, runs a read-only pilot, promotes existing schedule targets and
verifies readback with rollback handling. This PR adds regression coverage to
the validation job only; no infrastructure is recreated. After merge, this
existing pipeline targets all five carrier schedules, although the behavioral
change is MSC-scoped. No merge, AWS mutation, Lightsail change, finance change
or ClickUp write was performed for this fix.
