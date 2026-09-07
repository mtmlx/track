# Shipment to ClickUp Sync (Starter)

This project reads shipment references from ClickUp tasks, checks carrier tracking status, and writes status updates back to ClickUp.

## Workspace layout
- `src/shipment_sync/` Python application code
- `scripts/` operational wrappers and installers
- `apps/marketing-site/` static website project
- `config/examples/` sample JSON templates
- `config/local/` local working JSON configs
- `artifacts/output/` generated reports, exports, and review queues
- `artifacts/build/` scratch build outputs

Compatibility links are kept at the root for `build/`, `output/`, and `website/` so older commands still resolve.

Release history:
- [`WHATS_NEW.md`](WHATS_NEW.md)

## What it does
- Reads tasks from a ClickUp list
- Extracts shipping line + booking/container custom fields
- Calls a shipping-line adapter to fetch the latest status
- Updates a status field and adds a task comment in ClickUp
- Exposes the same preview and sync flow through an HTTP API

## Setup
1. Create and activate a Python 3.11+ virtual environment.
2. Install dependencies:
   ```bash
   pip install -e .
   ```
   For MSC real-browser tracking mode:
   ```bash
   pip install -e ".[browser]"
   playwright install chrome
   ```
3. Copy `.env.example` to `.env` and fill values.

## ClickUp auth modes
The shipment sync supports either ClickUp auth mode:
- Personal token: `CLICKUP_API_TOKEN`
- OAuth app bearer token: `CLICKUP_OAUTH_ACCESS_TOKEN`

OAuth is the better operational choice for this project because the shipment sync can use a dedicated app token instead of a manually rotated personal token.

Minimum OAuth app env:
- `CLICKUP_OAUTH_CLIENT_ID`
- `CLICKUP_OAUTH_CLIENT_SECRET`
- `CLICKUP_OAUTH_REDIRECT_URI` (default: `http://localhost:8080/callback`)

Authorize the ClickUp app and save the bearer token into `.env`:
```bash
clickup-oauth
```

macOS double-click option:
- `Authorize ClickUp.command`

## ClickUp fields required
Create or identify custom fields in your shipment list:
- Shipping line
- Booking number
- Container number

Then put their field IDs in `.env`.

To cover all open shipments across multiple lists, you can target by:
- `CLICKUP_LIST_ID` (primary list)
- `CLICKUP_LIST_IDS` (optional comma-separated additional list IDs)
- `CLICKUP_SPACE_IDS` (optional comma-separated space IDs; auto-discovers all lists in those spaces)
- `CLICKUP_FOLDER_IDS` (optional comma-separated folder IDs; includes all lists in those folders)
- `CLICKUP_DISCOVER_LISTS_FROM_SPACES=true` to enable space discovery

Optional:
- `CLICKUP_CF_SHIPMENT_STATUS` if you want a custom field that stores carrier status text.
- `CLICKUP_CF_STATUS_LAST_CHECKED` if you want a dedicated custom field for last checked timestamp.
- `CLICKUP_CF_TRACK_TRACE_SNAPSHOT` if you want updates/comments only when carrier data changes.
- `CLICKUP_CF_ETA`, `CLICKUP_CF_ETD`, `CLICKUP_CF_DISCHARGE_DATE`
- `CLICKUP_CF_GATE_IN_FULL`, `CLICKUP_CF_GATE_OUT_EMPTY`
- `CLICKUP_CF_GATE_OUT_DELIVERY`, `CLICKUP_CF_GATE_IN_EMPTY`

Native ClickUp Task Status updates are opt-in. Keep `CLICKUP_USE_TASK_STATUS=false`
until you are ready to move workflow statuses automatically. When enabled, Track &
Trace only moves tasks forward through this sequence: `Pendiente de booking` ->
`BK confirmado` -> `Recolectado` -> `En puerto Origen` -> `Tránsito` ->
`Por arribar` -> `arribado en puerto` -> `en ruta a almacén` / `en almacén` ->
`Vacío devuelto`.

The cross-carrier operating contract, terminal conditions, source ownership,
and DCSA adoption rules are defined in
[`docs/TRACK_TRACE_OPERATIONAL_POLICY.md`](docs/TRACK_TRACE_OPERATIONAL_POLICY.md).

## DCSA shadow pilot

`dcsa-tnt-shadow` is a separate, non-projecting ingestion lane for official
carrier DCSA payloads. It reads the existing ClickUp shipment inventory, calls
only official CMA CGM or Maersk event APIs, validates and redacts payloads, and
records evidence in an explicit ledger. It never writes a ClickUp field,
status, or comment.

Run a local configuration check only:
```bash
dcsa-tnt-shadow
```

The `--run` form makes external reads and writes validated evidence to the
configured ledger; it is not a replacement for `shipment-sync` and does not
activate any normal carrier schedule. See
[`docs/DCSA_SHADOW_PILOT.md`](docs/DCSA_SHADOW_PILOT.md) for required
environment variables, production infrastructure, and the go/no-go gates.

The default status labels can be overridden with:
`CLICKUP_STATUS_PENDING_BOOKING`, `CLICKUP_STATUS_BOOKING_CONFIRMED`,
`CLICKUP_STATUS_COLLECTED`, `CLICKUP_STATUS_ORIGIN_PORT`,
`CLICKUP_STATUS_IN_TRANSIT`, `CLICKUP_STATUS_ARRIVING`,
`CLICKUP_STATUS_ARRIVED_PORT`, `CLICKUP_STATUS_EN_ROUTE_WAREHOUSE`,
`CLICKUP_STATUS_IN_WAREHOUSE`, and `CLICKUP_STATUS_EMPTY_RETURNED`.

