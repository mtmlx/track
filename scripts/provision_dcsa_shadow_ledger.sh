#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-us-east-2}"
STACK_NAME="${DCSA_TNT_SHADOW_STACK_NAME:-track-trace-dcsa-shadow-ledger}"
TABLE_NAME="${DCSA_TNT_LEDGER_TABLE:-track-trace-dcsa-events}"
TASK_ROLE_NAME="${DCSA_TNT_SHADOW_TASK_ROLE_NAME:-TrackTraceDcsaShadowTaskRole}"
LOG_GROUP_NAME="${DCSA_TNT_SHADOW_LOG_GROUP_NAME:-/ecs/track-trace/dcsa-shadow}"

usage() {
  cat <<'USAGE'
Usage: scripts/provision_dcsa_shadow_ledger.sh [options]

Creates or updates only the isolated DynamoDB event ledger, dedicated ECS task
role, and DCSA shadow log group. It does not create an ECS task or schedule.

Options:
  --region REGION
  --stack-name NAME
  --table-name NAME
  --task-role-name NAME
  --log-group-name NAME
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --table-name) TABLE_NAME="$2"; shift 2 ;;
    --task-role-name) TASK_ROLE_NAME="$2"; shift 2 ;;
    --log-group-name) LOG_GROUP_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$ROOT_DIR/deploy/aws-ecs/dcsa-shadow-ledger.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    "TableName=$TABLE_NAME" \
    "TaskRoleName=$TASK_ROLE_NAME" \
    "LogGroupName=$LOG_GROUP_NAME"

aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs' \
  --output table
