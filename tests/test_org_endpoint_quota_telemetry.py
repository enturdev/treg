"""Observe fail-open admissions without affecting the call or exposing its inputs."""

import pytest

from treg import analytics
from treg.application.call import service as call_service
from treg.config import get_settings

from test_capacity_smoothing import _relay_script
from test_marketplace_call import EP, EP_MICRO, _balance, _fake_relay, platform_on  # noqa: F401
from test_org_endpoint_quota_call import _enable
from test_route_verify import ASK, FIND, TOMBA_HIT, _relay, verifiers_on  # noqa: F401


EVENT = "org_endpoint_quota_fail_open"


@pytest.fixture
def events(monkeypatch):
    observed = []

    def capture(distinct_id, event, properties=None, **kwargs):
        if event == EVENT:
            observed.append((distinct_id, properties, kwargs))

    monkeypatch.setattr(analytics, "capture", capture)
    monkeypatch.setattr(call_service, "_quota_warning_at", None)
    return observed


async def test_every_unknown_admission_is_observed_without_warning_rate_limit(
    clients, monkeypatch, platform_on, events, caplog,
):
    _enable(monkeypatch, unavailable=True)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    for _ in range(3):
        response = await clients.get(f"/call/{EP}?aweme_id=synthetic-private-input")
        assert response.status_code == 200, response.text
    assert events == [(analytics.SERVER_DISTINCT_ID, {"admissions": 1, "target_count": 1}, {})] * 3
    warnings = [r for r in caplog.records if r.name == "treg.org_endpoint_quota"]
    assert len(warnings) == 1


async def test_retry_and_idempotent_replay_do_not_duplicate_unknown_admission_event(
    clients, monkeypatch, platform_on, events,
):
    attempts, _ = _enable(monkeypatch, unavailable=True)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script([
        (429, ((b"retry-after", b"0"),), b'{"detail":"slow"}'),
        (200, (), b'{"ok":true}'),
    ], seen))
    url = f"/call/{EP}?aweme_id=7"
    headers = {"Idempotency-Key": "synthetic-quota-observation"}
    assert (await clients.get(url, headers=headers)).status_code == 200
    replay = await clients.get(url, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert len(seen) == 2 and attempts == {EP: 1}
    assert events == [(analytics.SERVER_DISTINCT_ID, {"admissions": 1, "target_count": 1}, {})]


async def test_routed_batch_reports_one_admission_with_both_targets(
    clients, monkeypatch, verifiers_on, events,
):
    _enable(monkeypatch, unavailable=True)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    response = await clients.post(f"/call/{FIND}", json=ASK)
    assert response.status_code == 200, response.text
    assert seen == ["tomba"]
    assert events == [(analytics.SERVER_DISTINCT_ID, {"admissions": 1, "target_count": 2}, {})]


@pytest.mark.parametrize("mode,status", [("disabled", 200), ("allowed", 200), ("exceeded", 429)])
async def test_confirmed_or_disabled_admission_does_not_emit_fail_open(
    clients, monkeypatch, platform_on, events, mode, status,
):
    _enable(monkeypatch, {EP: 0} if mode == "exceeded" else {})
    if mode == "disabled":
        monkeypatch.setattr(get_settings(), "org_endpoint_quota_enabled", False)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == status, response.text
    assert events == []


@pytest.mark.parametrize("observer", ["disabled", "broken"])
async def test_observation_cannot_block_or_change_billing(
    clients, monkeypatch, platform_on, observer,
):
    _enable(monkeypatch, unavailable=True)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    if observer == "disabled":
        monkeypatch.setattr(analytics, "enabled", lambda: False)
    else:
        def broken(distinct_id, event, properties=None, **kwargs):
            if event == EVENT:
                raise RuntimeError("synthetic observer failure")
        monkeypatch.setattr(analytics, "capture", broken)
    before = await _balance(clients)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200, response.text
    assert before - await _balance(clients) == EP_MICRO
