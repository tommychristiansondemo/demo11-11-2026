#!/usr/bin/env bash
# Create/update the CI/CD pipeline for an environment:
#   scripts/deploy-pipeline.sh <dev|staging|prod> [require-approval:true|false]
set -euo pipefail
ENV_NAME="${1:?usage: deploy-pipeline.sh <dev|staging|prod> [true|false]}"
APPROVAL="${2:-false}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${APP_REGION:-us-east-2}"
TOPIC=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-app" \
  --query "Stacks[0].Outputs[?OutputKey=='AlertTopicArn'].OutputValue" --output text 2>/dev/null || true)
[[ "$TOPIC" == "None" ]] && TOPIC=""
aws cloudformation deploy --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-pipeline" \
  --template-file "$ROOT/infra/pipeline.yaml" --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
  --tags app=sdge-rag env="$ENV_NAME" \
  --parameter-overrides EnvName="$ENV_NAME" RequireApproval="$APPROVAL" ApprovalTopicArn="$TOPIC"
