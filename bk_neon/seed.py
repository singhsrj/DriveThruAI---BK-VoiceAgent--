"""
Seeds the Neon DB via async SQLAlchemy sessions. Run AFTER
`alembic upgrade head` has created the schema.

Consolidates what used to be three separate SQLite scripts:
  seed_real_menu.py + merge_sides_into_snacks.py + migrate_combos_veg_nonveg.py
into one, since the schema is now created correctly-shaped from the start
(Snacks/Sides are already merged, combos already have the composite key) --
there's nothing to migrate, only to insert.

Usage:
    python3 seed.py
"""

import asyncio
from sqlalchemy import select

from database import AsyncSessionLocal
from models import Category, Item, Combo, ComboItem

CATEGORIES = ["BK Cafe", "Burgers & Wraps", "Snacks", "Beverages", "Desserts"]

# (name, category, price, calories, protein_g, piece_count, is_veg)
ITEMS = [
    # ---------------- BK CAFE ----------------
    ("Iced Americano", "BK Cafe", 180, 10, None, 1, True),
    ("Classic Cold Coffee", "BK Cafe", 199, 220, None, 1, True),
    ("Americano (Small)", "BK Cafe", 189, 11, None, 1, True),
    ("Americano (Regular)", "BK Cafe", 189, 15, None, 1, True),
    ("Cappuccino (Small)", "BK Cafe", 189, 201, None, 1, True),
    ("Cappuccino (Regular)", "BK Cafe", 189, 229, None, 1, True),
    ("Cafe Latte (Small)", "BK Cafe", 189, None, None, 1, True),
    ("Cafe Latte (Regular)", "BK Cafe", 189, 186, None, 1, True),
    ("Mocha Cappuccino (Small)", "BK Cafe", 179, 246, None, 1, True),
    ("Mocha Cappuccino (Regular)", "BK Cafe", 229, 329, None, 1, True),
    ("Mocha Frappe", "BK Cafe", 269, 328, None, 1, True),
    ("Hot Chocolate", "BK Cafe", 179, 209, None, 1, True),
    ("Chocolate Thickshake (Cafe)", "BK Cafe", 189, 496, None, 1, True),
    ("Mango Thickshake (Cafe)", "BK Cafe", 189, 395, None, 1, True),
    ("Berry Blast Thickshake (Cafe)", "BK Cafe", 189, 476, None, 1, True),
    ("Choco Lava Cup", "BK Cafe", 119, 349, None, 1, True),
    ("Masala Chai", "BK Cafe", 109, 89, None, 1, True),
    ("Iced Latte", "BK Cafe", 199, 146, None, 1, True),
    ("BK Fusion Shake (Made With KitKat)", "BK Cafe", 279, None, None, 1, True),

    # ---------------- BURGERS & WRAPS -- VEG ----------------
    ("Crispy Veg Burger", "Burgers & Wraps", 89, 382, None, 1, True),
    ("Crispy Veg Double Patty Burger", "Burgers & Wraps", 99, 624, None, 1, True),
    ("Veg Makhani Burst Burger", "Burgers & Wraps", 89, 359, None, 1, True),
    ("Veg Makhani Burst Double Patty Burger", "Burgers & Wraps", 79, 359, 10.3, 1, True),
    ("Veg Crunchy Taco", "Burgers & Wraps", 90, 234, None, 1, True),
    ("BK Veggie Burger", "Burgers & Wraps", 99, 328, None, 1, True),
    ("Crispy Veg Double Patty Burger With Cheese", "Burgers & Wraps", 99, None, None, 1, True),
    ("Crispy Veg Burger With Cheese", "Burgers & Wraps", 98, None, None, 1, True),
    ("Veg Whopper Deluxe", "Burgers & Wraps", 149, 656, 14.1, 1, True),
    ("Veg Whopper Deluxe With Cheese", "Burgers & Wraps", 174, None, None, 1, True),
    ("Paneer Whopper Deluxe", "Burgers & Wraps", 199, None, None, 1, True),
    ("Cheese Whopper Deluxe", "Burgers & Wraps", 199, None, None, 1, True),
    ("Paneer Royale Wrap", "Burgers & Wraps", 229, 575, None, 1, True),
    ("Veg Whopper Deluxe Double Patty", "Burgers & Wraps", 189, None, None, 1, True),
    ("BK Veggie Double Patty Burger", "Burgers & Wraps", 189, None, None, 1, True),
    ("Hot 'N' Cheesy Burger", "Burgers & Wraps", 299, 662, None, 1, True),

    # ---------------- BURGERS & WRAPS -- CHICKEN ----------------
    ("Crispy Chicken Burger", "Burgers & Wraps", 99, None, 21.4, 1, False),
    ("Crispy Chicken Double Patty Burger", "Burgers & Wraps", 129, 559, 20.4, 1, False),
    ("Chicken Makhani Burst Burger", "Burgers & Wraps", 100, 298, 13.2, 1, False),
    ("BK Chicken Burger", "Burgers & Wraps", 149, 928, 13.3, 1, False),
    ("Crunchy Chicken Taco", "Burgers & Wraps", 118, 262, None, 1, False),
    ("Crispy Chicken Double Patty Burger With Cheese", "Burgers & Wraps", 124, None, None, 1, False),
    ("BK Chicken Burger With Cheese", "Burgers & Wraps", 164, 928, None, 1, False),
    ("Grill Chicken Whopper Deluxe", "Burgers & Wraps", 199, 389, None, 1, False),
    ("Chicken Whopper Deluxe", "Burgers & Wraps", 199, None, None, 1, False),
    ("BK Chicken Double Patty Burger", "Burgers & Wraps", 179, None, None, 1, False),
    ("Grill Chicken Whopper Deluxe With Cheese", "Burgers & Wraps", 204, 492, None, 1, False),
    ("Crunchy Chicken Wrap", "Burgers & Wraps", 229, 431, None, 1, False),
    ("Grill Chicken Whopper Deluxe Double Patty", "Burgers & Wraps", 269, 678, None, 1, False),
    ("Fiery Chicken Burger", "Burgers & Wraps", 299, 588, 16.3, 1, False),
    ("Chicken Tandoori Burger", "Burgers & Wraps", 299, 1023, 26.1, 1, False),

    # ---------------- BEVERAGES ----------------
    ("Coke", "Beverages", 79, None, None, 1, True),
    ("Masala Fizz", "Beverages", 129, None, None, 1, True),
    ("Coca Cola Medium", "Beverages", 95, 190, None, 1, True),
    ("Tropical Fizz", "Beverages", 129, None, None, 1, True),
    ("Fanta Medium", "Beverages", 95, 243, None, 1, True),
    ("Sprite Medium", "Beverages", 95, 214, None, 1, True),
    ("Medium Thums Up", "Beverages", 95, 189, None, 1, True),
    ("Chocolate Thick Shake", "Beverages", 189, 496, None, 1, True),
    ("Mango Thick Shake", "Beverages", 189, 496, None, 1, True),
    ("Berry Blast Thick Shake", "Beverages", 189, 474, None, 1, True),
    ("Schweppes Water Bottle", "Beverages", 67, None, None, 1, True),
    ("Medium Coca Cola Zero", "Beverages", 95, None, None, 1, True),

    # ---------------- SNACKS (Sides + Snacks merged from the start) ----------------
    ("French Fries", "Snacks", 89, 340, 4.0, 1, True),
    ("Onion Rings", "Snacks", 99, 310, 3.5, 1, True),
    ("Chicken Nuggets (6pc)", "Snacks", 149, 280, 16.0, 6, False),
    ("Regular Fries", "Snacks", 89, None, None, 1, True),
    ("BK Veg Pizza Puff", "Snacks", 89, 208, None, 1, True),
    ("Veggie Strips - 5 Pcs", "Snacks", 89, 256, None, 5, True),
    ("(4Pc) Crunchy Chicken Nuggets", "Snacks", 109, 169, None, 4, False),
    ("Crunchy Chicken Nuggets (4pcs) Fiery", "Snacks", 99, None, None, 4, False),
    ("Fries (Medium)", "Snacks", 119, 333, None, 1, True),
    ("Fries (King)", "Snacks", 139, 499, None, 1, True),
    ("King Fries", "Snacks", 99, None, None, 1, True),
    ("Peri Peri Fries (Medium)", "Snacks", 144, 349, None, 1, True),
    ("Medium Peri Peri Fries", "Snacks", 99, None, None, 1, True),
    ("Peri Peri Fries (King)", "Snacks", 189, 463, None, 1, True),
    ("Saucy Fries", "Snacks", 148, 561, None, 1, True),
    ("(9Pc) Crunchy Chicken Nuggets + 2 Dips", "Snacks", 179, None, 20.5, 9, False),
    ("Chicken Wings - 3pc Fried", "Snacks", 109, None, 15.4, 3, False),
    ("Boneless Wings - Regular", "Snacks", 169, None, 18.4, 1, False),
    ("Boneless Wings - Large", "Snacks", 289, None, 30.2, 1, False),
    ("Masala Hashbrown", "Snacks", 99, None, None, 1, True),
    ("(6Pc) Crunchy Chicken Nuggets + 1 Dip", "Snacks", 149, None, None, 6, False),
    ("Chicken Wings - 4pc Fried", "Snacks", 209, None, 15.7, 4, False),

    # ---------------- DESSERTS ----------------
    ("Choco Lava Cake", "Desserts", 89, 310, 4.0, 1, True),
    ("Oreo Shake", "Desserts", 129, 480, 9.0, 1, True),
]

