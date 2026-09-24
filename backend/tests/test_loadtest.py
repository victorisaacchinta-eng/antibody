"""Load-test helpers: templating, header parsing, percentiles, auth rules."""

import pytest
from fastapi import HTTPException

import loadtest as lt


def test_template_fills_user_variables_and_escapes_json():
    vars = {"name": 'Ana "A" Rao', "email": "ana@example.com", "device_token": "dev_12345678"}
    body = lt.render('{"name":"{{name}}","email":"{{ email }}","x":"{{missing}}"}', vars, json_safe=True)
    assert body == '{"name":"Ana \\"A\\" Rao","email":"ana@example.com","x":"{{missing}}"}'


def test_headers_parse_and_percentiles():
    h = lt.parse_headers("Authorization: Bearer abc\nX-Device-Token: dev_1\nbad line")
    assert h == {"Authorization": "Bearer abc", "X-Device-Token": "dev_1"}
    assert lt.pct([10, 20, 30, 40, 50], 50) == 30
    assert lt.pct([], 95) is None


def test_token_is_bound_to_device():
    lt.SESSIONS["tok"] = {"name": "A", "email": "a@b.co", "device_token": "dev_12345678", "exp": 9e12}
    assert lt._session("Bearer tok", "dev_12345678")["email"] == "a@b.co"
    with pytest.raises(HTTPException) as e:
        lt._session("Bearer tok", "other_device")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        lt._session(None, "dev_12345678")
    assert e.value.status_code == 401
