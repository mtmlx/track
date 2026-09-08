#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-us-east-2}"
SOURCE_TASK_DEFINITION="${DCSA_CMA_SHADOW_SOURCE_TASK_DEFINITION:-track-trace-cma-cgm}"
TASK_FAMILY="${DCSA_CMA_SHADOW_TASK_FAMILY:-track-trace-dcsa-cma-shadow}"
TASK_ROLE_ARN="${DCSA_CMA_SHADOW_TASK_ROLE_ARN:-arn:aws:iam::525753067477:role/TrackTraceDcsaShadowTaskRole}"
LOG_GROUP="${DCSA_CMA_SHADOW_LOG_GROUP:-/ecs/track-trace/dcsa-shadow}"
TABLE_NAME="${DCSA_TNT_LEDGER_TABLE:-track-trace-dcsa-events}"
MAX_SHIPMENTS="${DCSA_CMA_SHADOW_MAX_SHIPMENTS:-25}"
IMAGE_URI="${DCSA_CMA_SHADOW_IMAGE_URI:-}"
DRY_RUN=false

usage() {
  cat <<'USAGE'
Usage: DCSA_CMA_SHADOW_IMAGE_URI=<ECR image digest> scripts/register_dcsa_cma_shadow_task.sh [--dry-run]

Registers a separate, non-projecting CMA DCSA shadow task definition. It
copies the current CMA worker's read-only inventory and CMA OAuth bindings,
removes normal write-related settings, and replaces the command with:

  dcsa-tnt-shadow --run

The image must be immutable (repository@sha256:...). This script never creates
or updates an EventBridge Scheduler schedule.
USAGE
}

while (($#)); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --dry-run)
      DRY_RUN=true
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -z "$IMAGE_URI" || "$IMAGE_URI" != *@sha256:* ]]; then
  echo "DCSA_CMA_SHADOW_IMAGE_URI must be an immutable ECR image digest (repository@sha256:...)." >&2
  exit 2
fi

if [[ ! "$MAX_SHIPMENTS" =~ ^[1-9][0-9]*$ ]] || (( MAX_SHIPMENTS > 250 )); then
  echo "DCSA_CMA_SHADOW_MAX_SHIPMENTS must be an integer from 1 through 250." >&2
  exit 2
fi

source_definition="$(mktemp "${TMPDIR:-/tmp}/dcsa-cma-source.XXXXXX.json")"
desired_definition="$(mktemp "${TMPDIR:-/tmp}/dcsa-cma-shadow.XXXXXX.json")"
trap 'rm -f "$source_definition" "$desired_definition"' EXIT

aws ecs describe-task-definition \
  --region "$REGION" \
  --task-definition "$SOURCE_TASK_DEFINITION" \
  --query 'taskDefinition' \
  --output json >"$source_definition"

required_secrets='[
  "CLICKUP_API_TOKEN",
  "CLICKUP_CF_BOOKING_NO",
  "CLICKUP_CF_CONTAINER_NO",
  "CLICKUP_CF_SHIPPING_LINE",
  "CLICKUP_CF_STATUS_LAST_CHECKED",
  "CLICKUP_DISCOVERY_LIST_NAME_INCLUDE",
  "CLICKUP_DISCOVERY_VALIDATE_SCHEMA",
  "CLICKUP_DISCOVER_LISTS_FROM_SPACES",
  "CLICKUP_DISCOVER_LISTS_FROM_TEAM",
  "CLICKUP_INCLUDE_ARCHIVED",
  "CLICKUP_INCLUDE_CLOSED",
  "CLICKUP_LIST_ID",
  "CLICKUP_LIST_IDS",
  "CLICKUP_OAUTH_ACCESS_TOKEN",
  "CLICKUP_REQUEST_MAX_RETRIES",
  "CLICKUP_REQUEST_RETRY_DELAY_SECONDS",
  "CLICKUP_SPACE_IDS",
  "CLICKUP_TEAM_ID",
  "CMA_CGM_OAUTH_CLIENT_SECRET"
]'

override_names='[
  "AWS_REGION",
  "CLICKUP_USE_TASK_STATUS",
  "DCSA_TNT_LEDGER_BACKEND",
  "DCSA_TNT_LEDGER_TABLE",
  "DCSA_TNT_SHADOW_CARRIERS",
  "DCSA_TNT_SHADOW_CMA_CGM_VERSION",
  "DCSA_TNT_SHADOW_ENABLED",
  "DCSA_TNT_SHADOW_MAX_SHIPMENTS",
  "SHIPMENT_AUDIT_SOURCE",
  "SHIPMENT_COMMENT_ON_NO_CHANGE"
]'

