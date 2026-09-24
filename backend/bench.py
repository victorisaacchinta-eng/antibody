"""
Benchmark: every service x every fault type, with a known ground-truth cause.
Runs on a simulated clock in an isolated world, so it takes seconds, not hours.

    python bench.py            # prints the scorecard
"""

from __future__ import annotations

import random
import statistics
import time

from engine import Engine
from sim import FAULT_TYPES, SERVICES, World

DIAGNOSIS_DELAY = 4   # seconds of evidence after detection before we read the ranking


def _settle(world: World, eng: Engine, t: float, ticks: int) -> float:
    for _ in range(ticks):
        t += 1.0
        eng.ingest(world.tick(t))
        eng.tick(t)
    return t


def baselines(eng: Engine, inc) -> dict:
    """Simpler rankers scored on the SAME incident at the SAME moment as the full model.
    A: loudest anomaly only (anomaly score, ties broken by error volume)
    B: anomaly + earliest onset
    C: anomaly + earliest onset + dependency graph (only services with no abnormal dependency)"""
    deps, _ = eng.graph()
    cands = [n for n in inc.services if n in inc.onsets] or list(inc.services)
    loud = lambda n: (eng.sig[n].score, eng.sig[n].log_err + eng.sig[n].span_err)
    a = max(cands, key=loud)
    earliest = lambda n: (inc.onsets.get(n, 1e18), -eng.sig[n].score)
    b = min(cands, key=earliest)
    roots = [n for n in cands if not any(d in cands for d in deps.get(n, ()))] or cands
    c = min(roots, key=earliest)
    return {"A": a, "B": b, "C": c}


def run_scenario(service: str, fault: str, seed: int, world: World | None = None,
                 eng: Engine | None = None, t: float = 1000.0, noise: float = 0.0) -> tuple[dict, float]:
    world = world or World(random.Random(seed), noise=noise)
    eng = eng or Engine()
    if eng.ticks == 0:
        t = _settle(world, eng, t, 25)
    before = len(eng.incidents)
    t0 = t
    eng.ingest(world.inject(service, fault, t))

    inc, detected_at = None, None
    for _ in range(40):
        t = _settle(world, eng, t, 1)
        new = eng.incidents[before:]
        # the fault may open a new incident OR be absorbed into one already open
        mine = next((i for i in eng.incidents if i.status != "resolved" and service in i.services
                     and eng.sig[service].onset is not None and eng.sig[service].onset >= t0), None)
        if mine and detected_at is None:
            detected_at = t
        if mine:
            inc = mine
            break
    row = {"service": service, "fault": fault, "seed": seed, "detected": inc is not None,
           "mttd": round(detected_at - t0, 1) if detected_at else None}
    if not inc:
        row.update(top1=False, top3=False, fixed=False, events=0, incidents=len(eng.incidents) - before,
                   A=False, B=False, C=False, label=None)
        return row, _settle(world, eng, t, 20)

    t = _settle(world, eng, t, DIAGNOSIS_DELAY)
    ranked = [c["service"] for c in inc.causes]
    row["top1"] = bool(ranked) and ranked[0] == service
    row["top3"] = service in ranked[:3]
    row["label"] = (inc.verdict or {}).get("label")
    row["score"] = inc.causes[0]["score"] if inc.causes else 0
    for k, v in baselines(eng, inc).items():
        row[k] = v == service
    row["signature"] = inc.causes[0]["signature"] if inc.causes else None
    row["remembered"] = bool(inc.similar)

    fixed = False
    if inc.suggestions:
        s = inc.suggestions[0]
        fixed, evs = world.remediate(s["service"], s["action"], t)
        eng.ingest(evs)
        eng.record_remediation(inc.id, s["service"], s["action"], "bench", fixed)
    for _ in range(20):
        t = _settle(world, eng, t, 1)
        if inc.recovered:
            break
    row["fixed"] = fixed and inc.recovered
    row["time_to_diagnosis"] = round(t0 and (DIAGNOSIS_DELAY + (row["mttd"] or 0)), 1)
    row["events"] = inc.event_count
    eng.resolve(inc.id, "bench")
    t = _settle(world, eng, t, 20)
    # any fault still active (a wrong fix) is cleaned up so the next scenario starts healthy
    for sv in world.services.values():
        if sv.active_fault:
            for a in ("restart", "rollback", "scale_out"):
                world.remediate(sv.name, a, t)
    t = _settle(world, eng, t, 20)
    row["incidents"] = len([i for i in eng.incidents[before:]])
    row["false_incidents"] = len([i for i in eng.incidents[before:] if service not in i.services])
    return row, t


