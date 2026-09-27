from langgraph.graph import END, START, StateGraph

from ops_agent.agent.nodes import AgentNodes
from ops_agent.agent.state import AgentState


def after_decide(state: AgentState) -> str:
    return "report" if state["stop_reason"] else "query"


def after_query(state: AgentState) -> str:
    return "report" if state["stop_reason"] else "decide"


def build_graph(checkpointer, trace=None, *, tool_executor=None):
    nodes = AgentNodes(trace, tool_executor=tool_executor)
    builder = StateGraph(AgentState)
    builder.add_node("decide", nodes.decide)
    builder.add_node("query", nodes.query)
    builder.add_node("report", nodes.report)
    builder.add_edge(START, "decide")
    builder.add_conditional_edges(
        "decide", after_decide, {"query": "query", "report": "report"}
    )
    builder.add_conditional_edges(
        "query", after_query, {"decide": "decide", "report": "report"}
    )
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)
