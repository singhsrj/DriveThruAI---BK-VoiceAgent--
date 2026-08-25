"""
The slots we try to fill BEFORE touching the database.
Each filled slot narrows the SQL WHERE clause, so the LLM only ever
sees a handful of candidate rows instead of the full menu.
"""

from typing import Optional, Literal
from pydantic import BaseModel


class OrderConstraints(BaseModel):
    num_people: Optional[int] = None
    veg_pref: Optional[Literal["veg", "nonveg", "both"]] = None
    budget_total: Optional[float] = None          # total budget in rupees
    category_interest: Optional[str] = None        # e.g. "burger", "combo", "dessert"

    def missing_slots(self) -> list[str]:
        return [f for f in ("num_people", "veg_pref", "budget_total")
                if getattr(self, f) is None]

    def is_ready(self) -> bool:
        """We don't need ALL slots filled -- just enough to make the
        candidate set small. veg_pref + budget alone is usually enough."""
        return self.veg_pref is not None and self.budget_total is not None
