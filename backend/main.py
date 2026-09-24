"""
Antibody API + live dashboard server.

    uvicorn main:app --port 8000
    open http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

import bench
import reports
from engine import Engine, SIGNATURES
from sim import FAULT_TYPES, SERVICES, World

app = FastAPI(title="Antibody", version="0.3")

world = World()
engine = Engine()
injected: dict[str, dict] = {}          # ground truth, shown only in the chaos panel
lock = threading.Lock()
bench_cache: dict = {"running": False, "result": None}


def health(name: str) -> float:
    s = engine.sig.get(name)
    if not s:
        return 100.0
    if s.health_failed > 0.6:
        return 0.0
    m = s.latest
    return round(max(0.0, min(100.0, 100 - m.get("error_rate", 0) * 1.2 - max(0.0, m.get("latency", 0) - 150) / 20)), 1)


def inc_summary(i) -> dict:
    top = i.causes[0] if i.causes else None
    return {"id": i.id, "status": i.status, "severity": i.severity, "services": i.services,
            "opened_at": i.opened_at, "resolved_at": i.resolved_at, "event_count": i.event_count,
            "top": {"service": top["service"], "score": top["score"],
                    "label": (i.verdict or {}).get("label")} if top else None,
            "transient": i.transient, "recovered": i.recovered}


def state() -> dict:
    deps, _ = engine.graph()
    warm = engine.ticks > engine.cfg.warmup_ticks
    incs = engine.incidents
    return {
        "now": time.time(),
        "warm": warm,
        "warmup": {"tick": min(engine.ticks, engine.cfg.warmup_ticks), "of": engine.cfg.warmup_ticks},
        "counters": {
            "total_events": engine.total_events,
            "eps": round(engine.eps),
            "open": sum(1 for i in incs if i.status != "resolved"),
            "incidents": len(incs),
            "transient": sum(1 for i in incs if i.transient),
            "correlated": sum(i.event_count for i in incs if not i.transient),
            "real": sum(1 for i in incs if not i.transient),
        },
        "services": {
            n: {"health": health(n), "anomalous": s.anomalous and s.persistent, "suspect": s.onset is not None,
                "metrics": {k: round(v, 1) for k, v in s.latest.items()},
                "signals": {"own_err": round(s.own_err, 1), "blame_in": round(s.blame_in, 1),
                            "span_err": round(s.span_err, 1), "log_err": round(s.log_err, 1)},
                "version": world.services[n].version if n in world.services else None,
                "replicas": world.services[n].replicas if n in world.services else None}
            for n, s in engine.sig.items()
        },
        "graph": [{"from": p, "to": c, "count": engine.edges[(p, c)]} for p in deps for c in deps[p]],
        "incidents": [inc_summary(i) for i in reversed(incs[-30:])],
        "logs": list(engine.recent_logs)[-18:],
        "memory": engine.memory[-8:],
        "injected": [{"service": k, **v} for k, v in injected.items()],
        "config": {"correlation_window": engine.cfg.correlation_window, "noise": world.noise,
                   "z_threshold": engine.cfg.z_threshold},
        "faults": FAULT_TYPES,
        "service_list": SERVICES,
    }


def detail(i) -> dict:
    return {**inc_summary(i), "verdict": i.verdict, "causes": i.causes, "suggestions": i.suggestions, "similar": i.similar,
            "timeline": i.timeline, "raw_alerts": i.raw_alerts, "acked_by": i.acked_by, "acked_at": i.acked_at,
            "resolved_by": i.resolved_by, "remediations": i.remediations, "explanation": i.explanation}


sockets: set[WebSocket] = set()


async def loop():
    while True:
        now = time.time()
        with lock:
            engine.ingest(world.tick(now))
            engine.tick(now)
            for k in [k for k, v in injected.items() if world.services[k].active_fault is None]:
                injected.pop(k)
            payload = state()
        for ws in list(sockets):
            try:
                await ws.send_json(payload)
            except Exception:
                sockets.discard(ws)
        await asyncio.sleep(1.0)


def _run_bench():
    bench_cache["running"] = True
    try:
        bench_cache["result"] = bench.run_all()
    finally:
        bench_cache["running"] = False


BENCH_FILE = Path(__file__).resolve().parent / "bench_result.json"


@app.on_event("startup")
async def startup():
    asyncio.create_task(loop())
    # a saved result (from `python bench.py --save`) makes the Benchmark tab instant on
    # small cloud instances; "Run again" still recomputes it live
    if BENCH_FILE.exists():
        bench_cache["result"] = json.loads(BENCH_FILE.read_text())
    else:
        threading.Thread(target=_run_bench, daemon=True).start()


# ------------------------------------------------------------ read

@app.get("/api/state")
def get_state():
    with lock:
        return state()


@app.get("/api/incidents/{iid}")
def get_incident(iid: int):
    with lock:
        i = engine.get(iid)
        if not i:
            raise HTTPException(404, "no such incident")
        return detail(i)


@app.get("/api/incidents/{iid}/postmortem", response_class=PlainTextResponse)
def get_postmortem(iid: int):
    with lock:
        i = engine.get(iid)
        if not i:
            raise HTTPException(404, "no such incident")
        return reports.postmortem(i, engine)


# ------------------------------------------------------------ write

class Chaos(BaseModel):
    service: str
    fault: str


@app.post("/api/chaos")
def chaos(c: Chaos):
    if c.service not in SERVICES or c.fault not in FAULT_TYPES:
        raise HTTPException(400, "unknown service or fault")
    with lock:
        engine.ingest(world.inject(c.service, c.fault, time.time()))
        injected[c.service] = {"fault": c.fault, "since": time.time()}
    return {"ok": True}


class Actor(BaseModel):
    by: str = Field("on-call engineer", max_length=40)
    note: str | None = Field(None, max_length=300)


@app.post("/api/incidents/{iid}/ack")
def ack(iid: int, a: Actor):
    with lock:
        return {"ok": engine.ack(iid, a.by)}


class Remediation(BaseModel):
    service: str
    action: str
    by: str = Field("on-call engineer", max_length=40)


@app.post("/api/incidents/{iid}/remediate")
def remediate(iid: int, r: Remediation):
    if r.service not in SERVICES or r.action not in {"restart", "rollback", "scale_out", "investigate"}:
        raise HTTPException(400, "unknown service or action")
    with lock:
        if not engine.get(iid):
            raise HTTPException(404, "no such incident")
        fixed = False
        if r.action != "investigate":
            fixed, evs = world.remediate(r.service, r.action, time.time())
            engine.ingest(evs)
        engine.record_remediation(iid, r.service, r.action, r.by, fixed)
    return {"ok": True}


@app.post("/api/incidents/{iid}/resolve")
def resolve(iid: int, a: Actor):
    with lock:
        return {"ok": engine.resolve(iid, a.by, a.note)}


@app.post("/api/incidents/{iid}/explain")
def explain(iid: int):
    with lock:
        i = engine.get(iid)
        if not i:
            raise HTTPException(404, "no such incident")
    out = reports.explain(i, engine)      # network call happens outside the lock
    with lock:
        i.explanation = out
    return out


class Event(BaseModel):
    ts: float | None = None
    service: str = Field(..., max_length=64)
    kind: str = Field(..., pattern="^(metric|log|trace|event)$")
    name: str = Field("log", max_length=64)
    value: float | None = None
    level: str | None = Field(None, max_length=8)
    message: str | None = Field(None, max_length=2000)
    labels: dict = Field(default_factory=dict)


@app.post("/api/ingest")
def ingest(events: list[Event]):
    """Accept logs, metrics, traces and service events from any source in the common schema."""
    if len(events) > 5000:
        raise HTTPException(413, "max 5000 events per batch")
    with lock:
        engine.ingest([{**e.model_dump(exclude_none=True), "ts": e.ts or time.time()} for e in events])
    return {"accepted": len(events)}


class Cfg(BaseModel):
    correlation_window: float | None = Field(None, ge=5, le=300)
    noise: float | None = Field(None, ge=0, le=0.05)


@app.post("/api/config")
def set_config(c: Cfg):
    with lock:
        if c.correlation_window is not None:
            engine.cfg.correlation_window = c.correlation_window
        if c.noise is not None:
            world.noise = c.noise
        return {"ok": True}


@app.post("/api/benchmark/run")
def run_benchmark():
    if not bench_cache["running"]:
        threading.Thread(target=_run_bench, daemon=True).start()
    return {"started": True}


@app.get("/api/benchmark")
def get_benchmark():
    return bench_cache


@app.get("/api/signatures")
def signatures():
    return {k: {"action": v[0], "label": v[2]} for k, v in SIGNATURES.items()}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    sockets.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sockets.discard(websocket)


@app.get("/api/traces")
def list_traces(limit: int = 30):
    with lock:
        out = []
        for tid in list(engine.trace_ids)[-limit:][::-1]:
            spans = engine.traces.get(tid, [])
            if not spans:
                continue
            out.append({"id": tid, "spans": len(spans),
                        "duration": round(max(sp["start"] + sp["dur"] for sp in spans), 1),
                        "error": any(sp["status"] == "error" for sp in spans),
                        "services": sorted({sp["service"] for sp in spans})})
        return out


@app.get("/api/traces/{tid}")
def get_trace(tid: str):
    with lock:
        spans = [dict(sp) for sp in engine.traces.get(tid, [])]
    if not spans:
        raise HTTPException(404, "trace not in recent window")
    by_id = {sp["id"]: sp for sp in spans}
    for sp in spans:
        d, p = 0, sp["parent"]
        while p in by_id and d < 20:
            d, p = d + 1, by_id[p]["parent"]
        sp["depth"] = d
    spans.sort(key=lambda sp: (sp["start"], sp["depth"]))
    total = max(sp["start"] + sp["dur"] for sp in spans)
    return {"id": tid, "duration": round(total, 1), "spans": spans}


FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/console")
def console():
    return FileResponse(FRONTEND / "console.html")
