"""Quota admission at the shared call boundary, with synthetic upstreams only."""

from collections import Counter
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from treg import archive
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.infra import org_endpoint_quota
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry

from test_archive import serve  # noqa: F401
from test_bounceban_async import (
    EMAIL, STATUS, VERIFY, VERIFYING, Upstream, _finished,
    bounceban_on, fast_polls,  # noqa: F401
)
from test_capacity_overflow import (
    O, OK_ANSWER, _exhausted, _orthogonal, _route, overflow_on,  # noqa: F401
)
from test_marketplace_call import EP, _balance, _fake_relay, platform_on  # noqa: F401
from test_mcp import _call_tool, mcp_session
from test_route_verify import ASK, FIND, ON, TOMBA_HIT, _relay, verifiers_on  # noqa: F401


def _enable(monkeypatch, limits=None, *, unavailable=False):
    monkeypatch.setattr(get_settings(), "org_endpoint_quota_enabled", True)
    attempts = Counter()
    used = Counter()

    async def admit(org_id, endpoint_id):
        attempts[endpoint_id] += 1
        if unavailable:
            return "unavailable"
        if endpoint_id not in (limits or {}):
            return "allowed"
        if used[endpoint_id] >= limits[endpoint_id]:
            return "exceeded"
        used[endpoint_id] += 1
        return "allowed"

    monkeypatch.setattr(org_endpoint_quota, "admit", admit)
    return attempts, used


async def _assert_no_money():
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        assert (await db.execute(select(LedgerEntry).where(
            LedgerEntry.kind.in_(["reserve", "settle"])))).scalars().all() == []


@pytest.mark.parametrize("surface", ["call", "catalog/call", "table"])
@pytest.mark.parametrize("unavailable,status,code", [
    (False, 429, "org_endpoint_quota_exceeded"),
    (True, 503, "org_endpoint_quota_unavailable"),
])
async def test_denial_precedes_cache_money_and_upstream(
    clients, monkeypatch, platform_on, surface, unavailable, status, code,
):
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    attempts, _ = _enable(monkeypatch, {EP: 0}, unavailable=unavailable)
    relay = AsyncMock(side_effect=AssertionError("upstream must not run"))
    cache = AsyncMock(side_effect=AssertionError("cache must not run"))
    overflow = AsyncMock(side_effect=AssertionError("overflow must not run"))
    monkeypatch.setattr(call_service, "relay", relay)
    monkeypatch.setattr(archive, "lookup", cache)
    monkeypatch.setattr(O, "maybe_overflow", overflow)
    before = await _balance(clients)
    response = await clients.get(f"/{surface}/{EP}?aweme_id=7")
    assert response.status_code == status, response.text
    assert response.json()["detail"]["error"] == code
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    assert attempts == {EP: 1}
    assert await _balance(clients) == before
    relay.assert_not_called()
    cache.assert_not_called()
    overflow.assert_not_called()
    await _assert_no_money()


async def test_mcp_uses_same_quota_gate(clients, monkeypatch, platform_on):
    attempts, _ = _enable(monkeypatch, {EP: 0})
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as client:
        out = await _call_tool(client, "call", {"endpoint_id": EP, "params": {"aweme_id": "7"}}, token=token)
    assert out["status"] == 429, out
    assert out["body"]["detail"]["error"] == "org_endpoint_quota_exceeded"
    assert attempts == {EP: 1}
    await _assert_no_money()


async def test_replay_does_not_consume_but_next_logical_call_does(clients, monkeypatch, platform_on):
    attempts, used = _enable(monkeypatch, {EP: 1})
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    url = f"/call/{EP}?aweme_id=7"
    first = await clients.get(url, headers={"Idempotency-Key": "quota-replay"})
    assert first.status_code == 200, first.text
    balance = await _balance(clients)
    replay = await clients.get(url, headers={"Idempotency-Key": "quota-replay"})
    assert replay.status_code == 200, replay.text
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert attempts == used == {EP: 1}
    denied = await clients.get(url)
    assert denied.status_code == 429
    assert attempts == {EP: 2} and used == {EP: 1}
    assert await _balance(clients) == balance


async def test_cache_hit_consumes_one_and_exhaustion_cannot_serve_cache(clients, monkeypatch, serve):
    attempts, used = _enable(monkeypatch, {EP: 2})
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"answer":"synthetic"}'))
    url = f"/call/{EP}?aweme_id=7"
    first = await clients.get(url)
    assert first.status_code == 200
    await archive.drain()
    second = await clients.get(url)
    assert second.status_code == 200 and second.headers["X-Treg-Cache"] == "hit"
    balance = await _balance(clients)
    third = await clients.get(url)
    assert third.status_code == 429
    assert attempts == {EP: 3} and used == {EP: 2}
    assert await _balance(clients) == balance


