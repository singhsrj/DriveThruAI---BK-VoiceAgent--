"""
Async LangGraph agent for the BK voice ordering flow, wired to Neon
Postgres via async SQLAlchemy.

Flow (same as the SQLite version):
  START -> route -> ask_question (interrupt) -> route -> query_menu -> present_options -> END

The LLM only ever sees the small candidate set returned by
query_builder, sized by however many constraint slots got filled --
not the full menu.
"""

from typing import TypedDict, Optional, Literal

from langgraph.graph import StateGraph, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver

from database import AsyncSessionLocal
from constraints import OrderConstraints
from info_gain import next_best_question, QUESTION_TEXT
from query_builder import build_candidate_items, build_candidate_combos


class AgentState(TypedDict):
    constraints: dict
    pending_slot: Optional[str]
    candidate_items: list
    candidate_combos: list
    turns: int


def route(state: AgentState) -> Literal["ask_question", "query_menu"]:
    constraints = OrderConstraints(**state["constraints"])
    if state["turns"] >= 2 or constraints.is_ready():
        return "query_menu"
    return "ask_question"


async def ask_question(state: AgentState) -> Command:
    async with AsyncSessionLocal() as session:
        constraints = OrderConstraints(**state["constraints"])
        slot = await next_best_question(session, constraints)

    if slot is None:
        return Command(goto="query_menu")

    question_text = QUESTION_TEXT[slot]
    answer = interrupt({"question": question_text, "slot": slot})

    updated = dict(state["constraints"])
    updated[slot] = answer
    return Command(
        update={
            "constraints": updated,
            "pending_slot": None,
            "turns": state["turns"] + 1,
        },
        goto="route_node",
    )


async def query_menu(state: AgentState) -> dict:
    async with AsyncSessionLocal() as session:
        constraints = OrderConstraints(**state["constraints"])
        items = await build_candidate_items(session, constraints)
        combos = await build_candidate_combos(session, constraints)
    return {"candidate_items": items, "candidate_combos": combos}


async def present_options(state: AgentState) -> dict:
    # In the real system this node calls the LLM with ONLY
    # state["candidate_items"]/candidate_combos in context, and asks it
    # to phrase a short spoken response. Left as a stub here.
    return state


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("route_node", lambda s: s)
    graph.add_node("ask_question", ask_question)
    graph.add_node("query_menu", query_menu)
    graph.add_node("present_options", present_options)

    graph.set_entry_point("route_node")
    graph.add_conditional_edges("route_node", route, {
        "ask_question": "ask_question",
        "query_menu": "query_menu",
    })
    graph.add_edge("query_menu", "present_options")
    graph.add_edge("present_options", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


async def demo():
    import uuid

    app = build_graph()
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    state = {"constraints": {}, "pending_slot": None,
              "candidate_items": [], "candidate_combos": [], "turns": 0}

    result = await app.ainvoke(state, config=config)

    canned_answers = {"veg_pref": "nonveg", "budget_total": 400, "num_people": 2}

    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        slot = payload["slot"]
        print(f"AGENT ASKS: {payload['question']}")
        answer = canned_answers[slot]
        print(f"CUSTOMER SAYS: {answer}")
        result = await app.ainvoke(Command(resume=answer), config=config)

    print("\nFinal candidate items sent to LLM context:")
    for item in result["candidate_items"]:
        print(" ", item)
    print("Final candidate combos sent to LLM context:")
    for combo in result["candidate_combos"]:
        print(" ", combo)


if __name__ == "__main__":
    import asyncio
    asyncio.run(demo())
