"""
Quick manual-testing script for the Neon Postgres DB.

Runs a handful of read-only sanity checks (row counts, sample rows,
a joined combo lookup) using the same async session setup as the
rest of the app (database.py -> AsyncSessionLocal).

Usage:
    python query_db.py            # run all built-in checks
    python query_db.py --sql "select count(*) from items"   # run raw SQL

Requires DATABASE_URL to be set (via .env), same as the main app.
"""

import argparse
import asyncio

from sqlalchemy import select, func, text

from database import AsyncSessionLocal
from models import Category, Item, ItemSize, Combo, ComboItem


async def print_counts(session):
    print("== Row counts ==")
    for model in (Category, Item, ItemSize, Combo, ComboItem):
        result = await session.execute(select(func.count()).select_from(model))
        print(f"  {model.__tablename__:<15} {result.scalar()}")


async def print_sample_items(session, limit=5):
    print(f"\n== Sample items (first {limit}) ==")
    result = await session.execute(
        select(Item).order_by(Item.id).limit(limit)
    )
    for item in result.scalars():
        print(f"  [{item.id}] {item.name!r} "
              f"veg={item.is_veg} price={item.price} "
              f"available={item.is_available}")


async def print_sample_combos(session, limit=5):
    print(f"\n== Sample combos (first {limit}, with items) ==")
    result = await session.execute(
        select(Combo).order_by(Combo.id, Combo.is_veg).limit(limit)
    )
    combos = result.scalars().all()
    for combo in combos:
        result = await session.execute(
            select(ComboItem, Item)
            .join(Item, Item.id == ComboItem.item_id)
            .where(
                ComboItem.combo_id == combo.id,
                ComboItem.combo_is_veg == combo.is_veg,
            )
        )
        rows = result.all()
        print(f"  [{combo.id}/{'veg' if combo.is_veg else 'nonveg'}] "
              f"{combo.name!r} price={combo.price} people={combo.num_people}")
        for ci, item in rows:
            print(f"      - {item.name} x{ci.quantity}")


async def run_raw_sql(session, sql: str):
    print(f"\n== Raw SQL ==\n  {sql}\n")
    result = await session.execute(text(sql))
    if result.returns_rows:
        rows = result.fetchall()
        for row in rows:
            print(" ", row)
        print(f"  ({len(rows)} row(s))")
    else:
        print("  (no rows returned)")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sql", help="run a raw SQL query instead of the built-in checks")
    parser.add_argument("--limit", type=int, default=5, help="sample row limit")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        if args.sql:
            await run_raw_sql(session, args.sql)
        else:
            await print_counts(session)
            await print_sample_items(session, args.limit)
            await print_sample_combos(session, args.limit)


if __name__ == "__main__":
    asyncio.run(main())