async def test_no_rule_passes_and_upstream_failure_keeps_admission(clients, monkeypatch, platform_on):
    attempts, _ = _enable(monkeypatch)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert attempts == {EP: 1}
    attempts, used = _enable(monkeypatch, {EP: 1})
    monkeypatch.setattr(call_service, "relay", _fake_relay(500))
    assert (await clients.get(f"/call/{EP}?aweme_id=8")).status_code == 500
    assert (await clients.get(f"/call/{EP}?aweme_id=8")).status_code == 429
    assert used == {EP: 1} and attempts == {EP: 2}


@pytest.mark.parametrize("skip_direct", [False, True])
@pytest.mark.parametrize("limit", [0, 1])
async def test_overflow_checks_original_endpoint_once(clients, monkeypatch, overflow_on, skip_direct, limit):
    await _route(price_micro=3000)
    if skip_direct:
        await _exhausted()
    attempts, used = _enable(monkeypatch, {EP: limit})
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([OK_ANSWER], seen))
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == (200 if limit else 429), response.text
    assert attempts == {EP: 1}
    assert used[EP] == limit and len(seen) == limit
    if not limit:
        await _assert_no_money()


@pytest.mark.parametrize("blocked", [FIND, "tomba.people.email.find", "treg.people.email.verify"])
async def test_routed_parent_child_and_verification_cannot_fallback(
    clients, monkeypatch, verifiers_on, blocked,
):
    attempts, used = _enable(monkeypatch, {blocked: 0})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    before = await _balance(clients)
    response = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error"] == "org_endpoint_quota_exceeded"
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    assert attempts[blocked] == 1 and used[blocked] == 0
    assert seen == (["tomba"] if blocked == "treg.people.email.verify" else [])
    assert await _balance(clients) == before
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        assert (await db.execute(select(LedgerEntry).where(LedgerEntry.kind == "settle"))).scalars().all() == []


async def test_route_internal_polls_consume_once_per_endpoint(clients, monkeypatch, bounceban_on, fast_polls):
    attempts, used = _enable(monkeypatch, {VERIFY: 1, STATUS: 1})
    upstream = Upstream(VERIFYING, [VERIFYING, _finished("undeliverable")])
    monkeypatch.setattr(call_service, "relay", upstream)
    response = await clients.post("/call/treg.people.email.verify", json={"email": EMAIL},
                                  headers={"X-Treg-Route-Prefer": "bounceban"})
    assert response.status_code == 200, response.text
    assert upstream.seen == ["submit", "poll", "poll"]
    assert attempts == {"treg.people.email.verify": 1, VERIFY: 1, STATUS: 1}
    assert used == {VERIFY: 1, STATUS: 1}


async def test_route_poll_quota_error_propagates_instead_of_retrying(clients, monkeypatch, bounceban_on, fast_polls):
    attempts, _ = _enable(monkeypatch, {STATUS: 0})
    upstream = Upstream(VERIFYING)
    monkeypatch.setattr(call_service, "relay", upstream)
    response = await clients.post("/call/treg.people.email.verify", json={"email": EMAIL},
                                  headers={"X-Treg-Route-Prefer": "bounceban"})
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error"] == "org_endpoint_quota_exceeded"
    assert attempts[STATUS] == 1
    assert upstream.seen == ["submit"]


async def test_own_credential_catalog_call_obeys_exact_canonical_quota(clients, monkeypatch):
    await clients.post("/secrets", json={"name": "tikhub", "value": "SYNTHETIC-OWN-KEY"})
    attempts, _ = _enable(monkeypatch, {EP: 0})
    relay = AsyncMock(side_effect=AssertionError("own-key upstream must not run"))
    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["endpoint_id"] == EP
    assert attempts == {EP: 1}
    relay.assert_not_called()
    await _assert_no_money()


async def test_provider_burst_retry_is_one_quota_admission(clients, monkeypatch, platform_on):
    from test_capacity_smoothing import _relay_script

    attempts, used = _enable(monkeypatch, {EP: 1})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script([
        (429, ((b"retry-after", b"0"),), b'{"detail":"slow"}'),
        (200, (), b'{"ok":true}'),
    ], seen))
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200, response.text
    assert len(seen) == 2
    assert attempts == used == {EP: 1}
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 429
    assert len(seen) == 2
