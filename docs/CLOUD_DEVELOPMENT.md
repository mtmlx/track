# Track: cloud fixes and releases

## Development

Open https://chatgpt.com/codex/cloud and select `mtmlx/track`. For normal fixes, select `main` after the existing security PR has been merged. Until then, use `codex/precloud-security` when work needs those pending fixes; main does not yet contain them.

Give Codex a specific error, expected behavior, and a sanitized log excerpt. Ask it to reproduce the issue, add an appropriate regression test, fix the cause, and prepare a pull request targeting `mtmlx/track`. Keep financial process code outside this task. Do not provide production AWS, ClickUp, or carrier credentials to the coding environment. Dependency setup must follow the repository lockfiles; use mocked integrations for cloud tests. The production image remains the authoritative runtime validation environment.

Suggested task prompt:

> In mtmlx/track, investigate the following error: [sanitized error]. Reproduce it with a focused test, implement the smallest reliable fix, and run the relevant tests. Prepare a pull request explaining the problem, change, and validation. Do not call production integrations or change AWS resources.

## Review

Review the diff and checks. An independent approving review is required by protected main. Codex's own review is useful but does not replace that approval. Merge only after required checks and approval pass.

## Release to the existing ECS deployment

The GitHub workflow `.github/workflows/deploy-track.yml` automatically releases pushes to protected `main`, including approved PR merges. It can also be manually rerun from main. Pull requests run the deployment recovery tests but cannot obtain AWS deployment credentials.

GitHub authenticates using short-lived OIDC credentials. The AWS trust policy requires repository ID `1180328188` and `refs/heads/main`; there are no stored AWS access keys in GitHub. The workflow uses the existing CodeBuild project, artifact bucket, ECR repository, ECS cluster and five worker schedules. It creates no ECS service and does not touch Lightsail.

The pipeline exports the exact commit, overrides CodeBuild's historical S3 source, runs the container test suite and Chromium smoke test, audits Python dependencies, and blocks on failed/incomplete image scans or critical/high findings. It then runs a read-only Maersk pilot. No eligible candidates, a timeout, or an unsuccessful pilot blocks deployment. Medium findings are retained in the evidence for review.

After these gates pass, the workflow snapshots all schedule settings, registers image-digest-pinned task revisions, updates the existing schedules, and verifies their settings. It attempts rollback on update failure without overwriting concurrent operator changes. Evidence is saved under `github-releases/<commit>-<run>-<attempt>/` in the existing S3 artifact bucket and as a GitHub Actions artifact. A failed rollback requires an operator to inspect `rollback-result.json` and restore the saved configuration.

Success means the schedule targets were verified. Workers run at their existing times; a release does not immediately execute production writes or verify every carrier's eventual runtime result. Do not confuse this with an API deployment: no ECS API service was present in the verified cluster.

Activation requires merging the workflow through the protected PR. The first main run must verify actual GitHub OIDC authentication and the complete AWS pipeline; those cannot be exercised from an untrusted review branch. Main protection and independent approval remain unchanged.
