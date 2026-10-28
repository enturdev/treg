"""Endpoint-local allowances do not discard unrelated routed results."""

import pytest
from sqlmodel import select

from treg.application.call import route as call_route, service as call_service
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing.contracts import Check
from treg.infra import org_endpoint_quota
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_org_endpoint_quota_call import _enable
from test_route_verify import (
    ASK, FIND, ON, TOMBA_FIND_PRICE, TOMBA_HIT, VERIFY, _bb, _relay,
    verifiers_on,  # noqa: F401
)


def _refuse(monkeypatch, endpoint, outcome, *, limits=None):
    attempts, used = _enable(monkeypatch, {**(limits or {}), endpoint: 0})
    admitted = org_endpoint_quota.admit

    async def admit(org_id, endpoint_id):
        result = await admitted(org_id, endpoint_id)
        return outcome if endpoint_id == endpoint else result

    monkeypatch.setattr(org_endpoint_quota, "admit", admit)
    return attempts, used


async def _settled_ids():
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.kind == "settle"))).scalars().all()
    return {entry.call_id for entry in entries}


@pytest.mark.parametrize("error_budget", [0, 2])
async def test_exhausted_candidate_skips_to_an_unrestricted_finder(
    clients, monkeypatch, verifiers_on, error_budget,
):
    monkeypatch.setattr(call_route, "MAX_ERROR_FALLBACKS", error_budget)
    blocked = "tomba.people.email.find"
    attempts, used = _refuse(monkeypatch, blocked, "exceeded", limits={FIND: 1})
    seen = []
    hunter_hit = (200, {"data": {"email": "synthetic@example.com", "score": 99,
                                "verification": {"status": "valid"}}})
    monkeypatch.setattr(call_service, "relay", _relay({"hunter": [hunter_hit]}, seen))
    before = await _balance(clients)
    response = await clients.post(f"/call/{FIND}", json=ASK,
                                  headers={"X-Treg-Route-Prefer": "tomba,hunter"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["output"]["email"] == "synthetic@example.com"
    assert result["_treg"]["served_by"] == "hunter.people.email.find"
    blocked_attempt = result["_treg"]["tried"][0]
    assert blocked_attempt["endpoint_id"] == blocked
    assert blocked_attempt["outcome"] == "skipped" and blocked_attempt["charged_micro"] == 0
    assert seen == ["hunter"] and attempts[blocked] == 1 and used[blocked] == 0
    assert used[FIND] == 1
    charged = int(response.headers["X-Treg-Cost-Micro"])
    assert charged > 0 and before - await _balance(clients) == charged
    assert await _settled_ids() == {response.headers["X-Treg-Call-Id"] + ":r1"}


async def test_unavailable_quota_allows_the_primary_candidate_and_normal_charge(
    clients, monkeypatch, verifiers_on,
):
    attempts, _ = _refuse(monkeypatch, "tomba.people.email.find", "unavailable")
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    before = await _balance(clients)
    response = await clients.post(f"/call/{FIND}", json=ASK,
                                  headers={"X-Treg-Route-Prefer": "tomba,hunter"})
    assert response.status_code == 200, response.text
    assert response.json()["_treg"]["served_by"] == "tomba.people.email.find"
    assert seen == ["tomba"] and attempts["hunter.people.email.find"] == 0
    assert attempts["tomba.people.email.find"] == 1
    assert before - await _balance(clients) == TOMBA_FIND_PRICE
    assert await _settled_ids() == {response.headers["X-Treg-Call-Id"] + ":r0"}


@pytest.mark.parametrize("checker", [VERIFY, "tomba.people.email.verify"])
@pytest.mark.parametrize("outcome", ["exceeded", "unavailable"])
async def test_optional_checker_quota_policy_preserves_find_and_correct_settlement(
    clients, monkeypatch, verifiers_on, checker, outcome,
):
    contract = catalog_store.load().contracts["people.email.find"]
    original_check = contract.check
    object.__setattr__(contract, "check", Check(endpoint=checker, field="email"))
    try:
        attempts, _ = _refuse(monkeypatch, checker, outcome)
        seen = []
        valid = (200, {"data": {"email": {"status": "valid", "score": 99}}})
        monkeypatch.setattr(call_service, "relay", _relay({
            "tomba": [TOMBA_HIT], "tomba-verify": [valid], "bounceban": [_bb("deliverable")],
        }, seen))
        before = await _balance(clients)
        response = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["output"]["email"] == TOMBA_HIT[1]["data"]["email"]
        verification = result["_treg"]["verification"]
        call_id = response.headers["X-Treg-Call-Id"]
        settled = {call_id + ":r0"}
        if outcome == "exceeded":
            assert verification == {
                "verdict": "unknown", "checked": False, "served_by": None,
                "cost_micro": 0, "reason": "quota_exceeded",
            }
            assert seen == ["tomba"]
        else:
            assert verification["checked"] is True and verification["verdict"] == "valid"
            assert verification["cost_micro"] > 0
            expected = "bounceban.people.email.verify" if checker == VERIFY else checker
            assert verification["served_by"] == expected
            assert seen == ["tomba", "bounceban" if checker == VERIFY else "tomba-verify"]
            settled.add(call_id + (":v:r0" if checker == VERIFY else ":v"))
        assert attempts[checker] == 1
        charged = TOMBA_FIND_PRICE + verification["cost_micro"]
        assert before - await _balance(clients) == charged
        assert result["_treg"]["charged_micro"] == charged
        assert await _settled_ids() == settled
    finally:
        object.__setattr__(contract, "check", original_check)


async def test_exhausted_verifier_candidate_uses_an_unrestricted_checker(
    clients, monkeypatch, verifiers_on,
):
    blocked = "bounceban.people.email.verify"
    attempts, used = _refuse(monkeypatch, blocked, "exceeded")
    seen = []
    valid = (200, {"data": {"email": {"status": "valid", "score": 99}}})
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [TOMBA_HIT], "tomba-verify": [valid]}, seen))
    before = await _balance(clients)
    response = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert response.status_code == 200, response.text
    verification = response.json()["_treg"]["verification"]
    assert verification["checked"] is True and verification["verdict"] == "valid"
    assert verification["served_by"] == "tomba.people.email.verify"
    assert seen == ["tomba", "tomba-verify"]
    assert attempts[blocked] == 1 and used[blocked] == 0
    assert before - await _balance(clients) == TOMBA_FIND_PRICE + verification["cost_micro"]
    call_id = response.headers["X-Treg-Call-Id"]
    assert await _settled_ids() == {call_id + ":r0", call_id + ":v:r1"}


async def test_unavailable_verifier_quota_allows_check_and_preserves_the_find(
    clients, monkeypatch, verifiers_on,
):
    attempts, _ = _refuse(monkeypatch, "bounceban.people.email.verify", "unavailable")
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [TOMBA_HIT], "bounceban": [_bb("deliverable")]}, seen))
    before = await _balance(clients)
    response = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["output"]["email"] == TOMBA_HIT[1]["data"]["email"]
    verification = result["_treg"]["verification"]
    assert verification["checked"] is True and verification["verdict"] == "valid"
    assert verification["served_by"] == "bounceban.people.email.verify"
    assert verification["cost_micro"] > 0
    assert seen == ["tomba", "bounceban"] and attempts["tomba.people.email.verify"] == 0
    charged = TOMBA_FIND_PRICE + verification["cost_micro"]
    assert before - await _balance(clients) == charged
    call_id = response.headers["X-Treg-Call-Id"]
    assert await _settled_ids() == {call_id + ":r0", call_id + ":v:r0"}
