"""
Antibody - simulated 7-service commerce system that emits logs, metrics,
traces and service events in one common schema.

The problem statement asks us to "accept simulated logs, metrics, traces and
service events". This module is the simulator; anything real could push the
same schema to POST /api/ingest instead.

Faults propagate up the call graph one hop per tick (a caller sees its
dependency's failure on the next request cycle), which is what gives the
root cause an earlier onset than its symptoms - exactly like production.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# value = services this service calls (its downstream dependencies)
DEPENDS_ON: dict[str, list[str]] = {
    "gateway": ["auth", "catalog", "cart"],
    "auth": [],
    "catalog": [],
    "cart": ["payments", "orders"],
    "payments": ["db"],
    "orders": ["db"],
    "db": [],
}
SERVICES = list(DEPENDS_ON)

FAULT_TYPES = ["crash", "latency_spike", "error_spike", "memory_leak", "cpu_burn", "bad_deploy"]

# which remediation actually fixes which fault (ground truth, used only by the simulator)
REMEDIATION_FIXES = {
    "restart": {"crash", "error_spike", "memory_leak", "cpu_burn"},
    "rollback": {"bad_deploy"},
    "scale_out": {"latency_spike", "cpu_burn"},
}

BASE = {"cpu": 22.0, "mem": 35.0, "latency": 35.0, "error_rate": 0.4}

ENDPOINTS = {
    "gateway": ["GET /", "GET /api/home", "GET /api/cart"],
    "auth": ["POST /login", "GET /session"],
    "catalog": ["GET /products", "GET /products/{id}"],
    "cart": ["POST /cart/items", "GET /cart", "POST /checkout"],
    "payments": ["POST /charge", "POST /refund"],
    "orders": ["POST /orders", "GET /orders/{id}"],
    "db": ["SELECT orders", "INSERT payment", "UPDATE inventory"],
}


@dataclass
class ServiceState:
    name: str
    cpu: float = BASE["cpu"]
    mem: float = BASE["mem"]
    own_latency: float = BASE["latency"]
    own_error: float = BASE["error_rate"]
    latency: float = BASE["latency"]      # effective, includes dependency impact
    error_rate: float = BASE["error_rate"]
    availability: float = 100.0
    version: str = "v2.3.0"
    prev_version: str = "v2.2.9"
    active_fault: str | None = None
    fault_started_at: float | None = None
    crash_logged: bool = False
    replicas: int = 2


def _event(ts, service, kind, name, value=None, level=None, message=None, **labels):
    e = {"ts": ts, "service": service, "kind": kind, "name": name}
    if value is not None:
        e["value"] = value
    if level:
        e["level"] = level
    if message:
        e["message"] = message
    if labels:
        e["labels"] = labels
    return e


class World:
    def __init__(self, rng: random.Random | None = None, requests_per_tick: int = 20, noise: float = 0.0):
        self.rng = rng or random.Random()
        self.noise = noise  # per-service, per-tick chance of a harmless 1-tick blip (red herrings)
        self.services = {n: ServiceState(n) for n in SERVICES}
        self.requests_per_tick = requests_per_tick
        self._trace_seq = 0
        # effective values from the previous tick; callers see these (one-hop lag)
        self._prev_err = {n: BASE["error_rate"] for n in SERVICES}
        self._prev_lat = {n: BASE["latency"] for n in SERVICES}
        self._prev_crashed = {n: False for n in SERVICES}

    # ---------- chaos + remediation ----------

    def inject(self, service: str, fault: str, now: float) -> list[dict]:
        s = self.services[service]
        s.active_fault, s.fault_started_at, s.crash_logged = fault, now, False
        events = []
        if fault == "bad_deploy":
            s.prev_version = s.version
            major, minor, patch = s.version[1:].split(".")
            s.version = f"v{major}.{minor}.{int(patch) + 1}"
            events.append(_event(now, service, "event", "deploy", message=f"deployed {service} {s.version}",
                                 version=s.version, previous=s.prev_version))
        return events

    def remediate(self, service: str, action: str, now: float) -> tuple[bool, list[dict]]:
        s = self.services[service]
        events = [_event(now, service, "event", action, message=f"{action} executed on {service}")]
        fixed = s.active_fault in REMEDIATION_FIXES.get(action, set())
        if action == "rollback" and s.active_fault == "bad_deploy":
            s.version = s.prev_version
        if action == "scale_out":
            s.replicas += 1
        if fixed:
            s.active_fault = None
            s.fault_started_at = None
            s.cpu, s.mem = BASE["cpu"], BASE["mem"]
            s.own_latency, s.own_error = BASE["latency"], BASE["error_rate"]
        return fixed, events

    # ---------- simulation step ----------

    def tick(self, now: float) -> list[dict]:
        r = self.rng
        events: list[dict] = []

        # 1. own metrics (random walk around baseline, or fault-driven)
        for s in self.services.values():
            f = s.active_fault
            # the metric a fault drives is NOT pulled back to baseline while the fault is active
            s.cpu = min(99.0, s.cpu + r.uniform(12, 20)) if f == "cpu_burn" else _walk(r, s.cpu, BASE["cpu"], 6)
            s.mem = min(97.0, s.mem + r.uniform(5, 8)) if f == "memory_leak" else _walk(r, s.mem, BASE["mem"], 4)

            if f == "latency_spike":
                s.own_latency = min(2400.0, max(s.own_latency, 150) * r.uniform(1.3, 1.5))
            elif f == "memory_leak" and s.mem > 85:
                s.own_latency = min(900.0, s.own_latency + r.uniform(40, 90))
            elif f == "cpu_burn" and s.cpu > 80:
                s.own_latency = min(1200.0, s.own_latency + r.uniform(60, 140))
            else:
                s.own_latency = _walk(r, s.own_latency, BASE["latency"], 10)

            if f == "error_spike":
                s.own_error = min(65.0, max(s.own_error, 6) * r.uniform(1.3, 1.6))
            elif f == "bad_deploy":
                s.own_error = min(55.0, max(s.own_error, 18) * r.uniform(1.05, 1.2))
            elif f == "memory_leak" and s.mem > 85:
                s.own_error = min(40.0, s.own_error + r.uniform(4, 9))
            else:
                s.own_error = _walk(r, s.own_error, BASE["error_rate"], 0.6)

            if self.noise and not f and r.random() < self.noise:
                # transient blip: GC pause, noisy neighbour, a burst of bad requests
                if r.random() < 0.5:
                    s.own_latency *= r.uniform(3, 6)
                else:
                    s.own_error += r.uniform(8, 20)

        # 2. effective values: own + what the dependencies did LAST tick
        crashed_now = {n: self.services[n].active_fault == "crash" for n in SERVICES}
        for s in self.services.values():
            if crashed_now[s.name]:
                s.error_rate, s.latency, s.availability = 100.0, 0.0, 0.0
                s.cpu, s.mem = 0.0, 0.0
                continue
            deps = DEPENDS_ON[s.name]
            dep_err = max((self._prev_err[d] for d in deps), default=0.0)
            dep_lat = max((self._prev_lat[d] if not self._prev_crashed[d] else 2000.0 for d in deps), default=0.0)
            s.error_rate = min(100.0, s.own_error + 0.6 * dep_err)
            s.latency = min(5000.0, s.own_latency + 0.5 * dep_lat)
            s.availability = max(0.0, 100.0 - s.error_rate)
        self._prev_err = {n: self.services[n].error_rate for n in SERVICES}
        self._prev_lat = {n: self.services[n].latency for n in SERVICES}
        self._prev_crashed = crashed_now

        # 3. emit metrics + health events
        for s in self.services.values():
            if crashed_now[s.name]:
                events.append(_event(now, s.name, "event", "health_check_failed",
                                     message=f"{s.name} /health: connection refused"))
                events.append(_event(now, s.name, "metric", "availability", 0.0))
                continue
            for m in ("cpu", "mem", "latency", "error_rate", "availability"):
                events.append(_event(now, s.name, "metric", m, round(getattr(s, m), 2)))

        # 4. logs
        for s in self.services.values():
            events.extend(self._logs_for(s, now, crashed_now))

        # 5. traces (request trees flowing down from the gateway)
        for _ in range(self.requests_per_tick):
            events.extend(self._trace(now, crashed_now))
        return events

    def _logs_for(self, s: ServiceState, now: float, crashed: dict[str, bool]) -> list[dict]:
        r = self.rng
        out = []
        if crashed[s.name]:
            if not s.crash_logged:
                s.crash_logged = True
                out.append(_event(now, s.name, "log", "log", level="FATAL",
                                  message=f"process exited with code 137 (OOMKilled) pid={r.randint(1000, 9999)}"))
            return out  # a dead process writes nothing else

        n = r.randint(5, 9)
        err_n = sum(1 for _ in range(n) if r.random() * 100 < s.error_rate)
        deps = DEPENDS_ON[s.name]
        worst = max(deps, key=lambda d: self._prev_err[d] + (200 if self._prev_crashed[d] else 0), default=None)
        own_share = s.own_error / max(s.error_rate, 0.01)

        for i in range(n):
            ep = r.choice(ENDPOINTS[s.name])
            if i < err_n:
                if worst and (own_share < 0.5) and (self._prev_err[worst] > 5 or self._prev_crashed[worst]):
                    reason = ("connection refused" if self._prev_crashed[worst]
                              else "timeout after 2000ms" if self._prev_lat[worst] > 800
                              else "503 Service Unavailable")
                    out.append(_event(now, s.name, "log", "log", level="ERROR",
                                      message=f"{ep} failed: call to {worst} failed: {reason}", target=worst))
                else:
                    out.append(_event(now, s.name, "log", "log", level="ERROR", message=self._own_error_msg(s, ep)))
            else:
                lat = int(max(3, s.latency * r.uniform(0.7, 1.3)))
                out.append(_event(now, s.name, "log", "log", level="INFO", message=f"{ep} 200 {lat}ms"))

        # fault-specific warnings even on successful requests
        f = s.active_fault
        if f == "latency_spike" and r.random() < 0.8:
            out.append(_event(now, s.name, "log", "log", level="WARN",
                              message=f"slow query took {int(s.own_latency)}ms on {r.choice(ENDPOINTS[s.name])}"))
        if f == "memory_leak" and r.random() < 0.8:
            out.append(_event(now, s.name, "log", "log", level="WARN", message=f"heap usage {int(s.mem)}% after GC"))
        if f == "cpu_burn" and r.random() < 0.8:
            out.append(_event(now, s.name, "log", "log", level="WARN",
                              message=f"event loop lag {int(s.cpu * 6)}ms, cpu {int(s.cpu)}%"))
        for d in deps:
            if not self._prev_crashed[d] and self._prev_lat[d] > 700 and r.random() < 0.6:
                out.append(_event(now, s.name, "log", "log", level="WARN",
                                  message=f"slow upstream {d}: {int(self._prev_lat[d])}ms", target=d))
        return out

    def _own_error_msg(self, s: ServiceState, ep: str) -> str:
        f = s.active_fault
        if f == "bad_deploy":
            return f"{ep} 500 NullPointerException at CheckoutHandler.java:142 ({s.version})"
        if f == "memory_leak":
            return f"{ep} 500 OutOfMemoryError: Java heap space"
        if f == "latency_spike":
            return f"{ep} 504 query timeout"
        if f == "cpu_burn":
            return f"{ep} 503 worker pool saturated"
        return f"{ep} 500 internal error"

    def _trace(self, now: float, crashed: dict[str, bool]) -> list[dict]:
        r = self.rng
        self._trace_seq += 1
        tid = f"t{self._trace_seq:07d}"
        spans = []
        seq = [0]

        # Spans carry real offsets now (start_ms relative to the trace, span/parent ids)
        # so the UI can draw a waterfall. RNG draws happen in exactly the same order as
        # before, so benchmark results are unchanged.
        def call(parent: str | None, parent_id: str | None, svc: str, start: float) -> tuple[bool, float]:
            sid = f"{tid}.{seq[0]}"
            seq[0] += 1
            if crashed[svc]:
                dur = 3.0  # connection refused comes back fast
                spans.append(_event(now, svc, "trace", "span", value=dur, trace_id=tid, span_id=sid,
                                    parent_span=parent_id, parent=parent, start_ms=round(start, 1),
                                    status="error", error="connection refused"))
                return False, start + dur
            s = self.services[svc]
            ok = r.random() * 100 >= s.own_error
            children = DEPENDS_ON[svc]
            if svc == "gateway":
                children = [r.choice(["auth", "catalog", "cart", "cart"])]
            elif svc == "cart":
                children = [c for c in children if r.random() < 0.8] or [r.choice(children)]
            t = start + s.own_latency * 0.3          # work before calling dependencies
            for c in children:
                c_ok, t = call(svc, sid, c, t + 0.5)
                ok = c_ok and ok
            end = t + s.own_latency * 0.7 * r.uniform(0.8, 1.2)  # work after they return
            spans.append(_event(now, svc, "trace", "span", value=round(end - start, 1), trace_id=tid,
                                span_id=sid, parent_span=parent_id, parent=parent, start_ms=round(start, 1),
                                status="ok" if ok else "error"))
            return ok, end

        call(None, None, "gateway", 0.0)
        return spans


def _walk(r: random.Random, value: float, base: float, jitter: float) -> float:
    value += r.uniform(-jitter, jitter) * 0.3
    value += (base - value) * 0.3
    return max(0.0, value)
