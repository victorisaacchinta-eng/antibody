"""Fast checks on the RCA engine. Run from backend/: python -m pytest -q"""

import random
from pathlib import Path

from bench import _settle, run_scenario
from engine import Engine
from sim import World


def test_db_crash_is_ranked_first_with_high_confidence():
    row, _ = run_scenario("db", "crash", seed=11)
    assert row["detected"]
    assert row["top1"], row
    assert row["label"] == "HIGH"


def test_bad_deploy_is_recognised_and_rollback_fixes_it():
    row, _ = run_scenario("payments", "bad_deploy", seed=12)
    assert row["top1"], row
    assert row["signature"] == "bad_deploy"
    assert row["fixed"]


def test_repeat_failure_is_recognised_from_memory():
    world, eng = World(random.Random(7)), Engine()
    _, t = run_scenario("payments", "crash", 7, world, eng)
    second, _ = run_scenario("payments", "crash", 7, world, eng, t)
    assert second["remembered"]


def test_healthy_traffic_raises_no_incidents():
    world, eng = World(random.Random(3)), Engine()
    _settle(world, eng, 1000.0, 90)
    assert eng.incidents == []


def test_engine_never_reads_simulator_ground_truth():
    src = (Path(__file__).resolve().parent.parent / "engine.py").read_text()
    for leak in ("active_fault", "fault_started_at", "import sim", "from sim"):
        assert leak not in src, f"engine.py must not use {leak}"
