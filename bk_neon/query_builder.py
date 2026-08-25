"""
Async version of the candidate-set query builder. Turns filled
OrderConstraints into the minimal set of rows sent to the LLM's context,
querying the real Postgres schema (items + combos with composite PK).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from constraints import OrderConstraints
from models import Item, Combo, Category


async def build_candidate_items(session: AsyncSession, constraints: OrderConstraints,
                                 limit: int = 8) -> list[dict]:
    stmt = (
        select(Item, Category.name.label("category_name"))
        .join(Category, Category.id == Item.category_id)
        .where(Item.is_available.is_(True))
    )

    if constraints.veg_pref == "veg":
        stmt = stmt.where(Item.is_veg.is_(True))
    elif constraints.veg_pref == "nonveg":
        stmt = stmt.where(Item.is_veg.is_(False))

    if constraints.budget_total is not None:
        stmt = stmt.where(Item.price <= constraints.budget_total)

    if constraints.category_interest:
        stmt = stmt.where(Category.name.ilike(f"%{constraints.category_interest}%"))

    stmt = stmt.order_by(Item.price.asc()).limit(limit)

    result = await session.execute(stmt)
    rows = result.all()

    return [
        {
            "name": item.name,
            "price": item.price,
            "calories": item.calories,
            "protein_g": item.protein_g,
            "is_veg": item.is_veg,
            "category": category_name,
        }
        for item, category_name in rows
    ]


async def build_candidate_combos(session: AsyncSession, constraints: OrderConstraints,
                                  limit: int = 5) -> list[dict]:
    stmt = select(Combo).where(Combo.is_available.is_(True))

    if constraints.budget_total is not None:
        stmt = stmt.where(Combo.price <= constraints.budget_total)

    if constraints.veg_pref == "veg":
        stmt = stmt.where(Combo.is_veg.is_(True))
    elif constraints.veg_pref == "nonveg":
        stmt = stmt.where(Combo.is_veg.is_(False))

    if constraints.num_people is not None:
        stmt = stmt.where(Combo.num_people == constraints.num_people)

    stmt = stmt.order_by(Combo.price.asc()).limit(limit)

    result = await session.execute(stmt)
    combos = result.scalars().all()

    return [
        {
            "id": c.id,
            "is_veg": c.is_veg,
            "num_people": c.num_people,
            "name": c.name,
            "price": c.price,
        }
        for c in combos
    ]
