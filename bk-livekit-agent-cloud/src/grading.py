"""Grade a simulated call on the order the agent actually placed.

The simulator's judge reads the transcript; this checks the final state. A
call that sounds right can still place the wrong order, or a made-up total.

A scenario opts in through its ``userdata`` in scenarios.yaml:

    userdata:
      expected_order: null          # no order may be placed
    userdata:
      expected_order:               # this exact order must be placed
        items:
          - {name: Veg Whopper Deluxe, quantity: 1}
          - {name: Coke, quantity: 1}

Without ``expected_order`` the call is graded on the conversation alone.

Failure reasons are short strings with a stable prefix, so the analyzer can
count them: ``order_missing``, ``unexpected_order``, ``unknown_item``,
``wrong_items``, ``line_price_mismatch``, ``total_mismatch``.
"""

from __future__ import annotations

from typing import Any

from sim_support import MenuFixture

PRICE_TOLERANCE = 0.01


def _key(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def _quantity(value: Any) -> int | None:
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    return int(qty) if qty.is_integer() and qty > 0 else None


def _money(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _fmt(value: float | None) -> str:
    return "none" if value is None else f"{value:.2f}"


def menu_prices(fx: MenuFixture) -> dict[str, tuple[str, float]]:
    """Normalized name -> (menu name, unit price), for items and combos."""
    prices = {_key(i["name"]): (i["name"], i["price"]) for i in fx.items}
    prices.update({_key(c["name"]): (c["name"], c["price"]) for c in fx.combos})
    return prices


def grade_order(
    userdata: dict[str, Any], receipt: dict | None, fx: MenuFixture
) -> list[str]:
    """Return failure reasons; an empty list means the order checks passed."""
    if "expected_order" not in userdata:
        return []
    expected = userdata["expected_order"]

    if expected is None:
        if receipt is None:
            return []
        return [f"unexpected_order: {receipt.get('order_id', '?')}"]
    if receipt is None:
        return ["order_missing"]

    prices = menu_prices(fx)
    failures: list[str] = []

    want: dict[str, int] = {}
    for line in expected.get("items", []):
        key = _key(line["name"])
        if key not in prices:
            raise ValueError(f"expected_order item not on the menu: {line['name']!r}")
        want[key] = want.get(key, 0) + int(line.get("quantity", 1))

    got: dict[str, int] = {}
    for line in receipt.get("items") or []:
        key = _key(line.get("name", ""))
        qty = _quantity(line.get("quantity", 1))
        if key not in prices:
            failures.append(f"unknown_item: {line.get('name')!r}")
            continue
        if qty is None:
            failures.append(f"wrong_items: bad quantity {line.get('quantity')!r}")
            continue
        got[key] = got.get(key, 0) + qty

        # place_order asks for the line total (unit price x quantity).
        said = _money(line.get("price"))
        unit_name, unit = prices[key]
        if said is None or abs(said - unit * qty) > PRICE_TOLERANCE:
            failures.append(
                f"line_price_mismatch: {unit_name} x{qty} said {_fmt(said)}, "
                f"expected {unit * qty:.2f}"
            )

    if got != want:
        missing = [f"{prices[k][0]} x{q}" for k, q in want.items() if k not in got]
        extra = [f"{prices[k][0]} x{q}" for k, q in got.items() if k not in want]
        qty_off = [
            f"{prices[k][0]} {got[k]}!={q}"
            for k, q in want.items()
            if k in got and got[k] != q
        ]
        parts = [
            f"{label} {', '.join(rows)}"
            for label, rows in (
                ("missing", missing),
                ("extra", extra),
                ("quantity", qty_off),
            )
            if rows
        ]
        failures.append("wrong_items: " + "; ".join(parts))

    expected_total = round(sum(prices[k][1] * q for k, q in want.items()), 2)
    said_total = _money(receipt.get("total"))
    if said_total is None or abs(said_total - expected_total) > PRICE_TOLERANCE:
        failures.append(
            f"total_mismatch: said {_fmt(said_total)}, expected {expected_total:.2f}"
        )
    return failures
