---
title: Exact organization and endpoint call allowances
status: shipped
sources:
  - src/treg/infra/org_endpoint_quota.py
  - src/treg/application/call/service.py
  - src/treg/application/call/types.py
  - src/treg/application/call/route.py
  - src/treg/application/call/async_bridge.py
  - src/treg/bootstrap.py
  - src/treg/config.py
  - tests/test_org_endpoint_quota_redis.py
  - tests/test_org_endpoint_quota_call.py
  - tests/test_org_endpoint_quota_hub.py
  - tests/test_call_architecture.py
related:
  - architecture/proxy-model.md
  - architecture/org-flow-control.md
  - architecture/money.md
---

# Exact organization and endpoint call allowances

`service._admit_org_endpoint` enforces an operator-configured, one-time call allowance for an
exact trusted Org ID and canonical catalog endpoint ID. It applies to every credential tier.
There is no wildcard, automatic replenishment, or time window. This is protective admission,
not money or the provider's credits. Changing a limit preserves `used`; reset is explicit.
`limit=0` refuses new logical calls immediately after the management write. Calls already
admitted continue. Clearing a rule keeps a disabled tombstone and its count.

The shared call application covers REST, catalog call, table and MCP, plus routed children and
hub steps that enter `execute_call`. Intake returns stored idempotent responses before admission.
Ordinary catalog calls check after authorization and final canonical resolution, before a hold,
archive lookup, public-resource I/O, `skip_direct`, relay or overflow. A cache hit consumes one
call. Direct relay retries and overflow remain the same logical call and consume no second slot.
Routed parents check their canonical ID before running the route, after authenticated intake;
the route's existing endpoint ACL checks remain with the children. Each distinct child endpoint
has its own allowance. Only route candidate, verification and poll contexts share
`quota_admissions`, so an internal retry/poll for that endpoint does not consume again. Separate
hub steps and separate requests get separate contexts.

Hub recipe IDs are not quota targets. Their catalog child calls are guarded, but the hub wrapper
retains its existing error and billing contract: it can translate a child refusal to 424 or
continue an `allow_fail` step, charging other completed steps and an applicable maker price.
The refused endpoint itself is neither executed nor charged. A hub run is not a routed endpoint.

An exhausted rule returns HTTP 429 with `detail.error=org_endpoint_quota_exceeded` and zero
call cost. It is not a supplier 429, has no automatic refill time, and cannot trigger routing
fallback or overflow. Redis uncertainty returns 503 `org_endpoint_quota_unavailable`, also
without beginning a new hold or provider call. Routed verification and async bridging propagate
these errors. Existing deferred holds are released when a route refuses. Previously accepted
asynchronous provider tasks still follow their existing settlement lifecycle; an admission
failure cannot undo a task already submitted. Accepted calls are never refunded to the quota
after upstream failure, cancellation or a lost Redis response.

## Store contract

Disabled by default. `TREG_ORG_ENDPOINT_QUOTA_ENABLED=true` requires an explicit
`TREG_ORG_ENDPOINT_QUOTA_REDIS_URL`; it does not inherit `TREG_KV_URL` or use a process-local
fallback. All enforcing instances and the private management API must point at the same
durable Redis database. The private admin uses its existing full machine authentication;
ordinary business keys cannot manage allowances.

One persistent hash, `treg:org-endpoint-quota:v1`, contains `__schema=1` and fields
`rule:<org_id>:<endpoint_id>`. Each rule is exactly:

```json
{"version":1,"limit":100,"used":0,"enabled":true}
```

All numeric fields are integers at most 2147483647; version starts at one and the others at zero.
The admission Lua script validates the namespace, absence of expiry, and complete rule before
checking `used < limit` and incrementing in the same atomic operation. A healthy namespace with
an absent rule is unconfigured and allows the call. Missing namespace/schema, malformed state,
missing count or Redis I/O failure is unknown and refuses admission, including for an otherwise
unconfigured pair. The rule and count share one field, so losing a separate counter cannot
accidentally create new allowance. The runtime never creates, expires or deletes rules.

Management uses a required unique operation ID, expected version and reason. It writes the new
rule and `op:<operation_id>` audit/idempotency record with one Redis HSET inside Lua. Audit records
include the authenticated actor/source, exact target, action, reason, before/after state and
Redis time. Consumption changes `used` without changing the management version, so a concurrent
limit edit preserves the count seen atomically at the edit. Retrying the same operation returns
its saved result, not a second reset; reusing an ID for different content or a stale version
conflicts. Operation results describe the original change; query the rule for its current count.
These records have no TTL. No best-effort audit queue is used for management writes.

## Activation and recovery boundary

Provision and verify durable Redis storage with no eviction and an appropriate AOF/backup policy
before activation. A disposable cache is unsuitable. Initialize the schema only once, on a
confirmed new empty namespace, before enabling the management API or consumers. Neither service
initializes missing storage on startup. Installation must atomically verify the key does not
exist before writing `__schema=1`; it is never an automated recovery action. Do not initialize
an unexpectedly missing or damaged namespace: restore the complete rules, counts and operation
records together from trusted durable state, or keep enforcement closed until reconciled.

The marker detects namespace loss, not rollback to a valid old snapshot or deliberate deletion
of a single rule. Redis replication/failover and persistence must be configured to the required
loss tolerance. If Redis reports a restored but stale count, this implementation cannot infer
the missing admissions. Do not enable until that operational boundary is accepted. The store
uses bounded I/O and disables Redis command retries; an ambiguous result returns 503 and can
conservatively spend one slot without executing upstream. In-memory routing deduplication never
substitutes for the shared count across requests or instances.

Deploy readers before enabling the private writer, then enable enforcement on every call worker
before creating a protective rule. An old or disabled worker cannot enforce it. Disabling the
feature bypasses all allowances and is not a safe incident recovery. No production configuration,
Redis initialization or rule application is performed by the code change itself.
