# AWS Lightsail deployment

This is the low-cost deployment path for the Track & Trace sync when carrier APIs need a stable cloud egress IP.

## One-time setup

Use an Ubuntu Lightsail instance with Docker installed, then clone the repo:

```bash
sudo apt-get update
sudo apt-get install -y git docker.io docker-compose-v2 docker-buildx
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

git clone https://github.com/mtmlx/track.git
cd track
docker build -t track-trace .
```

If the current shell cannot access Docker after `usermod`, reconnect to the instance or run Docker commands with `sudo`.

Create `.env` in the repo root using the same values currently configured in Render. Do not commit `.env`.

## Maersk validation

Before running the full sync, confirm Maersk OAuth works from AWS:

```bash
docker run --rm --env-file .env track-trace python -c 'import os,json,requests; r=requests.post(os.getenv("MAERSK_OAUTH_TOKEN_URL"),data={"grant_type":"client_credentials","client_id":os.getenv("MAERSK_OAUTH_CLIENT_ID"),"client_secret":os.getenv("MAERSK_OAUTH_CLIENT_SECRET")},headers={"Content-Type":"application/x-www-form-urlencoded","Consumer-Key":os.getenv("MAERSK_CONSUMER_KEY")},timeout=30); print("status", r.status_code); p=r.json(); p["access_token"]="<present redacted>" if "access_token" in p else p.get("access_token"); p["id_token"]="<present redacted>" if "id_token" in p else p.get("id_token"); print(json.dumps(p, indent=2)[:1500])'
```

Expected result:

```text
status 200
```

## Narrow Maersk preview

Use a single ClickUp list first to avoid scanning the whole workspace while testing:

```bash
docker run --rm --env-file .env \
  -e CLICKUP_DISCOVER_LISTS_FROM_SPACES=false \
  -e CLICKUP_LIST_ID=901706298710 \
  -e CLICKUP_LIST_IDS=901706298710 \
  -e SHIPMENT_ALLOWED_LINES=maersk \
  -e SHIPMENT_MIN_SYNC_INTERVAL_HOURS=0 \
  track-trace ./scripts/run_sync.sh --preview-updates
```

Run live only after the preview is correct:

```bash
docker run --rm --env-file .env \
  -e CLICKUP_DISCOVER_LISTS_FROM_SPACES=false \
  -e CLICKUP_LIST_ID=901706298710 \
  -e CLICKUP_LIST_IDS=901706298710 \
  -e SHIPMENT_ALLOWED_LINES=maersk \
  -e SHIPMENT_MIN_SYNC_INTERVAL_HOURS=0 \
  track-trace ./scripts/run_sync.sh
```

## Long-running services

Start only the API container:

```bash
docker compose -f deploy/aws-lightsail/docker-compose.yml up -d shipment-api
```

After the AWS test is approved, start the API and cron containers:

```bash
docker compose -f deploy/aws-lightsail/docker-compose.yml up -d
```

The API listens on container port `10000`, but the compose definition binds it only to the instance loopback interface. Do not open TCP ports `80` or `10000` in the Lightsail Networking tab. Use an authenticated SSH tunnel for an operator session instead:

```bash
ssh -N -L 10000:127.0.0.1:10000 ubuntu@YOUR_LIGHTSAIL_HOST
```

Then use `http://127.0.0.1:10000` from the operator machine. A future public API must sit behind a managed TLS endpoint with authentication; it must not expose the container port directly.

The cron container defaults to every 8 hours. Override it in `.env` if needed:

```env
SHIPMENT_CRON_INTERVAL_SECONDS=28800
```
