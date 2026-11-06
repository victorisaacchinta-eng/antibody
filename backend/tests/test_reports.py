"""Template explanation and the explain() fallback, with no network or LLM key."""

import reports
from engine import Engine, Incident

LLM_VARS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT", "LLM_API_KEY")


def _cause(service, score, label="Process crashed"):
    return {"service": service, "score": score, "signature_label": label,
            "evidence": ["PageRank 0.41", f"{service} error rate 92%"], "samples": []}


def _incident(**kw):
    inc = Incident(id=7, opened_at=1000.0, services=["db", "payments", "gateway"], event_count=1234)
    for k, v in kw.items():
        setattr(inc, k, v)
    return inc


def test_template_with_no_causes_says_so():
    assert reports.template_explanation(_incident(), Engine()) == "Not enough evidence yet to name a cause."


def test_template_names_cause_and_drops_pagerank_evidence():
    inc = _incident(causes=[_cause("db", 0.83), _cause("payments", 0.21)],
                    verdict={"label": "HIGH", "abstain": False, "reason": "clear margin"})
    text = reports.template_explanation(inc, Engine())
    assert text.startswith("db is the most likely root cause, HIGH confidence (RCA score 0.83)")
    assert "(payments, gateway) depend on it" in text
    assert "Evidence: db error rate 92%." in text and "PageRank" not in text
    assert "1,234 logs" in text
    assert "Second suspect: payments (score 0.21)." in text


def test_template_abstain_does_not_claim_a_root_cause():
    inc = _incident(causes=[_cause("db", 0.40)],
                    verdict={"label": "LOW", "abstain": True, "reason": "top two suspects are too close"})
    text = reports.template_explanation(inc, Engine())
    assert text.startswith("There is not enough evidence to name a root cause with confidence: "
                           "top two suspects are too close.")
    assert "leading candidate is db (RCA score 0.40), so verify it before acting" in text
    assert "most likely root cause" not in text


def test_explain_uses_template_when_no_llm_key_is_set(monkeypatch):
    for var in LLM_VARS:
        monkeypatch.delenv(var, raising=False)
    inc, eng = _incident(causes=[_cause("db", 0.83)]), Engine()
    out = reports.explain(inc, eng)
    assert out == {"text": reports.template_explanation(inc, eng),
                   "source": "template, built from the evidence (no LLM key set)", "ai": False}


def test_explain_falls_back_to_template_when_llm_call_fails(monkeypatch):
    def boom(prompt):
        raise TimeoutError("no network")

    monkeypatch.setattr(reports, "_call_llm", boom)
    inc, eng = _incident(causes=[_cause("db", 0.83)]), Engine()
    out = reports.explain(inc, eng)
    assert out["ai"] is False
    assert out["source"] == "template (LLM unavailable: TimeoutError)"
    assert out["text"] == reports.template_explanation(inc, eng)
