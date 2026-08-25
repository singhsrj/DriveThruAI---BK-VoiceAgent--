"""
Async version of the max-info-gain slot selector, querying the real
Postgres `items` table via SQLAlchemy instead of raw sqlite3.

Same logic as the original: for each unfilled constraint slot, measure
how much asking about it would shrink the candidate item set, and ask
the most informative question next.
"""

from typing import Optional
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from constraints import OrderConstraints
from models import Item

SLOT_CANDIDATES = {
    "veg_pref": ["veg", "nonveg"],
    "budget_total": ["under_150", "150_300", "over_300"],
    "num_people": ["solo", "group"],
}

QUESTION_TEXT = {
    "veg_pref": "Would you like veg, non-veg, or either?",
    "budget_total": "What's your budget for the order?",
    "num_people": "How many people are you ordering for?",
}


def _apply_base_filters(stmt, constraints: OrderConstraints):
    stmt = stmt.where(Item.is_available.is_(True))
    if constraints.veg_pref == "veg":
        stmt = stmt.where(Item.is_veg.is_(True))
    elif constraints.veg_pref == "nonveg":
        stmt = stmt.where(Item.is_veg.is_(False))
    if constraints.budget_total is not None:
        stmt = stmt.where(Item.price <= constraints.budget_total)
    return stmt


async def _count(session: AsyncSession, stmt) -> int:
    result = await session.execute(select(func.count()).select_from(stmt.subquery()))
    return result.scalar_one()


async def _entropy_reduction(session: AsyncSession, constraints: OrderConstraints, slot: str) -> float:
    base_stmt = _apply_base_filters(select(Item.id), constraints)
    current_count = await _count(session, base_stmt)
    if current_count == 0:
        return 0.0

    weighted_remaining = 0.0
    for value in SLOT_CANDIDATES[slot]:
        stmt = base_stmt
        if slot == "veg_pref":
            stmt = stmt.where(Item.is_veg.is_(value == "veg"))
        elif slot == "budget_total":
            if value == "under_150":
                stmt = stmt.where(Item.price <= 150)
            elif value == "150_300":
                stmt = stmt.where(Item.price > 150, Item.price <= 300)
            else:
                stmt = stmt.where(Item.price > 300)
        elif slot == "num_people":
            if value == "solo":
                stmt = stmt.where(Item.piece_count <= 2)
            else:
                stmt = stmt.where(Item.piece_count > 2)

        weighted_remaining += await _count(session, stmt)

    avg_remaining = weighted_remaining / len(SLOT_CANDIDATES[slot])
    return current_count - avg_remaining


async def next_best_question(session: AsyncSession, constraints: OrderConstraints) -> Optional[str]:
    missing = constraints.missing_slots()
    if not missing:
        return None

    scores = {}
    for slot in missing:
        if slot in SLOT_CANDIDATES:
            scores[slot] = await _entropy_reduction(session, constraints, slot)

    if not scores:
        return missing[0]

    # Prefer slots that directly partition items (veg_pref, budget_total)
    # over num_people, which only proxies via piece_count.
    priority = {"veg_pref": 2, "budget_total": 2, "num_people": 1}
    best_score = max(scores.values())
    tied = [slot for slot, score in scores.items() if score == best_score]
    return max(tied, key=lambda s: priority.get(s, 0))
