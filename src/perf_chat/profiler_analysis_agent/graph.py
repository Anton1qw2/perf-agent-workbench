import hashlib
import json
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..runtime import check_deadline
from .context_builder import ContextBudgetExceeded, RequiredSourceOmitted
from .contracts import AnalysisRequest, Evidence, Interpretation, ProfileAnalysis
from .source_context import collect_source
from .source_contracts import SourceContext
from .validation import metric_names


class AnalysisState(TypedDict, total=False):
    request: dict
    evidence: list[dict]
    selected: list[str]
    questions: list[str]
    documentation: dict
    interpretation: dict
    report: dict
    error: str
    source_context: dict
    delivered_ids: list[str]
    context_notes: list[str]
    question_kind: str


def evidence(operation, arguments, response, index):
    digest = hashlib.sha256(
        json.dumps(
            {"operation": operation, "arguments": arguments, "response": response},
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return Evidence(
        id=f"e{index}",
        operation=operation,
        arguments=arguments,
        response=response,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        sha256=digest,
    ).model_dump()


def build_analysis_graph(agent):
    def catalog(state):
        request = AnalysisRequest.model_validate(state["request"])
        items = []
        try:
            check_deadline()
            data = agent.gateway.get_list_metrics(request.result_id)
            items = [
                evidence(
                    "get_list_metrics",
                    {
                        "result_id": request.result_id,
                    },
                    data,
                    1,
                )
            ]
            check_deadline()
            return {"evidence": items}
        except Exception:
            return {
                "evidence": items,
                "error": "Metric discovery failed; check MCP configuration and response format.",
            }

    def plan(state):
        if state.get("error"):
            return {}
        request = AnalysisRequest.model_validate(state["request"])
        try:
            names = metric_names(state["evidence"][0]["response"])
        except ValueError:
            return {
                "error": "Unsupported metric catalog format; configure the MCP response adapter."
            }
        selected = request.metrics
        if not names:
            return {"error": "The profiler returned no available metrics."}
        if selected and not set(selected) <= set(names):
            return {"questions": ["Choose available metrics: " + ", ".join(names[:30])]}
        if not selected and len(names) > 6:
            return {
                "questions": ["Choose up to six metrics for analysis: " + ", ".join(names[:30])]
            }
        return {"selected": selected or names}

    def fetch(state):
        request = AnalysisRequest.model_validate(state["request"])
        items = list(state["evidence"])
        args = {"result_id": request.result_id, "metrics": state["selected"]}
        operations = ["get_hotspots"] + (["get_asm"] if request.include_asm else [])
        for operation in operations:
            try:
                check_deadline()
                data = getattr(agent.gateway, operation)(**args)
                items.append(evidence(operation, args, data, len(items) + 1))
                check_deadline()
                if data is None or data == [] or data == {} or data == "":
                    return {"evidence": items, "error": f"{operation} returned no data."}
            except Exception:
                return {
                    "evidence": items,
                    "error": f"{operation} failed; no conclusions were inferred from the failed call.",
                }
        return {"evidence": items}

    def source(state):
        request = AnalysisRequest.model_validate(state["request"])
        current = SourceContext.model_validate(state.get("source_context", {}))
        context = collect_source(
            agent.source_gateway,
            request.source,
            current.snapshot,
            model_enabled=agent.model is not None,
        )
        update = {"source_context": context.model_dump()}
        if context.needs_selection:
            update.update(
                questions=[
                    "Select a source path and line range, or explicitly continue without source."
                ],
                question_kind="source",
            )
        return update

    def interpret(state):
        request = AnalysisRequest.model_validate(state["request"])
        try:
            check_deadline()
            docs = agent.docs_agent.ask(" ".join(state["selected"])).model_dump()
            check_deadline()
        except Exception:
            docs = {
                "status": "unavailable",
                "answer": "Documentation lookup failed.",
                "sources": [],
            }
        try:
            check_deadline()
            result, delivered, notes = agent.interpret(
                request,
                [Evidence.model_validate(item) for item in state["evidence"]],
                docs,
                SourceContext.model_validate(state.get("source_context", {})),
            )
            check_deadline()
            source_context = SourceContext.model_validate(state.get("source_context", {}))
            if source_context.required_delivery == "pending":
                source_context.required_delivery = "delivered"
            return {
                "documentation": docs,
                "interpretation": result.model_dump(),
                "delivered_ids": sorted(delivered),
                "context_notes": notes,
                "source_context": source_context.model_dump(),
            }
        except RequiredSourceOmitted as exc:
            return {"documentation": docs, "questions": [str(exc)], "question_kind": "source"}
        except ContextBudgetExceeded as exc:
            return {"documentation": docs, "error": str(exc)}
        except Exception:
            return {
                "documentation": docs,
                "error": "Interpretation failed validation or the model call failed; raw evidence is retained.",
            }

    def report(state):
        result = Interpretation.model_validate(state.get("interpretation", {}))
        limitations = [
            "No metric calculations were performed by this agent.",
            "Collection quality, units and compatibility are only known where supplied by the server.",
            "Evidence references are structurally checked; semantic correctness requires review.",
        ]
        if agent.gateway.demo:
            limitations.append("Synthetic demo data; no real profile was read.")
        if agent.model is None:
            limitations.append("Model interpretation is disabled; see raw evidence.")
        if state.get("error"):
            limitations.append(state["error"])
        source_context = SourceContext.model_validate(state.get("source_context", {}))
        limitations.extend(source_context.limitations)
        limitations.extend(state.get("context_notes", []))
        docs = state.get("documentation")
        if docs and docs.get("status") != "found":
            limitations.append(docs["answer"])
        output = ProfileAnalysis(
            status="needs_input"
            if state.get("questions")
            else ("insufficient_data" if state.get("error") else "completed"),
            request=AnalysisRequest.model_validate(state["request"]),
            demo=agent.gateway.demo,
            **result.model_dump(),
            limitations=limitations,
            open_questions=state.get("questions", []),
            evidence=state.get("evidence", []),
            documentation=docs,
            source_context=source_context if source_context.snapshot else None,
            delivered_evidence_ids=state.get("delivered_ids", []),
            clarification_field=state.get("question_kind", "metrics"),
        )
        return {"report": output.model_dump()}

    graph = StateGraph(AnalysisState)
    for name, node in [
        ("metric_catalog", catalog),
        ("plan", plan),
        ("fetch", fetch),
        ("source_context", source),
        ("interpret", interpret),
        ("report", report),
    ]:
        graph.add_node(name, node)
    graph.add_edge(START, "metric_catalog")
    graph.add_edge("metric_catalog", "plan")
    graph.add_conditional_edges(
        "plan",
        lambda s: "report" if s.get("error") or s.get("questions") else "fetch",
        {"report": "report", "fetch": "fetch"},
    )
    graph.add_conditional_edges(
        "fetch",
        lambda s: "report" if s.get("error") else "source_context",
        {"report": "report", "source_context": "source_context"},
    )
    graph.add_conditional_edges(
        "source_context",
        lambda s: "report" if s.get("questions") else "interpret",
        {"report": "report", "interpret": "interpret"},
    )
    graph.add_edge("interpret", "report")
    graph.add_edge("report", END)
    return graph.compile()
