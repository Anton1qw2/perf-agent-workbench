"""Bridge the analysis graph to the existing durable worker and clarification flow."""

import json

from ..contracts import ClarificationRequest, ExecutionEvent, PerformanceResult
from ..runtime import execution_deadline
from .contracts import AnalysisRequest
from .source_context import prepare_source
from .source_contracts import SourceOptions, SourceRequest, SourceSnapshot


def render_report(report):
    lines = [
        "Profile analysis" + (" (SYNTHETIC DEMO)" if report.demo else ""),
        f"Result ID: {report.request.result_id}",
        f"Status: {report.status}",
    ]
    for title, items in [("Observations", report.observations), ("Hypotheses", report.hypotheses)]:
        lines.append("\n" + title)
        lines.extend(f"- {item.text} [{', '.join(item.evidence_ids)}]" for item in items)
    lines.extend("- " + item for item in report.recommendations)
    lines.append("\nLimitations")
    lines.extend("- " + item for item in report.limitations)
    lines.append("\nEvidence excerpts (full responses are saved in the run cursor)")
    for item in report.evidence:
        text = json.dumps(item.response, ensure_ascii=False)
        lines.append(f"[{item.id}] {item.operation}: " + text[:3000])
        if len(text) > 3000:
            lines.append("[Display excerpt truncated; full evidence retained.]")
    if report.source_context:
        snapshot = report.source_context.snapshot
        lines.append(
            f"\nSource: {snapshot.status}; commit {snapshot.commit or 'unavailable'}; profile match unverified"
        )
        for item in report.source_context.evidence:
            lines.append(
                f"[{item.id}] {item.path}:{item.start_line}-{item.end_line} ({item.resolution})\n{item.text}"
            )
    return "\n".join(lines)


