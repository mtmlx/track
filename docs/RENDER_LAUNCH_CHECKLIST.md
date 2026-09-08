# Render Launch Checklist

This is the exact connection map for the Track-and-Trace production launch.

## 1. GitHub

Repository:
- `https://github.com/mtmlx/track`

What should live there:
- application code
- [`render.yaml`](render.yaml)
- [`Dockerfile`](Dockerfile)
- scripts and docs

## 2. Render services

Blueprint file:
- [`render.yaml`](render.yaml)

Services created from the Blueprint:
- `shipment-api`
- `track-trace-cron`

Why two services:
- `shipment-api` receives health checks, manual triggers, and ClickUp webhooks
- `track-trace-cron` runs the scheduled reconciliation every 2 hours

## 3. shipment-api env vars

Set these in Render on `shipment-api`.

Required ClickUp:
- `CLICKUP_API_TOKEN` or `CLICKUP_OAUTH_ACCESS_TOKEN`
- `CLICKUP_LIST_ID`
- `CLICKUP_CF_CONTAINER_NO`
- `CLICKUP_CF_BOOKING_NO`
- `CLICKUP_CF_SHIPPING_LINE`

Recommended ClickUp:
- `CLICKUP_CF_SHIPMENT_STATUS`
- `CLICKUP_CF_STATUS_LAST_CHECKED`
- `CLICKUP_LIST_IDS` if you want more than one list
- `CLICKUP_SPACE_IDS` if discovery should be limited to selected spaces

Required API/security:
- `SHIPMENT_API_TRIGGER_TOKEN`

Recommended shipment behavior:
- `SHIPMENT_PREFLIGHT_ENABLED=true`
- `SHIPMENT_MIN_SYNC_INTERVAL_HOURS=2`
- `CLICKUP_DISCOVER_LISTS_FROM_SPACES=true`

MSC day-one settings:
- `MSC_USE_PLAYWRIGHT=true`
- `MSC_PLAYWRIGHT_REQUIRED=true`
- `MSC_PLAYWRIGHT_BROWSER=chromium`
- `MSC_PLAYWRIGHT_CHANNEL=` (use the pinned Playwright Chromium)
- `MSC_PLAYWRIGHT_HEADLESS=true`
- `MSC_PLAYWRIGHT_CHALLENGE_TIMEOUT_SECONDS=20`
- `MSC_PLAYWRIGHT_CHALLENGE_RELOAD_ATTEMPTS=1`
- `MSC_PLAYWRIGHT_TRACKING_URL=https://www.msc.com/en/track-a-shipment`
- `MSC_PLAYWRIGHT_API_ENDPOINT=https://www.msc.com/api/feature/tools/TrackingInfo`

Hapag-Lloyd browser settings:
- `HAPAG_USE_PLAYWRIGHT=true`
- `HAPAG_PLAYWRIGHT_REQUIRED=false`
- `HAPAG_PLAYWRIGHT_BROWSER=chromium`
- `HAPAG_PLAYWRIGHT_CHANNEL=` (use the pinned Playwright Chromium)
- `HAPAG_PLAYWRIGHT_HEADLESS=true`
- `HAPAG_PLAYWRIGHT_VIEW=S8510`
- `HAPAG_PLAYWRIGHT_CHALLENGE_TIMEOUT_SECONDS=45`
- `HAPAG_PLAYWRIGHT_CHALLENGE_RELOAD_ATTEMPTS=2`
- `HAPAG_PLAYWRIGHT_SESSION_REUSE=true`
- `HAPAG_PLAYWRIGHT_WARMUP_SECONDS=6`

Wan Hai browser settings:
- `WAN_HAI_USE_PLAYWRIGHT=true`
- `WAN_HAI_PLAYWRIGHT_REQUIRED=false`
- `WAN_HAI_PLAYWRIGHT_BROWSER=chromium`
- `WAN_HAI_PLAYWRIGHT_CHANNEL=` (use the pinned Playwright Chromium)
- `WAN_HAI_PLAYWRIGHT_HEADLESS=true`

Other carrier settings:
- copy only the carrier env vars you actually use in production
- examples: `ONE_*`, `MAERSK_*`, `HAPAG_*`, `CMA_CGM_*`, `COSCO_*`

## 4. track-trace-cron env vars

Set the same shipment-sync env vars on `track-trace-cron`.

At minimum, copy:
- ClickUp auth and field IDs
- shipment behavior vars
- all carrier vars used in production
- MSC Playwright vars

Optional on cron:
- `SHIPMENT_API_TRIGGER_TOKEN`

## 5. ClickUp connection

Immediate trigger URL:
- `POST https://<shipment-api-domain>/webhooks/clickup/track-trace` with `Authorization: Bearer <SHIPMENT_API_TRIGGER_TOKEN>`

Manual trigger URL:
- `POST https://<shipment-api-domain>/track-trace/run` with `Authorization: Bearer <SHIPMENT_API_TRIGGER_TOKEN>`

Protected trigger alternatives:
- `Authorization: Bearer <SHIPMENT_API_TRIGGER_TOKEN>`
- `X-Trigger-Token: <SHIPMENT_API_TRIGGER_TOKEN>`

## 6. What you need to provide for go-live

From GitHub:
- repo exists and is accessible from Render

From Render:
- workspace created
- Blueprint synced from the GitHub repo
- email or Slack notifications configured

From ClickUp:
- production list ID
- production custom field IDs for:
  - container number
  - booking number
  - shipping line
  - shipment status if used
  - last checked if used
- automation webhook created if you want immediate runs

From operations:
- the list of carriers that are in scope on day one
- who receives failure alerts

## 7. First production test

1. Open `/health`
2. Open `/track-trace/requirements`
3. Trigger `/track-trace/run`
4. Confirm shipments update in ClickUp
5. Confirm the cron job runs on schedule
6. Add the ClickUp automation webhook after the manual test passes

## 8. Monitoring and maintenance

Minimum monitoring:
- Render health check on `/health`
- Render notifications for deploy failures and cron failures
- review service logs when a sync fails

Ongoing maintenance:
- react to alerts
- review failed carrier runs weekly
- recheck MSC browser behavior when MSC changes its site
- recheck Hapag browser behavior when Hapag changes its bot protection or page structure
- rotate tokens when required
