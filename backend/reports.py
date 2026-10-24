"""
Plain-English explanation + post-incident report.

The LLM is optional and never decides anything: it only rewrites evidence the
engine already computed. With no key configured (or no network) we fall back
to a deterministic template built from the same evidence, so the demo never
depends on venue Wi-Fi.

Configure one of:
  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT
  LLM_API_KEY (+ optional LLM_BASE_URL, LLM_MODEL) for any OpenAI-compatible API
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime

from engine import Engine, Incident


def _t(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"


def facts(inc: Incident, eng: Engine) -> dict:
    return {
        "incident": inc.id,
        "severity": inc.severity,
        "opened": _t(inc.opened_at),
        "affected_services": inc.services,
        "events_correlated": inc.event_count,
        "ranked_causes": [
            {"service": c["service"], "rca_score": c["score"], "signature": c["signature_label"],
             "evidence": c["evidence"], "log_samples": c["samples"]}
            for c in inc.causes
        ],
        "confidence": inc.verdict,
        "suggested_fix": inc.suggestions[0]["label"] if inc.suggestions else None,
        "similar_past_incidents": inc.similar,
        "timeline": [f"{_t(e['ts'])} {e['actor']}: {e['message']}" for e in inc.timeline[-12:]],
    }


def template_explanation(inc: Incident, eng: Engine) -> str:
    if not inc.causes:
        return "Not enough evidence yet to name a cause."
    top = inc.causes[0]
    v = inc.verdict or {"label": "MEDIUM", "abstain": False, "reason": ""}
    others = [s for s in inc.services if s != top["service"]]
    if v["abstain"]:
        lines = [f"There is not enough evidence to name a root cause with confidence: {v['reason']}. "
                 f"The leading candidate is {top['service']} (RCA score {top['score']:.2f}), so verify it before acting."]
    else:
        lines = [f"{top['service']} is the most likely root cause, {v['label']} confidence "
                 f"(RCA score {top['score']:.2f}): {top['signature_label'].lower()}."]
    if others:
        lines.append(f"The other affected services ({', '.join(others)}) depend on it, so their errors are "
                     f"symptoms, not separate problems.")
    ev = [e for e in top["evidence"] if not e.startswith("PageRank")]
    if ev:
        lines.append("Evidence: " + "; ".join(ev[:3]) + ".")
    lines.append(f"Antibody folded {inc.event_count:,} logs, metrics and traces into this one incident.")
    if len(inc.causes) > 1:
        lines.append(f"Second suspect: {inc.causes[1]['service']} (score {inc.causes[1]['score']:.2f}).")
    if inc.similar:
        m = inc.similar[-1]
        lines.append(f"This matches incident #{m['incident']}, which was fixed by "
                     f"{m['action'] or 'a manual resolve'} in {m['ttr']:.0f}s.")
    if inc.suggestions:
        lines.append(f"Suggested next step: {inc.suggestions[0]['label']}.")
    return " ".join(lines)


SYSTEM = ("You are an on-call SRE assistant. Explain the incident to an engineer using ONLY the facts "
          "provided. Do not invent services, numbers or causes. If the evidence is uncertain, say so. "
          "Write 4 to 6 short plain-English sentences, no headings, no bullet points, no em dashes. "
          "End with one line starting 'Suggested next step:'.")


def _call_llm(prompt: str) -> tuple[str, str] | None:
    az_ep, az_key, az_dep = (os.getenv("AZURE_OPENAI_ENDPOINT"), os.getenv("AZURE_OPENAI_API_KEY"),
                             os.getenv("AZURE_OPENAI_DEPLOYMENT"))
    body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "temperature": 0.2, "max_tokens": 350}
    if az_ep and az_key and az_dep:
        url = f"{az_ep.rstrip('/')}/openai/deployments/{az_dep}/chat/completions?api-version=2024-10-21"
        headers = {"api-key": az_key, "Content-Type": "application/json"}
        label = f"Azure OpenAI ({az_dep})"
    elif os.getenv("LLM_API_KEY"):
        base = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
        model = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
        url, body["model"] = f"{base}/chat/completions", model
        headers = {"Authorization": f"Bearer {os.getenv('LLM_API_KEY')}", "Content-Type": "application/json"}
        label = model
    else:
        return None
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip().replace("—", ","), label


def explain(inc: Incident, eng: Engine) -> dict:
    prompt = "Incident facts (JSON):\n" + json.dumps(facts(inc, eng), indent=1)
    try:
        out = _call_llm(prompt)
        if out:
            return {"text": out[0], "source": f"AI-generated by {out[1]} from the evidence below", "ai": True}
    except Exception as e:  # network down, bad key, rate limit: fall back, never fail the demo
        return {"text": template_explanation(inc, eng),
                "source": f"template (LLM unavailable: {type(e).__name__})", "ai": False}
    return {"text": template_explanation(inc, eng), "source": "template, built from the evidence (no LLM key set)",
            "ai": False}


def postmortem(inc: Incident, eng: Engine) -> str:
    top = inc.causes[0] if inc.causes else None
    dur = (inc.resolved_at or eng.now) - inc.opened_at
    mtta = f"{inc.acked_at - inc.opened_at:.0f}s" if inc.acked_at else "not acknowledged"
    md = [
        f"# Post-incident report: INC-{inc.id}",
        "",
        f"**Severity:** {inc.severity} | **Status:** {inc.status} | **Opened:** {_t(inc.opened_at)} | "
        f"**Resolved:** {_t(inc.resolved_at)} | **Duration:** {dur:.0f}s | **Time to acknowledge:** {mtta}",
        "",
        "## Summary",
        "",
        (inc.explanation or {}).get("text") or template_explanation(inc, eng),
        "",
        "## Impact",
        "",
        f"- Affected services: {', '.join(inc.services)}",
        f"- User-facing: {'yes, gateway was affected' if 'gateway' in inc.services else 'no'}",
        f"- Events correlated into this incident: {inc.event_count:,}",
        "",
        "## Root cause",
        "",
    ]
    if top:
        v = inc.verdict or {}
        md += [f"**{top['service']}**, {v.get('label', '?')} confidence (RCA score {top['score']:.2f}): {top['signature_label']}.",
               "", f"Why this confidence: {v.get('reason', '')}.", ""]
        md += [f"- {e}" for e in top["evidence"]]
        if top["samples"]:
            md += ["", "Representative log lines:", "", "```"] + top["samples"] + ["```"]
        md += ["", "Other suspects considered:", ""]
        md += [f"- {c['service']}: score {c['score']:.2f} ({c['signature_label'].lower()})" for c in inc.causes[1:]] or ["- none"]
    md += ["", "## Remediation", ""]
    md += [f"- {_t(r['ts'])}: {r['by']} ran `{r['action']}` on {r['service']}" for r in inc.remediations] or ["- none recorded"]
    md += ["", "## Timeline", "", "| Time | Source | Event |", "| --- | --- | --- |"]
    md += [f"| {_t(e['ts'])} | {e['actor']} | {e['message']} |" for e in inc.timeline]
    md += ["", "## Follow-ups", "",
           f"- [ ] Add an alert on the leading signal for {top['service'] if top else 'the root service'} so it pages before callers do",
           "- [ ] Review whether callers need a circuit breaker or timeout on this dependency",
           "- [ ] Confirm the fix with the owning team and close this report",
           "", "_Generated by Antibody. Evidence is computed deterministically; the summary may be AI-written and should be reviewed._"]
    return "\n".join(md)
