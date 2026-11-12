"""Model Lab: preference-weighted LLM selection backed by AWS AppConfig.

AppConfig (application sdge-rag-<env>, environment <env>, profile model-selection)
holds the model catalog, the quality/cost/latency weights, the active model and
the last evaluation. The chat API reads it through the AppConfig *data plane*
(polled at most every 15 s). This module writes it through the *control plane*
(new hosted version + instant deployment).

Evaluation = live benchmark of every catalog model on two grounded-QA tests
(an answerable one and one that must be declined), run in parallel with the same
system prompt and request shape the chat API uses. Then each dimension is
normalised to 0-100 and the weights are applied."""
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from common import REGION, log
from prompts import SYSTEM_PROMPT

APP_ID = os.environ.get("APPCONFIG_APP_ID", "")
ENV_ID = os.environ.get("APPCONFIG_ENV_ID", "")
PROFILE_ID = os.environ.get("APPCONFIG_PROFILE_ID", "")
STRATEGY_ID = os.environ.get("APPCONFIG_STRATEGY_ID", "")
DEFAULT_MODEL_ID = os.environ.get("DEFAULT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

appconfig = boto3.client("appconfig", region_name=REGION)
appconfigdata = boto3.client("appconfigdata", region_name=REGION)
bench_bedrock = boto3.client("bedrock-runtime", region_name=REGION, config=Config(
    max_pool_connections=24, read_timeout=14, connect_timeout=4, retries={"max_attempts": 1, "mode": "standard"}))


# ------------------------------------------------------------------ request shape (shared with chat)
def build_request(model, system_text, messages, max_out=1200, thinking_budget=1024):
    """Converse request for any catalog model. Only Anthropic models get the
    extended-thinking field; native reasoning models get extra output room."""
    req = {"modelId": model["id"], "system": [{"text": system_text}], "messages": messages}
    kind = model.get("reasoning", "none")
    if kind == "extended-thinking" and thinking_budget >= 1024:
        req["inferenceConfig"] = {"maxTokens": max_out + thinking_budget}
        req["additionalModelRequestFields"] = {"thinking": {"type": "enabled", "budget_tokens": thinking_budget}}
    elif kind == "native":
        req["inferenceConfig"] = {"maxTokens": max_out + 2048, "temperature": 0.1}
    else:
        req["inferenceConfig"] = {"maxTokens": max_out, "temperature": 0.1}
    return req


def split_output(content):
    text = "".join(c.get("text", "") for c in content)
    reasoning = "".join(c["reasoningContent"]["reasoningText"].get("text", "")
                        for c in content if "reasoningContent" in c and "reasoningText" in c["reasoningContent"])
    # some open models inline their chain of thought in <think>…</think> / <reasoning>…</reasoning>
    m = re.search(r"<(think|reasoning)>(.*?)</\1>", text, re.S)
    if m:
        reasoning = (reasoning + "\n" + m.group(2)).strip()
        text = (text[:m.start()] + text[m.end():]).strip()
    return text.strip(), reasoning


# ------------------------------------------------------------------ AppConfig data plane (chat path)
_live = {"token": None, "next_poll": 0.0, "config": None}


def live_config():
    """Deployed configuration via the AppConfig data plane (min poll 15 s)."""
    if not APP_ID:
        return None
    now = time.time()
    if _live["config"] is not None and now < _live["next_poll"]:
        return _live["config"]
    try:
        if not _live["token"]:
            _live["token"] = appconfigdata.start_configuration_session(
                ApplicationIdentifier=APP_ID, EnvironmentIdentifier=ENV_ID, ConfigurationProfileIdentifier=PROFILE_ID,
                RequiredMinimumPollIntervalInSeconds=15)["InitialConfigurationToken"]
        r = appconfigdata.get_latest_configuration(ConfigurationToken=_live["token"])
        _live["token"] = r["NextPollConfigurationToken"]
        _live["next_poll"] = now + int(r.get("NextPollIntervalInSeconds", 15))
        body = r["Configuration"].read()
        if body:  # empty body = unchanged since last poll
            _live["config"] = json.loads(body)
    except Exception:  # noqa: BLE001 - never break chat because of config
        log.exception("appconfig poll failed; using cached/default model")
        _live["token"] = None
        _live["next_poll"] = now + 15
    return _live["config"]


def active_model():
    cfg = live_config() or {}
    mid = cfg.get("activeModelId") or DEFAULT_MODEL_ID
    for m in cfg.get("models", []):
        if m["id"] == mid:
            return m
    return {"id": mid, "name": mid, "vendor": "Anthropic" if "anthropic" in mid else "?",
            "reasoning": "extended-thinking" if "anthropic" in mid else "none"}


# ------------------------------------------------------------------ AppConfig control plane
def read_latest():
    versions = appconfig.list_hosted_configuration_versions(
        ApplicationId=APP_ID, ConfigurationProfileId=PROFILE_ID, MaxResults=1)["Items"]
    if not versions:
        raise RuntimeError("Model Lab configuration has not been seeded")
    v = versions[0]["VersionNumber"]
    body = appconfig.get_hosted_configuration_version(
        ApplicationId=APP_ID, ConfigurationProfileId=PROFILE_ID, VersionNumber=v)["Content"].read()
    return v, json.loads(body)


def write_and_deploy(cfg, base_version, description):
    v = appconfig.create_hosted_configuration_version(
        ApplicationId=APP_ID, ConfigurationProfileId=PROFILE_ID, Content=json.dumps(cfg).encode(),
        ContentType="application/json", Description=description[:1024], LatestVersionNumber=base_version)["VersionNumber"]
    for attempt in range(10):
        try:
            d = appconfig.start_deployment(
                ApplicationId=APP_ID, EnvironmentId=ENV_ID, DeploymentStrategyId=STRATEGY_ID,
                ConfigurationProfileId=PROFILE_ID, ConfigurationVersion=str(v), Description=description[:1024])
            break
        except appconfig.exceptions.ConflictException:   # another deployment still finishing
            time.sleep(1 + attempt * 0.5)
    else:
        raise RuntimeError("AppConfig deployment is busy; try again in a few seconds")
    # make this container use the new config immediately
    _live.update(config=cfg, next_poll=time.time() + 15)
    return v, d["DeploymentNumber"]


# ------------------------------------------------------------------ benchmark
_EXCERPTS = """[1] (Source: GS-MT-150 Distribution Maintenance Protocols.pdf, page 1)
2. Leak Surveys - Business districts: leak survey at intervals not exceeding 15 months, but at least once each calendar
year, using a flame ionization or laser methane detector, including tests of the atmosphere in gas, electric,
telephone, sewer and water manholes, at cracks in pavement and sidewalks. - Outside business districts: leak survey at
least once every 5 calendar years at intervals not exceeding 63 months. Company standard: unprotected steel and
cast-iron segments are surveyed every 3 years.

[2] (Source: GS-MT-150 Distribution Maintenance Protocols.pdf, page 1)
3. Cathodic Protection (CP) - Pipe-to-soil potential surveys for cathodically protected pipelines: at least once each
calendar year, at intervals not exceeding 15 months. The protection criterion is a negative (cathodic) potential of at
least -850 mV with reference to a saturated copper-copper sulfate electrode. Rectifiers: inspect six times each
calendar year, at intervals not exceeding 2 1/2 months.

[3] (Source: GS-SF-210 PPE and Field Safety Policy.pdf, page 1)
3. Flame-Resistant (FR) Clothing - FR clothing with an arc thermal performance value (ATPV) of at least 8 cal/cm2 is
required for any work on or within 10 feet of live gas facilities, including meter set work, tapping, squeezing off,
purging, and leak investigation. Synthetic base layers are prohibited under FR clothing because they melt.

[4] (Source: GS-EM-101 Gas Leak and Emergency Response.pdf, page 2)
3. Leak Grading - Grade 2 leaks must be repaired or cleared within 15 months of the date reported and re-evaluated at
least once every six months until cleared. Grade 3 leaks must be re-evaluated during the next scheduled survey or
within 15 months of the date reported, whichever occurs first."""

TESTS = [
    {"id": "answerable", "question": "How often must leak surveys be performed in business districts?",
     "check": lambda t: bool(re.search(r"15\s*months", t, re.I)) and bool(re.search(r"\[\d\]", t))},
    {"id": "must-decline", "question": "What is the maximum allowable operating pressure (MAOP) of the Kearny Mesa transmission line?",
     "check": lambda t: bool(re.search(r"(do(es)?n['’]?t|do(es)? not|not (contain|cover|include|provide|specify|mention|address|list|state)|"
                                       r"no (information|mention|details|data|specific)|cannot|can['’]t|unable|isn['’]t|aren['’]t|"
                                       r"not (available|found|covered|in the))", t, re.I))
                        and not re.search(r"\d+\s*psi", t, re.I)},
]


def _run_one(model, test):
    messages = [{"role": "user", "content": [{"text":
        f"Context excerpts from internal documents:\n\n{_EXCERPTS}\n\n---\nEmployee question: {test['question']}"}]}]
    req = build_request(model, SYSTEM_PROMPT, messages)
    t0 = time.time()
    try:
        out = bench_bedrock.converse(**req)
        ms = int((time.time() - t0) * 1000)
        text, reasoning = split_output(out["output"]["message"]["content"])
        usage = out.get("usage", {})
        passed = test["check"](text)
        result = {"test": test["id"], "ok": True, "passed": passed, "latencyMs": ms,
                  "inputTokens": usage.get("inputTokens", 0), "outputTokens": usage.get("outputTokens", 0),
                  "answerPreview": text[:300], "hasReasoning": bool(reasoning)}
        log.info("llm_trace", extra={
            "app": "gas-ops-assistant", "traceType": "model-benchmark", "user": "model-lab", "model": model["id"],
            "question": f"[benchmark:{test['id']}] {test['question']}", "systemPrompt": SYSTEM_PROMPT,
            "promptMessages": [{"role": "user", "text": messages[0]["content"][0]["text"]}],
            "reasoning": reasoning, "response": text, "inferenceConfig": req["inferenceConfig"],
            "usage": usage, "stopReason": out.get("stopReason"), "latencyMs": {"total": ms},
            "benchmark": {"test": test["id"], "passed": passed}})
        return result
    except Exception as e:  # noqa: BLE001
        ms = int((time.time() - t0) * 1000)
        log.warning("benchmark call failed", extra={"model": model["id"], "test": test["id"], "error": str(e)[:300]})
        return {"test": test["id"], "ok": False, "passed": False, "latencyMs": ms, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def benchmark(models):
    jobs = [(m, t) for m in models for t in TESTS]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda j: _run_one(*j), jobs))
    out = {}
    for (m, _), r in zip(jobs, results):
        out.setdefault(m["id"], []).append(r)
    measurements = {}
    for mid, rs in out.items():
        ok = [r for r in rs if r["ok"]]
        measurements[mid] = {
            "latencyMs": int(sum(r["latencyMs"] for r in ok) / len(ok)) if ok else None,
            "inputTokens": int(sum(r["inputTokens"] for r in ok) / len(ok)) if ok else None,
            "outputTokens": int(sum(r["outputTokens"] for r in ok) / len(ok)) if ok else None,
            "tests": {r["test"]: r["passed"] for r in rs},
            "liveQuality": 50 * sum(1 for r in rs if r["passed"]),
            "error": next((r["error"] for r in rs if not r["ok"]), None),
            "answerPreview": next((r.get("answerPreview") for r in rs if r["test"] == "answerable" and r["ok"]), None),
        }
    return measurements


