#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
CLUSTER="${CMA_CGM_COMPARISON_CLUSTER:-track-trace-prod}"
TASK_DEFINITION="${CMA_CGM_COMPARISON_TASK_DEFINITION:-track-trace-cma-cgm-comparison}"
NETWORK_SOURCE_SCHEDULE="${CMA_CGM_COMPARISON_NETWORK_SOURCE_SCHEDULE:-track-trace-cma-cgm-every-6h}"
DRY_RUN=false

usage() {
  cat <<'USAGE'
Usage: scripts/run_cma_cgm_comparison_canary.sh [--dry-run]

Runs exactly one bounded CMA legacy-versus-DCSA comparison task using the
existing CMA worker's network configuration. The task definition enforces the
read-only comparison mode; this script neither creates nor changes a schedule.
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
  --started-by cma-cgm-comparison-canary \
  --network-configuration "$network_configuration" \
  --enable-ecs-managed-tags \
  --propagate-tags TASK_DEFINITION \
  --query 'tasks[0].taskArn' \
  --output text)"

if [[ -z "$task_arn" || "$task_arn" == "None" ]]; then
  echo "ECS did not start the CMA comparison canary." >&2
  exit 1
fi

printf 'Started CMA comparison canary: %s\n' "$task_arn"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$task_arn"
aws ecs describe-tasks \
  --region "$REGION" \
  --cluster "$CLUSTER" \
  --tasks "$task_arn" \
  --query 'tasks[0].{LastStatus:lastStatus,StopCode:stopCode,StoppedReason:stoppedReason,Containers:containers[*].{Name:name,ExitCode:exitCode,Reason:reason,LogStream:logStreamName}}' \
  --output json
