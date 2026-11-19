"""GET /api/traces limit bounds and the single-run guard on POST /api/benchmark/run.

No TestClient (httpx is not installed in CI), so the limit check drives the ASGI app
directly and the benchmark check calls the route function.
"""

import asyncio
import json

import bench
import main


def _get(path):
    """Send one GET through the ASGI app and return (status, parsed JSON body)."""
    url, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": url, "raw_path": url.encode(), "query_string": query.encode(),
             "headers": [], "client": ("test", 1), "server": ("test", 80), "root_path": ""}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(main.app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


def test_traces_limit_out_of_range_is_rejected():
    for bad in ("0", "-5", "81"):
        status, _ = _get(f"/api/traces?limit={bad}")
        assert status == 422, bad


def test_traces_limit_in_range_is_accepted():
    for ok in ("1", "30", "80"):
        status, body = _get(f"/api/traces?limit={ok}")
        assert status == 200, ok
        assert isinstance(body, list)
    assert _get("/api/traces")[0] == 200


class _HeldThread:
    """Thread stand-in that does not run until the test says so, like a slow-starting thread."""

    held: list = []

    def __init__(self, target, daemon=None):
        self.target = target
        _HeldThread.held.append(self)

    def start(self):
        pass


def test_benchmark_second_click_does_not_start_another_run(monkeypatch):
    calls = []
    monkeypatch.setattr(bench, "run_all", lambda: calls.append(1) or {"done": True})
    monkeypatch.setitem(main.bench_cache, "running", False)
    monkeypatch.setattr(_HeldThread, "held", [])
    monkeypatch.setattr(main.threading, "Thread", _HeldThread)

    main.run_benchmark()
    main.run_benchmark()          # second click before the first thread has run
    assert len(_HeldThread.held) == 1

    _HeldThread.held[0].target()
    assert calls == [1]
    assert main.bench_cache["running"] is False