## Run
Dry run (no updates):
```bash
shipment-sync --dry-run
```

Preview one shipment and show planned field/comment writes without updating ClickUp:
```bash
shipment-sync --preview-updates --container ABCD1234567
```

Live sync:
```bash
shipment-sync
```

## ClickUp pricing and procurement sync
This repo also includes a ClickUp-to-ClickUp pricing sync for moving approved pricing/procurement values from a quote task into a shipment task.

Default safety rules:
- Only approved pricing/procurement fields are copied.
- Shipment values are only filled when empty unless you pass `--overwrite-existing`.
- The shipment `MTM Quote #` field is filled from the source quote when available.
- Live updates require a ClickUp token with permission to edit the destination shipment custom fields.

Environment variables:
- Reuses the same ClickUp auth as the shipment sync.
- Optional for bulk mode:
  - `CLICKUP_PRICING_LIST_ID` or `CLICKUP_PRICING_LIST_IDS`
  - `CLICKUP_PRICING_MATCH_FIELD` (default: `MTM Quote #`)
  - `CLICKUP_PRICING_SHIPMENT_MATCH_FIELDS` to try alternate shipment identifiers such as `MTM Booking`
  - `CLICKUP_PRICING_QUOTE_MATCH_FIELDS` to index quote relationships such as `Shipment associated`
  - `CLICKUP_PRICING_COPY_FIELDS` to override the default allowed field names/IDs
  - `CLICKUP_PRICING_ONLY_EMPTY_TARGETS=true`
  - `CLICKUP_PRICING_SET_QUOTE_NUMBER=true`

Preview one explicit shipment/quote pair:
```bash
clickup-pricing-sync \
  --shipment "https://app.clickup.com/t/8451352/MTMLXGT-24095" \
  --quote "https://app.clickup.com/t/8451352/MTMQUOTE-3404" \
  --dry-run
```

Preview one shipment and auto-discover the quote from the pricing list relationship:
```bash
CLICKUP_PRICING_LIST_ID=901705118181 \
clickup-pricing-sync \
  --shipment "https://app.clickup.com/t/8451352/MTMLXGT-25717" \
  --dry-run
```

Apply that pair live:
```bash
clickup-pricing-sync \
  --shipment "https://app.clickup.com/t/8451352/MTMLXGT-24095" \
  --quote "https://app.clickup.com/t/8451352/MTMQUOTE-3404"
```

Bulk preview shipments that already have `MTM Quote #` populated:
```bash
clickup-pricing-sync --sync-linked-shipments --dry-run
```

To avoid posting duplicate comments on unchanged shipments:
- create a plain text ClickUp custom field
- set `CLICKUP_CF_TRACK_TRACE_SNAPSHOT` to that field ID
- optionally set `SHIPMENT_COMMENT_ON_NO_CHANGE=true` if you want a small "No change" comment instead of silence

## API
Start the local API server:
```bash
shipment-api
```

### DocuSeal document test endpoints

The same FastAPI service includes a small DocuSeal middleware test surface:

```text
GET  /api/documents/requirements
POST /api/documents/nda
POST /api/webhooks/clickup/credit-contract
POST /api/webhooks/docuseal
```

NDA issuing is standalone. Credit contract issuing is ClickUp-triggered. Both issuing endpoints default to `dry_run: true` so you can inspect the DocuSeal submission payload before sending emails. See [`docs/docuseal-document-test-plan.md`](/Users/mario/Documents/Software_Development/docs/docuseal-document-test-plan.md).

By default it listens on `http://127.0.0.1:8000`.

Available endpoints:
- `GET /health` returns API health and whether the required environment variables are configured.
- `GET /track-trace/requirements` lists the exact Track-and-Trace inputs still missing for a live deployment.
- `GET /shipments` lists candidate shipment tasks from ClickUp without updating anything.
- `POST /sync` runs the live shipment sync and returns the updated items.
- `POST /track-trace/run` runs a protected Track-and-Trace sync trigger for production/webhook usage.
- `POST /webhooks/clickup/track-trace` runs a protected ClickUp-facing Track-and-Trace webhook trigger.
- `GET /pricing/health` returns whether ClickUp pricing sync auth is configured and whether bulk pricing list IDs are set.
- `POST /pricing/sync` runs either an explicit shipment/quote pricing sync or a bulk linked-shipment pricing sync.
- `GET /ap/health` returns whether the accounts-payable ClickUp config is ready.
- `GET /ap/invoices` lists invoice tasks from the configured AP ClickUp list(s).
- `GET /one/health` returns whether the ONE booking config is ready.
- `GET /one/requirements` lists the exact ONE booking inputs still missing for a live integration.
- `POST /one/bookings/request` submits a ONE booking request payload to the configured ONE booking endpoint.
- `GET /one/bookings/confirmation` fetches ONE booking confirmation/status details for a reference.

Pricing sync API examples:
```bash
curl -X POST http://127.0.0.1:8000/pricing/sync \
  -H "Content-Type: application/json" \
  -d '{
    "shipment": "https://app.clickup.com/t/8451352/MTMLXGT-24095",
    "quote": "https://app.clickup.com/t/8451352/MTMQUOTE-3404",
    "dry_run": true
  }'
```

```bash
curl -X POST http://127.0.0.1:8000/pricing/sync \
  -H "Content-Type: application/json" \
  -d '{
    "sync_linked_shipments": true,
    "dry_run": true
  }'
```

Interactive docs:
```bash
open http://127.0.0.1:8000/docs
```

Production deployment assets:
- Render Blueprint: [`render.yaml`](render.yaml)
- Track-and-Trace production runbook: [`docs/PRODUCTION_TRACK_TRACE.md`](docs/PRODUCTION_TRACK_TRACE.md)

