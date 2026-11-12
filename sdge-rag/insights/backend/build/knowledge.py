"""Regenerates s3://<knowledge-bucket>/projectknowledge.md =
   source/narrative.md (hand-maintained design + change log)
 + an auto-generated "Live inventory" appendix built from CloudFormation, Lambda,
   Parameter Store, CloudWatch, CodePipeline and S3.
Triggered by: CloudFormation stack status changes (EventBridge), a 6-hour schedule,
and by the deploy scripts after each deployment."""
import json
from datetime import datetime, timezone

import boto3

import tools
from tools import ENV, INSIGHTS_ENV, KNOWLEDGE_BUCKET, KNOWLEDGE_KEY, REGION, log, redact

lam = boto3.client("lambda", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)

FUNCTIONS = [f"sdge-rag-{ENV}-{n}" for n in ("dbinit", "presignup", "postconfirm")] + \
            [f"sdge-insights-{INSIGHTS_ENV}-{n}" for n in ("data", "knowledge")]


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_live_section():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inv = tools.list_resources()
    parts = [f"# Appendix: Live inventory (auto-generated)\n\nGenerated {now} by the `sdge-insights-{INSIGHTS_ENV}-knowledge` "
             "Lambda from the AWS APIs. Values here reflect what is actually deployed right now.\n"]

    t = inv["totals"]
    parts.append(f"## Totals\n\n- CloudFormation stacks: **{t['stacks']}**\n- Resources managed by CloudFormation: "
                 f"**{t['resources']}**\n- Distinct CloudFormation resource types: **{t['distinctResourceTypes']}**\n"
                 f"- AWS services represented ({len(t['awsServices'])}): {', '.join(t['awsServices'])}\n"
                 f"- Also used but not managed by these stacks: " + "; ".join(inv["notInStacks"]) + "\n")
    parts.append("## Stacks\n\n" + _table(["Stack", "Region", "Status", "Last updated", "Resources"],
                 [[s["stack"], s["region"], s["status"], s["lastUpdated"][:19], s["resourceCount"]] for s in inv["stacks"]]))
    parts.append("## Resource count by type (all stacks)\n\n" + _table(["Resource type", "Count"], list(t["byType"].items())))
    for s in inv["stacks"]:
        parts.append(f"### {s['stack']} ({s['region']})\n\n" + _table(["Resource type", "Count"], list(s["resourceTypes"].items())))

    rows = []
    for fn in FUNCTIONS:
        try:
            c = lam.get_function_configuration(FunctionName=fn)
        except lam.exceptions.ResourceNotFoundException:
            continue
        rows.append([fn, c.get("Runtime"), c.get("Handler"), c.get("MemorySize"), c.get("Timeout"),
                     (c.get("TracingConfig") or {}).get("Mode"), c.get("LastModified", "")[:19]])
    parts.append("## Lambda functions\n\n" + _table(["Function", "Runtime", "Handler", "Memory MB", "Timeout s", "X-Ray", "Last modified"], rows))
    try:
        fg = tools.get_fargate_status()
        parts.append("## ECS on Fargate (cluster " + fg["cluster"] + ")\n\n" + _table(
            ["Service", "Desired", "Running", "vCPU", "Memory GiB", "Arch", "Image", "Exposed via"],
            [[x["service"], x["desired"], x["running"], x["vCPU"], x["memoryGiB"], x["architecture"], x["image"], x["exposedVia"]]
             for x in fg["services"]]) + "\n\nThe document **ingest** worker is not a service: EventBridge starts one "
            "Fargate task (task definition `sdge-rag-" + ENV + "-ingest`, 1 vCPU / 2 GiB, arm64) per uploaded document.")
    except Exception:  # noqa: BLE001
        log.exception("fargate section failed")

    params = []
    for prefix in (f"/sdge-rag/{ENV}/", f"/sdge-insights/{INSIGHTS_ENV}/"):
        for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=prefix, Recursive=True):
            for p in page["Parameters"]:
                params.append([p["Name"], p["Value"] if p["Type"] == "String" else "(SecureString — not shown)"])
    parts.append("## Runtime configuration (Parameter Store)\n\n" + _table(["Parameter", "Value"], params))

    try:
        ms = tools.get_model_selection()
        ev = ms.get("lastEvaluation") or {}
        w = ms.get("weights") or {}
        parts.append(f"## Model Lab: current LLM selection (AWS AppConfig)\n\n- **Live chat model:** {ms.get('activeModelName')} "
                     f"(`{ms.get('activeModelId')}`)\n- **Weights:** quality {w.get('quality')}%, cost {w.get('cost')}%, latency {w.get('latency')}%\n"
                     f"- **Last evaluation:** {ev.get('at')} by {ev.get('by')} ({(ev.get('decision') or {}).get('reason', '')})\n"
                     f"- **Measured at:** {ev.get('measuredAt')}\n")
        if ev.get("ranking"):
            parts.append(_table(["Rank", "Model", "Vendor", "Score", "Quality", "Cost", "Latency", "Measured latency (ms)", "$ / 1k questions", "Eligible"],
                         [[r["rank"], r["name"], r["vendor"], r["composite"], r["scores"]["quality"], r["scores"]["cost"],
                           r["scores"]["latency"], r["latencyMs"], r["costPer1kQuestionsUsd"],
                           "yes" if r["eligible"] else "no: " + "; ".join(r["ineligibleReasons"])] for r in ev["ranking"]]))
        parts.append("### Model catalog\n\n" + _table(["Model", "Vendor", "Bedrock ID", "Quality prior", "$ in / out per 1M", "Reasoning", "Strengths"],
                     [[m["name"], m["vendor"], f"`{m['id']}`", m["qualityPrior"], f"{m['pricePer1M']['input']} / {m['pricePer1M']['output']}",
                       m["reasoning"], m["strengths"]] for m in ms.get("catalog", [])]))
        if ms.get("history"):
            parts.append("### Recent Model Lab changes\n\n" + _table(["When", "By", "Weights Q/C/L", "Action", "From", "To"],
                         [[h["at"], h["by"], f"{h['weights']['quality']}/{h['weights']['cost']}/{h['weights']['latency']}", h["action"],
                           h.get("fromName"), h.get("toName")] for h in ms["history"]]))
    except Exception:  # noqa: BLE001
        log.exception("model lab section failed")
    try:
        kbs = tools.get_knowledge_bases()["knowledgeBases"]
        parts.append("## Amazon Bedrock Knowledge Bases\n\n" + _table(
            ["Name", "ID", "Status", "Vector store", "Embedding model", "Last sync", "Last sync stats"],
            [[k["name"], k["id"], k["status"], k["vectorStore"], k["embeddingModel"],
              f"{k['recentSyncs'][0]['started']} {k['recentSyncs'][0]['status']}" if k["recentSyncs"] else "never",
              ", ".join(f"{a}={b}" for a, b in (k["recentSyncs"][0]["stats"] if k["recentSyncs"] else {}).items())] for k in kbs]))
    except Exception:  # noqa: BLE001
        log.exception("kb section failed")
    try:
        al = tools.get_alarms()["alarms"]
        parts.append("## CloudWatch alarms (current state)\n\n" + _table(["Alarm", "State", "Metric", "Threshold"],
                     [[a["name"], a["state"], a["metric"], a["threshold"]] for a in al]))
    except Exception:  # noqa: BLE001
        log.exception("alarms section failed")
    try:
        pr = tools.get_pipeline_runs(limit=5)
        parts.append(f"## CI/CD pipeline `{pr['pipeline']}` — recent runs\n\n" + _table(["Execution", "Status", "Started", "Trigger"],
                     [[r["id"][:8], r["status"], r["start"][:19], r["trigger"]] for r in pr["runs"]]))
    except Exception:  # noqa: BLE001
        log.exception("pipeline section failed")
    try:
        dl = tools.get_document_library()
        parts.append(f"## Gas Ops knowledge-base documents ({dl['count']})\n\n" + _table(["Document", "Bytes", "Uploaded"],
                     [[d["name"], d["bytes"], d["uploaded"][:19]] for d in dl["documents"]]))
    except Exception:  # noqa: BLE001
        log.exception("documents section failed")
    return "\n\n".join(parts) + "\n"