# ------------------------------------------------------------------ scoring
def _minmax(values, invert=False, log_scale=False):
    vals = {k: (math.log(v) if log_scale else v) for k, v in values.items() if v is not None and v > 0}
    if not vals:
        return {k: None for k in values}
    lo, hi = min(vals.values()), max(vals.values())
    res = {}
    for k in values:
        if k not in vals:
            res[k] = None
        elif hi == lo:
            res[k] = 100.0
        else:
            x = (vals[k] - lo) / (hi - lo) * 100
            res[k] = round(100 - x if invert else x, 1)
    return res


def score(cfg, weights=None):
    """Per-model sub-scores (0-100, independent of weights) and the weighted composite."""
    weights = weights or cfg["weights"]
    pol = cfg.get("selectionPolicy", {})
    meas = ((cfg.get("lastEvaluation") or {}).get("measurements")) or {}
    typical = pol.get("typicalRequest", {"inputTokens": 1500, "outputTokens": 300})
    qw, lw = pol.get("qualityPriorWeight", 0.75), pol.get("qualityLiveWeight", 0.25)
    raw_q, cost, lat = {}, {}, {}
    for m in cfg["models"]:
        me = meas.get(m["id"]) or {}
        live = me.get("liveQuality") if me and not me.get("error") else (0 if me.get("error") else None)
        raw_q[m["id"]] = m["qualityPrior"] * qw + (live if live is not None else m["qualityPrior"]) * lw
        tin = me.get("inputTokens") or typical["inputTokens"]
        tout = me.get("outputTokens") or typical["outputTokens"]
        cost[m["id"]] = (tin * m["pricePer1M"]["input"] + tout * m["pricePer1M"]["output"]) / 1e6
        lat[m["id"]] = me.get("latencyMs")
    q = _minmax(raw_q)
    c = _minmax(cost, invert=True, log_scale=True)
    lt = _minmax(lat, invert=True, log_scale=True)
    rows = []
    for m in cfg["models"]:
        mid = m["id"]
        me = meas.get(mid) or {}
        reasons = []
        if me.get("error"):
            reasons.append("benchmark call failed")
        if me.get("latencyMs") and me["latencyMs"] > pol.get("maxLatencyMs", 20000):
            reasons.append(f"latency over {pol.get('maxLatencyMs', 20000) // 1000}s API limit budget")
        if pol.get("requireGroundedAnswer", True) and me and me.get("tests", {}).get("answerable") is False and not me.get("error"):
            reasons.append("failed the grounded-answer test (correct fact + [n] citation)")
        measured = bool(me) and me.get("latencyMs") is not None
        if not me:
            reasons.append("not measured yet")
        parts = {"quality": q[mid], "cost": c[mid], "latency": lt[mid]}
        used = {k: weights[k] for k, v in parts.items() if v is not None}
        total_w = sum(used.values()) or 1
        composite = round(sum(parts[k] * w for k, w in used.items()) / total_w, 1)
        rows.append({
            "id": mid, "name": m["name"], "vendor": m["vendor"], "strengths": m["strengths"],
            "reasoning": m.get("reasoning"), "openWeights": m.get("openWeights"),
            "qualityPrior": m["qualityPrior"], "pricePer1M": m["pricePer1M"],
            "scores": parts, "composite": composite,
            "costPerRequestUsd": round(cost[mid], 6), "costPer1kQuestionsUsd": round(cost[mid] * 1000, 2),
            "latencyMs": me.get("latencyMs"), "tests": me.get("tests"), "liveQuality": me.get("liveQuality"),
            "error": me.get("error"), "answerPreview": me.get("answerPreview"),
            "eligible": measured and not reasons, "ineligibleReasons": reasons,
            "active": mid == cfg.get("activeModelId"),
        })
    rows.sort(key=lambda r: (-r["composite"], r["name"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def _valid_weights(w):
    try:
        w = {k: int(round(float(w[k]))) for k in ("quality", "cost", "latency")}
    except (KeyError, TypeError, ValueError):
        raise ValueError("weights must include quality, cost and latency")
    if any(v < 0 or v > 100 for v in w.values()) or sum(w.values()) != 100:
        raise ValueError("weights must each be 0-100 and sum to exactly 100")
    return w


def _public(cfg, version):
    ev = cfg.get("lastEvaluation") or {}
    return {"weights": cfg["weights"], "activeModelId": cfg["activeModelId"], "policy": cfg.get("selectionPolicy"),
            "ranking": score(cfg), "configVersion": version,
            "lastEvaluation": {k: ev.get(k) for k in ("at", "by", "weights", "measuredAt", "decision", "reusedMeasurements")} if ev else None,
            "history": cfg.get("history", [])[:10],
            "appconfig": {"application": f"sdge-rag-{os.environ.get('ENV_NAME', 'prod')}", "environment": os.environ.get("ENV_NAME", "prod"),
                          "profile": "model-selection"}}


def get_state():
    v, cfg = read_latest()
    return _public(cfg, v)


def evaluate(user, body):
    weights = _valid_weights(body.get("weights") or {})
    apply = body.get("apply", True) is not False
    force = bool(body.get("forceRemeasure"))
    t0 = time.time()
    version, cfg = read_latest()
    pol = cfg.get("selectionPolicy", {})
    prev = cfg.get("lastEvaluation") or {}
    age = time.time() - prev.get("measuredEpoch", 0)
    if prev.get("measurements") and age < pol.get("measurementTtlSeconds", 120) and not force:
        measurements, measured_epoch, reused = prev["measurements"], prev["measuredEpoch"], True
    else:
        measurements, measured_epoch, reused = benchmark(cfg["models"]), time.time(), False
    bench_ms = int((time.time() - t0) * 1000)

    for attempt in range(4):
        new = json.loads(json.dumps(cfg))
        new["weights"] = weights
        new["lastEvaluation"] = {"measurements": measurements, "measuredEpoch": measured_epoch,
                                 "measuredAt": datetime.fromtimestamp(measured_epoch, tz=timezone.utc).isoformat(timespec="seconds")}
        ranking = score(new, weights)
        current = cfg["activeModelId"]
        cur_row = next((r for r in ranking if r["id"] == current), None)
        best = next((r for r in ranking if r["eligible"]), None)
        margin = pol.get("switchMarginPoints", 2.0)
        if not apply:
            action, to, reason = "measure-only", current, "Measurements refreshed; active model unchanged (apply=false)."
        elif best is None:
            action, to, reason = "keep", current, "No model passed eligibility checks; keeping the current model."
        elif best["id"] == current:
            action, to, reason = "keep", current, f"{best['name']} is already the best fit for these preferences (score {best['composite']})."
        elif cur_row is None or not cur_row["eligible"]:
            action, to, reason = "switch", best["id"], (f"Current model is not eligible ({'; '.join(cur_row['ineligibleReasons']) if cur_row else 'not in catalog'}); "
                                                         f"switching to {best['name']} (score {best['composite']}).")
        elif best["composite"] - cur_row["composite"] >= margin:
            action, to, reason = "switch", best["id"], (f"{best['name']} scores {best['composite']} vs {cur_row['name']} {cur_row['composite']} "
                                                         f"(+{round(best['composite'] - cur_row['composite'], 1)} ≥ {margin}-point switch margin).")
        else:
            action, to, reason = "keep", current, (f"{best['name']} leads ({best['composite']}) but is within the {margin}-point switch margin of "
                                                   f"{cur_row['name']} ({cur_row['composite']}); keeping the current model to avoid flapping.")
        new["activeModelId"] = to
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        decision = {"action": action, "from": current, "to": to, "reason": reason}
        new["lastEvaluation"].update(at=now, by=user["username"], weights=weights, decision=decision, reusedMeasurements=reused,
                                     ranking=[{k: r[k] for k in ("rank", "id", "name", "vendor", "composite", "scores", "latencyMs",
                                                                 "costPer1kQuestionsUsd", "tests", "eligible", "ineligibleReasons")}
                                              for r in score(dict(new, activeModelId=to), weights)])
        new["history"] = ([{"at": now, "by": user["username"], "weights": weights, "action": action, "from": current, "to": to,
                            "top3": [r["name"] for r in ranking[:3]]}] + cfg.get("history", []))[:25]
        try:
            new_version, deployment = write_and_deploy(new, version, f"{action} by {user['username']}: {weights}")
            break
        except appconfig.exceptions.ConflictException:
            version, cfg = read_latest()   # someone else saved first: re-decide on top of theirs
    else:
        raise RuntimeError("Configuration changed concurrently; please retry")

    log.info("model_evaluation", extra={"user": user["username"], "weights": weights, "decision": decision,
                                        "reusedMeasurements": reused, "benchmarkMs": bench_ms, "configVersion": new_version,
                                        "deployment": deployment,
                                        "ranking": [{"rank": r["rank"], "model": r["name"], "composite": r["composite"],
                                                     "eligible": r["eligible"]} for r in ranking]})
    state = _public(new, new_version)
    state["decision"] = decision
    state["benchmarkMs"] = bench_ms
    return state