## ClickUp accounts payable invoice check
This repo also includes a read-only accounts payable lookup for ClickUp so you can confirm whether invoice tasks are present in your AP list(s).

Environment variables:
- Required:
  - `CLICKUP_API_TOKEN` or `CLICKUP_OAUTH_ACCESS_TOKEN`
  - `CLICKUP_AP_LIST_ID` or `CLICKUP_AP_LIST_IDS`
- Optional:
  - `CLICKUP_AP_INCLUDE_CLOSED`, `CLICKUP_AP_INCLUDE_ARCHIVED`
  - `CLICKUP_AP_TASK_NAME_AS_INVOICE_NUMBER=true`
  - `CLICKUP_AP_CF_INVOICE_NUMBER`
  - `CLICKUP_AP_CF_VENDOR`
  - `CLICKUP_AP_CF_AMOUNT`
  - `CLICKUP_AP_CF_CURRENCY`
  - `CLICKUP_AP_CF_STATUS`
  - `CLICKUP_AP_CF_DUE_DATE`

List invoices:
```bash
clickup-ap-invoices
```

Filter by invoice/vendor text:
```bash
clickup-ap-invoices --query "acme"
```

List custom fields in the configured AP lists:
```bash
clickup-ap-invoices --list-fields
```

API examples:
```bash
curl "http://127.0.0.1:8000/ap/health"
curl "http://127.0.0.1:8000/ap/invoices"
curl "http://127.0.0.1:8000/ap/invoices?query=acme&limit=25"
```

## ONE booking request and confirmation API
This repo now includes a carrier-specific ONE booking integration alongside track-and-trace.

Configuration:
- Required:
  - `ONE_BOOKING_REQUEST_ENDPOINT`
  - `ONE_BOOKING_CONFIRMATION_ENDPOINT`
- Optional auth:
  - `ONE_BOOKING_API_KEY`
  - `ONE_BOOKING_BEARER_TOKEN`
- Optional URL/auth tuning:
  - `ONE_BOOKING_API_BASE_URL`
  - `ONE_BOOKING_API_KEY_HEADER`
  - `ONE_BOOKING_CONFIRMATION_REF_PARAM`
  - `ONE_BOOKING_CONFIRMATION_TYPE_PARAM`

Notes:
- The public ONE eCommerce app exposes booking routes such as `/booking/booking-request` and `/booking/booking-confirm-information`, but the protected backend API paths are account-specific and not stable enough to hard-code here.
- This integration is therefore intentionally endpoint-configurable while keeping the FastAPI surface stable.

API examples:
```bash
curl "http://127.0.0.1:8000/one/health"
curl "http://127.0.0.1:8000/one/requirements"

curl -X POST "http://127.0.0.1:8000/one/bookings/request" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "bookingOffice": "MEX",
      "shipperRefNo": "REF-12345",
      "commodity": "GENERAL CARGO"
    }
  }'

curl "http://127.0.0.1:8000/one/bookings/confirmation?reference=BR12345678&reference_type=booking_request"
```

Suggested ask-list for the local ONE organization:
- The exact booking request API endpoint path or full URL for ONE eCommerce.
- The exact booking confirmation or booking status API endpoint path or full URL.
- The required authentication method for those endpoints.
- Any required headers beyond `Authorization` or `X-API-Key`.
- The confirmation lookup parameter names if they are not `reference` and `referenceType`.

Single terminal sequence:
```bash
source .venv/bin/activate
pip install -e .
./scripts/run_sync.sh
```

Schedule shipment sync on macOS (launchd):
```bash
# Install daily schedule at 06:00 and 15:00 (macOS local timezone)
./scripts/install_shipment_launchd.sh

# Check status
launchctl print "gui/$UID/com.mtm.shipment.clickup.sync"

# Tail logs
tail -f ~/Library/Logs/shipment-clickup-sync.log
tail -f ~/Library/Logs/shipment-clickup-sync.err.log

# Remove schedule
./scripts/install_shipment_launchd.sh remove
```

Backup bundle for another computer:
```bash
# Create portable backup archive on Desktop (includes .env by default)
./scripts/create_backup_bundle.sh

# Optional safer export without credentials
INCLUDE_ENV=false ./scripts/create_backup_bundle.sh

# On the backup Mac: install from bundle into ~/Backups
./scripts/install_backup_bundle.sh "/path/to/Software_Development-backup-YYYYMMDD-HHMMSS.tar.gz"

# Optional: skip scheduler install during restore
INSTALL_SCHEDULER=false ./scripts/install_backup_bundle.sh "/path/to/Software_Development-backup-YYYYMMDD-HHMMSS.tar.gz"

# Install directly from a share URL (Drive/Dropbox/S3 direct download URL)
./scripts/install_from_url.sh "https://your-share-link-or-direct-download-url.tar.gz"
```

No-terminal install for teammates (double-click):
1. Send them the backup `.tar.gz`.
2. They double-click the `.tar.gz` in Finder (this extracts the project folder).
3. Open extracted folder and double-click `Install Shipment Sync.command`.
4. If macOS blocks it, right-click the file, choose `Open`, then confirm.

Manual recovery if scheduled run was missed (computer offline):
1. Double-click `Run Shipment Sync Now.command` to force an immediate sync.
2. Optional terminal equivalent:
   - `./scripts/run_sync.sh`
3. Optional trigger through launchd scheduler:
   - `launchctl kickstart -k "gui/$UID/com.mtm.shipment.clickup.sync"`

Double-click app launcher with custom icon:
```bash
./scripts/build_track_and_trace_app.sh
```

