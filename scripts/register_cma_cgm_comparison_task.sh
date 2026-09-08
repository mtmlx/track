#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
SOURCE_TASK_DEFINITION="${CMA_CGM_COMPARISON_SOURCE_TASK_DEFINITION:-track-trace-cma-cgm}"
TASK_FAMILY="${CMA_CGM_COMPARISON_TASK_FAMILY:-track-trace-cma-cgm-comparison}"
TASK_ROLE_ARN="${CMA_CGM_COMPARISON_TASK_ROLE_ARN:-arn:aws:iam::525753067477:role/TrackTraceDcsaShadowTaskRole}"
LOG_GROUP="${CMA_CGM_COMPARISON_LOG_GROUP:-/ecs/track-trace/dcsa-shadow}"
MAX_SHIPMENTS="${CMA_CGM_COMPARISON_MAX_SHIPMENTS:-25}"
IMAGE_URI="${CMA_CGM_COMPARISON_IMAGE_URI:-}"
DRY_RUN=false

usage() {
  cat <<'USAGE'
Usage: CMA_CGM_COMPARISON_IMAGE_URI=<ECR image digest> scripts/register_cma_cgm_comparison_task.sh [--dry-run]

Registers an isolated, one-off CMA legacy-versus-DCSA comparison task. It uses
the normal CMA worker only as a source of read-only inventory/CMA OAuth secret
bindings and network-compatible runtime settings. The task command is:

  cma-cgm-dcsa-compare --run

The task does not write ClickUp, write the DCSA ledger, or create/update an
EventBridge Scheduler schedule. The image must be immutable
(repository@sha256:...).
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
  echo "CMA_CGM_COMPARISON_IMAGE_URI must be an immutable ECR image digest (repository@sha256:...)." >&2
  exit 2
fi

if [[ ! "$MAX_SHIPMENTS" =~ ^[1-9][0-9]*$ ]] || (( MAX_SHIPMENTS > 250 )); then
  echo "CMA_CGM_COMPARISON_MAX_SHIPMENTS must be an integer from 1 through 250." >&2
  exit 2
fi

source_definition="$(mktemp "${TMPDIR:-/tmp}/cma-comparison-source.XXXXXX.json")"
desired_definition="$(mktemp "${TMPDIR:-/tmp}/cma-comparison-task.XXXXXX.json")"
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

retained_environment_names='[
  "CARRIER_RESPONSE_MAX_BYTES",
  "CMA_CGM_API_BASE_URL",
  "CMA_CGM_API_KEY_HEADER",
  "CMA_CGM_API_METHOD",
  "CMA_CGM_API_METHOD_PATH",
  "CMA_CGM_BOOKING_REF_PARAM",
  "CMA_CGM_BOOKING_TYPE_CODE",
  "CMA_CGM_CONTAINER_REF_PARAM",
  "CMA_CGM_CONTAINER_TYPE_CODE",
  "CMA_CGM_DCSA_MAX_PAGES",
  "CMA_CGM_INCLUDE_TYPE_PARAM",
  "CMA_CGM_MAX_RETRIES",
  "CMA_CGM_OAUTH_CLIENT_ID",
  "CMA_CGM_OAUTH_SCOPE",
  "CMA_CGM_OAUTH_TOKEN_URL",
  "CMA_CGM_REF_PARAM",
  "CMA_CGM_RETRY_DELAY_SECONDS",
  "CMA_CGM_TIMEOUT_SECONDS",
  "CMA_CGM_TRACKING_API_URL",
  "CMA_CGM_TYPE_PARAM"
]'

jq -e \
  --arg family "$TASK_FAMILY" \
  --arg image "$IMAGE_URI" \
  --arg task_role "$TASK_ROLE_ARN" \
  --arg log_group "$LOG_GROUP" \
  --arg region "$REGION" \
  --arg max_shipments "$MAX_SHIPMENTS" \
  --argjson required_secrets "$required_secrets" \
  --argjson retained_environment_names "$retained_environment_names" \
  '
    . as $source
    | if ($source.containerDefinitions | length) != 1 then
        error("The CMA source task definition must contain exactly one container.")
      elif (($required_secrets - (($source.containerDefinitions[0].secrets // []) | map(.name))) | length) != 0 then
        error("The CMA source task definition is missing a required comparison secret binding.")
      else
        {
          family: $family,
          taskRoleArn: $task_role,
          executionRoleArn: $source.executionRoleArn,
          networkMode: $source.networkMode,
          containerDefinitions: [
            $source.containerDefinitions[0]
            | .name = "track-trace-cma-cgm-comparison"
            | .image = $image
            | .command = ["cma-cgm-dcsa-compare", "--run"]
            | .environment = (
                ((.environment // [])
                  | map(select(.name as $name | ($retained_environment_names | index($name)))))
                + [
                    {name: "AWS_REGION", value: $region},
                    {name: "CLICKUP_USE_TASK_STATUS", value: "false"},
                    {name: "CMA_CGM_COMPARISON_ENABLED", value: "true"},
                    {name: "CMA_CGM_COMPARISON_MAX_SHIPMENTS", value: $max_shipments},
                    {name: "SHIPMENT_ALLOWED_LINES", value: "cma cgm"},
                    {name: "SHIPMENT_AUDIT_SOURCE", value: "aws-ecs-cma-cgm-comparison"},
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
                    "awslogs-stream-prefix": "cma-comparison"
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
            comparisonSettings: [
              (.environment // [])[]
              | select(.name | test("^(AWS_REGION|CLICKUP_USE_TASK_STATUS|CMA_CGM_COMPARISON_|SHIPMENT_ALLOWED_LINES|SHIPMENT_AUDIT_SOURCE|SHIPMENT_COMMENT_ON_NO_CHANGE)"))
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
