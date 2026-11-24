"""POST /api/ingest rejects NaN and Infinity in value and ts.

A NaN metric would make that metric's Baseline mean NaN for good, so its z-score checks
are always false and anomaly detection stops for the service. No TestClient (httpx is not
installed in CI), so the request goes through the ASGI app directly.
"""

import asyncio
import json

import pytest
from pydantic import ValidationError

import main


def _post(path, raw_body):
    """Send one POST with a raw JSON body through the ASGI app and return (status, body)."""
    body = raw_body.encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())],
             "client": ("test", 1), "server": ("test", 80), "root_path": ""}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(main.app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(out)


@pytest.mark.parametrize("field", ["value", "ts"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_event_rejects_non_finite(field, bad):
    with pytest.raises(ValidationError):
        main.Event(service="api", kind="metric", name="latency_p99", **{field: bad})


def test_event_accepts_finite_and_missing():
    e = main.Event(service="api", kind="metric", name="latency_p99", value=120.5, ts=1700000000.0)
    assert e.value == 120.5
    assert main.Event(service="api", kind="log").value is None


def test_ingest_nan_literal_gets_422():
    # Python's json parser reads the bare NaN literal, so this reaches validation.
    status, body = _post("/api/ingest", '[{"service": "api", "kind": "metric", '
                                        '"name": "latency_p99", "value": NaN}]')
    assert status == 422
    assert body["detail"][0]["type"] == "finite_number"
    assert body["detail"][0]["loc"] == ["body", 0, "value"]


def test_other_validation_errors_keep_input():
    status, body = _post("/api/ingest", '[{"service": "api", "kind": "bogus"}]')
    assert status == 422
    assert body["detail"][0]["input"] == "bogus"