class ProfileAnalysisExecutor:
    def __init__(self, agent):
        self.agent = agent

    def step(self, run_id, task, cursor, answer=None, *, deadline=None):
        if not cursor:
            return ExecutionEvent(type="progress", message="Preparing profile metric analysis."), {
                "stage": "analyze",
            }
        raw = task.details.get("profile_analysis")
        supported = {"metrics", "include_asm", "source"}
        saved = cursor.get("options", {})
        options = {
            key: value
            for key, value in (raw.items() if isinstance(raw, dict) else [])
            if key in supported
        }
        options.update(saved)
        options["goal"] = task.summary
        options["result_id"] = cursor.get("result_id", task.target.profile or "")
        awaiting = cursor.get("awaiting")

        def ask(field, question):
            return self._clarification(
                run_id,
                field,
                question,
                {
                    **cursor,
                    "result_id": options.get("result_id", ""),
                    "options": {key: value for key, value in options.items() if key in supported},
                },
            )

        if awaiting and answer:
            if awaiting == "result_id":
                options["result_id"] = answer.answer
            else:
                try:
                    value = json.loads(answer.answer)
                except json.JSONDecodeError:
                    return ask(awaiting, "Reply with valid JSON for " + awaiting + ".")
                if awaiting == "options":
                    if not isinstance(value, dict):
                        return ask(
                            "options", "Provide a JSON object with metrics and/or include_asm."
                        )
                    options.update({key: value for key, value in value.items() if key in supported})
                else:
                    if awaiting == "source" and not isinstance(value, dict):
                        return ask(
                            "source", 'Provide a source options object, or {"policy":"off"}.'
                        )
                    if awaiting == "source" and isinstance(value, dict):
                        current_source = options.get("source")
                        options["source"] = (
                            {"policy": "off"}
                            if value.get("policy") == "off"
                            else {
                                **(current_source if isinstance(current_source, dict) else {}),
                                **value,
                            }
                        )
                    else:
                        options[awaiting] = value
        elif raw is not None and not isinstance(raw, dict) and not saved:
            return ask("options", "Provide analysis options as a JSON object, or {} for defaults.")
        if not options.get("result_id"):
            return ask("result_id", "Provide the profiler result_id to analyze.")
        validating_source = False
        try:
            data = dict(options)
            if options.get("source") is not None:
                validating_source = True
                source = SourceOptions.model_validate(options["source"])
                if (
                    source.policy != "off"
                    and source.commit
                    and (
                        source.scope_repository != task.target.repository
                        or source.scope_ref != task.target.branch
                    )
                ):
                    return ask(
                        "source",
                        "Confirm the exact commit with scope_repository and scope_ref matching the current project, or set commit to null.",
                    )
                if (
                    source.policy != "off"
                    and cursor.get("source_snapshot")
                    and source.commit != cursor["source_snapshot"].get("requested_commit")
                ):
                    return ask(
                        "source",
                        "The source snapshot is frozen. Start a new run to select a different commit, or set policy to off.",
                    )
                data["source"] = SourceRequest(
                    repository=task.target.repository or "",
                    ref=task.target.branch or "",
                    **source.model_dump(),
                ).model_dump()
                validating_source = False
            request = AnalysisRequest.model_validate(data)
        except ValueError as exc:
            from pydantic import ValidationError

            field = (
                str(exc.errors()[0]["loc"][0]) if isinstance(exc, ValidationError) else "options"
            )
            questions = {
                "metrics": "Reply with a JSON array of up to six metric names.",
                "include_asm": "Should assembly be requested? Reply with JSON true or false.",
                "result_id": "Provide a nonempty profiler result_id of at most 4096 characters.",
                "source": "Provide source options as JSON: policy, locations (path/start_line/end_line), symbols, and optional scoped commit.",
            }
            if validating_source:
                field = "source"
            return ask(
                field if field in questions else "options",
                questions.get(field, "Provide valid analysis options as a JSON object."),
            )
        saved_cursor = {
            **cursor,
            "result_id": request.result_id,
            "options": {key: value for key, value in options.items() if key in supported},
        }
        if request.source and "source_snapshot" not in cursor:
            with execution_deadline(deadline):
                snapshot = prepare_source(self.agent.source_gateway, request.source)
            return ExecutionEvent(
                type="progress",
                message="Source selection prepared; immutable revision snapshot saved.",
            ), {
                **saved_cursor,
                "stage": "analyze",
                "source_snapshot": snapshot.model_dump(),
                "awaiting": None,
            }
        snapshot = (
            SourceSnapshot.model_validate(cursor["source_snapshot"])
            if cursor.get("source_snapshot")
            else None
        )
        report = self.agent.analyze(request, deadline=deadline, source_snapshot=snapshot)
        if report.status == "needs_input":
            return self._clarification(
                run_id,
                report.clarification_field,
                "\n".join(report.open_questions)
                + (
                    '\nReply with source options JSON, or {"policy":"off"} to continue without source. Preparation failures require a new run to retry.'
                    if report.clarification_field == "source"
                    else '\nReply as a JSON array, for example ["cpu_samples"].'
                ),
                {
                    **saved_cursor,
                    "stage": "analyze",
                    "result_id": request.result_id,
                    "analysis": report.model_dump(),
                    "clarification_attempt": cursor.get("clarification_attempt", 0),
                },
            )
        return ExecutionEvent(
            type="result",
            result=PerformanceResult(
                task_id=task.task_id,
                status="failed"
                if report.status == "insufficient_data"
                else "demo"
                if report.demo
                else "succeeded",
                summary=render_report(report),
                limitations=report.limitations,
            ),
        ), {**saved_cursor, "stage": "done", "analysis": report.model_dump(), "awaiting": None}

    @staticmethod
    def _clarification(run_id, field, question, cursor):
        attempt = cursor.get("clarification_attempt", 0) + 1
        return ExecutionEvent(
            type="clarification",
            clarification=ClarificationRequest(
                request_id=f"{run_id}:analysis:{field}:{attempt}",
                question=question,
                fields=[field],
            ),
        ), {**cursor, "awaiting": field, "clarification_attempt": attempt}

    def cancel(self, run_id):
        # Read-only bounded calls; worker checks cancellation between durable steps.
        pass
