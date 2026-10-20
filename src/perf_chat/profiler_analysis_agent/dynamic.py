"""Built-in dynamic MCP investigation with one durable LangGraph node per step."""

import hashlib
import json
from importlib.resources import files
from typing import Literal, TypedDict

from jsonschema.exceptions import ValidationError as SchemaValidationError
from langgraph.graph import END, START, StateGraph
from pydantic import Field

from ..contracts import StrictModel
from ..integrations.mcp_schema import validate_schema
from ..runtime import check_deadline, execution_deadline
from .context_builder import ContextBudgetExceeded, RequiredSourceOmitted
from .contracts import AnalysisStep, Evidence, MCPTool, ProfileAnalysis
from .evidence import evidence
from .interpretation import EvidenceInterpreter
from .reporting import format_chat_report
from .source_context import collect_source, prepare_source
from .source_contracts import SourceContext, SourceLocation, SourceRequest


class ToolDecision(StrictModel):
    action: Literal["call", "source", "finish", "ask"]
    server: Literal["profiler", "git"] = "profiler"
    reason: str = Field(min_length=1, max_length=2000)
    tool: str | None = None
    arguments: dict = Field(default_factory=dict)
    question: str = Field(default="", max_length=4000)
    locations: list[SourceLocation] = Field(default_factory=list, max_length=5)
    symbols: list[str] = Field(default_factory=list, max_length=3)


class StepState(TypedDict):
    request: object
    checkpoint: dict
    source_snapshot: object
    step: dict


