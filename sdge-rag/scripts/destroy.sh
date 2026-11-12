#!/usr/bin/env bash
# Tear down one environment:  scripts/destroy.sh <dev|staging|prod>
set -euo pipefail
ENV_NAME="${1:?usage: destroy.sh <dev|staging|prod>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/infra/env/${ENV_NAME}.env"
export AWS_PAGER=""
REGION="${APP_REGION:-us-east-2}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

empty_bucket() {   # delete all objects and versions
  local b=$1
  aws s3api head-bucket --bucket "$b" 2>/dev/null || return 0
  python3 - "$b" "$REGION" <<'PY'
import json, subprocess, sys
b, region = sys.argv[1], sys.argv[2]
while True:
    out = json.loads(subprocess.check_output(["aws", "s3api", "list-object-versions", "--bucket", b, "--region", region, "--max-items", "1000", "--output", "json"]) or "{}")
    objs = [{"Key": o["Key"], "VersionId": o["VersionId"]} for o in (out.get("Versions") or []) + (out.get("DeleteMarkers") or [])]
    if not objs: break
    subprocess.check_call(["aws", "s3api", "delete-objects", "--bucket", b, "--region", region, "--delete", json.dumps({"Objects": objs, "Quiet": True})])
PY
}

for b in site docs source pipeline; do empty_bucket "sdge-rag-${ENV_NAME}-${b}-${ACCOUNT}-${REGION}"; done
for b in site knowledge; do empty_bucket "sdge-insights-${ENV_NAME}-${b}-${ACCOUNT}-${REGION}"; done
# Class Insights first (it depends on the Gas Ops user pool, VPC and ECS cluster)
aws cloudformation delete-stack --region "$REGION" --stack-name "sdge-insights-${ENV_NAME}-app" || true
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "sdge-insights-${ENV_NAME}-app" || true
aws cloudformation delete-stack --region us-east-1 --stack-name "sdge-insights-${ENV_NAME}-edge" || true
for s in pipeline app containers; do   # containers last: ECR repos (EmptyOnDelete) + image builder
  aws cloudformation delete-stack --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-${s}" || true
  aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-${s}" || true
done
aws cloudformation delete-stack --region us-east-1 --stack-name "sdge-rag-${ENV_NAME}-edge"
aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name "sdge-rag-${ENV_NAME}-edge"
aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name "sdge-insights-${ENV_NAME}-edge" || true

# ACM leaves its DNS validation CNAME behind; remove it.
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$HOSTED_ZONE_NAME" --query "HostedZones[0].Id" --output text)
aws route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --query "ResourceRecordSets[?Type=='CNAME' && starts_with(Name, '_') && (ends_with(Name, '.${DOMAIN_NAME}.') || ends_with(Name, '.${INSIGHTS_DOMAIN}.'))]" --output json |
python3 -c "
import json,sys,subprocess
for r in json.load(sys.stdin):
    subprocess.check_call(['aws','route53','change-resource-record-sets','--hosted-zone-id','$ZONE_ID','--change-batch',json.dumps({'Changes':[{'Action':'DELETE','ResourceRecordSet':r}]})])
    print('deleted', r['Name'])"
# Final DB snapshot (prod DeletionPolicy=Snapshot) is retained; delete manually if not needed:
aws rds describe-db-cluster-snapshots --region "$REGION" --snapshot-type manual \
  --query "DBClusterSnapshots[?DBClusterIdentifier=='sdge-rag-${ENV_NAME}'].DBClusterSnapshotIdentifier" --output text
echo "Environment ${ENV_NAME} removed. (Artifacts bucket sdge-rag-artifacts-${ACCOUNT}-${REGION} is shared and kept.)"
