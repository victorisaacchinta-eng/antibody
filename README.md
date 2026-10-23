# Antibody

[![tests](https://github.com/victorisaacchinta-eng/antibody/actions/workflows/tests.yml/badge.svg)](https://github.com/victorisaacchinta-eng/antibody/actions/workflows/tests.yml)

Antibody is a root cause analysis engine for microservices. It takes in logs, metrics, traces and service events,
learns the service dependency graph from the traces, groups everything that goes wrong in a time window into one
incident, and ranks the likely root causes with evidence, an RCA score and a confidence label (HIGH / MEDIUM / LOW).
When the evidence is weak it says "insufficient evidence" instead of picking one. An engineer acknowledges the
incident, applies the suggested fix and resolves it, and Antibody remembers what worked for next time.

We built it as team Jarvis for the Microsoft Codathon, Problem 3: *AI-Powered Incident Root Cause Analysis Engine*.

## Run it

You need Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cd backend && uvicorn main:app --port 8000
```

Open http://localhost:8000. The overview page shows a live event stream and the benchmark numbers, and the status chip
says "learning baseline" for about 20 seconds. Go to **Break it yourself** (or `/console`) and press **Crash the
database**. The other page is `/loadtest`, for authenticated API load testing.

For AI-written explanations, set one of these before starting the server. Without a key the explanation comes from a
template built from the same evidence, so the demo works offline.

```bash
# Azure OpenAI
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com AZURE_OPENAI_API_KEY=... AZURE_OPENAI_DEPLOYMENT=<deployment>
# or any OpenAI-compatible API (Groq by default)
export LLM_API_KEY=... LLM_MODEL=llama-3.3-70b-versatile
```

Benchmark: `cd backend && python bench.py` (about 7 seconds). Tests: `pip install pytest && cd backend && python -m
pytest -q`. There are eight, covering things like a database crash ranked first with HIGH confidence, a bad deploy
recognised and rolled back, a repeat failure recalled from memory, healthy traffic raising no incidents, and the
engine never reading the simulator's ground truth. GitHub Actions runs the tests and the benchmark on every push.

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

Detection, correlation, ranking and suggestions are deterministic and replayable. The LLM only rewrites evidence the
engine already computed, its output is labelled as AI-generated, and if it is down nothing breaks.

The 7 services are simulated in `sim.py`, but `POST /api/ingest` accepts the same schema from anything. A cascading db
crash touches 5 services and still ends up as 1 incident with db ranked first. The engine is in `engine.py`, the
benchmark in `bench.py`, explanations and post-mortems in `reports.py`, and the API plus WebSocket feed in `main.py`.

## Benchmark

84 scenarios (7 services × 6 fault types × 2 seeds), each with a known ground-truth cause. The engine never sees the
ground truth; it is only used for scoring. Reproduce with `python bench.py`.

| Root cause ranked #1 | Clean | Hard mode (random red-herring blips) |
| --- | --- | --- |
| A · loudest anomaly only | 88.1% | 84.5% |
| B · anomaly + earliest onset | 100% | 75.0% |
| C · onset + dependency graph | 100% | 79.8% |
| **Antibody (full)** | **100%** | **84.5%** (top 3: 92.9%) |

Confidence labels in hard mode use fixed thresholds that were not tuned, and they are not probabilities. HIGH is
given 78.6% of the time and is right 98.5% of the time. LOW ("insufficient evidence") is given 8.3% of the time; a
forced guess there would have been right 42.9% of the time. Accuracy when it does answer (HIGH or MEDIUM) is 95.8%.

Two unrelated faults at once were found as separate incidents in 6/6 pairs, and a repeat failure is recognised from
memory. Median time to detect is 2 s, with about 2,000 events folded into each incident.

Some caveats on those numbers. On clean traffic, baselines B and C also hit 100%, so our simulator is too easy to
separate the models there. In hard mode the full model ties "loudest anomaly" overall: it wins every quiet-crash
cascade (the root goes silent while its callers get loud) but loses when the fault is high in the graph and a red
herring sits below it. The confidence label flags 5 of those 6 losses as LOW or MEDIUM. Explicit contradiction
handling aimed at those losses is the next thing we planned.

## API load testing

`/loadtest` runs authenticated load tests. Each virtual user (`name, email, device_token`, one per line) logs in at
`POST /api/demo/auth/login`, gets a bearer token bound to its device token, then sends N requests. `{{name}}`,
`{{email}}`, `{{device_token}}`, `{{token}}` and `{{i}}` work in the path, headers and body. You get throughput,
p50/p95/p99 latency, status codes, a timeline and a per-user table. The demo API runs through the simulated services,
so crashing the database during a load test makes orders fail with 503 while Antibody names db as the root cause.

External APIs are off unless you start the server with `ANTIBODY_ALLOW_EXTERNAL_LOADTEST=1`; then choose *Custom URL*
and set the login endpoint, body and token field (`https://dummyjson.com` with `/auth/login` and token field
`accessToken` works). Runs are capped at 2,000 requests and 50 concurrent users.

## Deploy

On Render, *New + → Blueprint* with this repo; `render.yaml` does the rest. The free plan sleeps after 15 minutes idle
and takes about a minute to wake. Vercel won't work because Antibody is a long-running server (a background simulation
loop plus a WebSocket feed). The public link has no login, so anyone who has it can inject faults into the simulation.

## Limitations

- The 7 services are simulated. The ingest API accepts real data, but we have not connected a real system yet.
- Fault signatures are pattern rules, so a new kind of failure gets "investigate", not a confident fix.
- Memory is in-process and resets on restart; production would persist it.
- No auth on the API. It is a local demo, and the next step is token auth plus rate limits.

## License

MIT. See `LICENSE`.
