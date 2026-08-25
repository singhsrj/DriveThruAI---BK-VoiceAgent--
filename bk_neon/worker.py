"""
arq worker -- runs background jobs against Redis, using the SAME async
SQLAlchemy session setup as the main app (database.py).

Why arq over Celery: arq is natively async (built on asyncio + redis.asyncio),
so jobs can `await` the same AsyncSession/asyncpg calls the rest of the app
uses, with no sync/async bridging. Celery's worker model is fundamentally
sync (or needs extra eventlet/gevent tricks to fake async), which fights
an async-first stack like this one.

Run the worker with:
    arq worker.WorkerSettings

Enqueue a job from anywhere in the app with:
    from arq import create_pool
    from arq.connections import RedisSettings
    redis = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    await redis.enqueue_job("confirm_order", order_id=123)
"""

import os
from arq.connections import RedisSettings
from sqlalchemy import select

from database import AsyncSessionLocal
from models import Combo, ComboItem, Item

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


async def confirm_order(ctx, order_id: int, items: list[dict]):
    """
    Example job: a voice call finishes, the agent hands off order
    confirmation (e.g. SMS/print ticket/kitchen display push) to a
    background worker instead of blocking the call on it.
    """
    print(f"[worker] confirming order {order_id} with {len(items)} items")
    # In a real system: write an `orders` table row, call a printer/KDS
    # webhook, send an SMS via Twilio, etc. Kept as a stub here.
    return {"order_id": order_id, "status": "confirmed"}


async def recompute_combo_prices(ctx):
    """
    Example job: nightly/periodic task that recalculates combo bundle
    prices from current item prices (in case item prices changed since
    combos were seeded). Demonstrates a worker job that does real async
    DB work, not just a stub.
    """
    updated = 0
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Combo))
        combos = result.scalars().all()

        for combo in combos:
            result = await session.execute(
                select(ComboItem).where(
                    ComboItem.combo_id == combo.id,
                    ComboItem.combo_is_veg == combo.is_veg,
                )
            )
            combo_items = result.scalars().all()

            total = 0.0
            for ci in combo_items:
                item = await session.get(Item, ci.item_id)
                total += item.price * ci.quantity

            new_price = round(total * 0.85 / 9) * 9
            if new_price != combo.price:
                combo.price = new_price
                updated += 1

        await session.commit()

    print(f"[worker] recomputed prices for {len(combos)} combos, {updated} changed")
    return {"checked": len(combos), "updated": updated}


async def startup(ctx):
    print("[worker] starting up")


async def shutdown(ctx):
    print("[worker] shutting down")


class WorkerSettings:
    functions = [confirm_order, recompute_combo_prices]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
