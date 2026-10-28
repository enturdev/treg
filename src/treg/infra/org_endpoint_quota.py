"""Persistent, exact organization/endpoint admission in Redis."""
from __future__ import annotations

import asyncio
import re
from typing import Literal

from ..config import get_settings

KEY = "treg:org-endpoint-quota:v1"
_TIMEOUT_S = 1.0
_ENDPOINT = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+\Z")
Outcome = Literal["allowed", "exceeded", "unavailable"]
Target = tuple[int, str]

_ADMIT = """
if redis.call('TYPE', KEYS[1]).ok ~= 'hash'
    or redis.call('PTTL', KEYS[1]) ~= -1
    or redis.call('HGET', KEYS[1], '__schema') ~= '1' then return {2, 1} end
local function integer(value, low)
    return type(value) == 'number' and value >= low and value <= 2147483647
        and value == math.floor(value)
end
local writes = {}
for i, field in ipairs(ARGV) do
    local raw = redis.call('HGET', KEYS[1], field)
    if raw then
        if string.len(raw) > 1024 then return {2, i} end
        local ok, state = pcall(cjson.decode, raw)
        if not ok or type(state) ~= 'table' then return {2, i} end
        local count = 0
        for _ in pairs(state) do count = count + 1 end
        if count ~= 4 or not integer(state.version, 1)
            or not integer(state.limit, 0) or not integer(state.used, 0)
            or type(state.enabled) ~= 'boolean' then return {2, i} end
        if state.enabled then
            if state.used >= state.limit then return {1, i} end
            state.used = state.used + 1
            table.insert(writes, field)
            table.insert(writes, cjson.encode(state))
        end
    end
end
if #writes > 0 then redis.call('HSET', KEYS[1], unpack(writes)) end
return {0}
"""


class RedisQuotaStore:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        self._client = redis.Redis.from_url(
            url, socket_timeout=_TIMEOUT_S, socket_connect_timeout=_TIMEOUT_S,
            decode_responses=True, retry=Retry(NoBackoff(), 0),
        )

    async def admit(self, org_id: int, endpoint_id: str) -> Outcome:
        outcome, _ = await self.admit_many(((org_id, endpoint_id),))
        return outcome

    async def admit_many(self, targets: tuple[Target, ...]) -> tuple[Outcome, Target | None]:
        if not targets:
            return "allowed", None
        for org_id, endpoint_id in targets:
            if (type(org_id) is not int or not 0 < org_id <= 2147483647
                    or not isinstance(endpoint_id, str) or len(endpoint_id) > 255
                    or not _ENDPOINT.fullmatch(endpoint_id)):
                return "unavailable", (org_id, endpoint_id)
        unique = tuple(dict.fromkeys(targets))
        try:
            async with asyncio.timeout(_TIMEOUT_S):
                result = await self._client.eval(
                    _ADMIT, 1, KEY, *(f"rule:{org}:{endpoint}" for org, endpoint in unique))
            if (isinstance(result, list) and len(result) == 1
                    and type(result[0]) is int and result[0] == 0):
                return "allowed", None
            if (isinstance(result, list) and len(result) == 2
                    and type(result[0]) is int and result[0] in (1, 2)
                    and type(result[1]) is int and 1 <= result[1] <= len(unique)):
                return ("exceeded" if result[0] == 1 else "unavailable",
                        unique[result[1] - 1])
            return "unavailable", unique[0]
        except Exception:  # noqa: BLE001 - unknown admission must not run or retry a paid call
            return "unavailable", unique[0]

    async def aclose(self) -> None:
        await self._client.aclose()


_store: RedisQuotaStore | None = None


async def admit(org_id: int, endpoint_id: str) -> Outcome:
    outcome, _ = await admit_many(((org_id, endpoint_id),))
    return outcome


async def admit_many(targets: tuple[Target, ...]) -> tuple[Outcome, Target | None]:
    if not get_settings().org_endpoint_quota_enabled:
        return "allowed", None
    global _store
    try:
        if _store is None:
            _store = RedisQuotaStore(get_settings().org_endpoint_quota_redis_url)
        return await _store.admit_many(targets)
    except Exception:  # noqa: BLE001 - includes unavailable client initialization, never fail open
        return "unavailable", targets[0] if targets else None


async def close() -> None:
    global _store
    if _store is not None:
        await _store.aclose()
        _store = None