This creates `/Users/mario/Documents/Software_Development/T&Tv2.0.app`.
Users can double-click that app in Finder to open Terminal and run the shipment sync.

## ClickUp -> iCloud Contacts sync
This repo also includes a contact sync that reads tasks from a ClickUp list and upserts them into iCloud Contacts (CardDAV), so contacts show up on iPhone and Mac signed into the same iCloud account.

Environment variables:
- Required:
  - `CLICKUP_API_TOKEN`
  - `CLICKUP_CONTACTS_LIST_ID` (or fallback `CLICKUP_LIST_ID`)
  - `ICLOUD_APPLE_ID`
  - `ICLOUD_APP_SPECIFIC_PASSWORD` (Apple app-specific password)
- Recommended contact field selectors (custom field ID or field name):
  - `CLICKUP_CONTACT_CF_FULL_NAME`
  - `CLICKUP_CONTACT_CF_FIRST_NAME`
  - `CLICKUP_CONTACT_CF_LAST_NAME`
  - `CLICKUP_CONTACT_CF_EMAIL`
  - `CLICKUP_CONTACT_CF_PHONE`
  - `CLICKUP_CONTACT_CF_COMPANY`
  - `CLICKUP_CONTACT_CF_TITLE`
  - `CLICKUP_CONTACT_CF_LINKEDIN`
  - `CLICKUP_CONTACT_CF_NOTES`
- Optional:
  - `CLICKUP_CONTACTS_LIST_IDS` (comma-separated multiple lists)
  - `CLICKUP_CONTACTS_INCLUDE_CLOSED`, `CLICKUP_CONTACTS_INCLUDE_ARCHIVED`
  - `CLICKUP_CONTACT_TASK_NAME_AS_FULL_NAME=true` (uses task name if no full-name field)
  - `CLICKUP_CONTACT_SYNC_TASK_COMMENTS=true` (append task comments into Apple Notes)
  - `CLICKUP_CONTACT_COMMENTS_LIMIT=20` (`0` means all comments)
  - `ICLOUD_CARDDAV_URL=https://contacts.icloud.com`
  - `ICLOUD_ADDRESSBOOK_URL` (set this to skip discovery)
  - `ICLOUD_TIMEOUT_SECONDS=30`

Notes behavior:
- Contacts are upserted by stable UID derived from ClickUp task ID, so updates overwrite the same iCloud contact (no duplicate per sync run).
- LinkedIn URL is synced to vCard `URL` and also included in `NOTE`.
- ClickUp task comments are appended to contact `NOTE` (subject to comment limit).

Run contact sync dry run:
```bash
contacts-sync --dry-run
```

Discover and print iCloud addressbook URL:
```bash
contacts-sync --discover-addressbook
```

Live contact sync:
```bash
contacts-sync
```

Always-on sync on macOS (recommended):
```bash
# Install launchd job (default: every 300 seconds)
./scripts/install_contacts_launchd.sh 300

# Optional: check status
launchctl print "gui/$UID/com.mtm.clickup.icloud.contacts.sync"

# Optional: tail logs
tail -f ~/Library/Logs/clickup-icloud-contacts-sync.log
tail -f ~/Library/Logs/clickup-icloud-contacts-sync.err.log

# Remove job
./scripts/install_contacts_launchd.sh 300 remove
```

This is polling-based sync (near real-time). It updates existing contacts in place using a stable UID per ClickUp task, so it does not create a new contact on each run.
Installer note: it deploys a standalone runtime under `~/.local/share/clickup-icloud-contacts-sync` (including `.env`). Re-run the installer after changing `.env` or code so the background service picks up updates.

## LinkedIn candidate sourcing -> ClickUp
This repo now includes a candidate sourcing flow that:
- Loads job criteria from JSON (titles, locations, skills, include/exclude keywords)
- Discovers profile links using either:
  - Google Custom Search (`site:linkedin.com/in`)
  - your own LinkedIn connections export CSV
- Lets you review generated queries and candidate list before writing
- Creates candidate tasks in a dedicated ClickUp list and maps candidate metadata into custom fields

### Configure
Required environment variables:
- `CLICKUP_API_TOKEN`
- `CLICKUP_CANDIDATES_LIST_ID` (target list where candidate tasks are created)

Required only for Google CSE mode:
- `GOOGLE_CSE_API_KEY`
- `GOOGLE_CSE_ENGINE_ID`

Optional candidate field mappings:
- `CLICKUP_CANDIDATE_CF_LINKEDIN`
- `CLICKUP_CANDIDATE_CF_ROLE`
- `CLICKUP_CANDIDATE_CF_LOCATION`
- `CLICKUP_CANDIDATE_CF_MATCH_SCORE`
- `CLICKUP_CANDIDATE_CF_SOURCE_QUERY`
- `CLICKUP_CANDIDATE_TASK_STATUS`
- `CANDIDATE_DEFAULT_MAX_RESULTS`

Use the sample criteria template:
```bash
cp config/examples/linkedin_criteria.example.json config/local/linkedin_criteria.json
```

### Criteria format
`config/local/linkedin_criteria.json` accepts either:
- a JSON object with `jobs: [...]`
- or a top-level JSON array

Each job entry supports:
- `job_id` (optional; auto-generated if missing)
- `job_name` or `name` (required)
- `titles` (required list) or `title` (single value)
- `locations`, `skills`, `include_keywords`, `exclude_keywords` (optional lists)
- `max_results` (optional integer per job)

### Run
Preview only (recommended first):
```bash
linkedin-candidates --criteria-file config/local/linkedin_criteria.json --dry-run --print-queries
```

