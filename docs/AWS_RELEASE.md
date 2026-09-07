# Track AWS release

Source of truth: https://github.com/mtmlx/track. All releases require the protected main branch review and CodeQL check. Build a specific commit, record its ECR digest, and deploy only that digest. Financial process scripts are outside this release.

Existing deployment target: AWS account 525753067477, us-east-2; ECS cluster track-trace-prod and its five carrier schedules. Preserve the working ECS infrastructure. Lightsail access is not a prerequisite for cloud code development or releases to the existing ECS workers. Do not create or migrate an API service as part of this workflow. The local launch-agent file points to an old checkout and is not loaded as of 2026-09-07; never reinstall it for cloud production.

1. Merge the reviewed Track pull request after required checks and independent approval.
2. Build the exact commit with track-trace-image-build, run the test suite inside its Docker image, and inspect ECR and Python dependency vulnerability results.
3. Save each current schedule/task-definition ARN, image digest, enabled state, expression and network settings as the rollback record.
4. Run an isolated ECS task with the candidate digest and explicit read-only command. Use a transport guard rejecting non-GET ClickUp requests, native status updates disabled, no event projection, and no schedule registration. Read only a small existing shipment inventory. No financial commands.
5. Verify pilot exit code and logs, API authentication configuration, production IAM and secrets bindings. API document routes require SHIPMENT_API_TRIGGER_TOKEN; do not copy the example blank token into production.
6. After successful review and pilot, register new task revisions preserving existing scope, secret references and resource settings. Switch one carrier schedule first. Verify the first run before changing others. Never enable a second scheduler for a carrier already owned by ECS.
7. On failure, restore the saved TaskDefinitionArn on the affected schedule. Preserve original expressions/network settings. Do not rerun write operations blindly.

CLICKUP_USE_TASK_STATUS=false in the example prevents native task-status changes only; it is not a global dry-run. A dry-run or guarded read-only pilot is required to prevent all operational writes.

## Automatic GitHub releases

See [CLOUD_DEVELOPMENT.md](CLOUD_DEVELOPMENT.md). Once merged, `.github/workflows/deploy-track.yml` performs this build, validation and schedule-target promotion after approved changes reach main. It always overrides the existing CodeBuild source with the exact GitHub commit archive and preserves rollback evidence. Manual intervention is needed only for failed gates or recovery. Do not run a second manual deployment concurrently with the workflow.
