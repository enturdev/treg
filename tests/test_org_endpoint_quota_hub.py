"""Exact endpoint protection and the existing hub partial-failure billing contract."""

from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import funded_user
from sqlmodel import select

from treg import ratestore
from treg.application.call import overflow as O, route as call_route, service as call_service
from treg.config import get_settings
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing.plan import Candidate, Plan
from treg.infra import org_endpoint_quota
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry
from treg.timeutil import utcnow_naive

from test_capacity_overflow import overflow_on  # noqa: F401
from test_hub import _steps_manifest, hub_on  # noqa: F401
from test_influencersclub_overflow import SEARCH, _body, _sync, influencers_on  # noqa: F401
from test_marketplace_call import EP, _balance, _fake_relay, platform_on  # noqa: F401
from test_route_verify import ASK, FIND


def _block(monkeypatch, org_id, *, unavailable=False):
    monkeypatch.setattr(get_settings(), "org_endpoint_quota_enabled", True)
    attempts = Counter()

    async def admit(caller_org, endpoint_id):
        attempts[endpoint_id] += 1
        if caller_org == org_id and endpoint_id == SEARCH:
            return "unavailable" if unavailable else "exceeded"
        return "allowed"

    monkeypatch.setattr(org_endpoint_quota, "admit", admit)
    return attempts


async def _money():
    async with session_maker() as db:
        holds = (await db.execute(select(Hold))).scalars().all()
        entries = (await db.execute(select(LedgerEntry))).scalars().all()
    return holds, entries


@pytest.mark.parametrize("skip_direct", [False, True])
async def test_discovery_quota_precedes_direct_and_orthogonal(
    clients, monkeypatch, influencers_on, skip_direct,
):
    await _sync()
    if skip_direct:
        now = utcnow_naive()
        async with session_maker() as db:
            await ratestore.kv_put(db, STATE_NS, "influencersclub", LatestState(
                "influencersclub", 0.0, "credits", now, "exact",
                exhausted_until=now + timedelta(hours=1), health="exhausted",
            ).to_json(), ttl_s=3600)
            await db.commit()
        capacity_view.invalidate()
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    attempts = _block(monkeypatch, org_id)
    direct = AsyncMock(side_effect=AssertionError("blocked endpoint reached direct upstream"))
    overflow = AsyncMock(side_effect=AssertionError("blocked endpoint reached Orthogonal"))
    monkeypatch.setattr(call_service, "relay", direct)
    monkeypatch.setattr(O, "_send", overflow)
    before = await _balance(clients)
    response = await clients.post(f"/call/{SEARCH}", json=_body())
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error"] == "org_endpoint_quota_exceeded"
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    assert attempts == {SEARCH: 1}
    assert await _balance(clients) == before
    direct.assert_not_called()
    overflow.assert_not_called()
    holds, entries = await _money()
    assert not holds
    assert not [entry for entry in entries if entry.kind in {"reserve", "settle"}]


@pytest.mark.parametrize("unavailable,status", [(False, 429), (True, 503)])
async def test_discovery_in_a_synthetic_route_plan_stops_before_later_candidates(
    clients, monkeypatch, influencers_on, unavailable, status,
):
    """Discovery currently has no waterfall; inject only a plan to test the shared child road."""
    catalog = catalog_store.load()
    candidates = [Candidate(
        endpoint=catalog.by_id[endpoint_id],
        adapter=SimpleNamespace(to_upstream=lambda identity, variant: ({}, _body())),
        variant=(), tier="platform", price_micro=1000, hit_rate=None,
        ok_rate=None, p50_ms=None, last_ok_days=None,
    ) for endpoint_id in (SEARCH, EP)]
    monkeypatch.setattr(call_route, "build_plan", AsyncMock(return_value=Plan(
        contract=catalog.contracts["people.email.find"], identity={}, variant=(), candidates=candidates,
    )))
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    attempts = _block(monkeypatch, org_id, unavailable=unavailable)
    relay = AsyncMock(side_effect=AssertionError("quota refusal must stop the route"))
    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    response = await clients.post(f"/call/{FIND}", json=ASK)
    assert response.status_code == status, response.text
    assert response.json()["detail"]["error"] == (
        "org_endpoint_quota_unavailable" if unavailable else "org_endpoint_quota_exceeded")
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    assert attempts == {FIND: 1, SEARCH: 1}
    assert await _balance(clients) == before
    relay.assert_not_called()
    holds, entries = await _money()
    assert not holds
    assert not [entry for entry in entries if entry.kind in {"reserve", "settle"}]


