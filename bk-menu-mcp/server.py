"""
Minimal read-only MCP server over the Burger King menu Postgres database.

Run with:
    uv run server.py

Requires DATABASE_URL env var (or .env file) pointing at the Postgres DB.
"""

from typing import Optional

from fastmcp import FastMCP
from sqlalchemy import select, func, or_

from db import get_session
from models import Category, Item, ItemSize, Combo, ComboItem

mcp = FastMCP("bk-menu")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _item_to_dict(item: Item, sizes: Optional[list[ItemSize]] = None) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "category_id": item.category_id,
        "price": item.price,
        "calories": item.calories,
        "protein_g": item.protein_g,
        "piece_count": item.piece_count,
        "is_veg": item.is_veg,
        "is_available": item.is_available,
        "sizes": (
            [{"size": s.size, "price": s.price} for s in sizes]
            if sizes is not None
            else None
        ),
    }


def _combo_to_dict(combo: Combo, items: Optional[list[dict]] = None) -> dict:
    return {
        "id": combo.id,
        "is_veg": combo.is_veg,
        "name": combo.name,
        "num_people": combo.num_people,
        "price": combo.price,
        "is_available": combo.is_available,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_categories() -> list[dict]:
    """List all menu categories (id and name)."""
    async with get_session() as session:
        result = await session.execute(select(Category).order_by(Category.name))
        return [{"id": c.id, "name": c.name} for c in result.scalars().all()]


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_items(
    category: Optional[str] = None,
    is_veg: Optional[bool] = None,
    available_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List menu items, optionally filtered by category name and/or veg status.

    Args:
        category: Category name to filter by (case-insensitive exact match), e.g. "Burgers".
        is_veg: If set, filter to veg-only (True) or non-veg-only (False) items.
        available_only: If True (default), only return currently available items.
        limit: Max rows to return (default 50, capped at 200).
        offset: Row offset for pagination.
    """
    limit = min(max(limit, 1), 200)
    async with get_session() as session:
        stmt = select(Item).join(Category)
        if category:
            stmt = stmt.where(func.lower(Category.name) == category.lower())
        if is_veg is not None:
            stmt = stmt.where(Item.is_veg == is_veg)
        if available_only:
            stmt = stmt.where(Item.is_available.is_(True))
        stmt = stmt.order_by(Item.name).limit(limit).offset(offset)
        result = await session.execute(stmt)
        items = result.scalars().all()
        return [_item_to_dict(i) for i in items]


@mcp.tool()
async def search_items(query: str, limit: int = 20) -> list[dict]:
    """Search menu items by name (case-insensitive partial match).

    Args:
        query: Text to search for within item names.
        limit: Max rows to return (default 20, capped at 100).
    """
    limit = min(max(limit, 1), 100)
    async with get_session() as session:
        stmt = (
            select(Item)
            .where(Item.name.ilike(f"%{query}%"))
            .order_by(Item.name)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [_item_to_dict(i) for i in result.scalars().all()]


@mcp.tool()
async def get_item(item_id: int) -> Optional[dict]:
    """Get full details for a single menu item by id, including its size/price variants.

    Args:
        item_id: The item's numeric id.
    """
    async with get_session() as session:
        item = await session.get(Item, item_id)
        if item is None:
            return None
        sizes_result = await session.execute(
            select(ItemSize).where(ItemSize.item_id == item_id).order_by(ItemSize.price)
        )
        return _item_to_dict(item, sizes=list(sizes_result.scalars().all()))


@mcp.tool()
async def filter_items_by_price(
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    available_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    """List items within a price range (base item price, not size variants).

    Args:
        min_price: Minimum price (inclusive). Omit for no lower bound.
        max_price: Maximum price (inclusive). Omit for no upper bound.
        available_only: If True (default), only return currently available items.
        limit: Max rows to return (default 50, capped at 200).
    """
    limit = min(max(limit, 1), 200)
    async with get_session() as session:
        stmt = select(Item)
        if min_price is not None:
            stmt = stmt.where(Item.price >= min_price)
        if max_price is not None:
            stmt = stmt.where(Item.price <= max_price)
        if available_only:
            stmt = stmt.where(Item.is_available.is_(True))
        stmt = stmt.order_by(Item.price).limit(limit)
        result = await session.execute(stmt)
        return [_item_to_dict(i) for i in result.scalars().all()]


@mcp.tool()
async def filter_items_by_calories(
    max_calories: Optional[int] = None,
    min_calories: Optional[int] = None,
    available_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    """List items within a calorie range. Useful for "low calorie" / "under X cal" queries.

    Args:
        max_calories: Maximum calories (inclusive). Omit for no upper bound.
        min_calories: Minimum calories (inclusive). Omit for no lower bound.
        available_only: If True (default), only return currently available items.
        limit: Max rows to return (default 50, capped at 200).
    """
    limit = min(max(limit, 1), 200)
    async with get_session() as session:
        stmt = select(Item).where(Item.calories.is_not(None))
        if max_calories is not None:
            stmt = stmt.where(Item.calories <= max_calories)
        if min_calories is not None:
            stmt = stmt.where(Item.calories >= min_calories)
        if available_only:
            stmt = stmt.where(Item.is_available.is_(True))
        stmt = stmt.order_by(Item.calories).limit(limit)
        result = await session.execute(stmt)
        return [_item_to_dict(i) for i in result.scalars().all()]


@mcp.tool()
async def get_high_protein_items(
    min_protein_g: float = 15.0,
    available_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    """List items with at least a given amount of protein, sorted highest first.

    Args:
        min_protein_g: Minimum grams of protein (default 15g).
        available_only: If True (default), only return currently available items.
        limit: Max rows to return (default 20, capped at 100).
    """
    limit = min(max(limit, 1), 100)
    async with get_session() as session:
        stmt = (
            select(Item)
            .where(Item.protein_g.is_not(None))
            .where(Item.protein_g >= min_protein_g)
        )
        if available_only:
            stmt = stmt.where(Item.is_available.is_(True))
        stmt = stmt.order_by(Item.protein_g.desc()).limit(limit)
        result = await session.execute(stmt)
        return [_item_to_dict(i) for i in result.scalars().all()]


# ---------------------------------------------------------------------------
# Combos
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_combos(
    is_veg: Optional[bool] = None,
    available_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    """List combo meals, optionally filtered by veg status.

    Args:
        is_veg: If set, filter to veg-only (True) or non-veg-only (False) combos.
        available_only: If True (default), only return currently available combos.
        limit: Max rows to return (default 50, capped at 200).
    """
    limit = min(max(limit, 1), 200)
    async with get_session() as session:
        stmt = select(Combo)
        if is_veg is not None:
            stmt = stmt.where(Combo.is_veg == is_veg)
        if available_only:
            stmt = stmt.where(Combo.is_available.is_(True))
        stmt = stmt.order_by(Combo.name).limit(limit)
        result = await session.execute(stmt)
        return [_combo_to_dict(c) for c in result.scalars().all()]


@mcp.tool()
async def get_combo(combo_id: int, is_veg: bool) -> Optional[dict]:
    """Get full details for a single combo (by its id + veg variant), including its line items.

    Args:
        combo_id: The combo family id.
        is_veg: Whether to fetch the veg (True) or non-veg (False) variant of this combo.
    """
    async with get_session() as session:
        combo = await session.get(Combo, {"id": combo_id, "is_veg": is_veg})
        if combo is None:
            return None
        stmt = (
            select(ComboItem, Item)
            .join(Item, ComboItem.item_id == Item.id)
            .where(ComboItem.combo_id == combo_id, ComboItem.combo_is_veg == is_veg)
        )
        result = await session.execute(stmt)
        items = [
            {
                "item_id": item.id,
                "name": item.name,
                "quantity": combo_item.quantity,
            }
            for combo_item, item in result.all()
        ]
        return _combo_to_dict(combo, items=items)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_menu_stats() -> dict:
    """Get overall menu stats: item count, combo count, price range, category breakdown."""
    async with get_session() as session:
        item_count = (await session.execute(select(func.count(Item.id)))).scalar_one()
        combo_count = (await session.execute(select(func.count()).select_from(Combo))).scalar_one()
        price_row = (
            await session.execute(select(func.min(Item.price), func.max(Item.price)))
        ).one()
        cat_rows = (
            await session.execute(
                select(Category.name, func.count(Item.id))
                .join(Item, Item.category_id == Category.id)
                .group_by(Category.name)
                .order_by(Category.name)
            )
        ).all()
        return {
            "total_items": item_count,
            "total_combos": combo_count,
            "min_price": price_row[0],
            "max_price": price_row[1],
            "items_per_category": {name: count for name, count in cat_rows},
        }


if __name__ == "__main__":
    import os

    # Render (and most PaaS platforms) inject PORT and expect the service
    # to bind 0.0.0.0. Fall back to MCP_HOST/MCP_PORT for local dev, where
    # 127.0.0.1 is the safer default.
    mcp.run(
        transport="http",
        host=os.environ.get("MCP_HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"),
        port=8943,
    )
