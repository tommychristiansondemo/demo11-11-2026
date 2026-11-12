"""Class Insights chatbot: Claude on Bedrock answers questions about the class
deployment using projectknowledge.md (always in context) plus live, read-only
tools (logs, LLM traces, metrics, inventory, alarms, pipeline). Extended
thinking is enabled and every call is written to the log as an llm_trace."""
import json
import os
import time
import uuid

import boto3
from botocore.config import Config

import tools
from tools import log, redact

MODEL_ID = os.environ.get("CHAT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
THINKING_BUDGET = int(os.environ.get("THINKING_BUDGET", "1024"))
MAX_TOKENS = 2500
MAX_ROUNDS = 5
TIME_BUDGET_S = 22
bedrock = boto3.client("bedrock-runtime", config=Config(read_timeout=25, retries={"max_attempts": 2}))

SYSTEM_TEMPLATE = """You are "Class Insights", a teaching assistant for students in an AWS cloud architecture class.
Students are exploring a demo application deployed for the class: the "Gas Ops Knowledge Assistant", a RAG
(retrieval-augmented generation) Q&A app for gas-utility employees, plus this Class Insights app itself.

Your job: explain how everything was designed, built, deployed and operated, accurately and concretely.

Sources of truth:
1. PROJECT KNOWLEDGE (below): the maintained design and deployment document (projectknowledge.md). Its
   "Live inventory" section is regenerated automatically from CloudFormation whenever stacks change.
2. Tools: live, read-only data from AWS — CloudWatch logs, LLM prompt/reasoning traces, metrics, alarms,
   CloudFormation inventory, CodePipeline runs, the document list, Model Lab state and Bedrock Knowledge Base status.
   Use tools whenever a question is about current state, recent activity, counts that may have changed, errors, or
   "show me" requests.
3. search_project_kb: a Bedrock Knowledge Base over the project's real source code, CloudFormation templates and
   scripts. Use it to show actual code/template snippets (IAM policies, Lambda handlers, resource definitions);
   always name the file the snippet came from.

Guidelines:
- Prefer facts from the knowledge file and tools over general knowledge; say which you used.
- When presenting log or trace data, summarise first, then show the key records (Markdown tables or short code blocks).
- Never reveal secrets. API key values, passwords, tokens and secret values are deliberately unavailable; if asked,
  explain how they are stored and protected instead.
- If something is not in the knowledge file and cannot be fetched with a tool, say so plainly.
- Be educational: explain the "why" behind design decisions and trade-offs when relevant.
- Format answers in Markdown. Keep them focused.

==================== PROJECT KNOWLEDGE (projectknowledge.md) ====================
{knowledge}
==================== END PROJECT KNOWLEDGE ===================="""


def _resp(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, default=str)}


def _reasoning(content):
    return "".join(c["reasoningContent"]["reasoningText"].get("text", "")
                   for c in content if "reasoningContent" in c and "reasoningText" in c["reasoningContent"])


def handler(event, context):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims", {})
    user = claims.get("cognito:username", "unknown")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON"})
    question = (body.get("question") or "").strip()[:4000]
    if not question:
        return _resp(400, {"error": "question is required"})

    trace_id = uuid.uuid4().hex[:12]
    t0 = time.time()
    system = SYSTEM_TEMPLATE.format(knowledge=tools.get_knowledge())

    messages = []
    for turn in (body.get("history") or [])[-8:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            if messages and messages[-1]["role"] == turn["role"]:
                continue
            messages.append({"role": turn["role"], "content": [{"text": str(turn["content"])[:6000]}]})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    messages.append({"role": "user", "content": [{"text": question}]})
    first_prompt_index = len(messages) - 1

    rounds, tool_calls, usage_total = [], [], {"inputTokens": 0, "outputTokens": 0}
    answer, stop = "", None
    try:
        for rnd in range(MAX_ROUNDS):
            req = dict(modelId=MODEL_ID, system=[{"text": system}], messages=messages,
                       toolConfig={"tools": tools.TOOL_SPECS},
                       inferenceConfig={"maxTokens": MAX_TOKENS + THINKING_BUDGET},
                       additionalModelRequestFields={"thinking": {"type": "enabled", "budget_tokens": THINKING_BUDGET}})
            t1 = time.time()
            out = bedrock.converse(**req)
            msg = out["output"]["message"]
            stop = out.get("stopReason")
            u = out.get("usage", {})
            usage_total["inputTokens"] += u.get("inputTokens", 0)
            usage_total["outputTokens"] += u.get("outputTokens", 0)
            text = "".join(c.get("text", "") for c in msg["content"])
            uses = [c["toolUse"] for c in msg["content"] if "toolUse" in c]
            rounds.append({"round": rnd + 1, "reasoning": _reasoning(msg["content"]), "text": text,
                           "toolUses": [{"name": t["name"], "input": t.get("input")} for t in uses],
                           "stopReason": stop, "usage": u, "latencyMs": int((time.time() - t1) * 1000)})
            messages.append(msg)
            if stop != "tool_use" or not uses:
                answer = text
                break
            results = []
            for t in uses:
                ts = time.time()
                result = redact(tools.run_tool(t["name"], t.get("input")))
                rtext = json.dumps(result, default=str)
                if len(rtext) > 30000:
                    rtext = rtext[:30000] + '… [truncated]'
                tool_calls.append({"name": t["name"], "input": t.get("input"), "ms": int((time.time() - ts) * 1000),
                                   "resultPreview": rtext[:3000]})
                results.append({"toolResult": {"toolUseId": t["toolUseId"], "content": [{"text": rtext}]}})
            messages.append({"role": "user", "content": results})
            if time.time() - t0 > TIME_BUDGET_S:
                answer = (text + "\n\n" if text else "") + \
                    "_I ran out of time gathering data for this answer. Try a narrower question._"
                break
        else:
            answer = answer or "_I reached the tool-call limit for one question. Try a narrower question._"
    except Exception as e:  # noqa: BLE001
        log.exception("chat failed", extra={"traceId": trace_id})
        return _resp(502, {"error": f"The model call failed: {type(e).__name__}. Please try again.", "traceId": trace_id})

    total_ms = int((time.time() - t0) * 1000)
    reasoning = "\n\n".join(f"[round {r['round']}] {r['reasoning']}" for r in rounds if r["reasoning"])
    log.info("llm_trace", extra=redact({
        "app": "class-insights", "traceType": "agent-chat", "traceId": trace_id, "user": user, "model": MODEL_ID,
        "question": question,
        "systemPrompt": SYSTEM_TEMPLATE.replace("{knowledge}", f"<projectknowledge.md: {len(system)} chars>"),
        "promptMessages": [{"role": m["role"], "text": "".join(c.get("text", "") for c in m["content"])[:4000]}
                           for m in messages[:first_prompt_index + 1]],
        "reasoning": reasoning, "response": answer, "toolCalls": tool_calls, "rounds": rounds,
        "inferenceConfig": {"maxTokens": MAX_TOKENS + THINKING_BUDGET, "thinkingBudget": THINKING_BUDGET},
        "usage": usage_total, "stopReason": stop, "latencyMs": {"total": total_ms},
    }))
    return _resp(200, {"answer": answer, "reasoning": reasoning, "traceId": trace_id, "model": MODEL_ID,
                       "toolCalls": [{"name": c["name"], "input": c["input"], "ms": c["ms"]} for c in tool_calls],
                       "usage": usage_total, "latencyMs": total_ms})
