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

_ADMIT = """
if redis.call('TYPE', KEYS[1]).ok ~= 'hash'
    or redis.call('PTTL', KEYS[1]) ~= -1
    or redis.call('HGET', KEYS[1], '__schema') ~= '1' then return 2 end
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
if string.len(raw) > 1024 then return 2 end
local ok, state = pcall(cjson.decode, raw)
if not ok or type(state) ~= 'table' then return 2 end
local function integer(value, low)
    return type(value) == 'number' and value >= low and value <= 2147483647
        and value == math.floor(value)
end
local count = 0
for _ in pairs(state) do count = count + 1 end
if count ~= 4 or not integer(state.version, 1)
    or not integer(state.limit, 0) or not integer(state.used, 0)
    or type(state.enabled) ~= 'boolean' then return 2 end
if not state.enabled then return 0 end
if state.used >= state.limit then return 1 end
state.used = state.used + 1
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(state))
return 0
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
        if (type(org_id) is not int or not 0 < org_id <= 2147483647
                or not isinstance(endpoint_id, str) or len(endpoint_id) > 255
                or not _ENDPOINT.fullmatch(endpoint_id)):
            return "unavailable"
        try:
            async with asyncio.timeout(_TIMEOUT_S):
                result = await self._client.eval(
                    _ADMIT, 1, KEY, f"rule:{org_id}:{endpoint_id}")
            if type(result) is not int or result not in (0, 1, 2):
                return "unavailable"
            return ("allowed", "exceeded", "unavailable")[result]
        except Exception:  # noqa: BLE001 - unknown admission must not run or retry a paid call
            return "unavailable"

    async def aclose(self) -> None:
        await self._client.aclose()


_store: RedisQuotaStore | None = None


async def admit(org_id: int, endpoint_id: str) -> Outcome:
    if not get_settings().org_endpoint_quota_enabled:
        return "allowed"
    global _store
    try:
        if _store is None:
            _store = RedisQuotaStore(get_settings().org_endpoint_quota_redis_url)
        return await _store.admit(org_id, endpoint_id)
    except Exception:  # noqa: BLE001 - includes unavailable client initialization, never fail open
        return "unavailable"


async def close() -> None:
    global _store
    if _store is not None:
        await _store.aclose()
        _store = None
