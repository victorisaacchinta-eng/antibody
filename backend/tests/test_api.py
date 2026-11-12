"""Acknowledge and resolve endpoints: unknown ids are 404, wrong state is ok: false."""

import pytest
from fastapi import HTTPException

import main
from engine import Incident


@pytest.fixture
def incident():
    inc = Incident(id=9001, opened_at=1000.0, services=["db"])
    main.engine.incidents.append(inc)
    yield inc
    main.engine.incidents.remove(inc)


@pytest.mark.parametrize("endpoint", [main.ack, main.resolve])
def test_unknown_incident_is_404(endpoint):
    with pytest.raises(HTTPException) as e:
        endpoint(424242, main.Actor())
    assert e.value.status_code == 404
    assert e.value.detail == "no such incident"


def test_known_incident_in_wrong_state_is_ok_false(incident):
    assert main.ack(incident.id, main.Actor()) == {"ok": True}
    assert main.ack(incident.id, main.Actor()) == {"ok": False}       # already acknowledged
    assert main.resolve(incident.id, main.Actor(note="restarted db")) == {"ok": True}
    assert main.resolve(incident.id, main.Actor()) == {"ok": False}   # already resolved
    assert main.ack(incident.id, main.Actor()) == {"ok": False}       # resolved, cannot ack
    assert incident.status == "resolved"
