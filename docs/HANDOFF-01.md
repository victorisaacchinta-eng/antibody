==================================================
PROJECT SESSION HANDOFF
AI-Powered Incident Root Cause Analysis Engine
==================================================

HANDOFF VERSION: Handoff 01 (from the Claude / Cowork build session)

SESSION / TEAM: Claude session that wrote the current codebase ("Antibody"), working with Victor (team Ace Azael)

DATE / STAGE: 24 Sep 2026, 11:25 IST. Build exists and runs. Submission/demo cutoff 13:30 IST.

PURPOSE OF THIS SESSION: Build a working PS3 prototype end to end, benchmark it, and now align with the parallel architecture session so we stop designing and building in two different directions.

READ THIS FIRST: the context document assumes the joining session has not seen the build. This session wrote it. Everything below is from reading and running the actual code, not from the design doc. Where the design doc and the code disagree, both are stated.

==================================================
1. EXECUTIVE SUMMARY
==================================================

- A working prototype exists: Python/FastAPI backend (~1,450 lines) + single-file HTML dashboard (~520 lines).
- It simulates 7 services (gateway, auth, catalog, cart, payments, orders, db) that emit logs, metrics, traces and service events in one common schema, ~150 events/sec live.
- The dependency graph is learned from trace spans, not hardcoded in the engine.
- RCA today is a WEIGHTED-SUM model over 8 signals (PageRank on the anomaly subgraph, "no abnormal dependency", earliest onset, share of callers' error logs blaming the service, local fault signature, anomaly size, recent deploy, memory match). This is the "simplistic" direction the design doc warns against. It works on our simulator but has arbitrary weights, no explicit contradiction handling, no abstention, and an uncalibrated "%".
- Benchmark (reproducible, `python bench.py`, 7 s): 84 clean scenarios, top-1 100%. 84 hard-mode scenarios with random red-herring blips, top-1 84.5%, top-3 92.9%. Two simultaneous unrelated faults: both found in 6/6.
- Engineer workflow OPEN -> ACKNOWLEDGED -> RESOLVED works in the UI, as do remediation suggestions, memory of past fixes, a plain-English explanation (template; LLM path wired but untested) and a one-click post-mortem.
- Missing versus the design doc: baselines A/B/C, ablations, contradiction scoring, propagation-coverage scoring, abstention, calibrated confidence labels, missing-telemetry / out-of-order / cycle scenarios, a scalability test.

==================================================
2. CURRENT UNDERSTANDING OF THE SOLUTION (AS BUILT)
==================================================

Simulator (sim.py) or POST /api/ingest
-> ingest: common event schema {ts, service, kind: metric|log|trace|event, name, value, level, message, labels}
-> learn topology from trace spans (parent -> child edge counts; edge kept at >= 3 observations)
-> per-service rolling evidence: metric values, error-log ratio, error-span ratio, health-check failures, log pattern counts (FATAL, NullPointerException, OOM/heap, loop lag, slow query), blame counts (caller logs "call to X failed" -> X), last deploy event
-> detection: EWMA mean/variance per metric, z-score > 4 (with per-metric std floors), OR error-log ratio > 25%, OR error-span ratio > 30%, OR health check failed. Debounce: 2 consecutive bad ticks unless health check failed. 20 s warmup.
-> correlation: a new abnormal service joins an open incident if (a) it transitively calls a member (symptom climbing the graph) or (b) it is a direct dependency of the current top suspect, within a configurable window (default 30 s). Otherwise it opens its own incident. Unrelated simultaneous failures get separate incidents.
-> ranking: 8-signal weighted sum -> cubed and normalised -> shown as "confidence %" (NOT a probability).
-> root indicator ("signature"): process_crash / bad_deploy / memory_leak / cpu_saturation / latency_degradation / application_errors / downstream_symptom, from log patterns + events.
-> suggestion: signature -> restart / rollback / scale_out / investigate.
-> human: acknowledge -> apply fix -> "recovered" when all members healthy 3 ticks -> resolve -> memory stores (service, signature) -> fix.
-> transient auto-close: unacknowledged incident with no action that recovers on its own is closed as "transient".
-> ranking freezes once a fix is applied or the incident recovers, so decaying evidence can't flip the diagnosis after the fact.
-> reports: template or LLM explanation of the structured evidence; Markdown post-mortem.

Where this differs from the design doc's pipeline:
- "Change / anomaly evidence extraction" exists as detection only. No separate change-point step; deploy events are the only explicit change evidence.
- "Incident-time topology" = observed-from-traces only. No declared graph to combine with.
- "Candidate hypotheses -> evidence-consistent engine" is NOT built. Candidates = the incident's members, and the score is additive support only.
- "Confidence / abstention" is NOT built. The top candidate is always shown with a %.

==================================================
3. ARTIFACTS RECEIVED / INSPECTED
==================================================

NAME: backend/sim.py
PURPOSE: 7-service simulator, fault injection (crash, latency_spike, error_spike, memory_leak, cpu_burn, bad_deploy), remediation ground truth, optional background noise
STATUS: working, used by live server and benchmark
IMPORTANT CONTENT: faults propagate one hop per tick through metrics; trace errors propagate within the same tick (synchronous calls). Callers log "call to X failed: connection refused / timeout / 503". A crashed service emits one FATAL log then nothing, so the root is the QUIETEST service in logs (a good "symptoms != cause" case).

NAME: backend/engine.py
PURPOSE: ingest, topology, detection, correlation, ranking, workflow, memory
STATUS: working
IMPORTANT CONTENT: ranking weights at line ~430 are hand-picked (0.22 PageRank, 0.18 no-abnormal-dependency, 0.15 onset, 0.17 blame, 0.13 local signature, 0.05 x3). The engine never reads simulator ground truth (verified: no reference to active_fault / fault_started_at / sim imports).

NAME: backend/bench.py
PURPOSE: benchmark harness with known ground truth on a simulated clock
STATUS: working
IMPORTANT CONTENT: 7 services x 6 faults x 2 seeds, clean and hard (noise 0.02 per service per tick), 6 dual-fault pairs, memory-recall test. Ground truth is used only for scoring.

NAME: backend/reports.py
PURPOSE: explanation (Azure OpenAI or OpenAI-compatible, template fallback) and post-mortem
STATUS: template path tested; LLM path untested (no key yet)

NAME: backend/main.py
PURPOSE: FastAPI API + WebSocket + serves dashboard
STATUS: working

NAME: frontend/index.html
PURPOSE: dashboard: learned service map with root ring and failure-flow edges, chaos console, incident detail (ranked causes + evidence, suggested fix, memory hit, explanation, timeline), incident list, live log stream, benchmark tab
STATUS: working, verified by headless-browser screenshots, no console errors

NAME: Team context document ("PROJECT COLLABORATION CONTEXT") + handoff template
STATUS: read. The handoff template's header fields were placeholders and the requirement audit was cut off at Requirement 8.

NOT RECEIVED: any code from the parallel session. If the parallel session or a teammate has started a SEPARATE codebase, we have a merge problem and need to decide now which one is the submission.

==================================================
4. IMPLEMENTATION STATUS
==================================================

"Tested" below means covered by the automated benchmark or exercised in a headless-browser run. There is no unit-test suite.

Telemetry simulator: IMPLEMENTED + TESTED
Logs: IMPLEMENTED + TESTED
Metrics: IMPLEMENTED + TESTED
Traces: IMPLEMENTED + TESTED
Service events: IMPLEMENTED + TESTED (deploy, health_check_failed, remediation events)
Normalization: IMPLEMENTED + TESTED (the simulator emits the common schema; /api/ingest validates it). External-source ingest: IMPLEMENTED, NOT TESTED
Dependency graph: IMPLEMENTED + TESTED (learned from spans)
Incident-time graph: PARTIAL (anomaly subgraph of incident members used for PageRank; no declared + observed merge)
Anomaly/change detection: IMPLEMENTED + TESTED (EWMA z-score, ratios, health checks, debounce). Change-point detection: MISSING
Event correlation: IMPLEMENTED + TESTED (time window + call-path rule)
Candidate generation: PARTIAL (candidates = incident members with an onset; dependencies with missing telemetry are not added)
Supporting-evidence calculation: IMPLEMENTED + TESTED (8 signals, shown per candidate)
Contradiction handling: PARTIAL (only one implicit penalty: "depends on an abnormal service" cuts that term to 0.25. No explicit contradiction list, no penalty for unexplained members or earlier-failing alternatives)
Propagation analysis: PARTIAL (PageRank on the anomaly subgraph approximates it; no explicit "fraction of affected services this candidate explains")
Root-cause ranking: IMPLEMENTED + TESTED
Confidence: BROKEN relative to the design principle (displayed as "%" but it is a cubed normalised share, not calibrated)
Abstention: MISSING
Multiple-root handling: PARTIAL (unrelated simultaneous faults become separate incidents, 6/6; residual unexplained failures INSIDE one incident: MISSING)
Incident timeline: IMPLEMENTED + TESTED
Failure/dependency visualization: IMPLEMENTED + TESTED
Acknowledge/resolve lifecycle: IMPLEMENTED + TESTED
LLM explanation: IMPLEMENTED, NOT TESTED (template fallback tested)
Remediation suggestions: IMPLEMENTED + TESTED (top suggestion fixed the fault in 100% clean / 84.5% hard)
Testing framework: IMPLEMENTED + TESTED (benchmark only)
Baselines: MISSING
Scalability tests: MISSING (the benchmark pushes roughly 2M+ events in ~7 s on one core, but that was never measured or reported as throughput)
Dashboard/UI: IMPLEMENTED + TESTED

==================================================
5. PROBLEM-STATEMENT COVERAGE AUDIT
==================================================

Requirement 1: Accept simulated logs, metrics, traces, service events
STATUS: MET
EVIDENCE: sim.py emits all four in one schema; POST /api/ingest accepts the same schema (validated, 5,000/batch cap).

Requirement 2: Dependency graph
STATUS: MET
EVIDENCE: engine.graph() from trace spans; 7 edges learned; drawn in the UI as "learned from request traces".

Requirement 3: Configurable time-window correlation
STATUS: MET, weakly tested
EVIDENCE: Config.correlation_window (5-300 s) via POST /api/config and a UI slider. It governs incident joining and event counting. The benchmark only runs the 30 s default.

Requirement 4: Detect abnormal services/components
STATUS: MET
EVIDENCE: 100% detection clean, 92.9% hard; median 2 s.

Requirement 5: Rank probable root causes
STATUS: MET, with the confidence caveat above
EVIDENCE: top-3 list with evidence per candidate; benchmark numbers above.

Requirement 6: Incident timeline
STATUS: MET
EVIDENCE: detector, correlator, rca, human and verifier entries, timestamped, in the UI and the post-mortem.

Requirement 7: Show failure/affected-service relationships
STATUS: MET, and not only visually. The same graph drives correlation and ranking; the timeline records "cart joined (cart depends on db, 1s after open)".
EVIDENCE: service map with root ring, affected nodes and failure-flow edges.

Requirement 8: Engineers can acknowledge incidents
STATUS: MET
EVIDENCE: POST /api/incidents/{id}/ack; UI button; time-to-acknowledge recorded.

Requirement 9: Engineers can resolve incidents
STATUS: MET
EVIDENCE: POST /api/incidents/{id}/resolve; resolution feeds memory and suppresses echo incidents for 15 s.

Bonus coverage: LLM explanation (wired, untested) | graph-based RCA (yes) | cascading failures (yes: db crash = 5 services, 1 incident) | thousands of events into one incident (yes, ~2,000 per incident) | remediation (yes) | learning from resolved incidents (yes, memory recall test passes) | post-incident report (yes).

==================================================
6. RESPONSE TO THE COLLABORATION CONTEXT (items A-G)
==================================================

A. The problem, as I understand it
Many services look broken at once because one upstream fault spreads downstream. The job is to name the originating service and its failure mode, show why, and let an engineer act, not merely flag anomalies. The loudest service is often a symptom: in our db-crash case the db writes almost no logs and its callers write hundreds. The LLM must explain evidence, not produce the diagnosis.

B. The proposed architecture
Evidence-consistent RCA: for each candidate hypothesis, score supporting evidence (onset, propagation coverage, trace paths, multimodal agreement, interventions) minus contradicting evidence, account for missing telemetry, run residual analysis for extra roots, then output confidence or abstain.

C. The three strongest ideas in the design doc
1. Contradiction handling. It is the real fix for "loud symptom beats quiet root" and makes "why NOT payments?" answerable.
2. Abstention plus honest confidence labels. It protects us in Q&A, where a judge will break the demo with an unknown fault.
3. Baselines A/B/C plus ablations. They turn "our model is better" into a measured claim and expose double-counted signals.

D. The three biggest remaining risks
1. Weights and simulator are co-designed. We wrote both the faults and the detector, so clean-mode 100% proves very little. Hard mode helps, but tuning weights on this simulator would be overfitting to ourselves.
2. Double counting. PageRank and "no abnormal dependency" both encode graph position (40% of the weight). Blame logs and error spans are correlated too. Only an ablation will show whether these are separate evidence or the same fact counted twice.
3. Scope versus the clock. The full design (contradiction engine, residual multi-root, missing telemetry, out-of-order events, cycles, ablations, calibration) is several days of work. We have about two hours.

E. Where I disagree with the design doc
1. "Weights should be empirically tested" is right, but not by tuning on our own simulator. Use baselines and ablations to justify the signals, and keep the weights fixed and disclosed.
2. The full evidence-consistency engine should not replace the ranker today. Add explicit contradictions and a propagation-coverage term to the existing ranker (a thin, testable layer). Don't rewrite it.
3. Merging declared and observed topology adds little for the demo. Observed-from-traces is the stronger story. The declared graph matters only as a fallback when traces are missing, so it belongs with the missing-telemetry work, not before it.
4. Out-of-order events and graph cycles should be stated limitations, not built today. Out-of-order handling needs event-time windowing across the whole pipeline.
5. Agreed without reservation: stop showing "90%". Use HIGH / MEDIUM / LOW with a named "RCA score".

F. What I need
- Confirmation that there is no second codebase. If one exists, send it.
- The judging rubric and the demo format (live laptop or video; minutes allowed).
- The team's decision on which change proposals below to build before 13:30.

G. How I audit
Every claim above was checked against the running code and the benchmark output, not the design doc. After each change: rerun bench.py (clean, hard, dual, baselines) and one headless-browser run of the full workflow, and report deltas in the next handoff.

==================================================
7. CHANGE PROPOSALS (change-control format)
==================================================

CP-1 BASELINES A/B/C IN THE BENCHMARK
PROPOSED CHANGE: in bench.py, score the same incidents with A = largest anomaly, B = anomaly + earliest onset, C = anomaly + onset + "no abnormal dependency", next to the full model. Report top-1 / top-3 for clean and hard.
CURRENT PROBLEM: we can't show the ranker beats anything simpler.
PS REQUIREMENT AFFECTED: 5 (ranking quality evidence)
EXPECTED BENEFIT: a measured "model vs baseline" table for the deck. Baseline A should fail on cascades where the loudest service is a symptom.
NEW RISKS: a baseline may match the full model on our simulator, which would weaken the claim (but that is exactly what we need to know).
IMPLEMENTATION COST: low (~20 min)
TEST REQUIRED: bench.py output table
STATUS: proposal

CP-2 HONEST CONFIDENCE + ABSTENTION
PROPOSED CHANGE: replace "%" with an RCA score and a label. HIGH = top candidate has no contradictions, margin over #2 >= 0.15, and at least 3 independent evidence types. MEDIUM = otherwise. LOW = margin < 0.05, or the top candidate is "downstream_symptom", or it has an explicit contradiction; LOW shows "Insufficient evidence for a confident root cause" and still lists candidates. Thresholds are fixed and disclosed, not tuned.
CURRENT PROBLEM: the uncalibrated % reads as a probability; the engine never abstains.
PS REQUIREMENT AFFECTED: 5
EXPECTED BENEFIT: defensible under questioning; the benchmark reports how often it abstains and how often HIGH is correct.
NEW RISKS: more abstentions on hard mode reduce "answered" coverage, so the benchmark must report both accuracy-when-answered and abstention rate.
IMPLEMENTATION COST: low-medium (~30 min, engine + UI + bench)
TEST REQUIRED: bench: accuracy by label; abstention rate clean vs hard
STATUS: proposal

CP-3 EXPLICIT CONTRADICTIONS + PROPAGATION COVERAGE
PROPOSED CHANGE: for each candidate compute (a) coverage = share of the other incident members that transitively depend on it; (b) contradictions: "an abnormal dependency went abnormal earlier", "N affected services cannot be reached from it", "its own telemetry is normal and only callers complain". Add coverage as a supporting term; apply contradictions as a multiplicative penalty; show both as +/- lines in the UI.
CURRENT PROBLEM: only additive support; the "why not payments?" question has no explicit answer.
PS REQUIREMENT AFFECTED: 5, 7
EXPECTED BENEFIT: the open-box answer the design doc wants. It should also cut hard-mode misses where a noise blip on a sibling outranks the real root.
NEW RISKS: coverage duplicates PageRank. CP-5 (ablation) must check this.
IMPLEMENTATION COST: medium (~40 min)
TEST REQUIRED: bench hard mode before/after; UI screenshot of +/- evidence
STATUS: proposal

CP-4 MISSING-TELEMETRY SCENARIOS
PROPOSED CHANGE: simulator option to drop all logs, or all traces, for a chosen service. When a service has no traces, fall back to a declared dependency graph so it can still be a candidate. Evidence coverage lowers the confidence label.
CURRENT PROBLEM: untested. A service with no traces falls out of the learned graph entirely, so it can't be named as root.
PS REQUIREMENT AFFECTED: 2, 5
EXPECTED BENEFIT: covers scenarios 8-9 of the design doc.
NEW RISKS: a declared graph in the engine reintroduces hand configuration; keep it as fallback only.
IMPLEMENTATION COST: medium (~35 min)
TEST REQUIRED: bench "missing logs" and "missing traces" rows
STATUS: proposal

CP-5 ABLATION
PROPOSED CHANGE: rerun the hard benchmark with each signal's weight set to 0 (PageRank, no-abnormal-dependency, onset, blame, signature, deploy, memory).
CURRENT PROBLEM: we don't know which signals matter or whether some are double-counted.
EXPECTED BENEFIT: a table showing each signal earns its place, or a signal to delete.
IMPLEMENTATION COST: low once CP-1 exists (~15 min; runs in under a minute)
TEST REQUIRED: the ablation table itself
STATUS: proposal

DEFERRED (state as limitations): out-of-order event handling, residual multi-root analysis inside one incident, graph cycles test, weight calibration, formal throughput test.

==================================================
8. RECOMMENDED ORDER FOR THE NEXT ~2 HOURS
==================================================

11:30 CP-1 baselines -> 11:50 CP-2 confidence/abstention -> 12:20 CP-5 ablation -> 12:35 CP-3 only if the earlier items are green -> 13:00 FREEZE, record backup video, rehearse. CP-4 only if the team says missing telemetry is a judging priority.

==================================================
9. QUESTIONS FOR THE HUMAN TEAM
==================================================

1. Is there a second codebase anywhere?
2. Which of CP-1 to CP-5 are approved?
3. Live demo or video? How many minutes?
4. Do we have an Azure OpenAI or Groq key for the LLM explanation?
