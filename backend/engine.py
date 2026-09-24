"""
Antibody RCA engine.

ingest events -> detect abnormal services -> correlate into ONE incident ->
rank root causes with evidence -> suggest remediation -> human acks/resolves
-> remember what fixed it.

Everything here is deterministic statistics and graph logic. The LLM (see
reports.py) only turns the evidence into plain English; it never decides.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

METRICS = ["cpu", "mem", "latency", "error_rate"]
MIN_STD = {"cpu": 4.0, "mem": 4.0, "latency": 10.0, "error_rate": 1.5}

PATTERNS = [
    ("fatal", re.compile(r"FATAL|exited with code|OOMKilled")),
    ("npe", re.compile(r"NullPointerException")),
    ("oom", re.compile(r"OutOfMemoryError|heap usage")),
    ("loop_lag", re.compile(r"event loop lag|worker pool saturated")),
    ("slow_query", re.compile(r"slow query|query timeout")),
]
BLAME_RE = re.compile(r"call to ([\w-]+) failed|slow upstream ([\w-]+)")

SIGNATURES = {
    "process_crash": ("restart", "Restart {s}", "Process crashed: exit 137, health checks refused"),
    "bad_deploy": ("rollback", "Roll back {s} from {ver} to {prev}", "5xx errors began right after a deploy"),
    "memory_leak": ("restart", "Restart {s} and capture a heap dump", "Memory climbing until requests fail"),
    "cpu_saturation": ("scale_out", "Scale {s} out by 1 replica", "CPU saturated, event loop lagging"),
    "latency_degradation": ("scale_out", "Scale {s} out and review slow queries", "Slow queries and timeouts"),
    "application_errors": ("restart", "Restart {s}; roll back if errors persist", "5xx errors originating inside this service"),
    "downstream_symptom": ("investigate", "Inspect {s}'s dependencies, no local fault found", "Errors point at a dependency, not at this service"),
}


@dataclass
class Config:
    correlation_window: float = 30.0   # seconds; configurable from the UI
    z_threshold: float = 4.0
    warmup_ticks: int = 20
    resolve_grace: float = 15.0


class Baseline:
    """Exponentially weighted mean/variance per metric."""

    def __init__(self):
        self.mean: dict[str, float] = {}
        self.var: dict[str, float] = {}

    def update(self, values: dict[str, float], alpha: float = 0.05):
        for m, v in values.items():
            if m not in self.mean:
                self.mean[m], self.var[m] = v, MIN_STD[m] ** 2
                continue
            d = v - self.mean[m]
            self.mean[m] += alpha * d
            self.var[m] = (1 - alpha) * (self.var[m] + alpha * d * d)

    def z(self, m: str, v: float) -> float:
        if m not in self.mean:
            return 0.0
        return abs(v - self.mean[m]) / max(self.var[m] ** 0.5, MIN_STD[m])


@dataclass
class Signals:
    """Rolling (decayed) evidence for one service."""
    name: str
    latest: dict = field(default_factory=dict)
    log_total: float = 0.0
    log_err: float = 0.0
    own_err: float = 0.0          # error logs that do NOT blame a dependency
    blame_in: float = 0.0         # callers' error logs pointing at this service
    span_total: float = 0.0
    span_err: float = 0.0
    health_failed: float = 0.0
    patterns: dict = field(default_factory=lambda: defaultdict(float))
    last_deploy: dict | None = None
    samples: deque = field(default_factory=lambda: deque(maxlen=6))
    anomalous: bool = False
    score: float = 0.0
    zs: dict = field(default_factory=dict)
    onset: float | None = None
    pending_onset: float | None = None
    healthy_streak: int = 0
    anom_streak: int = 0
    hard: bool = False

    @property
    def persistent(self) -> bool:
        """Debounce: one bad tick is a blip, two in a row (or a failed health check) is real."""
        return self.hard or self.anom_streak >= 2

    def decay(self):
        self.log_total *= 0.6
        self.log_err *= 0.6
        self.own_err *= 0.6
        self.blame_in *= 0.6
        self.span_total *= 0.6
        self.span_err *= 0.6
        self.health_failed *= 0.5
        for k in list(self.patterns):
            self.patterns[k] *= 0.8


@dataclass
class Incident:
    id: int
    opened_at: float
    services: list[str]
    severity: str = "SEV3"
    status: str = "open"            # open -> acknowledged -> resolved
    causes: list[dict] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)
    similar: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    event_count: int = 0
    raw_alerts: int = 0
    acked_by: str | None = None
    acked_at: float | None = None
    resolved_at: float | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    remediations: list[dict] = field(default_factory=list)
    recovered: bool = False
    transient: bool = False
    explanation: dict | None = None
    onsets: dict = field(default_factory=dict)   # frozen per-incident onset of each member
    verdict: dict | None = None                  # HIGH / MEDIUM / LOW (abstain) + why

    def log(self, ts: float, actor: str, message: str):
        self.timeline.append({"ts": ts, "actor": actor, "message": message})


class Engine:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config()
        self.sig: dict[str, Signals] = {}
        self.base: dict[str, Baseline] = {}
        self.incidents: list[Incident] = []
        self._ids = itertools.count(1)
        self.edges: dict[tuple[str, str], int] = defaultdict(int)
        self.events: deque = deque(maxlen=80000)       # (ts, service) for window counts
        self.recent_logs: deque = deque(maxlen=40)
        self.traces: dict[str, list] = {}             # trace_id -> spans (recent only)
        self.trace_ids: deque = deque()
        self.memory: list[dict] = []
        self.recently_resolved: dict[str, float] = {}
        self.total_events = 0
        self.eps = 0.0
        self._tick_events = 0
        self.ticks = 0
        self.now = 0.0

    # ------------------------------------------------------------ ingest

    def _s(self, name: str) -> Signals:
        if name not in self.sig:
            self.sig[name] = Signals(name)
            self.base[name] = Baseline()
        return self.sig[name]

    def ingest(self, events: list[dict]):
        open_incs = [i for i in self.incidents if i.status != "resolved"]
        for e in events:
            svc = e.get("service")
            if not svc:
                continue
            ts = e.get("ts", self.now)
            kind = e.get("kind")
            s = self._s(svc)
            labels = e.get("labels") or {}
            self.total_events += 1
            self._tick_events += 1
            self.events.append((ts, svc))
            for inc in open_incs:
                if svc in inc.services and not inc.recovered:
                    inc.event_count += 1

            if kind == "metric":
                s.latest[e["name"]] = float(e.get("value", 0.0))
            elif kind == "log":
                level = (e.get("level") or "INFO").upper()
                msg = e.get("message") or ""
                s.log_total += 1
                target = labels.get("target")
                if not target:
                    m = BLAME_RE.search(msg)
                    target = (m.group(1) or m.group(2)) if m else None
                if level in ("ERROR", "FATAL"):
                    s.log_err += 1
                    s.samples.append(msg)
                    if not target:
                        s.own_err += 1
                if target and target != svc and level in ("ERROR", "WARN"):
                    self._s(target).blame_in += 1.0 if level == "ERROR" else 0.4
                for key, rx in PATTERNS:
                    if rx.search(msg):
                        s.patterns[key] += 1
                if level != "INFO":
                    self.recent_logs.append({"ts": ts, "service": svc, "level": level, "message": msg})
            elif kind == "trace":
                tid = labels.get("trace_id")
                if tid:
                    if tid not in self.traces:
                        self.traces[tid] = []
                        self.trace_ids.append(tid)
                        if len(self.trace_ids) > 80:
                            self.traces.pop(self.trace_ids.popleft(), None)
                    self.traces[tid].append({"service": svc, "id": labels.get("span_id"),
                                             "parent": labels.get("parent_span"), "start": labels.get("start_ms", 0.0),
                                             "dur": float(e.get("value", 0.0)), "status": labels.get("status", "ok")})
                parent = labels.get("parent")
                if parent:
                    self.edges[(parent, svc)] += 1
                s.span_total += 1
                if labels.get("status") == "error":
                    s.span_err += 1
            elif kind == "event":
                name = e.get("name")
                if name == "health_check_failed":
                    s.health_failed += 1
                    s.patterns["fatal"] += 0.5
                elif name == "deploy":
                    s.last_deploy = {"ts": ts, "version": labels.get("version"), "previous": labels.get("previous")}
                self.recent_logs.append({"ts": ts, "service": svc, "level": "EVENT", "message": e.get("message") or name})

    # ------------------------------------------------------------ graph (learned from traces)

    def graph(self) -> tuple[dict[str, set], dict[str, set]]:
        deps, callers = defaultdict(set), defaultdict(set)
        for (p, c), n in self.edges.items():
            if n >= 3 and p != c:
                deps[p].add(c)
                callers[c].add(p)
        return deps, callers

    @staticmethod
    def _reach(start: str, adj: dict[str, set]) -> set[str]:
        seen, stack = set(), [start]
        while stack:
            for n in adj.get(stack.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        return seen

    def _connected(self, a: str, others, deps, callers) -> str | None:
        for b in others:
            if a in self._reach(b, deps) or a in self._reach(b, callers):
                return b
        return None

    def _joins(self, name: str, inc: "Incident", deps, callers) -> str | None:
        """A new abnormal service joins an open incident if it calls into the
        failure (a symptom climbing the graph), or it is a direct dependency of
        the current top suspect (the real root surfacing late). Siblings that
        merely share the gateway start their own incident."""
        for member in inc.services:
            if name in self._reach(member, callers):
                return member
        if inc.causes:
            top = inc.causes[0]["service"]
            if name in deps.get(top, ()):
                return top
        return None

    # ------------------------------------------------------------ tick

    def tick(self, now: float):
        self.now = now
        self.ticks += 1
        self.eps = self.eps * 0.5 + self._tick_events * 0.5
        self._tick_events = 0
        warm = self.ticks > self.cfg.warmup_ticks

        for name, s in self.sig.items():
            vals = {m: s.latest[m] for m in METRICS if m in s.latest}
            log_ratio = s.log_err / s.log_total if s.log_total > 1 else 0.0
            span_ratio = s.span_err / s.span_total if s.span_total > 2 else 0.0
            hard = s.health_failed > 0.6 or s.latest.get("availability", 100) < 50
            b = self.base[name]
            s.zs = {m: round(b.z(m, v), 1) for m, v in vals.items()}
            maxz = max(s.zs.values(), default=0.0)
            anomalous = warm and (hard or maxz > self.cfg.z_threshold or log_ratio > 0.25 or span_ratio > 0.3)
            if not warm or (not anomalous and not s.anomalous):
                b.update(vals)
            s.anomalous = anomalous
            s.hard = bool(warm and hard)
            s.score = 50.0 if hard else min(50.0, max(maxz, log_ratio * 40, span_ratio * 30))
            if anomalous:
                s.healthy_streak = 0
                s.anom_streak += 1
                if s.anom_streak == 1:
                    s.pending_onset = now
                if s.persistent and s.onset is None:
                    s.onset = s.pending_onset or now
            else:
                s.anom_streak = 0
                s.healthy_streak += 1
                if s.healthy_streak >= 5:
                    s.onset = None

        if warm:
            self._correlate(now)
            self._update_incidents(now)
        for s in self.sig.values():
            s.decay()

    # ------------------------------------------------------------ correlate

    def _correlate(self, now: float):
        deps, callers = self.graph()
        active = [i for i in self.incidents if i.status != "resolved"]
        anomalous = sorted((s for s in self.sig.values() if s.anomalous and s.persistent), key=lambda s: s.onset or now)
        pending: list[str] = []
        for s in anomalous:
            name = s.name
            if any(name in i.services for i in active):
                continue
            joined = False
            for inc in active:
                if now - inc.opened_at > self.cfg.correlation_window:
                    continue
                via = self._joins(name, inc, deps, callers)
                if via:
                    inc.services.append(name)
                    inc.raw_alerts += 1
                    inc.event_count += self._count_events({name}, now)
                    rel = "depends on" if name in self._reach(via, callers) else "is a dependency of"
                    inc.log(now, "correlator", f"{name} joined this incident ({name} {rel} {via}, {now - inc.opened_at:.0f}s after open)")
                    joined = True
                    break
            if joined:
                continue
            if now - self.recently_resolved.get(name, -1e9) < self.cfg.resolve_grace:
                continue
            pending.append(name)

        # group new anomalies into connected components, one incident each
        while pending:
            seed = pending.pop(0)
            group = [seed]
            for other in list(pending):
                if self._connected(other, [seed], deps, callers):
                    group.append(other)
                    pending.remove(other)
            inc = Incident(id=next(self._ids), opened_at=now, services=group, raw_alerts=len(group))
            inc.event_count = self._count_events(set(group), now)
            details = ", ".join(f"{g} ({self._why(g)})" for g in group)
            inc.log(now, "detector", f"abnormal behaviour on {details}")
            self.incidents.append(inc)

    def _why(self, name: str) -> str:
        s = self.sig[name]
        if s.health_failed > 0.6:
            return "health checks failing"
        bits = []
        if s.log_total > 1 and s.log_err / s.log_total > 0.25:
            bits.append(f"{s.log_err / s.log_total:.0%} error logs")
        top = max(s.zs.items(), key=lambda kv: kv[1], default=None)
        if top and top[1] > self.cfg.z_threshold:
            bits.append(f"{top[0]} z={top[1]}")
        return ", ".join(bits) or "error spans"

    def _count_events(self, services: set[str], now: float) -> int:
        lo = now - self.cfg.correlation_window
        return sum(1 for ts, svc in self.events if ts >= lo and svc in services)

    # ------------------------------------------------------------ rank

    def _pagerank(self, nodes: list[str], deps) -> dict[str, float]:
        """Personalised PageRank that walks from symptoms toward the things they
        depend on; anomalous nodes with no anomalous dependency absorb mass."""
        if not nodes:
            return {}
        score = {n: max(self.sig[n].score, 0.5) for n in nodes}
        total = sum(score.values())
        pers = {n: score[n] / total for n in nodes}
        out = {n: {**{d: score[d] for d in deps.get(n, ()) if d in score}, n: 0.15 * score[n]} for n in nodes}
        for n in nodes:
            if len(out[n]) == 1:          # no anomalous dependency: keeps its mass
                out[n][n] = score[n]
        rank = dict(pers)
        for _ in range(40):
            new = {n: 0.15 * pers[n] for n in nodes}
            for u in nodes:
                w = sum(out[u].values())
                for v, wt in out[u].items():
                    new[v] += 0.85 * rank[u] * wt / w
            rank = new
        top = max(rank.values())
        return {n: v / top for n, v in rank.items()}

    def signature(self, name: str) -> str:
        s = self.sig[name]
        p = s.patterns
        if s.health_failed > 0.3 or p["fatal"] > 0.4:
            return "process_crash"
        dep = s.last_deploy
        if dep and self.now - dep["ts"] < 300 and (p["npe"] > 0.5 or s.own_err > 1):
            return "bad_deploy"
        if p["oom"] > 0.5:
            return "memory_leak"
        if p["loop_lag"] > 0.5:
            return "cpu_saturation"
        if p["slow_query"] > 0.5:
            return "latency_degradation"
        if s.own_err > 1.0:
            return "application_errors"
        return "downstream_symptom"

    def _rank(self, inc: Incident, now: float) -> list[dict]:
        deps, _ = self.graph()
        for n in inc.services:
            if n not in inc.onsets and self.sig[n].onset is not None:
                inc.onsets[n] = self.sig[n].onset
        cands = [n for n in inc.services if n in inc.onsets]
        if not cands:
            return inc.causes
        pr = self._pagerank(cands, deps)
        onsets = {n: inc.onsets[n] for n in cands}
        first = min(onsets.values())
        blame_total = sum(self.sig[n].blame_in for n in cands) or 1e-9
        rows = []
        for n in cands:
            s = self.sig[n]
            anom_deps = sorted(d for d in deps.get(n, ()) if d in cands)
            sig = self.signature(n)
            onset_score = max(0.0, 1 - (onsets[n] - first) / 5.0)
            blame = s.blame_in / blame_total
            local = 1.0 if sig != "downstream_symptom" else 0.0
            dep = s.last_deploy
            change = 1.0 if dep and -5 <= onsets[n] - dep["ts"] <= 300 else 0.0
            mem = [m for m in self.memory if m["service"] == n and m["signature"] == sig]
            raw = (0.22 * pr[n] + 0.18 * (0.25 if anom_deps else 1.0) + 0.15 * onset_score
                   + 0.17 * blame + 0.13 * local + 0.05 * min(s.score, 50) / 50
                   + 0.05 * change + 0.05 * (1.0 if mem else 0.0))
            ev = []
            if local:
                ev.append(SIGNATURES[sig][2])
            if s.blame_in > 0.5:
                ev.append(f"{blame:.0%} of callers' error logs point at it")
            if anom_deps:
                ev.append(f"depends on abnormal {', '.join(anom_deps)}")
            else:
                ev.append("none of its dependencies are abnormal")
            if onsets[n] == first:
                ties = sum(1 for k, v in onsets.items() if k != n and v == first)
                lag = sorted(v - first for k, v in onsets.items() if k != n and v > first)
                if ties:
                    ev.append(f"among the first to go abnormal (same second as {ties} other{'s' if ties > 1 else ''})")
                else:
                    ev.append("first service to go abnormal" + (f", {lag[0]:.0f}s before the next" if lag else ""))
            else:
                ev.append(f"went abnormal {onsets[n] - first:.0f}s after the first service")
            if change:
                ev.append(f"deploy {dep['version']} landed {onsets[n] - dep['ts']:.0f}s before onset")
            ev.append(f"PageRank {pr[n]:.2f} on the anomaly graph")
            if mem:
                ev.append(f"matches incident #{mem[-1]['incident']} (fixed by {mem[-1]['action'] or 'manual resolve'})")
            # independent evidence types that point at this service (used for the confidence label)
            types = [t for t, ok in (("topology", not anom_deps), ("timing", onsets[n] == first),
                                     ("caller logs", s.blame_in > 0.5 and blame >= 0.3),
                                     ("local fault signature", bool(local)), ("change event", bool(change)),
                                     ("past incident", bool(mem))) if ok]
            rows.append({"service": n, "raw": raw, "signature": sig, "signature_label": SIGNATURES[sig][2],
                         "evidence": ev, "samples": list(s.samples)[-3:], "anom_deps": anom_deps,
                         "evidence_types": types})
        # "share" is relative support among candidates, NOT a probability; it is only
        # used internally (suggestion cut-off). What we show is the RCA score + a label.
        tot = sum(r["raw"] ** 3 for r in rows) or 1e-9
        for r in rows:
            r["confidence"] = round(100 * r["raw"] ** 3 / tot, 1)
            r["score"] = round(r["raw"], 2)
            r["raw"] = round(r["raw"], 3)
        rows.sort(key=lambda r: -r["raw"])
        return rows[:3]

    @staticmethod
    def verdict(causes: list[dict]) -> dict | None:
        """Honest confidence label with fixed, disclosed thresholds (not tuned):
        HIGH   no contradiction, lead over #2 >= 0.15, >= 3 independent evidence types
        LOW    lead < 0.05, or a contradiction -> abstain: 'insufficient evidence'
        MEDIUM everything else"""
        if not causes:
            return None
        top = causes[0]
        lead = top["raw"] - (causes[1]["raw"] if len(causes) > 1 else 0.0)
        contra = []
        if top["anom_deps"]:
            contra.append(f"it depends on {', '.join(top['anom_deps'])}, which is also abnormal")
        if top["signature"] == "downstream_symptom":
            contra.append("it shows no fault of its own; its errors point at a dependency")
        n_types = len(top["evidence_types"])
        if contra or lead < 0.05:
            label = "LOW"
            reason = contra[0] if contra else f"the top two candidates are nearly tied (lead {lead:.2f})"
        elif lead >= 0.15 and n_types >= 3:
            label, reason = "HIGH", f"{n_types} independent evidence types agree and it leads #2 by {lead:.2f}"
        else:
            label = "MEDIUM"
            reason = (f"only {n_types} independent evidence type{'s' if n_types != 1 else ''}" if n_types < 3
                      else f"lead over #2 is only {lead:.2f}")
        return {"label": label, "abstain": label == "LOW", "lead": round(lead, 2),
                "evidence_types": top["evidence_types"], "contradictions": contra, "reason": reason}

    def _suggest(self, causes: list[dict]) -> list[dict]:
        out = []
        for c in causes[:2]:
            if out and c["confidence"] < 15:
                break
            action, label, _ = SIGNATURES[c["signature"]]
            dep = self.sig[c["service"]].last_deploy or {}
            out.append({"service": c["service"], "action": action,
                        "label": label.format(s=c["service"], ver=dep.get("version", "?"), prev=dep.get("previous", "?")),
                        "confidence": c["confidence"]})
        return out

    # ------------------------------------------------------------ incident upkeep

    def _update_incidents(self, now: float):
        for inc in self.incidents:
            if inc.status == "resolved":
                continue
            prev_top = inc.causes[0]["service"] if inc.causes else None
            # freeze the diagnosis once someone acted on it or the system recovered;
            # otherwise decaying evidence would drift the ranking after the fix
            prev_label = (inc.verdict or {}).get("label")
            if not (inc.remediations or inc.recovered):
                inc.causes = self._rank(inc, now)
                inc.verdict = self.verdict(inc.causes)
            if inc.causes:
                top = inc.causes[0]
                v = inc.verdict or {}
                if top["service"] != prev_top or v.get("label") != prev_label:
                    if v.get("abstain"):
                        inc.log(now, "rca", f"insufficient evidence for a confident root cause; leading candidate {top['service']} (score {top['score']:.2f}): {v['reason']}")
                    else:
                        inc.log(now, "rca", f"top suspected cause: {top['service']}, {v.get('label')} confidence (score {top['score']:.2f}, {top['signature_label'].lower()})")
                inc.suggestions = self._suggest(inc.causes)
                inc.similar = [m for m in self.memory
                               if m["service"] == top["service"] and m["signature"] == top["signature"]][-3:]
            n = len(inc.services)
            user_facing = "gateway" in inc.services
            inc.severity = "SEV1" if (user_facing and n >= 3) else "SEV2" if (user_facing or n >= 3) else "SEV3"
            healthy = all(self.sig[s].healthy_streak >= 3 for s in inc.services)
            if healthy and not inc.recovered:
                inc.recovered = True
                inc.log(now, "verifier", "all affected services healthy again, ready to resolve")
            elif not healthy and inc.recovered:
                inc.recovered = False
            # nobody touched it and it went away by itself: a transient, close it
            if (inc.status == "open" and not inc.remediations and inc.recovered
                    and all(self.sig[s].healthy_streak >= 5 for s in inc.services)):
                inc.status, inc.transient = "resolved", True
                inc.resolved_at, inc.resolved_by = now, "auto"
                inc.log(now, "verifier", f"closed as transient: recovered in {now - inc.opened_at:.0f}s with no action")

    # ------------------------------------------------------------ human workflow

    def get(self, iid: int) -> Incident | None:
        return next((i for i in self.incidents if i.id == iid), None)

    def ack(self, iid: int, by: str) -> bool:
        inc = self.get(iid)
        if not inc or inc.status != "open":
            return False
        inc.status, inc.acked_by, inc.acked_at = "acknowledged", by, self.now
        inc.log(self.now, "human", f"acknowledged by {by} ({self.now - inc.opened_at:.0f}s after open)")
        return True

    def record_remediation(self, iid: int, service: str, action: str, by: str, fixed_fault: bool):
        inc = self.get(iid)
        if not inc:
            return
        inc.remediations.append({"ts": self.now, "service": service, "action": action, "by": by})
        inc.log(self.now, "human", f"{by} ran '{action}' on {service}, watching for recovery")

    def resolve(self, iid: int, by: str, note: str | None = None) -> bool:
        inc = self.get(iid)
        if not inc or inc.status == "resolved":
            return False
        inc.status, inc.resolved_at, inc.resolved_by, inc.resolution_note = "resolved", self.now, by, note
        inc.log(self.now, "human", f"resolved by {by}" + (f": {note}" if note else ""))
        if inc.causes:
            top = inc.causes[0]
            fix = next((r for r in reversed(inc.remediations) if r["service"] == top["service"]), None)
            self.memory.append({"incident": inc.id, "service": top["service"], "signature": top["signature"],
                                "action": fix["action"] if fix else None,
                                "ttr": round(self.now - inc.opened_at, 1)})
        deps, callers = self.graph()
        for s in inc.services:
            self.recently_resolved[s] = self.now
            for a in self._reach(s, callers):
                self.recently_resolved[a] = self.now
        return True