def handler(event, context):
    s3 = tools.s3
    try:
        narrative = s3.get_object(Bucket=KNOWLEDGE_BUCKET, Key="source/narrative.md")["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        log.warning("narrative not uploaded yet; generating live appendix only")
        narrative = "# Project Knowledge\n\n(Design narrative is being uploaded by the deploy script.)"
    live = build_live_section()
    doc = redact(narrative.rstrip() + "\n\n---\n\n" + live)
    s3.put_object(Bucket=KNOWLEDGE_BUCKET, Key=KNOWLEDGE_KEY, Body=doc.encode(),
                  ContentType="text/markdown; charset=utf-8", CacheControl="no-cache")
    # copy into the project-corpus Knowledge Base prefix and start an incremental sync
    s3.put_object(Bucket=KNOWLEDGE_BUCKET, Key="kb/design/projectknowledge.md", Body=doc.encode(),
                  ContentType="text/markdown; charset=utf-8")
    try:
        job = tools.sync_project_kb(f"knowledge regenerated ({(event or {}).get('detail-type', 'manual')})")
    except Exception:  # noqa: BLE001
        log.exception("project kb sync failed")
        job = None
    log.info("knowledge regenerated", extra={"bytes": len(doc), "trigger": (event or {}).get("detail-type", "manual"),
                                             "kbIngestionJob": job})
    return {"ok": True, "bytes": len(doc)}


if __name__ == "__main__":  # local preview
    print(json.dumps({"preview": build_live_section()[:2000]}))
