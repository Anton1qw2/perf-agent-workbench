from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..contracts import PerformanceTask
from ..profiler_docs_agent import ProfilerDocsAgent
from ..profiler_docs_agent.trigger import matches_documentation_trigger
from ..task_agent.agent import TaskAgent
from .agent import OrchestratorAgent


class IntakeState(TypedDict, total=False):
    task: dict
    messages: list[dict]
    missing_fields: list[str]
    reply: str
    documentation: dict


def build_intake_graph(
    task_agent: TaskAgent, orchestrator: OrchestratorAgent,
    docs_agent: ProfilerDocsAgent | None = None,
):
    docs_agent = docs_agent or ProfilerDocsAgent()

    def route_documentation(state: IntakeState):
        question = next(
            (m["content"] for m in reversed(state["messages"]) if m["role"] == "user"), "",
        )
        return "perf_docs" if matches_documentation_trigger(question) else "orchestrator"

    def consult_documentation(state: IntakeState):
        question = next(m["content"] for m in reversed(state["messages"]) if m["role"] == "user")
        return {"documentation": docs_agent.ask(question).model_dump()}

    def specify(state: IntakeState):
        task = task_agent.extract(PerformanceTask.model_validate(state["task"]), state["messages"])
        return {"task": task.model_dump()}

    def validate(state: IntakeState):
        task = PerformanceTask.model_validate(state["task"])
        return {"missing_fields": task.missing_fields()}

    def respond(state: IntakeState):
        task = PerformanceTask.model_validate(state["task"])
        return {"reply": orchestrator.reply(task, state["messages"], state.get("documentation"))}

    graph = StateGraph(IntakeState)
    graph.add_node("task_agent", specify)
    graph.add_node("validate", validate)
    graph.add_node("orchestrator", respond)
    graph.add_node("perf_docs", consult_documentation)
    graph.add_edge(START, "task_agent")
    graph.add_edge("task_agent", "validate")
    graph.add_conditional_edges("validate", route_documentation, {
        "perf_docs": "perf_docs", "orchestrator": "orchestrator",
    })
    graph.add_edge("perf_docs", "orchestrator")
    graph.add_edge("orchestrator", END)
    # Intake finishes every turn; durable conversation state is owned by Store.
    return graph.compile()
