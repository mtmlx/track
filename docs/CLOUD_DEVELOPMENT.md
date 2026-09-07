# Track: cloud fixes and releases

## Development

Open https://chatgpt.com/codex/cloud and select `mtmlx/track`. For normal fixes, select `main` after the existing security PR has been merged. Until then, use `codex/precloud-security` when work needs those pending fixes; main does not yet contain them.

Give Codex a specific error, expected behavior, and a sanitized log excerpt. Ask it to reproduce the issue, add an appropriate regression test, fix the cause, and prepare a pull request targeting `mtmlx/track`. Keep financial process code outside this task. Do not provide production AWS, ClickUp, or carrier credentials to the coding environment. Dependency setup must follow the repository lockfiles; use mocked integrations for cloud tests. The production image remains the authoritative runtime validation environment.

Suggested task prompt:

> In mtmlx/track, investigate the following error: [sanitized error]. Reproduce it with a focused test, implement the smallest reliable fix, and run the relevant tests. Prepare a pull request explaining the problem, change, and validation. Do not call production integrations or change AWS resources.

## Review

Review the diff and checks. An independent approving review is required by protected main. Codex's own review is useful but does not replace that approval. Merge only after required checks and approval pass.

## Release to the existing ECS deployment

The verified current setup uses CodeBuild `track-trace-image-build` in `us-east-2`, ECR `track-trace`, and ECS `track-trace-prod`. CodeBuild's configured source is an older S3 archive. GitHub currently has CodeQL and dependency-graph workflows, but no deployment workflow. A GitHub merge alone does not change AWS.

Use an authorized release session with AWS access to:

1. Fetch the exact merged GitHub commit and export that commit as a clean source archive, excluding local secrets and financial files. Record its full SHA.
2. Upload the archive to a unique key under the existing build-artifact bucket and explicitly override CodeBuild's source location for this run. Do not reuse its default source archive.
3. Build and test the production container; audit dependencies and scan the resulting image. Record the commit, CodeBuild run, and immutable ECR digest together.
4. Follow [AWS_RELEASE.md](AWS_RELEASE.md): read-only pilot, rollback capture, and controlled updates to the existing ECS task revisions/schedule targets. Verify the result before proceeding to another carrier.

This uses the existing AWS infrastructure; it neither migrates services nor returns to Lightsail. Deployment is manual and controlled today. A future GitHub Actions deployment would need narrowly scoped AWS authentication (prefer OIDC) and explicit release controls; that integration is not configured by this document.
