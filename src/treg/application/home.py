"""What bare `treg` shows, in one read: the caller's account, team, balance and own tools, and how
many tools the catalog holds."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..domain import money
from ..domain.catalog import store as catalog_store
from ..domain.governance import access as access_policy
from ..domain.identity.access import Caller
from ..models import Tool
from . import billing


async def summary(db: AsyncSession, caller: Caller, *, teams: int) -> dict:
    balance = await money.balance_of(db, caller.org_id)
    # Reading the balance is one of auto top-up's trigger points, as on GET /orgs/{id}/balance.
    billing.maybe_schedule_autotopup(caller.org)
    rows = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    return {
        "email": caller.email, "org_id": caller.org_id, "org": caller.org.slug, "role": caller.role,
        "teams": teams, "balance_micro": balance,
        "own_tools": [t.name for t in rows if access_policy._tool_usable(caller, t)],
        "catalog_tools": catalog_store.headline_counts(catalog_store.load())[0],
    }
