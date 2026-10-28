# CloudWatch Logs IAM compatibility through April 26, 2027

Account: `525753067477`. The reviewed policy documents under
`deploy/aws-iam/cloudwatch-compatibility` retain every pre-existing statement and
add group ARNs for the existing group actions. These are reviewed baselines for
verification, not instructions to overwrite unrelated future permissions.

## Ownership and deployment gates

`sso-policy.json` belongs to Identity Center permission set
`MTMInvoiceOperatorTamara`, instance `ssoins-7223aa5b898b3761`, permission set
`ps-72239aac459db18d`, region `us-east-1`. Update the permission-set source,
provision it to this account, wait for SUCCEEDED, and check the generated role.
Do not edit the reserved role directly.

`cfo-policy.json` belongs to inline policy `MTMLxCFORecoveryRuntime` on role
`MTMLx_CFO_Recovery`. No existing checked-in generator was established. Any new
generator must preserve this baseline and validate its proposed output.

Before either policy write:

```sh
python scripts/check_cloudwatch_iam.py --candidate cfo --policy proposed-cfo.json
python scripts/check_cloudwatch_iam.py --candidate sso --policy proposed-sso.json
```

Before and after IAM changes, using an authorized operator identity:

```sh
python scripts/check_cloudwatch_iam.py --live --identity-center --evidence cloudwatch-iam.json
```

The checker is read-only and fails if an original or compatibility statement is
missing/changed, a new Deny is introduced, the account is wrong, or a read fails.
An intentional permission redesign requires separate review and baseline changes.
It is a structural preservation check, not a complete IAM/SCP/session simulation.

Track CI tests the canonical documents. The release entrypoint checks both live
role policies before any build, task revision, or schedule change and saves
`release-evidence/cloudwatch-iam.json`. It needs only `iam:GetRolePolicy` on the
two exact roles; `deploy/aws-ecs/release/iam-policy.json` includes that grant.
The release role has no policy-writing or Identity Center administration grant.
The operator's explicit `--identity-center` check also verifies the source.

These controls become active for GitHub deployments after this change is reviewed
and merged. They do not intercept independent tools or another repository's IAM
writes. Apply this same pre/post gate to any such automation before allowing it
to manage these policies. The AWS-managed ECS issue is tracked separately and
does not block existing scheduled work or this guard.

## Preserve features and validate

Keep existing stream ARNs for CreateLogStream, PutLogEvents, GetLogEvents, and
DeleteLogStream. Keep group ARNs for group actions. Never enable invoice log
streaming or alter schedules, task commands, images, secrets, alarms, ECS Exec,
or monitoring features solely to satisfy this migration. Live reads of invoice
logs remain unverified because streaming is currently disabled and no matching
groups were found. Existing GetLogEvents statements are retained; synthetic IAM
simulation did not establish live stream access.

The five Track carriers run from enabled EventBridge Scheduler schedules. An
empty ECS services/tasks list between runs does not establish an unused workload.
Their task logs use `/ecs/track-trace/<carrier>`, whereas the AWS-owned ECS
statement under review uses `/aws/ecs/*`; do not conflate the two paths.

## AWS-owned unresolved dependency

`AmazonECSServiceRolePolicy` v24 has DescribeLogStreams on
`arn:aws:logs:*:*:log-group:/aws/ecs/*:log-stream:*` in CWLogStreamManagement.
AWS does not allow customers to edit AWSServiceRoleForECS. Close this item only
after AWS confirms non-impact or publishes a compatible policy, with evidence
for preserved logging/monitoring and the next normal scheduled worker runs.
Do not delete/recreate the role, remove logging, or move workloads as a workaround.

Sources:
- https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/iam-identity-based-access-control-cwl.html
- https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using-service-linked-roles-for-clusters.html

## Recovery

The guard never changes IAM. If it fails, leave active workloads running and
review the differing source policy. Restore only the lost reviewed statements
with current-policy concurrency checks and authoritative readback. Preserve
unrelated changes. A rollback of the application release must not roll back the
CloudWatch compatibility additions. Historical before/after evidence is retained
in the operator workspace under outputs/cloudwatch-iam-20261027.
