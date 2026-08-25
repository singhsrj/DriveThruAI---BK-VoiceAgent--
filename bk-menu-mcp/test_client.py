"""
Mini MCP client to sanity-check the bk-menu MCP server.

Three modes:
  1. Stdio mode (default) -- spawns `uv run server.py` as a real subprocess
     over stdio, exactly like Claude Desktop / Claude Code would. This is
     the closest thing to an end-to-end test for the stdio transport.
  2. In-process mode (--in-process) -- imports the FastMCP server object
     directly and talks to it without spawning a subprocess. Faster, useful
     for iterating, but skips the transport layer entirely.
  3. HTTP mode (--url) -- connects to an already-running HTTP server
     (started separately with `uv run server.py` when transport="http").

Usage:
    uv run test_client.py                          # stdio, full smoke test
    uv run test_client.py --in-process              # in-process, full smoke test
    uv run test_client.py --url http://127.0.0.1:8000/mcp   # HTTP, full smoke test
    uv run test_client.py --tool list_items --args '{"limit": 5}'

Requires DATABASE_URL to be set (via .env or the environment) pointing at
a real Postgres DB with data, since every tool call hits the database.
"""

import argparse
import asyncio
import json
import sys

from fastmcp import Client


def _print_header(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def _print_result(label: str, result) -> None:
    # fastmcp Client tool calls return a CallToolResult; .data holds the
    # parsed structured content when available, otherwise fall back to
    # the raw content blocks.
    data = getattr(result, "data", None)
    if data is None:
        data = [getattr(block, "text", str(block)) for block in result.content]
    print(f"\n--- {label} ---")
    try:
        print(json.dumps(data, indent=2, default=str)[:2000])
    except TypeError:
        print(str(data)[:2000])


async def run_smoke_test(client: Client) -> bool:
    """Exercise every tool once with safe, minimal arguments. Returns True
    if everything ran without raising."""
    ok = True

    async with client:
        _print_header("1. Connectivity: ping + list_tools")
        await client.ping()
        print("ping: OK")

        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        print(f"{len(names)} tools registered:")
        for n in names:
            print(f"  - {n}")

        expected = {
            "list_categories", "list_items", "search_items", "get_item",
            "filter_items_by_price", "filter_items_by_calories",
            "get_high_protein_items", "list_combos", "get_combo",
            "get_menu_stats",
        }
        missing = expected - set(names)
        if missing:
            print(f"WARNING: missing expected tools: {missing}")
            ok = False

        _print_header("2. get_menu_stats (basic DB connectivity check)")
        try:
            result = await client.call_tool("get_menu_stats", {})
            _print_result("get_menu_stats", result)
        except Exception as e:
            print(f"FAILED: {e}")
            print(
                "\nIf this is a connection error, double check DATABASE_URL "
                "is set and reachable from this environment."
            )
            return False

        _print_header("3. list_categories")
        try:
            result = await client.call_tool("list_categories", {})
            _print_result("list_categories", result)
            categories = result.data or []
        except Exception as e:
            print(f"FAILED: {e}")
            ok = False
            categories = []

        _print_header("4. list_items (limit 5)")
        try:
            result = await client.call_tool("list_items", {"limit": 5})
            _print_result("list_items", result)
        except Exception as e:
            print(f"FAILED: {e}")
            ok = False

        if categories:
            cat_name = categories[0]["name"]
            _print_header(f"5. list_items filtered by category={cat_name!r}")
            try:
                result = await client.call_tool(
                    "list_items", {"category": cat_name, "limit": 5}
                )
                _print_result("list_items (filtered)", result)
            except Exception as e:
                print(f"FAILED: {e}")
                ok = False

        _print_header("6. search_items (query='chicken')")
        try:
            result = await client.call_tool("search_items", {"query": "chicken", "limit": 5})
            _print_result("search_items", result)
            search_items = result.data or []
        except Exception as e:
            print(f"FAILED: {e}")
            ok = False
            search_items = []

        if search_items:
            item_id = search_items[0]["id"]
            _print_header(f"7. get_item (id={item_id})")
            try:
                result = await client.call_tool("get_item", {"item_id": item_id})
                _print_result("get_item", result)
            except Exception as e:
                print(f"FAILED: {e}")
                ok = False

        _print_header("8. get_item with a bogus id (should return null, not error)")
        try:
            result = await client.call_tool("get_item", {"item_id": -1})
            _print_result("get_item(-1)", result)
            if result.data is not None:
                print("WARNING: expected null for nonexistent item id")
        except Exception as e:
            print(f"FAILED: {e}")
            ok = False

        _print_header("9. filter_items_by_price (0-200)")
        try:
            result = await client.call_tool(
                "filter_items_by_price", {"min_price": 0, "max_price": 200, "limit": 5}
            )
            _print_result("filter_items_by_price", result)
        except Exception as e:
            print(f"FAILED: {e}")
            ok = False

        _print_header("10. filter_items_by_calories (max 500)")
        try:
            result = await client.call_tool(
                "filter_items_by_calories", {"max_calories": 500, "limit": 5}
            )
            _print_result("filter_items_by_calories", result)
        except Exception as e:
            print(f"FAILED: {e}")
            ok = False

        _print_header("11. get_high_protein_items (min 10g)")
        try:
            result = await client.call_tool(
                "get_high_protein_items", {"min_protein_g": 10, "limit": 5}
            )
            _print_result("get_high_protein_items", result)
        except Exception as e:
            print(f"FAILED: {e}")
            ok = False

        _print_header("12. list_combos")
        try:
            result = await client.call_tool("list_combos", {"limit": 5})
            _print_result("list_combos", result)
            combos = result.data or []
        except Exception as e:
            print(f"FAILED: {e}")
            ok = False
            combos = []

        if combos:
            combo_id, combo_is_veg = combos[0]["id"], combos[0]["is_veg"]
            _print_header(f"13. get_combo (id={combo_id}, is_veg={combo_is_veg})")
            try:
                result = await client.call_tool(
                    "get_combo", {"combo_id": combo_id, "is_veg": combo_is_veg}
                )
                _print_result("get_combo", result)
            except Exception as e:
                print(f"FAILED: {e}")
                ok = False

    return ok


async def run_single_tool(client: Client, tool_name: str, tool_args: dict) -> bool:
    async with client:
        try:
            result = await client.call_tool(tool_name, tool_args)
        except Exception as e:
            print(f"FAILED: {e}")
            return False
        _print_result(tool_name, result)
        return True


def build_client(in_process: bool, url: str | None) -> Client:
    if url:
        # Connect to an already-running HTTP server, e.g. `uv run server.py`
        # started separately with transport="http".
        return Client(url)
    if in_process:
        # Import the live FastMCP server object and connect directly --
        # no subprocess, no stdio framing. Fast, good for iterating.
        from server import mcp
        return Client(mcp)
    else:
        # Spawn `uv run server.py` as a real subprocess over stdio, the
        # same way Claude Desktop / Claude Code would launch it. Point
        # the client at server.py directly; fastmcp infers the stdio
        # transport and runs it with the right interpreter.
        return Client("server.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test client for the bk-menu MCP server")
    parser.add_argument(
        "--in-process", action="store_true",
        help="Connect directly to the server object instead of spawning a subprocess",
    )
    parser.add_argument(
        "--url", default=None,
        help="Connect to an already-running HTTP server instead of spawning stdio, "
             "e.g. --url http://127.0.0.1:8000/mcp",
    )
    parser.add_argument(
        "--tool", help="Run a single tool instead of the full smoke test"
    )
    parser.add_argument(
        "--args", default="{}", help="JSON args for --tool, e.g. '{\"limit\": 5}'"
    )
    args = parser.parse_args()

    client = build_client(args.in_process, args.url)

    mode = "HTTP" if args.url else ("in-process" if args.in_process else "stdio (subprocess)")
    print(f"Connecting in {mode} mode...")

    if args.tool:
        try:
            tool_args = json.loads(args.args)
        except json.JSONDecodeError as e:
            print(f"Invalid --args JSON: {e}")
            sys.exit(2)
        ok = asyncio.run(run_single_tool(client, args.tool, tool_args))
        sys.exit(0 if ok else 1)

    ok = asyncio.run(run_smoke_test(client))
    _print_header("RESULT")
    if ok:
        print("All checks passed.")
        sys.exit(0)
    else:
        print("Some checks failed -- see FAILED lines above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
