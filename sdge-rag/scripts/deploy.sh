#!/usr/bin/env bash
# Deploy one environment end-to-end:  scripts/deploy.sh <dev|staging|prod>
# Used both locally and by the CodePipeline build stage (buildspec.yml).
# Optional env: CFN_ROLE_ARN (CloudFormation service role, set by the pipeline)
set -euo pipefail
ENV_NAME="${1:?usage: deploy.sh <dev|staging|prod>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1090
source "$ROOT/infra/env/${ENV_NAME}.env"
export AWS_PAGER=""
REGION="${APP_REGION:-us-east-2}"     # everything that can live outside us-east-1
EDGE_REGION="us-east-1"               # CloudFront ACM cert + WAF must be here
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS="sdge-rag-artifacts-${ACCOUNT}-${REGION}"
ROLE_ARGS=(); [[ -n "${CFN_ROLE_ARN:-}" ]] && ROLE_ARGS=(--role-arn "$CFN_ROLE_ARN")
TAGS=(--tags app=sdge-rag env="$ENV_NAME")
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$HOSTED_ZONE_NAME" \
  --query "HostedZones[?Name=='${HOSTED_ZONE_NAME}.'].Id | [0]" --output text | sed 's|/hostedzone/||')
[[ "$ZONE_ID" == "None" || -z "$ZONE_ID" ]] && { echo "Hosted zone $HOSTED_ZONE_NAME not found"; exit 1; }

log "Artifacts bucket $ARTIFACTS"
if ! aws s3api head-bucket --bucket "$ARTIFACTS" 2>/dev/null; then
  aws s3api create-bucket --bucket "$ARTIFACTS" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$ARTIFACTS" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-lifecycle-configuration --bucket "$ARTIFACTS" --lifecycle-configuration \
    '{"Rules":[{"ID":"expire","Status":"Enabled","Filter":{},"Expiration":{"Days":30}}]}'
fi

log "Container images (built on Graviton CodeBuild, tagged by content hash)"
"$ROOT/scripts/build-images.sh" "$ENV_NAME"
# shellcheck disable=SC1090
source "$ROOT/infra/.images-${ENV_NAME}.env"