Write candidates into ClickUp:
```bash
linkedin-candidates --criteria-file config/local/linkedin_criteria.json
```

Inspect candidate list custom fields:
```bash
linkedin-candidates --inspect-fields
```

## LinkedIn engagement copilot (manual approval workflow)
Use this when you want help prioritizing your own LinkedIn connections and drafting outreach/comments, while keeping all actions manual.

What it does:
- Reads your LinkedIn connections export CSV.
- Scores/prioritizes contacts using optional `config/local/linkedin_criteria.json`.
- Applies optional filters (score threshold, title/company include/exclude, recency window).
- Generates per-contact draft messages and draft comment text.
- Writes a review queue CSV with explicit `approved` + `edited_*` columns.
- Optionally builds an approved-only send queue from your reviewed CSV.
- Optionally uses OpenAI for better personalized drafts (with template fallback on errors).

What it does not do:
- It does not log into LinkedIn.
- It does not auto-comment, auto-message, or auto-post.

Configure (optional campaign template):
```bash
cp config/examples/linkedin_copilot_campaign.example.json config/local/linkedin_copilot_campaign.json
```

Optional environment for AI drafts:
- `OPENAI_API_KEY`
- `LINKEDIN_COPILOT_USE_AI_DRAFTS=false`
- `LINKEDIN_COPILOT_OPENAI_MODEL=gpt-4.1-mini`
- `LINKEDIN_COPILOT_OPENAI_TIMEOUT_SECONDS=45`
- `LINKEDIN_COPILOT_OPENAI_MAX_OUTPUT_TOKENS=220`

Run draft generation:
```bash
linkedin-copilot \
  --connections-csv linkedin_connections.csv \
  --campaign-file config/local/linkedin_copilot_campaign.json \
  --criteria-file config/local/linkedin_criteria.json
```

Review outputs:
- `artifacts/output/linkedin_copilot/drafts-*.json`
- `artifacts/output/linkedin_copilot/drafts-*.md`
- `artifacts/output/linkedin_copilot/review-queue-*.csv`
- `artifacts/output/linkedin_copilot/filtered-out-*.csv` (only when filters exclude rows)

Example with CLI filter overrides:
```bash
linkedin-copilot \
  --connections-csv linkedin_connections.csv \
  --criteria-file config/local/linkedin_criteria.json \
  --min-score 55 \
  --connected-within-days 180 \
  --exclude-company competitor \
  --exclude-title intern
```

Example enabling AI drafts:
```bash
OPENAI_API_KEY=... linkedin-copilot \
  --connections-csv linkedin_connections.csv \
  --campaign-file config/local/linkedin_copilot_campaign.json \
  --criteria-file config/local/linkedin_criteria.json \
  --use-ai-drafts \
  --ai-model gpt-4.1-mini
```

After manual review, mark approved rows in the `approved` column (`yes`, `true`, `1`, etc.), optionally edit drafts in `edited_message` / `edited_comment`, then build approved queue:
```bash
linkedin-copilot \
  --connections-csv linkedin_connections.csv \
  --campaign-file config/local/linkedin_copilot_campaign.json \
  --criteria-file config/local/linkedin_criteria.json \
  --approval-file artifacts/output/linkedin_copilot/review-queue-YYYYMMDD-HHMMSS.csv
```

Wrapper script:
```bash
./scripts/run_linkedin_copilot.sh --connections-csv linkedin_connections.csv --criteria-file config/local/linkedin_criteria.json
```

## ClickUp custom field inventory export (Spaces -> Lists -> fields)
Use this when you want a deduplicated custom-field map across multiple Spaces.

What it does:
- Accepts Space IDs or Space URLs
- Resolves and prints the Space IDs
- Enumerates Lists under each Space (folderless + folders)
- Fetches custom field metadata for each List
- Normalizes/deduplicates fields into one CSV table

Run:
```bash
clickup-fields-export \
  --space "https://app.clickup.com/1234567/v/s/901111" \
  --space "https://app.clickup.com/1234567/v/s/902222" \
  --output-csv artifacts/output/spreadsheet/clickup_custom_fields_normalized.csv
```

Optional outputs:
```bash
# Also write per-list field occurrences (non-deduplicated detail)
clickup-fields-export \
  --space 901111 \
  --space 902222 \
  --details-csv artifacts/output/spreadsheet/clickup_custom_fields_by_list.csv

# Also write Excel file (requires openpyxl)
clickup-fields-export \
  --space 901111 \
  --space 902222 \
  --xlsx-output artifacts/output/spreadsheet/clickup_custom_fields_normalized.xlsx
```

Auth:
- Uses `CLICKUP_API_TOKEN` from `.env` by default
- Or pass `--token <YOUR_TOKEN>`

Save output for manual review:
```bash
linkedin-candidates --criteria-file config/local/linkedin_criteria.json --dry-run --output-json artifacts/build/candidates.json
```

Use your own LinkedIn export CSV (no Google keys required):
```bash
linkedin-candidates --criteria-file config/local/linkedin_criteria.json --input-csv linkedin_connections.csv --dry-run
```

Push CSV-based matches into ClickUp:
```bash
linkedin-candidates --criteria-file config/local/linkedin_criteria.json --input-csv linkedin_connections.csv
```

CSV headers accepted (case-insensitive; spaces/underscores are fine):
- Required practical fields: `firstName`, `lastName`, `profileUrl` (or `url`)
- Optional fields for scoring/context: `position`, `company`, `location`, `connectedOn`

Exporting your own LinkedIn data:
- In LinkedIn, request/export your Connections data as CSV.
- Save it as something like `linkedin_connections.csv` in this project.
- Run the `--input-csv` command above.

