"""T0 tests for the fixture-backed menu tools used under simulation."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from livekit.agents.llm import ToolError

import sim_support as sim
from agent import BKAgent


@pytest.fixture(scope="module")
def fx() -> sim.MenuFixture:
    return sim.load_fixture()


def _names(rows: list[dict]) -> list[str]:
    return [r["name"] for r in rows]


def _first_item(fx: sim.MenuFixture, **match) -> dict:
    return next(i for i in fx.items if all(i[k] == v for k, v in match.items()))


# --- contract: same tools, same schemas as the live server --------------------


def test_one_fake_per_mcp_tool(fx: sim.MenuFixture) -> None:
    assert {t["name"] for t in fx.tools} == set(sim.QUERIES)


def test_tools_carry_server_schemas(fx: sim.MenuFixture) -> None:
    tools = {t.info.name: t.info.raw_schema for t in sim.build_menu_tools(fx)}
    for spec in fx.tools:
        assert tools[spec["name"]]["parameters"] == spec["input_schema"]
        assert tools[spec["name"]]["description"] == spec["description"]


def test_fake_arguments_match_schemas(fx: sim.MenuFixture) -> None:
    import inspect

    for spec in fx.tools:
        params = set(inspect.signature(sim.QUERIES[spec["name"]]).parameters) - {"fx"}
        assert params == set(spec["input_schema"].get("properties", {})), spec["name"]


# --- query behavior -----------------------------------------------------------


def test_list_items_filters_by_category_and_veg(fx: sim.MenuFixture) -> None:
    rows = sim.list_items(fx, category="beverages", is_veg=True)
    bev_id = next(c["id"] for c in fx.categories if c["name"] == "Beverages")
    assert rows and all(r["category_id"] == bev_id and r["is_veg"] for r in rows)
    assert _names(rows) == sorted(_names(rows))
    assert all(r["sizes"] is None for r in rows)


def test_list_items_limit_and_offset(fx: sim.MenuFixture) -> None:
    everything = sim.list_items(fx, limit=200)
    assert sim.list_items(fx, limit=5, offset=5) == everything[5:10]
    assert len(sim.list_items(fx, limit=0)) == 1  # clamped like the server


def test_search_is_case_insensitive_substring(fx: sim.MenuFixture) -> None:
    assert _names(sim.search_items(fx, "PIZZA")) == ["BK Veg Pizza Puff"]
    assert sim.search_items(fx, "sushi") == []


def test_unknown_ids_return_none(fx: sim.MenuFixture) -> None:
    assert sim.get_item(fx, 9999) is None
    assert sim.get_combo(fx, 9999, is_veg=True) is None


def test_get_combo_includes_items(fx: sim.MenuFixture) -> None:
    combo = sim.get_combo(fx, 1, is_veg=False)
    assert combo["items"] and all(i["quantity"] >= 1 for i in combo["items"])
    assert all(c["items"] is None for c in sim.list_combos(fx))


def test_price_and_calorie_filters(fx: sim.MenuFixture) -> None:
    cheap = sim.filter_items_by_price(fx, max_price=100)
    assert cheap and all(r["price"] <= 100 for r in cheap)
    assert [r["price"] for r in cheap] == sorted(r["price"] for r in cheap)
    light = sim.filter_items_by_calories(fx, max_calories=200)
    assert light and all(r["calories"] <= 200 for r in light)


def test_high_protein_sorted_desc(fx: sim.MenuFixture) -> None:
    rows = sim.get_high_protein_items(fx, min_protein_g=10)
    proteins = [r["protein_g"] for r in rows]
    assert proteins and proteins == sorted(proteins, reverse=True)
    assert min(proteins) >= 10


def test_menu_stats(fx: sim.MenuFixture) -> None:
    stats = sim.get_menu_stats(fx)
    assert stats["total_items"] == len(fx.items)
    assert sum(stats["items_per_category"].values()) == len(fx.items)


# --- scenario controls --------------------------------------------------------


def test_unavailable_items_are_hidden_and_flagged() -> None:
    fx = sim.load_fixture(unavailable_items=["BK Veg Pizza Puff"])
    item = _first_item(fx, name="BK Veg Pizza Puff")
    assert "BK Veg Pizza Puff" not in _names(sim.list_items(fx, limit=200))
    assert sim.search_items(fx, "pizza")[0]["is_available"] is False
    assert sim.get_item(fx, item["id"])["is_available"] is False


def test_unknown_unavailable_item_is_rejected() -> None:
    with pytest.raises(ValueError, match="Pepperoni"):
        sim.load_fixture(unavailable_items=["Pepperoni Pizza"])


# --- what the LLM sees --------------------------------------------------------


def test_result_formatted_like_livekit_mcp(fx: sim.MenuFixture) -> None:
    out = sim.call(fx, "list_categories", {})
    block = json.loads(out)
    assert set(block) == {"type", "text", "annotations", "meta"}
    assert block["type"] == "text"
    assert json.loads(block["text"]) == sim.list_categories(fx)
    assert ", " not in block["text"]  # compact JSON, as FastMCP sends it


@pytest.mark.parametrize(
    ("tool", "args"),
    [("get_item", {"item_id": 9999}), ("search_items", {"query": "sushi"})],
)
def test_empty_result_raises_like_livekit(
    fx: sim.MenuFixture, tool: str, args: dict
) -> None:
    # Mirrors the live server: None and [] arrive with no content blocks.
    with pytest.raises(ToolError, match="completed without producing a result"):
        sim.call(fx, tool, args)


async def test_built_tool_is_callable(fx: sim.MenuFixture) -> None:
    tool = next(t for t in sim.build_menu_tools(fx) if t.info.name == "search_items")
    out = await tool({"query": "whopper", "limit": 3})
    assert len(json.loads(json.loads(out)["text"])) == 3


def test_agent_uses_menu_tools_instead_of_mcp(fx: sim.MenuFixture) -> None:
    tools = sim.build_menu_tools(fx)
    agent = BKAgent(room=object(), menu_tools=tools)  # type: ignore[arg-type]
    names = {t.info.name for t in agent.tools if hasattr(t, "info")}
    assert {"search_items", "get_combo", "place_order"} <= names


def test_agent_requires_a_menu_source() -> None:
    with pytest.raises(ValueError):
        BKAgent(room=object())  # type: ignore[arg-type]
