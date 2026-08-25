"""
Enqueues jobs onto the arq worker -- run this while worker.py's worker
process is running separately, to test the full async job pipeline.

Usage:
    python3 enqueue_test_job.py
"""

import asyncio
import os
from arq import create_pool
from arq.connections import RedisSettings

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


async def main():
    redis = await create_pool(RedisSettings.from_dsn(REDIS_URL))

    job1 = await redis.enqueue_job(
        "confirm_order", order_id=42,
        items=[{"name": "Whopper", "qty": 1}, {"name": "Coke", "qty": 1}],
    )
    print(f"Enqueued confirm_order job: {job1.job_id}")

    job2 = await redis.enqueue_job("recompute_combo_prices")
    print(f"Enqueued recompute_combo_prices job: {job2.job_id}")

    # Wait for results (only works because we're polling here -- in a real
    # app you wouldn't block on this, the whole point is not blocking).
    result1 = await job1.result(timeout=10)
    print("confirm_order result:", result1)

    result2 = await job2.result(timeout=10)
    print("recompute_combo_prices result:", result2)

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