Run with wrapper script:
```bash
./scripts/run_linkedin_candidates.sh --criteria-file config/local/linkedin_criteria.json --dry-run
```

ETA-only mode:
- `SHIPMENT_ETA_ONLY=true` (default) keeps sync focused on ETA in output and ClickUp comments.
- Displayed shipment dates are normalized to `YYYY-MM-DD` across carriers (ETA, move dates, and summary output).
- ClickUp comments include:
  - ETA timestamp (carrier local event time when available; otherwise `n/a`)
  - Carrier source link (shipping line page for manual cross-check)
  - Move sequence with state labels (`ACTUAL`/`ESTIMATE`) for each event (all available events by default; set `SHIPMENT_RECENT_MOVES_LIMIT` to a positive number to cap)
- Upfront line filtering (recommended to reduce run time/noise):
  - `SHIPMENT_ALLOWED_LINES=one,maersk` to process only selected lines.
  - `SHIPMENT_EXCLUDED_LINES=msc,hapag lloyd` to always skip specific lines.
  - `SHIPMENT_SKIP_UNSUPPORTED_LINES=true` to drop lines without adapters before any sync calls.
- Carrier preflight (recommended):
  - `SHIPMENT_PREFLIGHT_ENABLED=true` checks DNS/HTTPS reachability for active lines before task sync.
  - `SHIPMENT_PREFLIGHT_TIMEOUT_SECONDS=8` sets probe timeout.
  - When preflight fails, the line is skipped once with a summary reason instead of task-by-task timeout errors.
- Recent-sync cache behavior:
  - `SHIPMENT_MIN_SYNC_INTERVAL_HOURS=4` skips tasks that were already synced in the last 4 hours.
  - Requires `CLICKUP_CF_STATUS_LAST_CHECKED` to be configured (same field written by sync).

## Included carriers
- CMA-CGM (`cma cgm`, `cma-cgm`, `cma - cgm`)
- COSCO (`cosco`, `cosco shipping`, `cosco shipping lines`)
- MSC (`msc`, `msc shipping line`, `mediterranean shipping company`)
- PIL (`pil`, `pacific international lines`)
- Evergreen (`evergreen`, `evergreen line`, `evergreen marine`)
- Wan Hai (`wan hai`, `wan hai lines`)
- OOCL (`oocl`, `orient overseas container line`)
- Hapag-Lloyd (`hapag lloyd`, `hapag-lloyd`)
- Maersk (`maersk`, `maersk line`, `a.p. moller - maersk`)
- ONE (`one`, `ocean network express`)
- Demo (`demo`)

CMA-CGM, COSCO, MSC, PIL, Evergreen, Wan Hai, OOCL, Hapag-Lloyd, Maersk and ONE adapters support API mode and website mode.
For Maersk, you can also use website mode with:
- `MAERSK_TRACKING_URL_TEMPLATE=https://www.maersk.com/tracking/{reference}`

For Maersk official Events API mode (recommended):
- `MAERSK_API_MODE=events`
- `MAERSK_TRACKING_URL_TEMPLATE=` (leave empty)
- `MAERSK_TRACKING_API_URL=https://api.maersk.com/track-and-trace-private/events`
- `MAERSK_CONSUMER_KEY=...`
- OAuth option A: set `MAERSK_BEARER_TOKEN=...`
- OAuth option B: set `MAERSK_OAUTH_TOKEN_URL`, `MAERSK_OAUTH_CLIENT_ID`, `MAERSK_OAUTH_CLIENT_SECRET`
- `MAERSK_EVENTS_LIMIT=100` (page size)
- `MAERSK_FETCH_ALL_EVENTS=true` (fetch all pages for full shipment event history)
- `MAERSK_WEB_FALLBACK_ON_API_ERROR=true` (fallback to public tracking page when events API fails)

For MSC anti-bot resistant mode (recommended):
- `MSC_USE_PLAYWRIGHT=true`
- `MSC_PLAYWRIGHT_CHANNEL=chrome`
- `MSC_MIN_SYNC_INTERVAL_HOURS=18`
- `MSC_PER_TASK_TIMEOUT_SECONDS=75`
- `MSC_MAX_REFERENCE_ATTEMPTS=4`
- `MSC_PLAYWRIGHT_CHALLENGE_TIMEOUT_SECONDS=20`
- `MSC_PLAYWRIGHT_CHALLENGE_RELOAD_ATTEMPTS=1`
- `MSC_PLAYWRIGHT_TRACKING_URL=https://www.msc.com/en/track-a-shipment`
- `MSC_PLAYWRIGHT_API_ENDPOINT=https://www.msc.com/api/feature/tools/TrackingInfo`
- Optional hard requirement: `MSC_PLAYWRIGHT_REQUIRED=true` (fail run if browser mode fails)
- Operator-assisted alternative: `msc-browser-assisted --export-queue /tmp/msc-browser-queue.json`
  creates a local review queue with normal MSC tracking URLs and exact references. After an operator copies a
  complete MSC result page, `msc-browser-assisted --preview-capture /path/to/capture.txt --container CONTAINER`
  produces the normal ClickUp update plan without writing anything. This lane does not automate browser access,
  solve challenges, or run in ECS.
- Carrier access denials open a per-run circuit breaker. Remaining shipments for that carrier are skipped and
  the task exits failed, providing a clear ECS/CloudWatch failure signal instead of repeatedly querying a blocked site.
For ONE website mode:
- `ONE_TRACKING_URL_TEMPLATE=https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking`
- Default query keys are already set for ONE:
  - `ONE_REF_PARAM=trakNoParam`
  - `ONE_TYPE_PARAM=trakNoTpCdParam`
  - `ONE_BOOKING_TYPE_CODE=B`
  - `ONE_CONTAINER_TYPE_CODE=C`
