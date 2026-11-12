#!/usr/bin/env bash
# Build/push the Fargate container images with CodeBuild (arm64), no local Docker needed.
#   scripts/build-images.sh <dev|staging|prod>
# Images are tagged with a content hash of their build inputs, so unchanged code is never rebuilt
# and every deploy references an immutable tag. Writes infra/.images-<env>.env for the deploy scripts.
set -euo pipefail
ENV_NAME="${1:?usage: scripts/build-images.sh <dev|staging|prod>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/infra/env/${ENV_NAME}.env"
export AWS_PAGER=""
REGION="${APP_REGION:-us-east-2}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS="sdge-rag-artifacts-${ACCOUNT}-${REGION}"
ROLE_ARGS=(); [[ -n "${CFN_ROLE_ARN:-}" ]] && ROLE_ARGS=(--role-arn "$CFN_ROLE_ARN")
log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

log "Container build stack (ECR repositories + Graviton CodeBuild image builder)"
aws s3api head-bucket --bucket "$ARTIFACTS" >/dev/null 2>&1 || aws s3 mb "s3://$ARTIFACTS" --region "$REGION"
aws cloudformation deploy --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-containers" \
  --template-file "$ROOT/infra/containers.yaml" --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset "${ROLE_ARGS[@]}" \
  --tags app=sdge-rag env="$ENV_NAME" --parameter-overrides EnvName="$ENV_NAME" ArtifactsBucket="$ARTIFACTS"
cout() { aws cloudformation describe-stacks --region "$REGION" --stack-name "sdge-rag-${ENV_NAME}-containers" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
PROJECT=$(cout ImageBuilderProject)

# name|dockerfile|repo uri|inputs that affect the image
IMAGES=(
  "app|backend/Dockerfile|$(cout AppRepositoryUri)|backend/Dockerfile backend/requirements.txt backend/requirements-container.txt backend/src"
  "insights|insights/backend/Dockerfile|$(cout InsightsRepositoryUri)|insights/backend/Dockerfile backend/requirements-container.txt backend/src/runtime.py insights/backend/src"
)
declare -A URI BUILD
for spec in "${IMAGES[@]}"; do
  IFS='|' read -r name dockerfile repo inputs <<<"$spec"
  tag=$(cd "$ROOT" && find $inputs -type f -not -name '*.pyc' -not -path '*/__pycache__/*' | sort | xargs sha256sum | sha256sum | cut -c1-16)
  URI[$name]="$repo:$tag"
  if aws ecr describe-images --region "$REGION" --repository-name "${repo#*/}" --image-ids imageTag="$tag" >/dev/null 2>&1; then
    echo "$name: $tag already in ECR, skipping build"
    continue
  fi
  zipkey="image-context/${name}-${tag}.zip"
  (cd "$ROOT" && python3 - "$ROOT/.ctx-$name.zip" backend/Dockerfile backend/requirements.txt backend/requirements-container.txt \
      backend/src insights/backend/Dockerfile insights/backend/src <<'PY'
import os, sys, zipfile
out, paths = sys.argv[1], sys.argv[2:]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in paths:
        for f in ([p] if os.path.isfile(p) else [os.path.join(d, n) for d, _, ns in os.walk(p) for n in ns]):
            if "__pycache__" not in f and not f.endswith(".pyc"):
                z.write(f, f)
PY
  )
  aws s3 cp "$ROOT/.ctx-$name.zip" "s3://$ARTIFACTS/$zipkey" --only-show-errors && rm -f "$ROOT/.ctx-$name.zip"
  BUILD[$name]=$(aws codebuild start-build --region "$REGION" --project-name "$PROJECT" \
    --source-location-override "$ARTIFACTS/$zipkey" \
    --environment-variables-override name=REPO_URI,value="$repo" name=IMAGE_TAG,value="$tag" name=DOCKERFILE,value="$dockerfile" \
    --query build.id --output text)
  echo "$name: building $tag (${BUILD[$name]})"
done

for name in "${!BUILD[@]}"; do
  while :; do
    st=$(aws codebuild batch-get-builds --region "$REGION" --ids "${BUILD[$name]}" --query "builds[0].buildStatus" --output text)
    [[ $st != IN_PROGRESS ]] && break
    sleep 10
  done
  echo "$name build: $st"
  [[ $st == SUCCEEDED ]] || { echo "image build failed: see CodeBuild ${BUILD[$name]}"; exit 1; }
done

cat > "$ROOT/infra/.images-${ENV_NAME}.env" <<OUT
APP_IMAGE_URI=${URI[app]}
INSIGHTS_IMAGE_URI=${URI[insights]}
OUT
cat "$ROOT/infra/.images-${ENV_NAME}.env"
