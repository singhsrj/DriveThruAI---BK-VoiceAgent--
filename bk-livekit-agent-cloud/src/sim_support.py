"""Fixture-backed menu tools, used only when the agent runs under a simulation.

Production talks to the live bk-menu MCP server. Simulations instead get ten
local tools with the same names, descriptions and input schemas (copied from
``fixtures/mcp_tools.json``), answering from a frozen menu snapshot
(``fixtures/menu.json``). That makes runs repeatable and lets a scenario mark
items as out of stock.

The query logic is ported from ``bk-menu-mcp/server.py``, and results are
formatted exactly as LiveKit's MCP toolset hands them to the LLM, so the model
cannot tell the difference. ``evals/scripts/snapshot_menu.py --check`` compares
both against the live server.

Scenario ``userdata`` keys (in scenarios.yaml):
    menu: "live"               use the real MCP server instead of the fixture
    unavailable_items: [...]   item names to mark is_available=false
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from livekit.agents import function_tool
from livekit.agents.llm import RawFunctionTool, ToolError
from mcp.types import TextContent
from pydantic_core import to_json

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass
class MenuFixture:
    categories: list[dict]
    items: list[dict]
    combos: list[dict]
    tools: list[dict]
    unavailable: set[str] = field(default_factory=set)

    def item_available(self, item: dict) -> bool:
        return item["is_available"] and item["name"] not in self.unavailable


def load_fixture(
    directory: Path = FIXTURES, unavailable_items: list[str] | None = None
) -> MenuFixture:
    menu = json.loads((directory / "menu.json").read_text(encoding="utf-8"))
    tools = json.loads((directory / "mcp_tools.json").read_text(encoding="utf-8"))
    unavailable = set(unavailable_items or [])
    unknown = unavailable - {i["name"] for i in menu["items"]}
    if unknown:
        raise ValueError(
            f"unavailable_items not on the fixture menu: {sorted(unknown)}"
        )
    return MenuFixture(
        categories=menu["categories"],
        items=menu["items"],
        combos=menu["combos"],
        tools=tools,
        unavailable=unavailable,
    )


# --- serialization, matching bk-menu-mcp/server.py field order ---------------


def _item(fx: MenuFixture, item: dict, *, with_sizes: bool = False) -> dict:
    return {
        "id": item["id"],
        "name": item["name"],
        "category_id": item["category_id"],
        "price": item["price"],
        "calories": item["calories"],
        "protein_g": item["protein_g"],
        "piece_count": item["piece_count"],
        "is_veg": item["is_veg"],
        "is_available": fx.item_available(item),
        "sizes": (
            [{"size": s["size"], "price": s["price"]} for s in item["sizes"]]
            if with_sizes
            else None
        ),
    }


def _combo(combo: dict, *, with_items: bool = False) -> dict:
    return {
        "id": combo["id"],
        "is_veg": combo["is_veg"],
        "name": combo["name"],
        "num_people": combo["num_people"],
        "price": combo["price"],
        "is_available": combo["is_available"],
        "items": (
            [
                {"item_id": i["item_id"], "name": i["name"], "quantity": i["quantity"]}
                for i in combo["items"]
            ]
            if with_items
            else None
        ),
    }


def _clamp(limit: int, cap: int) -> int:
    return min(max(limit, 1), cap)


def _by_name(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: r["name"])


# --- the ten tools, ported query by query ------------------------------------


def list_categories(fx: MenuFixture) -> list[dict]:
    return [{"id": c["id"], "name": c["name"]} for c in _by_name(fx.categories)]


def list_items(
    fx: MenuFixture,
    category: str | None = None,
    is_veg: bool | None = None,
    available_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    limit = _clamp(limit, 200)
    cat_ids = {c["id"] for c in fx.categories if category is None}
    if category:
        cat_ids = {
            c["id"] for c in fx.categories if c["name"].lower() == category.lower()
        }
    rows = [
        i
        for i in fx.items
        if i["category_id"] in cat_ids
        and (is_veg is None or i["is_veg"] == is_veg)
        and (not available_only or fx.item_available(i))
    ]
    return [_item(fx, i) for i in _by_name(rows)[offset : offset + limit]]


def search_items(fx: MenuFixture, query: str, limit: int = 20) -> list[dict]:
    limit = _clamp(limit, 100)
    rows = [i for i in fx.items if query.lower() in i["name"].lower()]
    return [_item(fx, i) for i in _by_name(rows)[:limit]]


def get_item(fx: MenuFixture, item_id: int) -> dict | None:
    item = next((i for i in fx.items if i["id"] == item_id), None)
    if item is None:
        return None
    item = {**item, "sizes": sorted(item["sizes"], key=lambda s: s["price"])}
    return _item(fx, item, with_sizes=True)


def filter_items_by_price(
    fx: MenuFixture,
    min_price: float | None = None,
    max_price: float | None = None,
    available_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    limit = _clamp(limit, 200)
    rows = [
        i
        for i in fx.items
        if (min_price is None or i["price"] >= min_price)
        and (max_price is None or i["price"] <= max_price)
        and (not available_only or fx.item_available(i))
    ]
    return [_item(fx, i) for i in sorted(rows, key=lambda r: r["price"])[:limit]]


def filter_items_by_calories(
    fx: MenuFixture,
    max_calories: int | None = None,
    min_calories: int | None = None,
    available_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    limit = _clamp(limit, 200)
    rows = [
        i
        for i in fx.items
        if i["calories"] is not None
        and (max_calories is None or i["calories"] <= max_calories)
        and (min_calories is None or i["calories"] >= min_calories)
        and (not available_only or fx.item_available(i))
    ]
    return [_item(fx, i) for i in sorted(rows, key=lambda r: r["calories"])[:limit]]


def get_high_protein_items(
    fx: MenuFixture,
    min_protein_g: float = 15.0,
    available_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    limit = _clamp(limit, 100)
    rows = [
        i
        for i in fx.items
        if i["protein_g"] is not None
        and i["protein_g"] >= min_protein_g
        and (not available_only or fx.item_available(i))
    ]
    rows.sort(key=lambda r: r["protein_g"], reverse=True)
    return [_item(fx, i) for i in rows[:limit]]


def list_combos(
    fx: MenuFixture,
    is_veg: bool | None = None,
    available_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    limit = _clamp(limit, 200)
    rows = [
        c
        for c in fx.combos
        if (is_veg is None or c["is_veg"] == is_veg)
        and (not available_only or c["is_available"])
    ]
    return [_combo(c) for c in _by_name(rows)[:limit]]


def get_combo(fx: MenuFixture, combo_id: int, is_veg: bool) -> dict | None:
    combo = next(
        (c for c in fx.combos if c["id"] == combo_id and c["is_veg"] == is_veg), None
    )
    return None if combo is None else _combo(combo, with_items=True)


def get_menu_stats(fx: MenuFixture) -> dict:
    names = {c["id"]: c["name"] for c in fx.categories}
    per_cat: dict[str, int] = {}
    for i in fx.items:
        per_cat[names[i["category_id"]]] = per_cat.get(names[i["category_id"]], 0) + 1
    prices = [i["price"] for i in fx.items]
    return {
        "total_items": len(fx.items),
        "total_combos": len(fx.combos),
        "min_price": min(prices),
        "max_price": max(prices),
        "items_per_category": dict(sorted(per_cat.items())),
    }


QUERIES: dict[str, Callable[..., Any]] = {
    f.__name__: f
    for f in (
        list_categories,
        list_items,
        search_items,
        get_item,
        filter_items_by_price,
        filter_items_by_calories,
        get_high_protein_items,
        list_combos,
        get_combo,
        get_menu_stats,
    )
}


# --- LiveKit tools ------------------------------------------------------------


def format_result(tool_name: str, result: Any) -> str:
    """Render a result the way LiveKit's MCP toolset hands it to the LLM.

    FastMCP sends one JSON text block. A ``None`` or empty-list return sends
    no content at all, which LiveKit turns into this ToolError. That is a
    known bk-menu-mcp bug (an unknown id, or a search with no matches, breaks
    the tool call); it is kept here on purpose so simulations reproduce it
    until the server is fixed.
    """
    if result is None or result == []:
        raise ToolError(
            f"Tool '{tool_name}' completed without producing a result. "
            "This might indicate an issue with internal processing."
        )
    return TextContent(type="text", text=to_json(result).decode()).model_dump_json()


def call(fx: MenuFixture, tool_name: str, raw_arguments: dict[str, Any]) -> str:
    return format_result(tool_name, QUERIES[tool_name](fx, **raw_arguments))


def build_menu_tools(fx: MenuFixture) -> list[RawFunctionTool]:
    """One raw function tool per MCP tool, with the server's exact schema."""

    def make(spec: dict) -> RawFunctionTool:
        name = spec["name"]

        async def _tool(raw_arguments: dict[str, Any]) -> str:
            return call(fx, name, raw_arguments)

        impl: Callable[[dict[str, Any]], Awaitable[str]] = _tool
        return function_tool(
            impl,
            raw_schema={
                "name": name,
                "description": spec["description"],
                "parameters": spec["input_schema"],
            },
        )

    missing = {t["name"] for t in fx.tools} ^ set(QUERIES)
    if missing:
        raise RuntimeError(f"fixture tools and fakes differ: {sorted(missing)}")
    return [make(spec) for spec in fx.tools]