@pytest.mark.parametrize("allow_fail", [False, True])
async def test_hub_quota_refusal_has_zero_child_cost_but_preserves_other_step_billing(
    clients, monkeypatch, platform_on, hub_on, allow_fail,
):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub,influencersclub")
    monkeypatch.setenv("TREG_PLATFORM_KEY_INFLUENCERSCLUB", "SYNTHETIC-KEY")
    get_settings.cache_clear()
    # The refs make each step wait for its predecessor, so the billing assertion is deterministic.
    manifest = _steps_manifest(
        name="quota-boundary", uses=[EP, SEARCH],
        steps=[
            {"name": "paid", "call": EP, "input": {"aweme_id": "$input.domain"}},
            {"name": "blocked", "call": SEARCH, "allow_fail": allow_fail,
             "input": {**_body(), "nlp_search": "$paid.data"}},
            {"name": "later", "call": EP,
             "input": {"aweme_id": "later", "cursor": "$blocked.total"}},
        ], output={"rows": "$later.data", "prior": "$paid.data"},
    )
    seen = []
    blocked = False

    async def relay(request, upstream_url, *args, **kwargs):
        if "influencers.club" in upstream_url:
            assert not blocked, "quota-protected provider must never receive the buyer's request"
            body = b'{"accounts":[{"user_id":"synthetic"}],"total":1,"credits_cost":0.01}'
        else:
            seen.append(upstream_url)
            body = b'{"data":"synthetic"}'
        return await _fake_relay(200, body)(request, upstream_url, *args, **kwargs)

    monkeypatch.setattr(call_service, "relay", relay)
    published = await clients.post("/hub/tools", json={
        "manifest": manifest, "check": {"inputs": {"domain": "synthetic"}, "fields": ["rows"]},
        "readme": "Local synthetic quota boundary check.",
    })
    assert published.status_code == 201, published.text
    assert published.json()["status"] == "live", published.text
    tool_id = published.json()["tool_id"]
    buyer = await funded_user(clients, "quota-boundary-buyer@example.com")
    headers = {"X-Treg-Token": buyer["token"]}

    async def buyer_balance():
        return (await clients.get(f"/orgs/{buyer['org_id']}/balance", headers=headers)).json()["balance_micro"]

    buyer_before, maker_before = await buyer_balance(), await _balance(clients)
    attempts = _block(monkeypatch, buyer["org_id"])
    blocked = True
    seen.clear()
    response = await clients.post(f"/call/{tool_id}", json={"domain": "synthetic"}, headers=headers)
    assert response.status_code == (200 if allow_fail else 424), response.text
    result = response.json() if allow_fail else response.json()["detail"]
    step = next(entry for entry in result["trace"] if entry["name"] == "blocked")
    assert step["status"] == 429 and step["cost_micro"] == 0
    assert step["outcome"] == ("failed_allowed" if allow_fail else "failed")
    assert attempts == {EP: 2 if allow_fail else 1, SEARCH: 1}
    assert len(seen) == (2 if allow_fail else 1)
    assert buyer_before - await buyer_balance() == (12_000 if allow_fail else 1000)
    assert await _balance(clients) - maker_before == (10_000 if allow_fail else 0)
    if allow_fail:
        assert result["output"]["rows"] == "synthetic"
        assert result["usage"]["steps_micro"] == 2000
        assert result["usage"]["price_micro"] == 10_000
    else:
        assert result["error"] == "hub_step_failed"
        assert result["charged_micro"] == 1000 and result["price_micro"] == 0
        assert not any(entry["name"] == "later" for entry in result["trace"])
    holds, entries = await _money()
    assert not holds
    assert not [entry for entry in entries if entry.call_id == result["run_id"] + ":s1"]
