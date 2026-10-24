"""
Authenticated API load testing.

Two parts:

1. A demo API with real bearer-token auth, served by this app and backed by the
   simulated services. Log in with name, email and device token, get a token bound
   to that device, then call protected endpoints. Latency and errors come from the
   live simulation, so a fault injected in the console shows up in the load test.

2. A load runner. Each virtual user gets its own variables ({{name}}, {{email}},
   {{device_token}}, then {{token}} after login) and runs login + N requests.
   Results: throughput, p50/p95/p99, status codes, a per-second timeline and a
   per-user breakdown.

External targets are off by default so a public deploy cannot be used to flood
other sites. Set ANTIBODY_ALLOW_EXTERNAL_LOADTEST=1 when running locally.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

_world = None


def bind(world) -> None:
    global _world
    _world = world


# ------------------------------------------------------------------ demo API

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TOKEN_TTL = 900
SESSIONS: dict[str, dict] = {}
PATHS = {"profile": ["gateway", "auth"],
         "orders": ["gateway", "auth", "cart", "orders", "payments", "db"]}


class Login(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    email: str = Field(..., max_length=120)
    device_token: str = Field(..., max_length=200)


class Order(BaseModel):
    item: str = Field("sku-1", max_length=60)
    qty: int = Field(1, ge=1, le=100)


def mask(v: str, keep: int = 4) -> str:
    return v if len(v) <= keep * 2 else f"{v[:keep]}…{v[-keep:]}"


def _session(authorization: str | None, device: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    s = SESSIONS.get(authorization[7:].strip())
    if not s or s["exp"] < time.time():
        raise HTTPException(401, "invalid or expired token")
    if device != s["device_token"]:
        raise HTTPException(403, "token is bound to a different device")
    return s


async def _through(path: list[str]) -> float:
    """Pass a request through the simulated services: real latency, real failures."""
    if _world is None:
        return 0.0
    svcs = [_world.services[n] for n in path if n in _world.services]
    down = [s for s in svcs if s.availability < 50]
    if down:
        raise HTTPException(503, f"{down[-1].name} unavailable")
    lat = max(s.latency for s in svcs)
    ok = 1.0
    for s in svcs:
        ok *= 1 - max(0.0, min(s.error_rate, 100) - 1.0) / 100   # ignore the ~0.4% background noise
    await asyncio.sleep(min(lat, 1500) / 1000)
    if random.random() > ok:
        raise HTTPException(500, "upstream error")
    return lat


@router.post("/api/demo/auth/login")
async def demo_login(body: Login):
    if not EMAIL_RE.match(body.email):
        raise HTTPException(400, "invalid email")
    if len(body.device_token) < 8:
        raise HTTPException(400, "device_token must be at least 8 characters")
    await _through(["gateway", "auth"])
    if len(SESSIONS) > 20000:
        SESSIONS.clear()
    tok = secrets.token_urlsafe(24)
    SESSIONS[tok] = {"name": body.name, "email": body.email,
                     "device_token": body.device_token, "exp": time.time() + TOKEN_TTL}
    return {"access_token": tok, "token_type": "bearer", "expires_in": TOKEN_TTL,
            "user": {"name": body.name, "email": body.email}}


@router.get("/api/demo/profile")
async def demo_profile(authorization: str | None = Header(None),
                       x_device_token: str | None = Header(None)):
    s = _session(authorization, x_device_token)
    await _through(PATHS["profile"])
    return {"name": s["name"], "email": s["email"], "device": mask(s["device_token"])}


@router.post("/api/demo/orders")
async def demo_order(body: Order, authorization: str | None = Header(None),
                     x_device_token: str | None = Header(None)):
    s = _session(authorization, x_device_token)
    lat = await _through(PATHS["orders"])
    return {"order_id": uuid.uuid4().hex[:10], "item": body.item, "qty": body.qty,
            "customer": s["email"], "path": PATHS["orders"], "sim_latency_ms": round(lat, 1)}


# ------------------------------------------------------------------ runner

VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(tpl: str, vars: dict, json_safe: bool = False) -> str:
    def sub(m):
        if m[1] not in vars:
            return m[0]
        v = str(vars[m[1]])
        return json.dumps(v)[1:-1] if json_safe else v
    return VAR_RE.sub(sub, tpl or "")


def parse_headers(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


class AuthCfg(BaseModel):
    enabled: bool = True
    login_path: str = "/api/demo/auth/login"
    login_body: str = '{"name":"{{name}}","email":"{{email}}","device_token":"{{device_token}}"}'
    token_field: str = "access_token"


class ReqCfg(BaseModel):
    method: str = Field("POST", pattern="^(GET|POST|PUT|PATCH|DELETE)$")
    path: str = "/api/demo/orders"
    headers: str = "Authorization: Bearer {{token}}\nX-Device-Token: {{device_token}}"
    body: str = '{"item":"sku-{{i}}","qty":1}'


class VUser(BaseModel):
    name: str = Field(..., max_length=80)
    email: str = Field(..., max_length=120)
    device_token: str = Field(..., max_length=200)


class RunCfg(BaseModel):
    target: str = ""
    auth: AuthCfg = AuthCfg()
    request: ReqCfg = ReqCfg()
    users: list[VUser] = Field(..., min_length=1, max_length=200)
    requests_per_user: int = Field(5, ge=1, le=50)
    concurrency: int = Field(10, ge=1, le=50)


RUNS: dict[str, dict] = {}
_runs_lock = threading.Lock()
MAX_TOTAL = 2000


def _call(method: str, url: str, headers: dict, body: str | None) -> tuple[int, float, str]:
    data = body.encode() if body and method != "GET" else None
    h = {"User-Agent": "Antibody-LoadTest/1.0", "Accept": "application/json, */*", **headers}
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read(4000).decode(errors="replace")
            return r.status, (time.perf_counter() - t0) * 1000, text
    except urllib.error.HTTPError as e:
        text = e.read(2000).decode(errors="replace")
        return e.code, (time.perf_counter() - t0) * 1000, text
    except Exception as e:  # timeout, refused, DNS
        return 0, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}"


def _run(run: dict, cfg: RunCfg, base: str) -> None:
    t_start = time.perf_counter()

    def rec(**kw):
        kw["t"] = round(time.perf_counter() - t_start, 3)
        with _runs_lock:
            run["records"].append(kw)

    def vu(idx: int, u: VUser):
        row = run["users"][idx]
        vars = {"name": u.name, "email": u.email, "device_token": u.device_token, "user": idx + 1}
        if cfg.auth.enabled:
            code, ms, text = _call("POST", base + render(cfg.auth.login_path, vars),
                                   {"Content-Type": "application/json"},
                                   render(cfg.auth.login_body, vars, json_safe=True))
            rec(user=idx, phase="login", status=code, ms=round(ms, 1))
            try:
                vars["token"] = json.loads(text)[cfg.auth.token_field] if code == 200 else ""
            except Exception:
                vars["token"] = ""
            row["token"] = "issued" if vars["token"] else f"failed ({code or 'no response'})"
            if run["sample"] is None and code != 200:
                run["sample_login_error"] = {"user": u.email, "status": code, "response": text[:300]}
        for k in range(cfg.requests_per_user):
            if run["cancel"]:
                return
            vars["i"] = idx * cfg.requests_per_user + k + 1
            hdrs = {h: render(v, vars) for h, v in parse_headers(cfg.request.headers).items()}
            body = render(cfg.request.body, vars, json_safe=True) if cfg.request.method != "GET" else None
            url = base + render(cfg.request.path, vars)
            code, ms, text = _call(cfg.request.method, url, hdrs, body)
            rec(user=idx, phase="request", status=code, ms=round(ms, 1))
            row["sent"] += 1
            row["ok" if 200 <= code < 300 else "err"] += 1
            row["ms"] += ms
            if run["sample"] is None and 200 <= code < 300:
                shown = {h: (("Bearer " + mask(vars.get("token", ""), 6)) if h.lower() == "authorization"
                             else (mask(v) if "token" in h.lower() else v)) for h, v in hdrs.items()}
                run["sample"] = {"method": cfg.request.method, "url": url, "headers": shown,
                                 "body": body, "status": code, "response": text[:600]}

    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            list(pool.map(lambda a: vu(*a), enumerate(cfg.users)))
        run["state"] = "cancelled" if run["cancel"] else "done"
    except Exception as e:
        run["state"], run["error"] = "error", str(e)
    run["elapsed"] = round(time.perf_counter() - t_start, 2)


def snapshot(run: dict) -> dict:
    with _runs_lock:
        recs = list(run["records"])
    reqs = [r for r in recs if r["phase"] == "request"]
    logins = [r for r in recs if r["phase"] == "login"]
    ok = [r for r in reqs if 200 <= r["status"] < 300]
    elapsed = run.get("elapsed") or (time.time() - run["started"])
    codes: dict[str, int] = {}
    for r in recs:
        key = str(r["status"] or "ERR")
        codes[key] = codes.get(key, 0) + 1
    span = max([r["t"] for r in reqs], default=0)
    step = 0.1 if span < 4 else 0.25 if span < 10 else 0.5 if span < 20 else 1.0
    timeline: dict[int, dict] = {}
    for r in reqs:
        b = timeline.setdefault(int(r["t"] / step), {"n": 0, "err": 0, "ms": []})
        b["n"] += 1
        b["err"] += 0 if 200 <= r["status"] < 300 else 1
        b["ms"].append(r["ms"])
    lat = [r["ms"] for r in reqs]
    return {
        "id": run["id"], "state": run["state"], "error": run.get("error"),
        "target": run["target"], "elapsed": round(elapsed, 2),
        "total": run["total"], "done": len(reqs),
        "requests": len(reqs), "ok": len(ok), "failed": len(reqs) - len(ok),
        "success_rate": round(100 * len(ok) / len(reqs), 1) if reqs else None,
        "rps": round(len(reqs) / elapsed, 1) if elapsed > 0 and reqs else 0,
        "p50": pct(lat, 50), "p95": pct(lat, 95), "p99": pct(lat, 99),
        "max": round(max(lat), 1) if lat else None,
        "logins": {"attempted": len(logins), "ok": sum(1 for r in logins if r["status"] == 200),
                   "p50": pct([r["ms"] for r in logins], 50)},
        "codes": codes,
        "step": step,
        "timeline": [{"s": k, "t": round(k * step, 2), "n": round(b["n"] / step, 1),
                      "err": round(b["err"] / step, 1), "p95": pct(b["ms"], 95)}
                     for k, b in sorted(timeline.items())],
        "users": [{**u, "avg": round(u["ms"] / u["sent"], 1) if u["sent"] else None, "ms": None}
                  for u in run["users"]],
        "sample": run["sample"], "login_error": run.get("sample_login_error"),
    }


@router.post("/api/loadtest/run")
def start(cfg: RunCfg, request: Request):
    total = len(cfg.users) * cfg.requests_per_user
    if total > MAX_TOTAL:
        raise HTTPException(400, f"max {MAX_TOTAL} requests per run (you asked for {total})")
    if any(r["state"] == "running" for r in RUNS.values()):
        raise HTTPException(409, "a load test is already running")
    target = cfg.target.strip().rstrip("/")
    if target:
        if not re.match(r"^https?://", target):
            raise HTTPException(400, "target must start with http:// or https://")
        if os.environ.get("ANTIBODY_ALLOW_EXTERNAL_LOADTEST") != "1":
            raise HTTPException(403, "external targets are disabled on this server; run locally with "
                                     "ANTIBODY_ALLOW_EXTERNAL_LOADTEST=1")
        base = target
    else:
        port = os.environ.get("PORT") or request.url.port or 8000
        base = f"http://127.0.0.1:{port}"
    rid = uuid.uuid4().hex[:8]
    run = {"id": rid, "state": "running", "started": time.time(), "target": target or "this server",
           "total": total, "records": [], "cancel": False, "sample": None,
           "users": [{"name": u.name, "email": u.email, "device": mask(u.device_token),
                      "token": "n/a" if not cfg.auth.enabled else "pending",
                      "sent": 0, "ok": 0, "err": 0, "ms": 0.0} for u in cfg.users]}
    RUNS[rid] = run
    for old in list(RUNS)[:-5]:
        RUNS.pop(old, None)
    threading.Thread(target=_run, args=(run, cfg, base), daemon=True).start()
    return {"id": rid, "total": total}


@router.get("/api/loadtest/{rid}")
def status(rid: str):
    run = RUNS.get(rid) if rid != "latest" else (list(RUNS.values())[-1] if RUNS else None)
    if not run:
        raise HTTPException(404, "no such run")
    return snapshot(run)


@router.post("/api/loadtest/{rid}/cancel")
def cancel(rid: str):
    run = RUNS.get(rid)
    if not run:
        raise HTTPException(404, "no such run")
    run["cancel"] = True
    return {"ok": True}
