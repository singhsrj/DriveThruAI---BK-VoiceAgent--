"""Snapshot the live bk-menu MCP server into test fixtures.

Writes, under src/fixtures/:
  menu.json       categories, items (with sizes) and combos (with items)
  mcp_tools.json  every tool's name, description and input schema, exactly
                  as the server advertises them

Simulations and tests serve the menu from these files instead of the live
server, so runs are repeatable. Rerun this when the menu or the tools change:

    uv run python evals/scripts/snapshot_menu.py            # write fixtures
    uv run python evals/scripts/snapshot_menu.py --check    # exit 1 on drift
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Shipped inside src/ so the deployed (staging) agent can serve them.
FIXTURES = Path(__file__).resolve().parents[2] / "src" / "fixtures"

# Calls replayed against both the live server and the fixture fakes in
# --check mode; their outputs must match byte for byte.
PARITY_CALLS: list[tuple[str, dict]] = [
    ("list_categories", {}),
    ("list_items", {"category": "Beverages", "is_veg": True, "limit": 10}),
    ("list_items", {"limit": 15, "offset": 20}),
    ("search_items", {"query": "whopper"}),
    ("search_items", {"query": "pizza"}),
    ("search_items", {"query": "sushi"}),
    ("get_item", {"item_id": 1}),
    ("get_item", {"item_id": 9999}),
    ("filter_items_by_price", {"max_price": 120}),
    ("filter_items_by_calories", {"max_calories": 250, "limit": 200}),
    ("get_high_protein_items", {}),
    ("list_combos", {"is_veg": True}),
    ("get_combo", {"combo_id": 1, "is_veg": False}),
    ("get_combo", {"combo_id": 9999, "is_veg": True}),
    ("get_menu_stats", {}),
]

NO_RESULT = "<no content: ToolError>"


async def _call(session: ClientSession, name: str, **args):
    result = await session.call_tool(name, args)
    if result.isError:
        raise RuntimeError(f"{name}({args}) failed: {result.content}")
    # FastMCP wraps list/None returns as {"result": ...} in structured content.
    data = result.structuredContent
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def _as_llm_sees_it(result) -> str:
    """Mirror LiveKit's default MCP result resolver (one content block)."""
    if not result.content:
        return NO_RESULT
    assert len(result.content) == 1, result.content
    return result.content[0].model_dump_json()


def _same_rows(live: str, fake: str) -> bool:
    """Same rows in a different order. Postgres orders ties (equal price,
    calories, protein) unstably, and the order even changes with LIMIT, so
    only the set of rows has to match. Queries
    above are chosen so LIMIT never cuts through a tie."""
    try:
        a, b = (json.loads(json.loads(x)["text"]) for x in (live, fake))
    except (ValueError, KeyError, TypeError):
        return False
    if not (isinstance(a, list) and isinstance(b, list)):
        return False

    def canon(rows: list) -> list[str]:
        return sorted(json.dumps(r, sort_keys=True) for r in rows)

    return canon(a) == canon(b)


async def parity(session: ClientSession) -> list[str]:
    from livekit.agents.llm import ToolError

    import sim_support

    fx = sim_support.load_fixture(FIXTURES)
    diffs = []
    for name, args in PARITY_CALLS:
        live = _as_llm_sees_it(await session.call_tool(name, args))
        try:
            fake = sim_support.call(fx, name, args)
        except ToolError:
            fake = NO_RESULT
        if live != fake and not _same_rows(live, fake):
            diffs.append(f"{name}({args}):\n  live: {live[:300]}\n  fake: {fake[:300]}")
    return diffs


async def snapshot(url: str, check_parity: bool = False):
    async with (
        streamablehttp_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        diffs = await parity(session) if check_parity else []
        tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
            }
            for t in (await session.list_tools()).tools
        ]
        categories = await _call(session, "list_categories")
        listed = await _call(session, "list_items", available_only=False, limit=200)
        items = [await _call(session, "get_item", item_id=i["id"]) for i in listed]
        combos = [
            await _call(session, "get_combo", combo_id=c["id"], is_veg=c["is_veg"])
            for c in await _call(
                session, "list_combos", available_only=False, limit=200
            )
        ]
    menu = {
        "source": "bk-menu-mcp",
        "currency": "INR",
        "categories": sorted(categories, key=lambda c: c["id"]),
        "items": sorted(items, key=lambda i: i["id"]),
        "combos": sorted(combos, key=lambda c: (c["id"], c["is_veg"])),
    }
    return menu, sorted(tools, key=lambda t: t["name"]), diffs


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="MCP endpoint (default: $MCP_SERVER_URL)")
    parser.add_argument(
        "--check", action="store_true", help="compare with fixtures, write nothing"
    )
    args = parser.parse_args()

    load_dotenv(".env.local")
    url = args.url or os.getenv("MCP_SERVER_URL")
    if not url:
        sys.exit("Set MCP_SERVER_URL or pass --url")

    menu, tools, diffs = asyncio.run(snapshot(url, check_parity=args.check))
    outputs = {"menu.json": _dump(menu), "mcp_tools.json": _dump(tools)}

    if args.check:
        drifted = [
            name
            for name, text in outputs.items()
            if not (FIXTURES / name).exists()
            or (FIXTURES / name).read_text(encoding="utf-8") != text
        ]
        for name in drifted:
            print(f"DRIFT: src/fixtures/{name} differs from the live server")
        for diff in diffs:
            print(f"PARITY: {diff}")
        if not (drifted or diffs):
            print(f"OK: fixtures current, {len(PARITY_CALLS)} parity calls match")
        return 1 if drifted or diffs else 0

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        (FIXTURES / name).write_text(text, encoding="utf-8", newline="\n")
    print(
        f"Wrote {len(menu['items'])} items, {len(menu['combos'])} combos, "
        f"{len(tools)} tools to {FIXTURES}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
