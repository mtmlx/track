#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
CLUSTER="${DCSA_CMA_SHADOW_CLUSTER:-track-trace-prod}"
TASK_DEFINITION="${DCSA_CMA_SHADOW_TASK_DEFINITION:-track-trace-dcsa-cma-shadow}"
NETWORK_SOURCE_SCHEDULE="${DCSA_CMA_SHADOW_NETWORK_SOURCE_SCHEDULE:-track-trace-cma-cgm-every-6h}"
DRY_RUN=false

usage() {
  cat <<'USAGE'
Usage: scripts/run_dcsa_cma_shadow_canary.sh [--dry-run]

Runs exactly one bounded CMA DCSA shadow task using the existing CMA worker's
network configuration. The task definition itself enforces DCSA shadow mode;
this script neither creates nor changes any schedule.
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

network_configuration="$(aws scheduler get-schedule \
  --region "$REGION" \
  --name "$NETWORK_SOURCE_SCHEDULE" \
  --query 'Target.EcsParameters.NetworkConfiguration' \
  --output json \
  | jq -ce '
      .awsvpcConfiguration as $source
      | {
          awsvpcConfiguration: {
            assignPublicIp: ($source.AssignPublicIp // error("missing AssignPublicIp")),
            securityGroups: ($source.SecurityGroups // error("missing SecurityGroups")),
            subnets: ($source.Subnets // error("missing Subnets"))
          }
        }
      | if ((.awsvpcConfiguration.securityGroups | length) == 0 or (.awsvpcConfiguration.subnets | length) == 0) then
          error("at least one security group and subnet are required")
        else . end
    ')"

if [[ -z "$network_configuration" || "$network_configuration" == "null" ]]; then
  echo "Could not load the CMA worker network configuration from $NETWORK_SOURCE_SCHEDULE." >&2
  exit 1
fi

if [[ "$DRY_RUN" == "true" ]]; then
  jq '{awsvpcConfiguration: {assignPublicIp: .awsvpcConfiguration.assignPublicIp, subnetCount: (.awsvpcConfiguration.subnets | length), securityGroupCount: (.awsvpcConfiguration.securityGroups | length)}}' <<<"$network_configuration"
  exit 0
fi

task_arn="$(aws ecs run-task \
  --region "$REGION" \
  --cluster "$CLUSTER" \
  --launch-type FARGATE \
  --platform-version LATEST \
  --task-definition "$TASK_DEFINITION" \
  --count 1 \
  --started-by dcsa-cma-shadow-canary \
  --network-configuration "$network_configuration" \
  --enable-ecs-managed-tags \
  --propagate-tags TASK_DEFINITION \
  --query 'tasks[0].taskArn' \
  --output text)"

if [[ -z "$task_arn" || "$task_arn" == "None" ]]; then
  echo "ECS did not start the CMA DCSA shadow canary." >&2
  exit 1
fi

printf 'Started CMA DCSA shadow canary: %s\n' "$task_arn"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$task_arn"
aws ecs describe-tasks \
  --region "$REGION" \
  --cluster "$CLUSTER" \
  --tasks "$task_arn" \
  --query 'tasks[0].{LastStatus:lastStatus,StopCode:stopCode,StoppedReason:stoppedReason,Containers:containers[*].{Name:name,ExitCode:exitCode,Reason:reason,LogStream:logStreamName}}' \
  --output json