jq -e \
  --arg family "$TASK_FAMILY" \
  --arg image "$IMAGE_URI" \
  --arg task_role "$TASK_ROLE_ARN" \
  --arg log_group "$LOG_GROUP" \
  --arg region "$REGION" \
  --arg table_name "$TABLE_NAME" \
  --arg max_shipments "$MAX_SHIPMENTS" \
  --argjson required_secrets "$required_secrets" \
  --argjson override_names "$override_names" \
  '
    . as $source
    | if ($source.containerDefinitions | length) != 1 then
        error("The CMA source task definition must contain exactly one container.")
      elif (($required_secrets - (($source.containerDefinitions[0].secrets // []) | map(.name))) | length) != 0 then
        error("The CMA source task definition is missing a required shadow secret binding.")
      else
        {
          family: $family,
          taskRoleArn: $task_role,
          executionRoleArn: $source.executionRoleArn,
          networkMode: $source.networkMode,
          containerDefinitions: [
            $source.containerDefinitions[0]
            | .name = "track-trace-dcsa-cma-shadow"
            | .image = $image
            | .command = ["dcsa-tnt-shadow", "--run"]
            | .environment = (
                ((.environment // [])
                  | map(select(.name as $name | ($override_names | index($name) | not))))
                + [
                    {name: "AWS_REGION", value: $region},
                    {name: "CLICKUP_USE_TASK_STATUS", value: "false"},
                    {name: "DCSA_TNT_SHADOW_ENABLED", value: "true"},
                    {name: "DCSA_TNT_SHADOW_CARRIERS", value: "cma cgm"},
                    {name: "DCSA_TNT_SHADOW_CMA_CGM_VERSION", value: "2.2"},
                    {name: "DCSA_TNT_SHADOW_MAX_SHIPMENTS", value: $max_shipments},
                    {name: "DCSA_TNT_LEDGER_BACKEND", value: "dynamodb"},
                    {name: "DCSA_TNT_LEDGER_TABLE", value: $table_name},
                    {name: "SHIPMENT_AUDIT_SOURCE", value: "aws-ecs-dcsa-cma-shadow"},
                    {name: "SHIPMENT_COMMENT_ON_NO_CHANGE", value: "false"}
                  ]
              )
            | .secrets = (
                (.secrets // [])
                | map(select(.name as $name | ($required_secrets | index($name))))
              )
            | .logConfiguration = (
                (.logConfiguration // {})
                | .logDriver = "awslogs"
                | .options = ((.options // {}) + {
                    "awslogs-group": $log_group,
                    "awslogs-region": $region,
                    "awslogs-stream-prefix": "cma-shadow"
                  })
              )
          ],
          volumes: ($source.volumes // []),
          placementConstraints: ($source.placementConstraints // []),
          requiresCompatibilities: $source.requiresCompatibilities,
          cpu: $source.cpu,
          memory: $source.memory,
          ephemeralStorage: $source.ephemeralStorage,
          runtimePlatform: $source.runtimePlatform,
          proxyConfiguration: $source.proxyConfiguration,
          inferenceAccelerators: $source.inferenceAccelerators,
          pidMode: $source.pidMode,
          ipcMode: $source.ipcMode
        }
        | with_entries(select(.value != null))
      end
  ' "$source_definition" >"$desired_definition"

if [[ "$DRY_RUN" == "true" ]]; then
  jq '
    {
      family,
      taskRoleArn,
      executionRoleArn,
      networkMode,
      cpu,
      memory,
      runtimePlatform,
      requiresCompatibilities,
      containers: [
        .containerDefinitions[]
        | {
            name,
            image,
            command,
            environmentNames: [(.environment // [])[].name],
            shadowSettings: [
              (.environment // [])[]
              | select(.name | test("^(AWS_REGION|CLICKUP_USE_TASK_STATUS|DCSA_TNT_|SHIPMENT_AUDIT_SOURCE|SHIPMENT_COMMENT_ON_NO_CHANGE)"))
              | {name, value}
            ],
            secretNames: [(.secrets // [])[].name],
            logConfiguration
          }
      ]
    }
  ' "$desired_definition"
  exit 0
fi

aws ecs register-task-definition \
  --region "$REGION" \
  --cli-input-json "file://$desired_definition" \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text
