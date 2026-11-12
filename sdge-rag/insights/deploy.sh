#!/usr/bin/env bash
# Deploy the Class Insights companion app:  insights/deploy.sh <dev|staging|prod>
# Requires the Gas Ops app stack (sdge-rag-<env>-app) to exist (it trusts that Cognito user pool).
set -euo pipefail
ENV_NAME="${1:?usage: insights/deploy.sh <dev|staging|prod>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$ROOT/infra/env/${ENV_NAME}.env"
export AWS_PAGER=""
REGION="${APP_REGION:-us-east-2}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS="sdge-rag-artifacts-${ACCOUNT}-${REGION}"
ROLE_ARGS=(); [[ -n "${CFN_ROLE_ARN:-}" ]] && ROLE_ARGS=(--role-arn "$CFN_ROLE_ARN")
log() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$HOSTED_ZONE_NAME" \
  --query "HostedZones[?Name=='${HOSTED_ZONE_NAME}.'].Id | [0]" --output text | sed 's|/hostedzone/||')
main_out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-app" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

log "Container images (shared builder; skips unchanged images)"
"$ROOT/scripts/build-images.sh" "$ENV_NAME"
# shellcheck disable=SC1090
source "$ROOT/infra/.images-${ENV_NAME}.env"

log "Building Lambda package"
rm -rf "$HERE/backend/build" && mkdir -p "$HERE/backend/build" && cp "$HERE"/backend/src/*.py "$HERE/backend/build/"

log "Rendering knowledge narrative (verbatim prompts from source)"
python3 - "$ROOT" "$HERE/.narrative.rendered.md" <<'PY'
import re, sys
root, out = sys.argv[1], sys.argv[2]
main = re.search(r'SYSTEM_PROMPT = """(.*?)"""', open(f"{root}/backend/src/prompts.py").read(), re.S).group(1)
ins = re.search(r'SYSTEM_TEMPLATE = """(.*?)"""', open(f"{root}/insights/backend/src/chat.py").read(), re.S).group(1)
n = open(f"{root}/insights/knowledge/narrative.md").read()
open(out, "w").write(n.replace("{{MAIN_SYSTEM_PROMPT}}", main.strip()).replace("{{INSIGHTS_SYSTEM_PROMPT}}", ins.strip()))
PY

log "Edge stack (us-east-1)"
aws cloudformation deploy --region us-east-1 --stack-name "sdge-insights-${ENV_NAME}-edge" \
  --template-file "$ROOT/infra/edge.yaml" --no-fail-on-empty-changeset "${ROLE_ARGS[@]}" --tags app=sdge-insights env="$ENV_NAME" \
  --parameter-overrides AppName=sdge-insights EnvName="$ENV_NAME" DomainName="$INSIGHTS_DOMAIN" HostedZoneId="$ZONE_ID" \
    RateLimitPer5Min="$WAF_RATE_LIMIT" AllowedCidrs="${ALLOWED_CIDRS}"
edge() { aws cloudformation describe-stacks --region us-east-1 --stack-name "sdge-insights-${ENV_NAME}-edge" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

log "App stack ($REGION)"
aws cloudformation package --region "$REGION" --template-file "$HERE/infra/insights.yaml" --s3-bucket "$ARTIFACTS" \
  --s3-prefix "insights-${ENV_NAME}" --output-template-file "$HERE/infra/.packaged-insights.yaml" >/dev/null
aws cloudformation deploy --region "$REGION" --stack-name "sdge-insights-${ENV_NAME}-app" \
  --template-file "$HERE/infra/.packaged-insights.yaml" --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset "${ROLE_ARGS[@]}" \
  --s3-bucket "$ARTIFACTS" --s3-prefix "insights-${ENV_NAME}/templates" \
  --tags app=sdge-insights env="$ENV_NAME" \
  --parameter-overrides EnvName="$ENV_NAME" TargetEnv="$ENV_NAME" DomainName="$INSIGHTS_DOMAIN" HostedZoneId="$ZONE_ID" \
    CertificateArn="$(edge CertificateArn)" WebAclArn="$(edge WebAclArn)" \
    UserPoolId="$(main_out UserPoolId)" UserPoolClientId="$(main_out UserPoolClientId)" \
    ChatModelId="$CHAT_MODEL_ID" LogRetentionDays="$LOG_RETENTION_DAYS" \
    ChatImageUri="$INSIGHTS_IMAGE_URI" VpcId="$(main_out VpcId)" TaskSubnetIds="$(main_out TaskSubnetIds)" \
    EcsClusterName="$(main_out EcsClusterName)"
out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "sdge-insights-${ENV_NAME}-app" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

log "Publishing projectknowledge.md"
KB=$(out KnowledgeBucketName)
aws s3 cp "$HERE/.narrative.rendered.md" "s3://$KB/source/narrative.md" --content-type "text/markdown; charset=utf-8" --only-show-errors
log "Project-corpus Knowledge Base: publishing source snapshot to s3://$KB/kb/source/"
CORPUS=$(mktemp -d)
python3 "$HERE/kb_corpus.py" "$ROOT" "$CORPUS"
aws s3 sync "$CORPUS" "s3://$KB/kb/source" --delete --only-show-errors
rm -rf "$CORPUS"
# the knowledge Lambda writes kb/design/projectknowledge.md and starts the KB ingestion (sync) job
aws lambda invoke --region "$REGION" --function-name "sdge-insights-${ENV_NAME}-knowledge" --cli-read-timeout 300 \
  "$HERE/.knowledge-result.json" --query StatusCode --output text
cat "$HERE/.knowledge-result.json"; echo

log "Building and publishing SPA"
cd "$HERE/frontend"
npm ci --silent --no-audit --no-fund
npx vite build --logLevel warn
cat > dist/config.js <<CFG
window.APP_CONFIG = {
  env: "${ENV_NAME}",
  region: "${REGION}",
  clientId: "$(main_out UserPoolClientId)",
  mainAppUrl: "https://${DOMAIN_NAME}"
};
CFG
SITE=$(out SiteBucketName)
aws s3 sync dist/assets "s3://$SITE/assets" --delete --cache-control "public,max-age=31536000,immutable" --only-show-errors
aws s3 sync dist "s3://$SITE" --exclude "assets/*" --delete --cache-control "no-cache" --only-show-errors
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" --query Invalidation.Id --output text
log "Done: $(out SiteUrl)"
