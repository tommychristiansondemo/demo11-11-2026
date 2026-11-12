#!/usr/bin/env bash
# Package the repo and upload it to the pipeline's source bucket, which
# triggers CodePipeline:  scripts/push-source.sh <dev|staging|prod>
set -euo pipefail
ENV_NAME="${1:?usage: push-source.sh <dev|staging|prod>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${APP_REGION:-us-east-2}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
TMP=$(mktemp -d)
python3 - "$ROOT" "$TMP/source.zip" <<'PY'
import os, sys, zipfile
root, out = sys.argv[1], sys.argv[2]
skip = ("frontend/node_modules", "frontend/dist", "backend/build", ".git", "insights/frontend/node_modules", "insights/frontend/dist", "insights/backend/build")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        dirs[:] = [x for x in dirs if x != "__pycache__" and not os.path.join(rel, x).lstrip("./").startswith(skip)]
        for f in files:
            if f.startswith((".packaged-", ".narrative.rendered", ".knowledge-result")):
                continue
            full = os.path.join(d, f)
            info = zipfile.ZipInfo.from_file(full, os.path.relpath(full, root))
            with open(full, "rb") as fh:
                z.writestr(info, fh.read(), zipfile.ZIP_DEFLATED)
PY
aws s3 cp "$TMP/source.zip" "s3://sdge-rag-${ENV_NAME}-source-${ACCOUNT}-${REGION}/source.zip" --region "$REGION"
rm -rf "$TMP"
echo "Uploaded; pipeline sdge-rag-${ENV_NAME} will start automatically."
