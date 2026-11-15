"""T0 tests for final-state grading of simulated calls."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sim_support as sim
from grading import grade_order

EXPECTED = {
    "expected_order": {
        "items": [
            {"name": "Veg Whopper Deluxe", "quantity": 1},
            {"name": "Coke", "quantity": 2},
        ]
    }
}


@pytest.fixture(scope="module")
def fx() -> sim.MenuFixture:
    return sim.load_fixture()


def _receipt(items: list[tuple[str, object, object]], total: object) -> dict:
    return {
        "order_id": "AB12CD34",
        "items": [{"name": n, "quantity": q, "price": p} for n, q, p in items],
        "total": total,
    }


GOOD = _receipt([("Veg Whopper Deluxe", 1, 149.0), ("Coke", 2, 158.0)], 307.0)


def test_no_expectation_means_no_check(fx: sim.MenuFixture) -> None:
    assert grade_order({}, None, fx) == []
    assert grade_order({"menu": "live"}, GOOD, fx) == []


def test_correct_order_passes(fx: sim.MenuFixture) -> None:
    assert grade_order(EXPECTED, GOOD, fx) == []


def test_names_are_matched_loosely_and_lines_merged(fx: sim.MenuFixture) -> None:
    receipt = _receipt(
        [("veg  whopper DELUXE", 1, 149), ("coke", 1, 79), ("Coke", "1", "79.0")],
        "307",
    )
    assert grade_order(EXPECTED, receipt, fx) == []


def test_order_missing(fx: sim.MenuFixture) -> None:
    assert grade_order(EXPECTED, None, fx) == ["order_missing"]


def test_unexpected_order(fx: sim.MenuFixture) -> None:
    assert grade_order({"expected_order": None}, GOOD, fx) == [
        "unexpected_order: AB12CD34"
    ]
    assert grade_order({"expected_order": None}, None, fx) == []


def test_wrong_items_reports_missing_extra_and_quantity(fx: sim.MenuFixture) -> None:
    receipt = _receipt([("Coke", 1, 79.0), ("Masala Chai", 1, 109.0)], 188.0)
    reasons = grade_order(EXPECTED, receipt, fx)
    assert (
        "wrong_items: missing Veg Whopper Deluxe x1; extra Masala Chai x1; "
        "quantity Coke 1!=2"
    ) in reasons
    assert "total_mismatch: said 188.00, expected 307.00" in reasons


def test_invented_item(fx: sim.MenuFixture) -> None:
    receipt = _receipt(
        [("Veg Whopper Deluxe", 1, 149.0), ("Coke", 2, 158.0), ("Pizza", 1, 299)],
        307.0,
    )
    assert "unknown_item: 'Pizza'" in grade_order(EXPECTED, receipt, fx)


def test_unit_price_instead_of_line_total(fx: sim.MenuFixture) -> None:
    receipt = _receipt([("Veg Whopper Deluxe", 1, 149.0), ("Coke", 2, 79.0)], 307.0)
    assert grade_order(EXPECTED, receipt, fx) == [
        "line_price_mismatch: Coke x2 said 79.00, expected 158.00"
    ]


def test_llm_arithmetic_error_in_total(fx: sim.MenuFixture) -> None:
    receipt = _receipt([("Veg Whopper Deluxe", 1, 149.0), ("Coke", 2, 158.0)], 297.0)
    assert grade_order(EXPECTED, receipt, fx) == [
        "total_mismatch: said 297.00, expected 307.00"
    ]


def test_discounted_total_fails(fx: sim.MenuFixture) -> None:
    # The coupon guardrail: a 50% "discount" must never reach the receipt.
    receipt = _receipt([("Veg Whopper Deluxe", 1, 149.0), ("Coke", 2, 158.0)], 153.5)
    assert grade_order(EXPECTED, receipt, fx)[0].startswith("total_mismatch")


def test_combo_scale_bug_is_caught(fx: sim.MenuFixture) -> None:
    # Production run SR_AMDAose6mksx quoted the ₹1,233 combo as "12.33".
    expected = {"expected_order": {"items": [{"name": "Classic Combo for 4 (Veg)"}]}}
    receipt = _receipt([("Classic Combo for 4 (Veg)", 1, 12.33)], 12.33)
    assert grade_order(expected, receipt, fx) == [
        "line_price_mismatch: Classic Combo for 4 (Veg) x1 said 12.33, "
        "expected 1233.00",
        "total_mismatch: said 12.33, expected 1233.00",
    ]


def test_bad_values_do_not_crash(fx: sim.MenuFixture) -> None:
    receipt = _receipt([("Coke", "two", None)], None)
    reasons = grade_order(EXPECTED, receipt, fx)
    assert any(r.startswith("wrong_items: bad quantity") for r in reasons)
    assert "total_mismatch: said none, expected 307.00" in reasons


def test_typo_in_scenario_is_an_error(fx: sim.MenuFixture) -> None:
    with pytest.raises(ValueError, match="Veg Wopper"):
        grade_order(
            {"expected_order": {"items": [{"name": "Veg Wopper", "quantity": 1}]}},
            GOOD,
            fx,
        )


# --- the on_simulation_end callback -------------------------------------------


class _FakeSim:
    def __init__(self, job_id: str, userdata: dict) -> None:
        self.simulation_job_id = job_id
        self._userdata = userdata
        self.failed_with: str | None = None

    def userdata(self) -> dict:
        return self._userdata

    def fail(self, reason: str = "") -> None:
        self.failed_with = reason


class _AgentWithReceipt:
    def __init__(self, receipt: dict | None) -> None:
        self.receipt = receipt


@pytest.mark.parametrize(
    ("receipt", "failed_with"),
    [
        (GOOD, None),
        (None, "order_missing"),
        (
            _receipt([("Veg Whopper Deluxe", 1, 149.0), ("Coke", 2, 158.0)], 297.0),
            "total_mismatch: said 297.00, expected 307.00",
        ),
    ],
)
def test_on_simulation_end_vetoes_bad_orders(
    monkeypatch: pytest.MonkeyPatch, receipt: dict | None, failed_with: str | None
) -> None:
    import agent as agent_module

    monkeypatch.setitem(agent_module._sim_agents, "SRJ_x", _AgentWithReceipt(receipt))
    fake = _FakeSim("SRJ_x", EXPECTED)
    agent_module.on_simulation_end(fake)  # type: ignore[arg-type]
    assert fake.failed_with == failed_with
    assert "SRJ_x" not in agent_module._sim_agents


def test_on_simulation_end_without_agent_counts_as_no_order() -> None:
    import agent as agent_module

    fake = _FakeSim("SRJ_unknown", {"expected_order": None})
    agent_module.on_simulation_end(fake)  # type: ignore[arg-type]
    assert fake.failed_with is None


def test_scenario_expectations_name_real_menu_items(fx: sim.MenuFixture) -> None:
    import yaml

    path = Path(__file__).resolve().parents[1] / "scenarios.yaml"
    scenarios = yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]
    for scenario in scenarios:
        userdata = scenario.get("userdata", {})
        grade_order(userdata, None, fx)  # raises on an unknown item name
        sim.load_fixture(unavailable_items=userdata.get("unavailable_items"))
