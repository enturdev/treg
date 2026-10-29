"""Measured verdict quality per endpoint, for `X-Treg-Route-Decisive` (route.py).

A verifier is worth asking next in proportion to how often it decides (`decisive_rate`: valid or
invalid, not unknown / catch-all / risky) and how often that decision agrees with the other
verifiers' majority (`agreement`). Expected net correct verdicts per call:

    decisive_rate x (agreement - (1 - agreement)) = decisive_rate x (2 x agreement - 1)

a right verdict +1, a wrong one -1, none 0. The numbers come from production call events scored
outside the request path; this module only reads them. An endpoint with too few comparable
verdicts is not rated and keeps its price order behind the rated ones."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

MIN_COMPARABLE = 200  # verdicts compared with other verifiers before a rate is trusted
SLICES = ("all", "catch_all", "normal")


@dataclass(frozen=True)
class VerdictQuality:
    decisive_rate: float
    agreement: float
    comparable: int

    @property
    def expected_net(self) -> float:
        return self.decisive_rate * (2 * self.agreement - 1)


Snapshot = dict[str, dict[str, VerdictQuality]]   # slice -> endpoint_id -> quality


class RouteQualityReader(Protocol):
    async def get(self, capability: str) -> Snapshot | None: ...


def parse(doc: Any) -> Snapshot:
    """The routing file the scorer publishes: `{"slices": {slice: {endpoint_id: {decisive_rate,
    agreement, comparable}}}}`. Entries missing a number are left out, never guessed."""
    out: Snapshot = {}
    slices = doc.get("slices") if isinstance(doc, dict) else None
    for name, rows in (slices.items() if isinstance(slices, dict) else ()):
        if name not in SLICES or not isinstance(rows, dict):
            continue
        for endpoint_id, row in rows.items():
            try:
                q = VerdictQuality(float(row["decisive_rate"]), float(row["agreement"]), int(row["comparable"]))
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= q.decisive_rate <= 1 and 0 <= q.agreement <= 1 and q.comparable >= 0):
                continue
            out.setdefault(name, {})[str(endpoint_id)] = q
    return out


def rated(snapshot: Snapshot | None, endpoint_id: str, slice_: str) -> VerdictQuality | None:
    """This endpoint's quality on the slice; None when it has too few compared verdicts there."""
    q = (snapshot or {}).get(slice_, {}).get(endpoint_id)
    return q if q is not None and q.comparable >= MIN_COMPARABLE else None


T = TypeVar("T")


def by_quality(items: list[T], endpoint_of, snapshot: Snapshot | None, slice_: str) -> list[T]:
    """Endpoints rated on the slice first, best expected net first; then those rated only on all
    calls (a catch-all address is harder than the average one, so the two never compete on one
    number); the unrated last, in their given order."""
    tiers = (slice_, "all") if slice_ != "all" else ("all",)
    ranked: list[tuple[int, float, int, T]] = []
    for i, x in enumerate(items):
        tier = next(((t, q) for t, name in enumerate(tiers) if (q := rated(snapshot, endpoint_of(x), name))), None)
        ranked.append((tier[0], -tier[1].expected_net, i, x) if tier else (len(tiers), 0.0, i, x))
    return [x for *_, x in sorted(ranked, key=lambda r: r[:3])]
