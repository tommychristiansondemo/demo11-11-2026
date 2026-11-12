"""Build the Class Insights project-corpus snapshot for the Bedrock Knowledge Base.

Copies the project's source code, IaC templates, scripts and docs into <out>/ as .txt/.md
(Bedrock KB parses .txt/.md/.html/.pdf/.docx/.csv/.xlsx, not .py/.yaml/.jsx), each prefixed with a
header naming the original path and component. Refuses to proceed if anything looks like a secret."""
import os
import re
import sys

root, out = sys.argv[1], sys.argv[2]
INCLUDE = [
    ("backend/src", (".py",), "Gas Ops API / ingest / Model Lab Lambda code"),
    ("frontend/src", (".jsx", ".js"), "Gas Ops React front end"),
    ("infra", (".yaml", ".json"), "Gas Ops CloudFormation templates and Model Lab catalog"),
    ("infra/env", (".env",), "Per-environment deployment parameters (no secrets)"),
    ("scripts", (".sh", ".py"), "Deploy / destroy / test scripts"),
    ("insights/backend/src", (".py",), "Class Insights Lambda code"),
    ("insights/frontend/src", (".jsx", ".js"), "Class Insights React front end"),
    ("insights/infra", (".yaml",), "Class Insights CloudFormation template"),
    ("insights", (".sh", ".py"), "Class Insights deploy tooling"),
    ("sample-docs", (".py",), "Sample document generator"),
    ("", (".yml", ".md"), "Build specs and README"),
]
SECRET = re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"                         # access key id
                    r"|aws_secret_access_key\s*=\s*[A-Za-z0-9/+]{40}"          # secret key value
                    r"|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"        # private key
                    r"|apiKey:\s*\"[A-Za-z0-9]{20,}\"")                       # rendered API key in config.js
count = 0
for rel, exts, component in INCLUDE:
    d = os.path.join(root, rel)
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        if not os.path.isfile(path) or name.startswith(".") or not name.endswith(exts) or name == "package-lock.json":
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        if SECRET.search(text):
            sys.exit(f"refusing to publish {rel}/{name}: looks like it contains a secret")
        relpath = f"{rel}/{name}" if rel else name
        dest = os.path.join(out, relpath + ("" if name.endswith(".md") else ".txt"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(f"File: {relpath}\nComponent: {component}\nProject: Gas Ops Knowledge Assistant + Class Insights (AWS class demo)\n\n{text}")
        count += 1
print(f"{count} files in corpus")
