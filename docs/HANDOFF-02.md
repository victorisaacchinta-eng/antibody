==================================================
PROJECT SESSION HANDOFF
AI-Powered Incident Root Cause Analysis Engine
==================================================

HANDOFF VERSION: Handoff 02 (delta on Handoff 01)
SESSION / TEAM: Claude / Cowork build session, with Victor (team Ace Azael)
DATE / STAGE: 24 Sep 2026, ~11:50 IST. Freeze planned 13:00, cutoff 13:30.
PURPOSE: implement the two change proposals the human team approved, and report what the new evidence says.

==================================================
0. HUMAN DECISIONS RECORDED (since Handoff 01)
==================================================

- DECIDED: Antibody (this codebase) is the submission. There is no second codebase.
- APPROVED + IMPLEMENTED: CP-1 (baselines), CP-2 (confidence labels + abstention).
- NOT APPROVED (yet): CP-3 contradictions/coverage, CP-4 missing telemetry, CP-5 ablation.

==================================================
1. EXECUTIVE SUMMARY
==================================================

- CP-1 done: the benchmark now scores baselines A/B/C on the same incident at the same moment as the full model.
- CP-2 done: "90%" is gone everywhere (UI, reports, post-mortem, graph tag). Each incident now carries an RCA score (0 to 1, explicitly not a probability) and a verdict: HIGH / MEDIUM / LOW. LOW means abstain: the UI says "Insufficient evidence for a confident root cause" and lists the leading candidate as a suspect.
- Key finding: our model does NOT beat the simplest baseline on hard mode overall (84.5% vs 84.5%). It beats it on clean traffic (100% vs 88.1%). The confidence labels are the real win: HIGH is right 98.5% of the time.
- Everything was re-verified: benchmark rerun, headless-browser run of both views, no console errors.

==================================================
2. RESULTS (python bench.py, 7 s, reproducible)
==================================================

Top-1 accuracy, same incidents, same moment:

| Ranker | Clean (84) | Hard (84) |
| --- | --- | --- |
| A loudest anomaly only | 88.1% | 84.5% |
| B anomaly + earliest onset | 100% | 75.0% |
| C onset + dependency graph | 100% | 79.8% |
| Full model | 100% | 84.5% |

Head-to-head, full vs A:
- Clean: full wins 10, A wins 0. All 10 are CRASH cascades: the root goes silent (one FATAL line, then nothing) while its callers log hundreds of errors, so "loudest" picks a symptom. This is the design doc's "symptoms != root cause" case, now measured.
- Hard: full wins 6 (all crash cascades), A wins 6: gateway crash, gateway cpu_burn, gateway error_spike, cart error_spike, cart memory_leak, cart cpu_burn. Pattern: the fault is HIGH in the graph (gateway/cart), a noise blip hits a service BELOW it, and our "no abnormal dependency" + PageRank terms point downward at the red herring.
- 5 of those 6 losses were labelled LOW or MEDIUM; only 1 (cart cpu_burn) was a wrong HIGH.

Confidence labels (hard mode):

| Label | Given | Top-1 correct when given |
| --- | --- | --- |
| HIGH | 78.6% | 98.5% |
| MEDIUM | 6.0% | 60.0% |
| LOW (abstain) | 8.3% | 42.9% |
| Answered (HIGH+MEDIUM) | | 95.8% |

(The remaining 7.1% of hard runs were not detected within the window, so they have no label.)

Clean: 100% HIGH and 100% correct, which again says the clean simulator is too easy.

==================================================
3. WHAT CHANGED IN THE CODE
==================================================

engine.py
- Each ranked candidate now carries: score (raw, 0-1), anom_deps, evidence_types (independent types: topology, timing, caller logs, local fault signature, change event, past incident).
- New Engine.verdict(causes), with fixed, disclosed thresholds (NOT tuned on the benchmark):
  HIGH   = no contradiction AND lead over #2 >= 0.15 AND >= 3 independent evidence types
  LOW    = lead < 0.05 OR a contradiction ("depends on an abnormal service" / "no local fault, errors point at a dependency") -> abstain
  MEDIUM = otherwise
- Ranking sorts by raw score (was: by normalised share; same order).
- The timeline logs when the label changes, and logs abstention explicitly.
- The internal "share" is kept only as the cut-off for showing a second remediation. It is never displayed.

bench.py: baselines() and label statistics; summary gains baselines_top1 and labels.
main.py: incident summary exposes top.score + top.label; detail exposes verdict.
reports.py: explanation and post-mortem use label + score and state the reason. The abstain case says "verify before acting".
frontend: verdict banner above the ranked causes (reason + evidence types, or the insufficient-evidence message with contradictions); score instead of %; graph tag "ROOT · HIGH" or "SUSPECT?"; benchmark tab gains "Versus simpler baselines" and "Honest confidence" tables with plain-language caveats.

==================================================
4. STATUS CHANGES (vs Handoff 01, section 4)
==================================================

Confidence: BROKEN -> IMPLEMENTED + TESTED (labels, not probabilities; accuracy per label measured)
Abstention: MISSING -> IMPLEMENTED + TESTED (8.3% abstain rate in hard mode)
Baselines: MISSING -> IMPLEMENTED + TESTED
Contradiction handling: PARTIAL (unchanged in the ranking; the two contradictions now drive the LOW label)
Everything else: unchanged from Handoff 01.

==================================================
5. WHAT THIS MEANS (for the architecture session to challenge)
==================================================

1. Honest pitch claim: "Our ranker beats the loudest-service baseline when the root cause goes quiet, and we know when we're unsure: HIGH answers are right 98.5% of the time on noisy data." Do NOT claim "our model beats baselines" in general. On hard mode it ties A.
2. The measured weakness is now specific: faults high in the graph plus a red herring below them. That is exactly the design doc's contradiction idea, now backed by data. For example: the top candidate's own anomaly started AFTER a caller that shows a strong local fault signature, or the candidate explains fewer affected services than a caller does.
3. So CP-3 is now evidence-justified, but narrowly. Recommended CP-3-lite, ~30 min: (a) a propagation-coverage term; (b) one contradiction, "a caller of this candidate has its own local fault signature and went abnormal no later than it" -> penalty. Success test: the 6 hard-mode A-wins; no regression on the 10 crash cascades.
4. Weights are still hand-picked. With baselines in place, CP-5 ablation (~15 min) is the cheapest way to see which terms matter.

==================================================
6. DISAGREEMENTS / OPEN QUESTIONS FOR THE ARCHITECTURE SESSION
==================================================

- Threshold choice: HIGH/LOW cut-offs (0.15 / 0.05 lead, 3 evidence types) were set once, before running the benchmark, and not adjusted after. If the other session wants different thresholds, argue from principle, not from the benchmark, or we are tuning on the test set.
- Does the other session accept "ties baseline A on hard mode, wins on quiet-root cascades, calibrated abstention" as the headline? I think it is more defensible than any accuracy claim.
- Out-of-order events, cycles, and missing telemetry remain limitations for the demo. Agree?

==================================================
7. QUESTIONS FOR THE HUMAN TEAM
==================================================

1. Approve CP-3-lite (targets the 6 measured losses) and/or CP-5 ablation before the 13:00 freeze?
2. Live demo or video? Minutes allowed?
3. LLM key available (Azure OpenAI or Groq)?