- For ETA-focused tracking, enable ONE EDH API mode (recommended):
  - `ONE_USE_EDH_API=true`
  - `ONE_EDH_BASE_URL=https://ecomm.one-line.com/api/v1/edh`
  - Adapter uses ONE track-and-trace endpoints and maps arrival date to ETA.
  - Latest move sequence is read from ONE `cop-events` track-and-trace endpoint.

For COSCO:
- Official COP API mode (recommended; based on [COP GitHub](https://github.com/cop-cos/COP)):
  - `COSCO_MODE=cop`
  - `COSCO_COP_BASE_URL=https://api-pp.lines.coscoshipping.com` (UAT)
  - `COSCO_COP_SERVICE_PREFIX=/service`
  - `COSCO_COP_API_KEY=...`
  - `COSCO_COP_SECRET_KEY=...`
  - Request pattern used by adapter:
    - `GET /service/info/tracking/{reference}?numberType=cntr|bkg|bl`
  - HMAC headers are generated automatically:
    - `X-Coscon-Date`
    - `X-Coscon-Content-Md5`
    - `X-Coscon-Digest`
    - `X-Coscon-Authorization`
    - optional `X-Coscon-Hmac` (enabled by default)
- Optional mode:
  - `COSCO_MODE=auto` (COP first if keys exist; otherwise legacy fallback)
- Legacy fallback mode (public pages/endpoints):
  - `COSCO_MODE=legacy`
  - `COSCO_TRACKING_URL_TEMPLATE=https://elines.coscoshipping.com/ebusiness/cargoTracking?trackingType={type}&number={reference}`
  - `COSCO_TRACKING_API_BASE_URL=https://elines.coscoshipping.com/ebtracking`
  - Legacy type codes:
    - `COSCO_CONTAINER_TYPE_CODE=CONTAINER`
    - `COSCO_BOOKING_TYPE_CODE=BOOKING`
    - `COSCO_BL_TYPE_CODE=BILLOFLADING`
- Notes:
  - If `COSCO_MODE=cop` and keys are missing, sync reports a configuration error (no network spam).
  - Legacy COSCO endpoints often return anti-bot HTML (`This page can't be displayed`).

For CMA-CGM:
- Public tracking detail template:
  - `CMA_CGM_TRACKING_URL_TEMPLATE=https://www.cma-cgm.com/ebusiness/tracking/detail/{reference}`
- Optional API mode:
  - Direct endpoint:
    - `CMA_CGM_TRACKING_API_URL=...`
  - Or base + method:
    - `CMA_CGM_API_BASE_URL=...`
    - `CMA_CGM_API_METHOD=searchMoveOnCommercialCycle`
    - optional explicit path: `CMA_CGM_API_METHOD_PATH=/searchMoveOnCommercialCycle`
  - Auth headers:
    - `CMA_CGM_API_KEY=...`
    - `CMA_CGM_API_KEY_HEADER=keyId` (from CMA OpenAPI `ApiKeyAuth`)
  - OAuth2 client credentials for the Track & Trace API:
    - `CMA_CGM_OAUTH_TOKEN_URL=https://auth.cma-cgm.com/as/token.oauth2`
    - `CMA_CGM_OAUTH_CLIENT_ID=...`
    - `CMA_CGM_OAUTH_CLIENT_SECRET=...`
    - `CMA_CGM_OAUTH_SCOPE=tandtcommercial:read:be`
  - Optional query keys:
    - DCSA defaults used by adapter:
      - `CMA_CGM_CONTAINER_REF_PARAM=equipmentReference`
      - `CMA_CGM_BOOKING_REF_PARAM=carrierBookingReference`
    - optional generic keys: `CMA_CGM_REF_PARAM`, `CMA_CGM_TYPE_PARAM`
    - `CMA_CGM_INCLUDE_TYPE_PARAM=true|false`
  - DCSA cursor safety:
    - `CMA_CGM_DCSA_MAX_PAGES=25` bounds official `Next-Page` traversal.
  - Read-only legacy-versus-DCSA comparison:
    - `CMA_CGM_COMPARISON_ENABLED=true`
    - `CMA_CGM_COMPARISON_MAX_SHIPMENTS=25`
    - `SHIPMENT_ALLOWED_LINES=cma cgm` is required and enforced as a strict
      API-side ClickUp carrier prefilter.
    - Run `cma-cgm-dcsa-compare --run`; it reads inventory and CMA's official
      API but never writes ClickUp, creates a schedule, or replaces the normal
      CMA worker.
- Type codes:
  - `CMA_CGM_CONTAINER_TYPE_CODE=container`
  - `CMA_CGM_BOOKING_TYPE_CODE=booking`
- Notes:
  - DCSA endpoint in the provided spec is `GET /events` (`operationId: searchMoveOnCommercialCycle`) and uses OAuth2 client credentials.
  - CMA-CGM may return anti-bot challenge pages (`Please enable JS and disable any ad blocker`).
  - Preflight behavior:
    - If CMA API env is configured, preflight checks the API host (not the public web tracking page).
  - When blocked, the adapter fails with a clear message and preflight can skip the line upfront.

For PIL, Evergreen, and OOCL (generic adapters):
- Each line supports either API URL or website template mode.
- Use the corresponding env prefix (`MSC_`, `PIL_`, `EVERGREEN_`, `WAN_HAI_`, `OOCL_`):
  - `{PREFIX}_TRACKING_API_URL` or `{PREFIX}_TRACKING_URL_TEMPLATE`
  - optional auth headers: `{PREFIX}_API_KEY`, `{PREFIX}_API_KEY_HEADER`
  - optional query keys: `{PREFIX}_REF_PARAM`, `{PREFIX}_TYPE_PARAM`
  - reference type codes: `{PREFIX}_BOOKING_TYPE_CODE`, `{PREFIX}_CONTAINER_TYPE_CODE`
- If both URL settings are empty, adapter uses a default public tracking page URL for that carrier.

For Wan Hai:
- Dedicated browser mode is recommended:
  - `WAN_HAI_USE_PLAYWRIGHT=true`
  - `WAN_HAI_PLAYWRIGHT_HEADLESS=true` for Render/server deployments
  - `WAN_HAI_PLAYWRIGHT_BROWSER=chromium`
  - `WAN_HAI_PLAYWRIGHT_CHANNEL=chrome`
  - `WAN_HAI_PLAYWRIGHT_TRACKING_URL=https://vip.wanhai.com/views/cargo_track_v2/tracking_query.xhtml`
- Current implementation uses the Wan Hai query form, opens the result popup, and reads:
  - latest container/booking status
  - vessel and voyage
  - loading/discharge ports when booking detail is available
  - estimated arrival date when booking detail is available
- Generic fallback env vars remain available, but Wan Hai's public site is commonly blocked by anti-bot protection in direct HTTP mode.

For Hapag-Lloyd:
- Browser/page mode:
  - `HAPAG_USE_PLAYWRIGHT=true`
  - Opens the public Hapag tracking page in Chrome/Playwright and parses the rendered tracking tables.
  - Default container page template:
    - `HAPAG_PLAYWRIGHT_CONTAINER_URL_TEMPLATE=https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html?view={view}&container={reference}`
  - Default booking page template:
    - `HAPAG_PLAYWRIGHT_BOOKING_URL_TEMPLATE=https://www.hapag-lloyd.com/en/online-business/track/track-by-booking-solution.html?view={view}&booking={reference}`
  - Tuning:
    - `HAPAG_PLAYWRIGHT_HEADLESS`
    - `HAPAG_PLAYWRIGHT_BROWSER`
    - `HAPAG_PLAYWRIGHT_CHANNEL`
    - `HAPAG_PLAYWRIGHT_CHALLENGE_TIMEOUT_SECONDS`
    - `HAPAG_PLAYWRIGHT_CHALLENGE_RELOAD_ATTEMPTS`
    - `HAPAG_PLAYWRIGHT_SESSION_REUSE`
    - `HAPAG_PLAYWRIGHT_WARMUP_SECONDS`
    - `HAPAG_PLAYWRIGHT_TIMEOUT_SECONDS`
    - `HAPAG_PLAYWRIGHT_REQUEST_DELAY_SECONDS`
    - `HAPAG_PLAYWRIGHT_VIEW`
    - `HAPAG_PLAYWRIGHT_USER_AGENT`
- Website/API template mode:
  - `HAPAG_TRACKING_URL_TEMPLATE=...` (supports `{reference}` and optional `{type}`)
- API mode:
  - `HAPAG_TRACKING_API_URL=https://api.hlag.com/hlag/external/v2/events/`
  - The Hapag API is protected; if you use `HAPAG_TRACKING_API_URL`, configure at least one auth mode below.
  - One of these auth setups:
    - `HAPAG_API_KEY`, `HAPAG_API_KEY_HEADER` (if required)
    - or gateway client headers: `HAPAG_CLIENT_ID`, `HAPAG_CLIENT_SECRET` (headers configurable)
  - DCSA query params are supported by default:
    - `HAPAG_EQUIPMENT_REF_PARAM=equipmentReference`
    - `HAPAG_BOOKING_REF_PARAM=carrierBookingReference`
    - `HAPAG_TRANSPORT_DOCUMENT_REF_PARAM=transportDocumentReference`
  - Optional fallback keys for non-standard endpoint variants:
    - `HAPAG_REF_PARAM`, `HAPAG_TYPE_PARAM`
- Optional auth:
  - `HAPAG_BEARER_TOKEN=...`
  - or OAuth client credentials: `HAPAG_OAUTH_TOKEN_URL`, `HAPAG_OAUTH_CLIENT_ID`, `HAPAG_OAUTH_CLIENT_SECRET`, optional `HAPAG_OAUTH_SCOPE`
- Source-link in comments:
  - `HAPAG_TRACKING_PAGE_URL_TEMPLATE`

If your endpoint needs auth headers, set `*_API_KEY` and `*_API_KEY_HEADER`.
If your endpoint uses different query names, set `*_REF_PARAM` and `*_TYPE_PARAM`.

Reference type is sent with carrier-specific values (for ONE, default booking is `B`).

## Add more carrier integrations
Edit:
- `src/shipment_sync/carriers/registry.py`
- Add adapters under `src/shipment_sync/carriers/`

Use `template_requests_adapter.py` as a base for API-based carriers.

## Browser automation fallback
If a carrier has no API, use Playwright login/scraping in an adapter.

Install optional package:
```bash
pip install -e .[browser]
playwright install
```

## Notes
- Some carrier websites use CAPTCHA/anti-bot protections; prefer official APIs when possible.

## Scheduler (cron)
Install an hourly schedule:
```bash
./scripts/install_cron.sh
```

Install a custom schedule (example: every 30 minutes):
```bash
./scripts/install_cron.sh "*/30 * * * *"
```

View current cron entries:
```bash
crontab -l
```

Remove the sync schedule:
```bash
./scripts/install_cron.sh "0 * * * *" remove
```

Log file default:
- `sync.log` in project root