class DynamicProfilerAnalyzer(EvidenceInterpreter):
    """Replace with any ProfileAnalyzer through Worker(analyzer_factory=...)."""

    analyzer_id = "builtin-dynamic-mcp-v3"

    def __init__(self, *args, max_calls=8, **kwargs):
        super().__init__(*args, **kwargs)
        if not 1 <= max_calls <= 30:
            raise ValueError("max_calls must be between 1 and 30")
        self.max_calls = max_calls
        self.planner_system = files(__package__).joinpath(
            "instructions/dynamic_analysis.md",
        ).read_text(encoding="utf-8")
        graph = StateGraph(StepState)
        for phase in ("discover", "plan", "call", "source", "report", "format_report"):
            def node(state, method=getattr(self, "_" + phase)):
                return {"step": method(
                    state["request"], state["checkpoint"], state["source_snapshot"],
                ).model_dump()}
            graph.add_node(phase, node)
            graph.add_edge(phase, END)
        graph.add_conditional_edges(START, lambda state: state["checkpoint"].get("phase", "discover"),
                                    {name: name for name in ("discover", "plan", "call", "source", "report", "format_report")})
        self.workflow = graph.compile()

    def _output(self, request, state, *, error=None, question=None, field="analysis_input",
                interpretation=None, documentation=None, source_context=None, delivered=None):
        source_context = source_context or self._saved_source_context(request, state)
        if question and field == "analysis_input":
            if "analysis_questions" not in state:
                questions = [d["question"] for d in state.get("decisions", []) if d["action"] == "ask"]
                if state.get("decisions") and state["decisions"][-1]["action"] == "ask" and questions[-1:] == [question]:
                    questions.pop()
                state["analysis_questions"] = questions
            state["analysis_questions"].append(question)
        notes = list(state.get("limitations", []))
        notes.extend([
            "No derived metric calculations or code changes were performed.",
            "Tool descriptions and schemas do not establish undocumented metric semantics.",
            "Tool arguments are selected from MCP schemas and task context; schema validation "
            "alone does not verify profile identity.",
        ])
        if self.gateway.demo:
            notes.append("Synthetic demo data; no real profile was read.")
        if error:
            notes.append(error)
        report = ProfileAnalysis(
            status="needs_input" if question else "insufficient_data" if error else "completed",
            request=request, demo=self.gateway.demo, analyzer="dynamic-mcp-v3",
            evidence=state.get("evidence", []), tool_catalog=state.get("catalog", []),
            git_tool_catalog=state.get("git_catalog", []),
            limitations=notes, open_questions=[question] if question else [],
            clarification_field=field, documentation=documentation, source_context=source_context,
            delivered_evidence_ids=sorted(delivered or []),
            **(interpretation.model_dump() if interpretation else {}),
        )
        if error:
            report.chat_report = format_chat_report(report)
        return AnalysisStep(checkpoint=state, report=report)

    def _discover(self, request, state, snapshot):
        tools = (self.gateway.list_tools()
                 if request.result_id and getattr(self.gateway, "url", True) else [])
        git_tools = self.git_gateway.list_tools() if self._git_enabled(request) else []
        state.update(catalog=[tool.model_dump() for tool in tools], evidence=[], phase="plan",
                     git_catalog=[tool.model_dump() for tool in git_tools],
                     calls=0, rounds=0, answers=[], decisions=[], limitations=[], analysis_questions=[])
        if git_tools:
            state["limitations"].append(
                "Git MCP responses are untrusted repository evidence. Generic schemas do not "
                "verify repository scope, immutable commit/blob identity or profile-build linkage. "
                "Only the local Git adapter provides host-verified source snapshots."
            )
        return AnalysisStep(checkpoint=state, message="MCP tool catalogs discovered and pinned.")

    def _git_enabled(self, request):
        return bool(self.git_gateway and request.repository
                    and (request.source is None or request.source.policy != "off"))

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
        questions = state.get("analysis_questions")
        if questions is None:
            questions = [d["question"] for d in state.get("decisions", []) if d["action"] == "ask"]
        return [
            {"question": questions[index] if index < len(questions) else "Question context unavailable",
             "answer": answer}
            for index, answer in enumerate(state.get("answers", []))
        ]

    def _plan(self, request, state, snapshot):
        if self.model is None:
            return self._output(request, state, error="Dynamic tool selection requires LLM mode.")
        tools = [{**item, "server": "profiler"} for item in state["catalog"]
                 if item["name"] in self.gateway.allowed_tools]
        if self._git_enabled(request):
            tools.extend({**item, "server": "git"} for item in state.get("git_catalog", [])
                         if item["name"] in self.git_gateway.allowed_tools)
        if not tools:
            return self._output(request, state, error=(
                "No discovered tools are allowed for the supplied inputs. Configure "
                "PROFILER_MCP_ALLOWED_TOOLS or GIT_MCP_ALLOWED_TOOLS."
            ))
        if state["calls"] >= self.max_calls or state["rounds"] >= self.max_calls * 2:
            if not self._has_usable_evidence(state):
                return self._output(request, state, error="Dynamic investigation budget exhausted.")
            state["limitations"].append("Investigation budget reached; report uses collected evidence only.")
            state["phase"] = "report"
            return AnalysisStep(checkpoint=state, message="Investigation budget reached; preparing report.")
        source_context = self._saved_source_context(request, state) or SourceContext(snapshot=snapshot)
        payload = {
            "source_snapshot": snapshot.model_dump() if snapshot else None,
            "remaining_source_calls": 5 - state.get("source_calls", 0),
            "schema": ToolDecision.model_json_schema(), "request": request.model_dump(),
            "tools": tools, "evidence": state["evidence"],
            "clarifications": self._clarification_context(state),
            "remaining_calls": self.max_calls - state["calls"],
            "argument_feedback": state.get("argument_feedback"),
            "limitations": state["limitations"],
        }
        context = self.context_builder.build(self.model, self.planner_system, payload, source_context)
        check_deadline()
        decision = ToolDecision.model_validate_json(
            self.model.complete(self.planner_system, context.payload, json_output=True),
        )
        check_deadline()
        state.pop("argument_feedback", None)
        state["rounds"] += 1
        state.setdefault("decisions", []).append(decision.model_dump())
        if decision.action == "ask":
            if not decision.question.strip():
                raise ValueError("An analysis question is required")
            return self._output(request, state, question=decision.question)
        if decision.action == "source":
            if not request.source or request.source.policy == "off" or snapshot is None or snapshot.status != "resolved":
                return self._output(request, state, question=(
                    "Repository source is unavailable for this run. Provide an accessible repository "
                    "and revision in project settings and start a new run, or continue using available MCP evidence."
                ))
            if not decision.locations and not decision.symbols:
                raise ValueError("Select a concrete source range or symbol")
            if state.get("source_calls", 0) >= 5:
                raise ValueError("Source query budget exhausted")
            selection = {"locations": [p.model_dump() for p in decision.locations], "symbols": decision.symbols}
            if selection in state.get("source_queries", []):
                raise ValueError("Repeated source query")
            state.update(phase="source", source_selection=selection)
            return AnalysisStep(checkpoint=state, message="Repository source query planned.")
        if decision.action == "finish":
            if not self._has_usable_evidence(state):
                return self._output(request, state, error="No usable MCP evidence was collected.")
            state["phase"] = "report"
            return AnalysisStep(checkpoint=state, message="Profiler evidence ready for interpretation.")
        if not decision.tool or (decision.server, decision.tool) not in {
            (tool["server"], tool["name"]) for tool in tools
        }:
            raise ValueError("The planner selected an unavailable or disallowed tool")
        selected_tool = next(tool for tool in tools if tool["name"] == decision.tool
                             and tool["server"] == decision.server)
        try:
            validate_schema(decision.arguments, selected_tool["input_schema"])
        except SchemaValidationError as error:
            # Return only schema locations/rules, never argument values or server errors.
            state["argument_feedback"] = {
                "server": decision.server, "tool": decision.tool,
                "path": list(error.absolute_path), "rule": error.validator,
                "message": "Arguments do not match the discovered input schema. Correct them "
                "using known context or ask a concrete question if a value is unknown.",
            }
            return AnalysisStep(
                checkpoint=state, message="Reviewing profiler arguments against the discovered schema.",
            )
        if any(e.get("server", "profiler") == decision.server
               and e["operation"] == decision.tool and e["arguments"] == decision.arguments
               for e in state["evidence"]):
            raise ValueError("Repeated identical profiler query")
        state.update(phase="call", decision=decision.model_dump())
        return AnalysisStep(checkpoint=state, message="MCP query planned and checkpointed.")

    def _call(self, request, state, snapshot):
        decision = ToolDecision.model_validate(state["decision"])
        if decision.server == "git" and not self._git_enabled(request):
            raise ValueError("Git MCP access is unavailable or disabled")
        gateway = self.git_gateway if decision.server == "git" else self.gateway
        catalog = state["git_catalog"] if decision.server == "git" else state["catalog"]
        tool = MCPTool.model_validate(next(t for t in catalog if t["name"] == decision.tool))
        # The gateway enforces schemas, the allowlist, profile binding and schema drift.
        check_deadline()
        data = gateway.call_tool(tool, decision.arguments, result_id=request.result_id)
        check_deadline()
        item = evidence(tool.name, decision.arguments, data, len(state["evidence"]) + 1)
        item["server"] = decision.server
        state["evidence"].append(item)
        state["calls"] += 1
        state["phase"] = "plan"
        return AnalysisStep(checkpoint=state, message="MCP response saved as evidence.")

    @staticmethod
    def _saved_source_context(request, state):
        if (request.source and request.source.policy != "off" and state.get("source_context")
                and state.get("source_request") == request.source.model_dump()):
            return SourceContext.model_validate(state["source_context"])
        return None

    def _source(self, request, state, snapshot):
        selection = state["source_selection"]
        selected_request = request.source.model_copy(update={
            "locations": [SourceLocation.model_validate(p) for p in selection["locations"]],
            "symbols": selection["symbols"],
        })
        context = collect_source(self.source_gateway, selected_request, snapshot,
                                 model_enabled=self.model is not None)
        for item in context.evidence:
            item.origin = "model_selection"
            if item.resolution == "provided_location":
                item.resolution = "model_location"
        previous = self._saved_source_context(request, state)
        if previous:
            merged = {item.id: item for item in previous.evidence + context.evidence}
            context.evidence = []
            total = 0
            for item in merged.values():
                size = len(item.text.encode("utf-8"))
                if len(context.evidence) >= 5 or total + size > 32768:
                    context.limitations.append("Combined source evidence budget reached.")
                    break
                context.evidence.append(item)
                total += size
            context.limitations = list(dict.fromkeys(previous.limitations + context.limitations))
        context.needs_selection = request.source.policy == "required"
        state["source_context"] = context.model_dump()
        state["source_request"] = request.source.model_dump()
        state["source_calls"] = state.get("source_calls", 0) + 1
        state.setdefault("source_queries", []).append(selection)
        state["phase"] = "plan"
        return AnalysisStep(checkpoint=state, message="Repository source query completed.")

    def _report(self, request, state, snapshot):
        saved = self._saved_source_context(request, state)
        required = bool(request.source and request.source.policy == "required")
        context = (collect_source(self.source_gateway, request.source, snapshot,
                                  model_enabled=self.model is not None)
                   if required or saved is None else saved)
        if required and request.source.locations:
            if any(not any(item.path == location.path and item.start_line == location.start_line
                           and item.origin == "user_selection"
                           and item.resolution == "provided_location"
                           for item in context.evidence)
                   for location in request.source.locations):
                context.needs_selection = True
                context.limitations.append("Not all required user-selected source locations were available.")
        if required and saved and not context.needs_selection:
            # Preserve user-required evidence first; model reads can only add context.
            merged = {item.id: item for item in context.evidence}
            total = sum(len(item.text.encode("utf-8")) for item in context.evidence)
            for item in saved.evidence:
                if item.id in merged:
                    continue
                size = len(item.text.encode("utf-8"))
                if len(merged) >= 5 or total + size > 32768:
                    context.limitations.append("Additional model-selected source omitted due to the combined budget.")
                    break
                merged[item.id] = item
                total += size
            context.evidence = list(merged.values())
            context.limitations = list(dict.fromkeys(context.limitations + saved.limitations))
        if context.needs_selection:
            return self._output(request, state, question="Select an explicit source path and line range.",
                                field="source", source_context=context)
        docs = self.docs_agent.ask(request.goal).model_dump()
        state["limitations"].extend(context.limitations)
        if docs.get("status") != "found":
            state["limitations"].append(docs["answer"])
        state.pop("raw_interpretation", None)
        state.pop("unvalidated_interpretation", None)
        try:
            interpretation, delivered, notes = self.interpret(
                request, [Evidence.model_validate(item) for item in state["evidence"]], docs, context,
                analysis_context={"clarifications": self._clarification_context(state)},
                raw_output_sink=lambda text: state.update(raw_interpretation=text),
            )
        except (ContextBudgetExceeded, RequiredSourceOmitted):
            raise
        except ValueError:
            state["unvalidated_interpretation"] = True
            state["limitations"].append(
                "Analyzer output did not pass the structured analysis or evidence citation contract. "
                "No findings from that output were accepted."
            )
            pending = self._output(request, state, error="Analysis output requires validation.",
                                   documentation=docs, source_context=context).report
        else:
            check_deadline()
            if context.required_delivery == "pending":
                context.required_delivery = "delivered"
            state["limitations"].extend(notes)
            pending = self._output(request, state, interpretation=interpretation, documentation=docs,
                                   source_context=context, delivered=delivered).report
        state["pending_report"] = pending.model_dump(exclude={"chat_report"})
        state["phase"] = "format_report"
        return AnalysisStep(checkpoint=state, message="Preparing the chat report.")

    def _format_report(self, request, state, snapshot):
        report = ProfileAnalysis.model_validate(state["pending_report"])
        report.chat_report = format_chat_report(
            report, unvalidated_output=state.get("raw_interpretation")
            if state.get("unvalidated_interpretation") else None,
        )
        check_deadline()
        return AnalysisStep(checkpoint=state, report=report)

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
        git_connection = self.git_gateway.configuration_fingerprint if self.git_gateway else None
        if state.get("git_connection_fingerprint", git_connection) != git_connection:
            return self._output(request, state, error="Git MCP endpoint or tool policy changed; start a new run.")
        state["git_connection_fingerprint"] = git_connection
        identity = hashlib.sha256(json.dumps({
            "result_id": request.result_id, "goal": request.goal,
            "metrics": request.metrics, "include_asm": request.include_asm,
            "repository": request.repository, "branch": request.branch,
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
        if request.repository and request.source is None:
            request = request.model_copy(update={"source": SourceRequest(
                repository=request.repository, ref=request.branch or "HEAD",
            )})
        with execution_deadline(deadline):
            snapshot = source_snapshot or prepare_source(self.source_gateway, request.source)
            checkpoint = {}
            for _ in range(self.max_calls * 4 + 20):
                step = self.step_analysis(request, checkpoint=checkpoint, deadline=deadline,
                                          source_snapshot=snapshot)
                if step.report is not None:
                    return step.report
                checkpoint = step.checkpoint
            return self._output(request, checkpoint, error="Analysis step limit reached.").report


    def as_tool(self):
        from langchain_core.tools import StructuredTool

        from .contracts import AnalysisRequest

        return StructuredTool.from_function(
            name="analyze_profile", description="Investigate profiler evidence using discovered MCP tools.",
            args_schema=AnalysisRequest,
            func=lambda **kwargs: self.analyze(AnalysisRequest(**kwargs)).model_dump(),
        )