# Combo generation, ported from migrate_combos_veg_nonveg.py
FAMILIES = [
    {"name": "Classic Combo", "roles": ["burger", "fries", "drink"]},
    {"name": "Value Combo", "roles": ["burger", "fries"]},
    {"name": "Feast Combo", "roles": ["burger", "snack", "fries", "drink"]},
    {"name": "Party Combo", "roles": ["burger", "fries", "drink", "dessert"]},
]

ROLE_ITEMS = {
    "burger": ("Veg Whopper Deluxe", "Chicken Whopper Deluxe"),
    "fries": ("Fries (Medium)", "Fries (Medium)"),
    "drink": ("Coca Cola Medium", "Coca Cola Medium"),
    "snack": ("Veggie Strips - 5 Pcs", "(9Pc) Crunchy Chicken Nuggets + 2 Dips"),
    "dessert": ("Choco Lava Cake", "Choco Lava Cake"),
}

BUNDLE_DISCOUNT = 0.85


async def seed_categories(session) -> dict[str, Category]:
    result = await session.execute(select(Category))
    existing = {c.name: c for c in result.scalars().all()}
    for name in CATEGORIES:
        if name not in existing:
            cat = Category(name=name)
            session.add(cat)
            existing[name] = cat
    await session.flush()
    return existing