def run_dual(a: tuple, b: tuple, seed: int, noise: float) -> dict:
    """Two unrelated faults at the same moment: each must be top-ranked in its own incident."""
    world, eng, t = World(random.Random(seed), noise=noise), Engine(), 1000.0
    t = _settle(world, eng, t, 25)
    before = len(eng.incidents)
    eng.ingest(world.inject(a[0], a[1], t))
    eng.ingest(world.inject(b[0], b[1], t))
    t = _settle(world, eng, t, 3 + DIAGNOSIS_DELAY)
    new = eng.incidents[before:]
    hits = 0
    for svc in (a[0], b[0]):
        tops = [i.causes[0]["service"] for i in eng.incidents if i.status != "resolved" and svc in i.services and i.causes]
        hits += svc in tops
    return {"pair": f"{a[0]}:{a[1]} + {b[0]}:{b[1]}", "both_found": hits == 2, "found": hits, "incidents": len(new)}


DUALS = [(("auth", "crash"), ("orders", "latency_spike")), (("catalog", "error_spike"), ("db", "memory_leak")),
         (("auth", "bad_deploy"), ("payments", "cpu_burn")), (("catalog", "crash"), ("orders", "bad_deploy")),
         (("auth", "memory_leak"), ("db", "crash")), (("catalog", "latency_spike"), ("payments", "error_spike"))]


def _score(rows: list[dict]) -> dict:
    det = [r for r in rows if r["detected"]]
    n = len(rows)
    return {
        "scenarios": n,
        "detection_rate": round(100 * len(det) / n, 1),
        "median_mttd_s": statistics.median(r["mttd"] for r in det) if det else None,
        "rca_top1": round(100 * sum(r["top1"] for r in rows) / n, 1),
        "rca_top3": round(100 * sum(r["top3"] for r in rows) / n, 1),
        "fix_success": round(100 * sum(r["fixed"] for r in rows) / n, 1),
        "avg_events_per_incident": round(statistics.mean(r["events"] for r in det)) if det else 0,
        "false_incidents_per_run": round(statistics.mean(r.get("false_incidents", 0) for r in rows), 2),
        "baselines_top1": {k: round(100 * sum(r[k] for r in rows) / n, 1) for k in ("A", "B", "C")},
        "labels": _labels(rows),
    }


def _labels(rows: list[dict]) -> dict:
    """How often each confidence label is given, and how often it is right."""
    out = {}
    for lab in ("HIGH", "MEDIUM", "LOW"):
        rs = [r for r in rows if r.get("label") == lab]
        out[lab] = {"share": round(100 * len(rs) / len(rows), 1),
                    "top1_when_given": round(100 * sum(r["top1"] for r in rs) / len(rs), 1) if rs else None}
    answered = [r for r in rows if r.get("label") in ("HIGH", "MEDIUM")]
    out["abstain_rate"] = out["LOW"]["share"]
    out["top1_when_answered"] = round(100 * sum(r["top1"] for r in answered) / len(answered), 1) if answered else None
    return out


def run_all(seeds=(1, 2), noise: float = 0.02) -> dict:
    started = time.time()
    rows, hard = [], []
    for seed in seeds:
        for svc in SERVICES:
            for fault in FAULT_TYPES:
                sd = seed * 1000 + (SERVICES.index(svc) * 10 + FAULT_TYPES.index(fault))
                rows.append(run_scenario(svc, fault, sd)[0])
                hard.append(run_scenario(svc, fault, sd, noise=noise)[0])
    duals = [run_dual(a, b, 50 + i, noise) for i, (a, b) in enumerate(DUALS)]

    # memory test: same engine sees the same failure twice
    world, eng = World(random.Random(7)), Engine()
    first, t = run_scenario("payments", "crash", 7, world, eng)
    second, t = run_scenario("payments", "crash", 7, world, eng, t)

    summary = {"clean": _score(rows), "hard": _score(hard),
               "dual_fault_both_found": f"{sum(d['both_found'] for d in duals)}/{len(duals)}",
               "memory_recall": second.get("remembered", False),
               "runtime_s": round(time.time() - started, 1)}
    by_fault = {}
    for f in FAULT_TYPES:
        fr = [r for r in rows if r["fault"] == f]
        by_fault[f] = {"top1": round(100 * sum(r["top1"] for r in fr) / len(fr)),
                       "top3": round(100 * sum(r["top3"] for r in fr) / len(fr))}
    return {"summary": summary, "by_fault": by_fault, "results": rows, "hard_results": hard, "duals": duals}


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path
    out = run_all()
    if "--save" in sys.argv:
        Path(__file__).resolve().parent.joinpath("bench_result.json").write_text(json.dumps(out))
        print("saved bench_result.json")
    print(json.dumps(out["summary"], indent=2))
    print(json.dumps(out["by_fault"], indent=2))
    for r in out["hard_results"]:
        if not r["top1"]:
            print("HARD MISS", r)
    for d in out["duals"]:
        print("DUAL", d)
