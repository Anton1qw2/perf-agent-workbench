import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..contracts import PerformanceTask
from ..experts import ExpertResponse, ExpertServices
from ..experts.defaults import default_experts
from ..task_agent.agent import TaskAgent
from .agent import OrchestratorAgent
from .capabilities import capabilities_reply
from .routing import retain_pending, route_intake


class IntakeState(TypedDict, total=False):
    task: dict
    messages: list[dict]
    task_messages: list[dict]
    reply: str
    task_reply: str
    route: dict
    consultation: dict
    pending_expert: dict | None
    deadline: float | None


def _response(state):
    return ExpertResponse.model_validate({
        key: value for key, value in state["consultation"].items() if key != "expert_id"
    })


def _render_consultation(response):
    text = response.answer
    if response.sources:
        lines = []
        for source in response.sources:
            title = re.sub(r"([\\`*{}_\[\]()<>#!|])", r"\\\1", source.title.replace("\n", " "))
            lines.append(f"- [{title}](<{source.url}>)" if source.url else f"- {title} (local reference)")
        text += "\n\nSources:\n" + "\n".join(lines)
    if response.limitations:
        text += "\n\nLimitations:\n" + "\n".join("- " + item for item in response.limitations)
    if response.status == "needs_input":
        text += "\n\n" + response.question
    return text


def build_intake_graph(
    task_agent: TaskAgent, orchestrator: OrchestratorAgent,
    docs_agent=None, *, experts=None, max_input_bytes=120000, execution_capabilities=None,
):
    registry = experts if experts is not None else default_experts(docs_agent)
    model = getattr(orchestrator, "model", None)

    def route(state):
        decision = route_intake(registry, model, state["messages"], state.get("pending_expert"),
                                max_bytes=max_input_bytes)
        route = decision.model_dump()
        return {"route": route, "pending_expert": retain_pending(
            state.get("pending_expert"), route, state["messages"],
        )}

    def specify(state: IntakeState):
        messages = state.get("task_messages", state["messages"])
        if messages and messages[-1]["content"].startswith("/task "):
            messages = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"][6:]}]
        task = task_agent.extract(PerformanceTask.model_validate(state["task"]),
                                  messages)
        return {"task": task.model_dump()}

    def consult(state):
        decision = state["route"]
        if decision["expert_id"]:
            response = registry.consult(
                decision["expert_id"], decision["inputs"], services=ExpertServices(model),
                messages=state["messages"], task=state["task"], deadline=state.get("deadline"),
                max_bytes=max_input_bytes,
            )
        else:
            response = ExpertResponse(status="unavailable", answer=decision["limitation"] or
                                     "No matching consultation expert is registered for this question.")
        output = {"consultation": {"expert_id": decision["expert_id"], **response.model_dump()}}
        if response.status == "needs_input":
            output["pending_expert"] = {"expert_id": decision["expert_id"],
                                        "inputs": decision["inputs"], "question": response.question}
        elif response.status != "unavailable":
            output["pending_expert"] = None
        return output

    def respond(state: IntakeState):
        if state["route"]["intent"] == "capabilities":
            return {"reply": capabilities_reply(registry, execution_capabilities)}
        if state["route"]["intent"] == "consultation":
            return {"reply": _render_consultation(_response(state))}
        task = PerformanceTask.model_validate(state["task"])
        task_reply = orchestrator.reply(task, state["messages"])
        reply = task_reply
        if state["route"]["intent"] == "mixed":
            reply = _render_consultation(_response(state)) + "\n\n" + reply
        return {"reply": reply, "task_reply": task_reply}

    graph = StateGraph(IntakeState)
    graph.add_node("route", route)
    graph.add_node("task_agent", specify)
    graph.add_node("orchestrator", respond)
    graph.add_node("consult", consult)
    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", lambda state: state["route"]["intent"], {
        "task": "task_agent", "mixed": "task_agent", "consultation": "consult",
        "capabilities": "orchestrator",
    })
    graph.add_conditional_edges("task_agent", lambda state: state["route"]["intent"], {
        "task": "orchestrator", "mixed": "consult",
    })
    graph.add_edge("consult", "orchestrator")
    graph.add_edge("orchestrator", END)
    # Intake finishes every turn; durable conversation state is owned by Store.
    return graph.compile()
