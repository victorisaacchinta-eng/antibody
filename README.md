# Antibody

[![tests](https://github.com/victorisaacchinta-eng/antibody/actions/workflows/tests.yml/badge.svg)](https://github.com/victorisaacchinta-eng/antibody/actions/workflows/tests.yml) ![python](https://img.shields.io/badge/python-3.11%2B-52a8ff) ![license](https://img.shields.io/badge/license-MIT-62c073)

> Finds the one event that actually caused the failure, out of thousands.

Antibody is an AI-assisted root cause analysis engine for microservices. It ingests logs, metrics, traces and service events, learns the service dependency graph from traces, correlates everything that happens in a time window into **one** incident, and ranks the probable root causes with evidence, an RCA score and an honest confidence label (HIGH / MEDIUM / LOW). When it isn't sure, it says "insufficient evidence" instead of guessing. Engineers acknowledge, apply the suggested fix, and resolve. Antibody remembers what fixed it for next time.

Built for the Microsoft Codathon, Problem 3: *AI-Powered Incident Root Cause Analysis Engine*.

## Run it (Mac)

You need Python 3.11+.

**Step 1.** Open Terminal in the `antibody` folder and create a virtual environment:

```bash
python3 -m venv .venv && source .venv/bin/activate
```

Expected: your prompt now starts with `(.venv)`.

**Step 2.** Install dependencies:

```bash
pip install -r backend/requirements.txt
```

Expected: ends with `Successfully installed fastapi ... uvicorn ...`.

**Step 3.** Start the server:

```bash
cd backend && uvicorn main:app --port 8000
```

Expected: `Uvicorn running on http://127.0.0.1:8000`.

**Step 4.** Open http://localhost:8000 in Chrome. You get the overview page: halftone hero, live event stream, bento cards and benchmark numbers. The hero chip shows *learning baseline* for about 20 seconds.

**Step 5.** Click **Break it yourself** (or go to http://localhost:8000/console) and press **Crash the database**.

Pages: `/` overview (live traces, failure clustering, incident replay, benchmark), `/console` the live incident console, `/loadtest` authenticated API load testing.

**Optional, AI explanations:** before step 3, set one of these. Without a key, Antibody writes the explanation from a template built from the same evidence, so the demo never depends on Wi-Fi.

```bash
# Azure OpenAI
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com AZURE_OPENAI_API_KEY=... AZURE_OPENAI_DEPLOYMENT=<deployment>
# or any OpenAI-compatible API (Groq default)
export LLM_API_KEY=... LLM_MODEL=llama-3.3-70b-versatile
```

**Benchmark from the terminal:** `cd backend && python bench.py`. Takes about 7 seconds.

**Tests:** `pip install pytest && cd backend && python -m pytest -q`. Eight checks, including: a database crash is ranked first with HIGH confidence, a bad deploy is recognised and rolled back, a repeat failure is recalled from memory, healthy traffic raises no incidents, and the engine never reads the simulator's ground truth. GitHub Actions runs them and the benchmark on every push.

## API load testing (authenticated)

Open `/loadtest`. Paste or generate virtual users (one per line: `name, email, device_token`), then run.

- **Per-user variables:** `{{name}}`, `{{email}}`, `{{device_token}}`, `{{token}}` (the bearer token from login) and `{{i}}` (request number) can be used in the path, headers and body.
- **Authenticated flow:** each virtual user first calls `POST /api/demo/auth/login`, receives its own bearer token bound to its device token, then sends N requests with `Authorization: Bearer {{token}}` and `X-Device-Token: {{device_token}}`. A wrong device token returns 403, a missing or expired token returns 401.
- **Results:** throughput, success rate, p50/p95/p99 latency, status codes, a timeline chart, a per-user table and the first request/response exactly as sent (tokens masked).
- **Tied to RCA:** the demo API runs through the simulated services. Crash the database in the console during a load test: orders fail with 503 in the load test while Antibody names db as the root cause.
- **Any live public API:** start the server with `ANTIBODY_ALLOW_EXTERNAL_LOADTEST=1 uvicorn main:app --port 8000`, choose *Custom URL*, and set the login endpoint, login body and token field for that API. Example that works today: base URL `https://dummyjson.com`, login `/auth/login` with body `{"username":"emilys","password":"emilyspass"}`, token field `accessToken`, request `GET /auth/me` with header `Authorization: Bearer {{token}}`.
- **Safety:** max 2,000 requests and 50 concurrent users per run. External targets stay off unless that flag is set, so the public deploy can't be used to flood other sites.

## How it works

```
simulator / POST /api/ingest
        │  logs, metrics, traces, service events (one common schema)
        ▼
   ingest ── learn dependency graph from trace spans (caller → callee)
        │
   detect ── per-service EWMA baselines, z-scores, error-log and error-span ratios
        │    debounce: 2 bad ticks in a row (or a failed health check) before it counts
        ▼
 correlate ── new abnormal service joins an open incident if it calls into the failure
        │     (within a configurable time window); unrelated failures get their own incident
        ▼
      rank ── for each suspect, 8 signals → RCA score (0 to 1, not a probability)
        │     personalised PageRank on the anomaly graph · no abnormal dependencies ·
        │     earliest onset · share of callers' error logs blaming it · local fault
        │     signature (crash, bad deploy, leak, CPU, slow queries) · recent deploy ·
        │     anomaly size · match with a past resolved incident
        ▼
  verdict ── HIGH / MEDIUM / LOW from fixed rules: lead over #2, independent evidence
        │    types, contradictions. LOW = abstain ("insufficient evidence")
        ▼
   suggest ── fault signature → remediation (restart / roll back / scale out)
        ▼
    human ── acknowledge → apply fix → verify recovery → resolve → remember
        ▼
   report ── plain-English explanation (LLM optional) + one-click post-mortem
```

**Design rule: automation before AI.** Every decision (detect, correlate, rank, suggest) is deterministic and replayable. The LLM only rewrites evidence the engine already computed into plain English, and the output is labelled as AI-generated. If the LLM is down, nothing breaks.

## Benchmark (reproducible, `python bench.py`)

84 scenarios (7 services × 6 fault types × 2 seeds), each with a known ground-truth cause. The engine never sees the ground truth; it is only used for scoring.

| Root cause ranked #1 | Clean | Hard mode (random red-herring blips) |
| --- | --- | --- |
| A · loudest anomaly only | 88.1% | 84.5% |
| B · anomaly + earliest onset | 100% | 75.0% |
| C · onset + dependency graph | 100% | 79.8% |
| **Antibody (full)** | **100%** | **84.5%** (top 3: 92.9%) |

**Confidence labels (hard mode).** Fixed thresholds, not tuned, and not probabilities. HIGH is given 78.6% of the time and is right 98.5% of the time. LOW (abstain, "insufficient evidence") is given 8.3% of the time; a forced guess there would have been right 42.9% of the time. Accuracy when it answers (HIGH or MEDIUM): 95.8%.

Also: two unrelated faults at once, both found as separate incidents in 6/6 pairs. A repeat failure is recognised from memory. Median time to detect: 2 s. About 2,000 events folded per incident.

**Read these honestly.**

- On clean traffic, baselines B and C also hit 100%. Our simulator is too easy to separate the models there.
- In hard mode the full model ties "loudest anomaly" overall. It wins every quiet-crash cascade (the root goes silent while its callers get loud). It loses when the fault is high in the graph and a red herring sits below it. The confidence label flags 5 of those 6 losses as LOW or MEDIUM.
- Planned next: explicit contradiction handling (CP-3), aimed at exactly those losses.

## Problem-statement coverage

| Requirement | Where |
| --- | --- |
| Accept simulated logs, metrics, traces, service events | `sim.py`; `POST /api/ingest` accepts the same schema from anything |
| Build a dependency graph | learned from trace spans in `engine.graph()` |
| Correlate events within configurable time windows | `correlation_window`, slider in the UI |
| Identify abnormal services | EWMA z-scores + log/span error ratios + health checks |
| Ranked list of possible root causes | `_rank()`: top 3 with RCA score and evidence, plus a `verdict()` label |
| Incident timeline | every detector, correlator, RCA and human step, timestamped |
| Relationships between failure and affected services | service map: root ring, affected nodes, failure-flow edges |
| Acknowledge and resolve | engineer actions in the UI and API |
| Bonus: LLM explanation | `reports.explain()`, Azure OpenAI or any OpenAI-compatible API |
| Bonus: graph-based RCA | personalised PageRank on the anomaly subgraph |
| Bonus: cascading failures | db crash: 5 services, 1 incident, db ranked #1 |
| Bonus: thousands of events into one incident | counter on every incident |
| Bonus: suggest remediation | signature → action, one click |
| Bonus: learn from resolved incidents | memory of (service, signature) → fix that worked |
| Bonus: post-incident report | one-click Markdown post-mortem |

## Limitations (say these before a judge does)

- The 7 services are simulated. The ingest API accepts real data, but we have not connected a real system yet.
- Fault signatures are pattern rules, so a new kind of failure gets "investigate", not a confident fix.
- Memory is in-process. It resets on restart; production would persist it.
- No auth on the API. It is a local demo, and the next step is token auth plus rate limits.

## Repository layout

```
backend/   sim.py (simulated services) · engine.py (detect, correlate, rank) · bench.py
           loadtest.py (authenticated demo API + load runner)
           reports.py (explanations, post-mortem) · main.py (API + WebSocket) · tests/
frontend/  index.html (overview) · console.html (live incident console) · loadtest.html
docs/      design handoffs
```

## API

`GET /api/state` · `GET /api/incidents/{id}` · `POST /api/chaos` · `POST /api/incidents/{id}/ack | remediate | resolve | explain` · `GET /api/incidents/{id}/postmortem` · `POST /api/ingest` · `POST /api/config` · `GET /api/benchmark` · `WS /ws` · `POST /api/demo/auth/login` · `GET /api/demo/profile` · `POST /api/demo/orders` · `POST /api/loadtest/run` · `GET /api/loadtest/{id}`

## Deploy

**Render (recommended):** push this repo to GitHub, then on render.com choose *New + → Blueprint* and pick the repo. `render.yaml` sets everything up: Python 3.11, the build and start commands, and a health check. The free plan sleeps after 15 minutes idle; the first visit after that takes about a minute to wake, so open the link a couple of minutes before a demo.

**Why not Vercel:** Antibody is a long-running server (a background simulation loop plus a live WebSocket feed). Vercel runs short-lived functions, so the loop would stop between requests.

**Optional AI explanations:** add `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_DEPLOYMENT` (or `LLM_API_KEY`) as environment variables in Render. Never commit keys.

**Heads-up:** the public link has no login, so anyone who has it can inject faults. That's fine for a demo of a simulation; don't point it at real systems as is.

## Team

Built by Team Ace Azael for the Microsoft Codathon. Teammates: fork this repo to keep a copy on your profile.

## License

MIT. See `LICENSE`.