log "Building Lambda package (Cognito triggers + DB schema custom resource still run on Lambda)"
rm -rf "$ROOT/backend/build" && mkdir -p "$ROOT/backend/build"
cp "$ROOT"/backend/src/*.py "$ROOT/backend/build/"
python3 -m pip install --quiet --no-compile --target "$ROOT/backend/build" -r "$ROOT/backend/requirements.txt"
find "$ROOT/backend/build" -name "__pycache__" -prune -exec rm -rf {} +

log "Edge stack (us-east-1): ACM certificate + CloudFront WAF"
aws cloudformation deploy --region "$EDGE_REGION" --stack-name "sdge-rag-${ENV_NAME}-edge" \
  --template-file "$ROOT/infra/edge.yaml" --no-fail-on-empty-changeset "${ROLE_ARGS[@]}" "${TAGS[@]}" \
  --parameter-overrides EnvName="$ENV_NAME" DomainName="$DOMAIN_NAME" HostedZoneId="$ZONE_ID" \
    RateLimitPer5Min="$WAF_RATE_LIMIT" AllowedCidrs="${ALLOWED_CIDRS}"
edge_out() { aws cloudformation describe-stacks --region "$EDGE_REGION" --stack-name "sdge-rag-${ENV_NAME}-edge" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
CERT_ARN=$(edge_out CertificateArn); WEBACL_ARN=$(edge_out WebAclArn)

log "App stack ($REGION)"
aws cloudformation package --region "$REGION" --template-file "$ROOT/infra/app.yaml" \
  --s3-bucket "$ARTIFACTS" --s3-prefix "$ENV_NAME" --output-template-file "$ROOT/infra/.packaged-app.yaml" >/dev/null
aws cloudformation deploy --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-app" \
  --template-file "$ROOT/infra/.packaged-app.yaml" --capabilities CAPABILITY_IAM \
  --s3-bucket "$ARTIFACTS" --s3-prefix "$ENV_NAME/templates" \
  --no-fail-on-empty-changeset "${ROLE_ARGS[@]}" "${TAGS[@]}" \
  --parameter-overrides EnvName="$ENV_NAME" DomainName="$DOMAIN_NAME" HostedZoneId="$ZONE_ID" \
    CertificateArn="$CERT_ARN" WebAclArn="$WEBACL_ARN" \
    DbMinCapacity="$DB_MIN_CAPACITY" DbMaxCapacity="$DB_MAX_CAPACITY" \
    ApiRateLimit="$API_RATE_LIMIT" ApiBurstLimit="$API_BURST_LIMIT" ApiDailyQuota="$API_DAILY_QUOTA" \
    LogRetentionDays="$LOG_RETENTION_DAYS" AlertEmail="${ALERT_EMAIL}" SamlMetadataUrl="${SAML_METADATA_URL}" \
    AppImageUri="$APP_IMAGE_URI" ApiDesiredCount="${API_DESIRED_COUNT:-1}"
out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-app" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
log "Bedrock Knowledge Base: incremental sync of the document library"
KB_ID=$(out KnowledgeBaseId); KB_DS=$(out KnowledgeBaseDataSourceId)
if [[ $(aws bedrock-agent list-ingestion-jobs --region "$REGION" --knowledge-base-id "$KB_ID" --data-source-id "$KB_DS" \
      --filters attribute=STATUS,operator=EQ,values=STARTING,IN_PROGRESS --query "length(ingestionJobSummaries)" --output text) == "0" ]]; then
  aws bedrock-agent start-ingestion-job --region "$REGION" --knowledge-base-id "$KB_ID" --data-source-id "$KB_DS" \
    --description "deploy.sh sync" --query "ingestionJob.[ingestionJobId,status]" --output text || true
else
  echo "An ingestion job is already running; skipping."
fi

log "Model Lab: seeding AppConfig (only if no version exists yet)"
AC_APP=$(out AppConfigApplicationId); AC_ENV=$(out AppConfigEnvironmentId)
AC_PROF=$(out ModelSelectionProfileId); AC_STRAT=$(out AppConfigStrategyId)
if [[ $(aws appconfig list-hosted-configuration-versions --region "$REGION" --application-id "$AC_APP" \
      --configuration-profile-id "$AC_PROF" --query "length(Items)" --output text) == "0" ]]; then
  SEED=$(mktemp); cp "$ROOT/infra/model-catalog.json" "$SEED"
  V=$(aws appconfig create-hosted-configuration-version --region "$REGION" --application-id "$AC_APP" \
      --configuration-profile-id "$AC_PROF" --content-type application/json --content "fileb://$SEED" \
      --description "Initial catalog and 50/25/25 weights" "$SEED.out" --query VersionNumber --output text)
  aws appconfig start-deployment --region "$REGION" --application-id "$AC_APP" --environment-id "$AC_ENV" \
    --deployment-strategy-id "$AC_STRAT" --configuration-profile-id "$AC_PROF" --configuration-version "$V" \
    --description "Seed" --query DeploymentNumber --output text
  rm -f "$SEED" "$SEED.out"
  # measure-only evaluation (one-off Fargate task) so the ranking shows real data from the start
  aws ecs run-task --region "$REGION" --cluster "$(out EcsClusterName)" --task-definition "$(out IngestTaskFamily)" \
    --launch-type FARGATE --started-by deploy-seed \
    --network-configuration "awsvpcConfiguration={subnets=[$(out TaskSubnetIds)],securityGroups=[$(out IngestSecurityGroupId)],assignPublicIp=ENABLED}" \
    --overrides '{"containerOverrides":[{"name":"ingest","command":["python","seed_model_lab.py"]}]}' \
    --query "tasks[0].taskArn" --output text || true
else
  echo "AppConfig already has versions; leaving live Model Lab state untouched."
fi

API_KEY=$(aws apigateway get-api-key --region "$REGION" --api-key "$(out ApiKeyId)" --include-value --query value --output text)

log "Building and publishing frontend"
cd "$ROOT/frontend"
npm ci --silent --no-audit --no-fund
npx vite build --logLevel warn
cat > dist/config.js <<CFG
window.APP_CONFIG = {
  env: "${ENV_NAME}",
  region: "${REGION}",
  userPoolId: "$(out UserPoolId)",
  clientId: "$(out UserPoolClientId)",
  apiKey: "${API_KEY}",
  cognitoDomain: "$(out CognitoDomain)",
  ssoProvider: "$(out SsoProviderName)",
  insightsUrl: "${INSIGHTS_DOMAIN:+https://${INSIGHTS_DOMAIN}}"
};
CFG
SITE=$(out SiteBucketName)
aws s3 sync dist/assets "s3://$SITE/assets" --delete --cache-control "public,max-age=31536000,immutable" --only-show-errors
aws s3 sync dist "s3://$SITE" --exclude "assets/*" --delete --cache-control "no-cache" --only-show-errors
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" --query Invalidation.Id --output text

# Refresh the Class Insights knowledge file (projectknowledge.md) if that app is deployed
if aws lambda get-function --region "$REGION" --function-name "sdge-insights-${ENV_NAME}-knowledge" >/dev/null 2>&1; then
  log "Refreshing Class Insights knowledge file"
  aws lambda invoke --region "$REGION" --function-name "sdge-insights-${ENV_NAME}-knowledge" \
    --cli-read-timeout 300 /dev/null --query StatusCode --output text || true
fi

log "Done: $(out SiteUrl)"
echo "Dashboard: $(out DashboardUrl)"