async def seed_items(session, categories: dict[str, Category]) -> dict[str, Item]:
    result = await session.execute(select(Item))
    existing = {(i.name, i.category_id): i for i in result.scalars().all()}
    item_by_name: dict[str, Item] = {}
    inserted = 0

    for name, cat_name, price, calories, protein, pieces, veg in ITEMS:
        cat = categories[cat_name]
        key = (name, cat.id)
        if key in existing:
            item_by_name[name] = existing[key]
            continue
        item = Item(
            name=name, category=cat, price=price, calories=calories,
            protein_g=protein, piece_count=pieces, is_veg=veg, is_available=True,
        )
        session.add(item)
        item_by_name[name] = item
        inserted += 1

    await session.flush()
    print(f"Items: inserted {inserted}, already present {len(ITEMS) - inserted}")
    return item_by_name


async def seed_combos(session, items: dict[str, Item]):
    result = await session.execute(select(Combo.name))
    existing_names = set(result.scalars().all())
    result = await session.execute(select(Combo.id))
    existing_ids = set(result.scalars().all())
    next_id = (max(existing_ids) + 1) if existing_ids else 1

    created = 0
    for num_people in (1, 2, 3, 4):
        for family in FAMILIES:
            # Check by name FIRST, across both variants, before consuming an id --
            # otherwise re-running this script keeps minting new ids for combos
            # that already exist, silently duplicating everything.
            veg_name = f"{family['name']} for {num_people} (Veg)"
            nonveg_name = f"{family['name']} for {num_people} (Non-Veg)"
            if veg_name in existing_names or nonveg_name in existing_names:
                continue

            combo_id = next_id
            next_id += 1

            for is_veg in (True, False):
                combo_name = f"{family['name']} for {num_people} ({'Veg' if is_veg else 'Non-Veg'})"
                total_price = 0.0
                resolved: list[tuple[Item, int]] = []

                for role in family["roles"]:
                    veg_item_name, nonveg_item_name = ROLE_ITEMS[role]
                    item = items[veg_item_name if is_veg else nonveg_item_name]
                    qty = num_people
                    resolved.append((item, qty))
                    total_price += item.price * qty

                price = round(total_price * BUNDLE_DISCOUNT / 9) * 9

                combo = Combo(
                    id=combo_id, is_veg=is_veg, num_people=num_people,
                    name=combo_name, price=price, is_available=True,
                )
                session.add(combo)
                await session.flush()  # needed before combo_items FK references it

                for item, qty in resolved:
                    session.add(ComboItem(
                        combo_id=combo_id, combo_is_veg=is_veg,
                        item_id=item.id, quantity=qty,
                    ))
                created += 1

    await session.flush()
    print(f"Combos: inserted {created} new rows")


async def main():
    async with AsyncSessionLocal() as session:
        try:
            categories = await seed_categories(session)
            items = await seed_items(session, categories)
            await seed_combos(session, items)
            await session.commit()

            item_count = (await session.execute(select(Item))).scalars().all()
            combo_count = (await session.execute(select(Combo))).scalars().all()
            print(f"\nDone. items={len(item_count)}, combos={len(combo_count)}")
        except Exception:
            await session.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(main())
