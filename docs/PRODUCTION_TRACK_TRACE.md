# Track-and-Trace Production Runbook

This is the fastest production path for the current project.

Companion checklist:
- See [`docs/RENDER_LAUNCH_CHECKLIST.md`](docs/RENDER_LAUNCH_CHECKLIST.md) for the exact service-by-service connection map.

Phase 1 scope:
- Deploy Track-and-Trace first
- Use Render Docker web service for API/manual/webhook trigger
- Use Render Docker cron job for scheduled reconciliation
- Keep ONE booking in staging until ONE provides the protected booking endpoints and auth
- Support MSC from day one with Playwright in the container image

## What will run in production

1. `shipment-api`
- Render web service
- Public base URL
- Used for health checks and ClickUp-triggered sync runs

2. `track-trace-cron`
- Render cron job
- Runs `./scripts/run_sync.sh` every 2 hours by default

3. Shared Docker image
- Built from [`Dockerfile`](Dockerfile)
- Includes Playwright and browser dependencies for MSC

## What to connect where

### 1. GitHub -> Render

Connect the private GitHub repository to Render and deploy the Blueprint:
- File: `render.yaml`
- Repo: `https://github.com/mtmlx/track`

Render will create:
- `shipment-api`
- `track-trace-cron`

### 2. Render Web Service -> Environment Variables

Set these on `shipment-api`:

Required ClickUp config:
- `CLICKUP_API_TOKEN` or `CLICKUP_OAUTH_ACCESS_TOKEN`
- `CLICKUP_LIST_ID`
- `CLICKUP_CF_CONTAINER_NO`
- `CLICKUP_CF_BOOKING_NO`
- `CLICKUP_CF_SHIPPING_LINE`

Recommended ClickUp config:
- `CLICKUP_CF_SHIPMENT_STATUS`
- `CLICKUP_CF_STATUS_LAST_CHECKED`

Recommended API/security config:
- `SHIPMENT_API_TRIGGER_TOKEN`

Carrier config:
- whichever carrier credentials or URLs you already use locally
- copy only the carriers you actually need in production

MSC day-one requirement:
- keep `MSC_USE_PLAYWRIGHT=true`
- keep `MSC_PLAYWRIGHT_REQUIRED=true`
- use `MSC_PLAYWRIGHT_BROWSER=chromium`
- leave `MSC_PLAYWRIGHT_CHANNEL` blank to use the pinned Playwright Chromium
- keep `MSC_PLAYWRIGHT_HEADLESS=true`
- start with `MSC_PLAYWRIGHT_CHALLENGE_TIMEOUT_SECONDS=20`
- start with `MSC_PLAYWRIGHT_CHALLENGE_RELOAD_ATTEMPTS=1`

Hapag-Lloyd browser mode:
- keep `HAPAG_USE_PLAYWRIGHT=true`
- keep `HAPAG_PLAYWRIGHT_REQUIRED=false`
- use `HAPAG_PLAYWRIGHT_BROWSER=chromium`
- leave `HAPAG_PLAYWRIGHT_CHANNEL` blank to use the pinned Playwright Chromium
- keep `HAPAG_PLAYWRIGHT_HEADLESS=true`
- keep `HAPAG_PLAYWRIGHT_VIEW=S8510`
- start with `HAPAG_PLAYWRIGHT_CHALLENGE_TIMEOUT_SECONDS=45`
- start with `HAPAG_PLAYWRIGHT_CHALLENGE_RELOAD_ATTEMPTS=2`
- keep `HAPAG_PLAYWRIGHT_SESSION_REUSE=true`
- start with `HAPAG_PLAYWRIGHT_WARMUP_SECONDS=6`

Wan Hai browser mode:
- keep `WAN_HAI_USE_PLAYWRIGHT=true`
- keep `WAN_HAI_PLAYWRIGHT_REQUIRED=false`
- use `WAN_HAI_PLAYWRIGHT_BROWSER=chromium`
- leave `WAN_HAI_PLAYWRIGHT_CHANNEL` blank to use the pinned Playwright Chromium
- keep `WAN_HAI_PLAYWRIGHT_HEADLESS=true`

Important:
- Copy the same shipment-sync env vars to `track-trace-cron`
- The cron job needs the same ClickUp and carrier env as the API

### 3. ClickUp -> Render Webhook URL

For an immediate trigger from ClickUp Automation Webhooks, call:

`POST https://<your-render-domain>/webhooks/clickup/track-trace`

You can also trigger manually with:

`POST https://<your-render-domain>/track-trace/run`

The protected routes also accept:
- `Authorization: Bearer <SHIPMENT_API_TRIGGER_TOKEN>`
- `X-Trigger-Token: <SHIPMENT_API_TRIGGER_TOKEN>`

### 4. Render -> Health Check

Use:
- `/health`

Optional operational checks:
- `/track-trace/requirements`
- `/one/requirements`

## Fastest production checklist

1. Push the current repo to a private GitHub repository.
2. Create a Render account/workspace.
3. Create services from `render.yaml`.
4. Set the required env vars on both:
- `shipment-api`
- `track-trace-cron`
5. Open:
- `https://<your-render-domain>/health`
- `https://<your-render-domain>/track-trace/requirements`
6. Trigger a manual test:
- `POST /track-trace/run` with `Authorization: Bearer <SHIPMENT_API_TRIGGER_TOKEN>`
7. Confirm updates land back in ClickUp.
8. Add the ClickUp Automation Webhook URL.
9. Leave cron enabled as the safety net.

## Repository target

Production repo target:
- `https://github.com/mtmlx/track`

If this local repo has not been connected to that remote yet, add it locally with:

```bash
git remote add origin https://github.com/mtmlx/track
```

If `origin` already exists, update it with:

```bash
git remote set-url origin https://github.com/mtmlx/track
```

## Current blockers before full rollout

### ONE booking

Still missing from ONE:
- booking request endpoint
- booking confirmation/status endpoint
- auth method and required headers

### MSC

MSC is included in phase 1 through the Docker-based deployment.

Operational note:
- if MSC starts failing in Render, use the Render shell and service logs first
- keep an eye on browser-related timeouts and upstream bot protection changes

## Monitoring

Minimum:
- Render health check on `/health`
- Render email/Slack notifications for failed deploys and failed cron runs
- Review Render logs when a sync fails

Recommended:
- check `/track-trace/requirements` after every env change
- monitor that cron is still running every 2 hours
- watch ClickUp for stale shipments that were not updated
- review MSC-specific failures separately because browser-driven failures tend to differ from normal API failures
- review Hapag browser-mode failures separately because Hapag may intermittently present a security-check page

## Maintenance

Daily:
- only react to alerts

Weekly:
- review failed sync runs
- review carrier-specific failures

Monthly:
- rotate tokens if required
- confirm ClickUp automation webhook still points to the correct Render URL
- review whether additional carriers need credentials or endpoint updates

## What I still need from you to fully go live

- Render workspace access or confirmation you will create the services yourself
- The exact ClickUp lists/spaces that production should scan
- Which carriers are truly in production scope right now
- Slack or email destination for alerts
