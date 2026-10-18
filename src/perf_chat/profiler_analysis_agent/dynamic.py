"""Built-in dynamic MCP investigation with one durable LangGraph node per step."""

import hashlib
import json
from importlib.resources import files
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import Field

from ..contracts import StrictModel
from ..runtime import check_deadline, execution_deadline
from .agent import ProfileAnalysisAgent
from .context_builder import ContextBudgetExceeded, RequiredSourceOmitted
from .contracts import AnalysisStep, Evidence, MCPTool, ProfileAnalysis
from .graph import evidence
from .source_context import collect_source, prepare_source


class ToolDecision(StrictModel):
    action: Literal["call", "finish", "ask"]
    reason: str = Field(min_length=1, max_length=2000)
    tool: str | None = None
    arguments: dict = Field(default_factory=dict)
    question: str = Field(default="", max_length=4000)


class StepState(TypedDict):
    request: object
    checkpoint: dict
    source_snapshot: object
    step: dict


class DynamicProfilerAnalyzer(ProfileAnalysisAgent):
    """Replace with any ProfileAnalyzer through Worker(analyzer_factory=...)."""

    analyzer_id = "builtin-dynamic-mcp-v1"

    def __init__(self, *args, max_calls=8, **kwargs):
        super().__init__(*args, **kwargs)
        if not 1 <= max_calls <= 30:
            raise ValueError("max_calls must be between 1 and 30")
        self.max_calls = max_calls
        self.planner_system = files(__package__).joinpath(
            "instructions/dynamic_analysis.md",
        ).read_text(encoding="utf-8")
        graph = StateGraph(StepState)
        for phase in ("discover", "plan", "call", "report"):
            def node(state, method=getattr(self, "_" + phase)):
                return {"step": method(
                    state["request"], state["checkpoint"], state["source_snapshot"],
                ).model_dump()}
            graph.add_node(phase, node)
            graph.add_edge(phase, END)
        graph.add_conditional_edges(START, lambda state: state["checkpoint"].get("phase", "discover"),
                                    {name: name for name in ("discover", "plan", "call", "report")})
        self.workflow = graph.compile()

    def _output(self, request, state, *, error=None, question=None, field="analysis_input",
                interpretation=None, documentation=None, source_context=None, delivered=None):
        notes = list(state.get("limitations", []))
        notes.extend([
            "No derived metric calculations or code changes were performed.",
            "Tool descriptions and schemas do not establish undocumented metric semantics.",
            "Profile argument binding is enforced only for backend-configured tool bindings.",
        ])
        if self.gateway.demo:
            notes.append("Synthetic demo data; no real profile was read.")
        if error:
            notes.append(error)
        return AnalysisStep(checkpoint=state, report=ProfileAnalysis(
            status="needs_input" if question else "insufficient_data" if error else "completed",
            request=request, demo=self.gateway.demo, analyzer="dynamic-mcp-v1",
            evidence=state.get("evidence", []), tool_catalog=state.get("catalog", []),
            limitations=notes, open_questions=[question] if question else [],
            clarification_field=field, documentation=documentation, source_context=source_context,
            delivered_evidence_ids=sorted(delivered or []),
            **(interpretation.model_dump() if interpretation else {}),
        ))

    def _discover(self, request, state, snapshot):
        tools = self.gateway.list_tools()
        state.update(catalog=[tool.model_dump() for tool in tools], evidence=[], phase="plan",
                     calls=0, rounds=0, answers=[], decisions=[], limitations=[])
        return AnalysisStep(checkpoint=state, message="Profiler tool catalog discovered and pinned.")

    @staticmethod
    def _has_usable_evidence(state):
        for item in state.get("evidence", []):
            value = item["response"]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, (dict, list)) and not value:
                continue
            return True
        return False

    @staticmethod
    def _clarification_context(state):
        questions = [d["question"] for d in state.get("decisions", []) if d["action"] == "ask"]
        return [
            {"question": questions[index] if index < len(questions) else "Question context unavailable",
             "answer": answer}
            for index, answer in enumerate(state.get("answers", []))
        ]

    def _plan(self, request, state, snapshot):
        if self.model is None:
            return self._output(request, state, error="Dynamic tool selection requires LLM mode.")
        tools = [item for item in state["catalog"] if item["name"] in self.gateway.allowed_tools]
        if not tools:
            return self._output(request, state, error=(
                "No discovered profiler tools are allowed. Configure PROFILER_MCP_ALLOWED_TOOLS."
            ))
        if state["calls"] >= self.max_calls or state["rounds"] >= self.max_calls * 2:
            if not self._has_usable_evidence(state):
                return self._output(request, state, error="Dynamic investigation budget exhausted.")
            state["limitations"].append("Investigation budget reached; report uses collected evidence only.")
            state["phase"] = "report"
            return AnalysisStep(checkpoint=state, message="Investigation budget reached; preparing report.")
        payload = {
            "schema": ToolDecision.model_json_schema(), "request": request.model_dump(),
            "tools": tools, "evidence": state["evidence"],
            "clarifications": self._clarification_context(state),
            "remaining_calls": self.max_calls - state["calls"],
            "limitations": state["limitations"],
        }
        context = self.context_builder.build(self.model, self.planner_system, payload, None)
        check_deadline()
        decision = ToolDecision.model_validate_json(
            self.model.complete(self.planner_system, context.payload, json_output=True),
        )
        check_deadline()
        state["rounds"] += 1
        state.setdefault("decisions", []).append(decision.model_dump())
        if decision.action == "ask":
            if not decision.question.strip():
                raise ValueError("An analysis question is required")
            return self._output(request, state, question=decision.question)
        if decision.action == "finish":
            if not self._has_usable_evidence(state):
                return self._output(request, state, error="No usable profiler evidence was collected.")
            state["phase"] = "report"
            return AnalysisStep(checkpoint=state, message="Profiler evidence ready for interpretation.")
        if not decision.tool or decision.tool not in {tool["name"] for tool in tools}:
            raise ValueError("The planner selected an unavailable or disallowed tool")
        if decision.tool == "get_asm" and not request.include_asm:
            raise ValueError("Assembly was not requested")
        if any(e["operation"] == decision.tool and e["arguments"] == decision.arguments
               for e in state["evidence"]):
            raise ValueError("Repeated identical profiler query")
        state.update(phase="call", decision=decision.model_dump())
        return AnalysisStep(checkpoint=state, message="Profiler query planned and checkpointed.")

    def _call(self, request, state, snapshot):
        decision = ToolDecision.model_validate(state["decision"])
        tool = MCPTool.model_validate(next(t for t in state["catalog"] if t["name"] == decision.tool))
        # The gateway enforces schemas, the allowlist, profile binding and schema drift.
        check_deadline()
        data = self.gateway.call_tool(tool, decision.arguments, result_id=request.result_id)
        check_deadline()
        state["evidence"].append(evidence(tool.name, decision.arguments, data, len(state["evidence"]) + 1))
        state["calls"] += 1
        state["phase"] = "plan"
        return AnalysisStep(checkpoint=state, message="Profiler response saved as evidence.")

    def _report(self, request, state, snapshot):
        context = collect_source(self.source_gateway, request.source, snapshot,
                                 model_enabled=self.model is not None)
        if context.needs_selection:
            return self._output(request, state, question="Select an explicit source path and line range.",
                                field="source", source_context=context)
        docs = self.docs_agent.ask("Perf " + request.goal).model_dump()
        interpretation, delivered, notes = self.interpret(
            request, [Evidence.model_validate(item) for item in state["evidence"]], docs, context,
            analysis_context={
                "clarifications": self._clarification_context(state),
            },
        )
        check_deadline()
        if context.required_delivery == "pending":
            context.required_delivery = "delivered"
        state["limitations"].extend(context.limitations + notes)
        if docs.get("status") != "found":
            state["limitations"].append(docs["answer"])
        return self._output(request, state, interpretation=interpretation, documentation=docs,
                            source_context=context, delivered=delivered)

    def step_analysis(self, request, *, checkpoint, answer=None, deadline=None, source_snapshot=None):
        if source_snapshot is not None and (
            request.source is None
            or source_snapshot.repository != request.source.repository
            or source_snapshot.requested_ref != request.source.ref
            or (request.source.policy != "off"
                and source_snapshot.requested_commit != request.source.commit)
        ):
            raise ValueError("Source snapshot does not match the analysis request")
        state = json.loads(json.dumps(checkpoint, allow_nan=False))
        connection = self.gateway.configuration_fingerprint
        if state.get("connection_fingerprint", connection) != connection:
            return self._output(request, state, error=(
                "MCP endpoint or tool policy changed; start a new run."
            ))
        state["connection_fingerprint"] = connection
        identity = hashlib.sha256(json.dumps({
            "result_id": request.result_id, "goal": request.goal,
            "metrics": request.metrics, "include_asm": request.include_asm,
        }, sort_keys=True).encode()).hexdigest()
        if state.get("identity", identity) != identity:
            return self._output(request, state, error="Analysis request changed; start a new run.")
        state["identity"] = identity
        if answer is not None:
            answers = state.setdefault("answers", [])
            if len(answers) >= 16:
                return self._output(request, state, error="Analysis clarification limit reached.")
            answers.append(answer)
        try:
            with execution_deadline(deadline):
                result = self.workflow.invoke({
                    "request": request, "checkpoint": state, "source_snapshot": source_snapshot,
                })
                check_deadline()
                return AnalysisStep.model_validate(result["step"])
        except RequiredSourceOmitted:
            return self._output(request, state, question="Select a smaller explicit source range.",
                                field="source")
        except ContextBudgetExceeded as exc:
            return self._output(request, state, error=str(exc))
        except Exception:
            return self._output(request, state, error=(
                "Dynamic analysis step failed policy, schema, deadline or model validation; "
                "previous evidence is retained. Check the server configuration and start a new run."
            ))

    def analyze(self, request, *, deadline=None, source_snapshot=None):
        with execution_deadline(deadline):
            snapshot = source_snapshot or prepare_source(self.source_gateway, request.source)
            checkpoint = {}
            for _ in range(self.max_calls * 3 + 5):
                step = self.step_analysis(request, checkpoint=checkpoint, deadline=deadline,
                                          source_snapshot=snapshot)
                if step.report is not None:
                    return step.report
                checkpoint = step.checkpoint
            return self._output(request, checkpoint, error="Analysis step limit reached.").report
