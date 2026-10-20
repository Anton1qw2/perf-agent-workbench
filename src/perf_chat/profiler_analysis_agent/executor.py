"""Bridge the analysis graph to the existing durable worker and clarification flow."""

import json

from ..contracts import ClarificationRequest, ExecutionEvent, PerformanceResult
from ..runtime import execution_deadline
from .context_contracts import AnalysisContextUpdate, ContextUpdateProposal
from .context_updates import effective_context
from .contracts import AnalysisRequest, AnalysisStep
from .reporting import format_chat_report
from .source_context import prepare_source
from .source_contracts import SourceOptions, SourceRequest, SourceSnapshot


def render_report(report):
    """The same deterministic formatter also supports older/custom structured reports."""
    return format_chat_report(report).content


class ProfileAnalysisExecutor:
    def __init__(self, agent, *, source_gateway=None, git_gateway=None):
        self.agent = agent
        self.source_gateway = (
            source_gateway if source_gateway is not None else getattr(agent, "source_gateway", None)
        )
        self.git_gateway = git_gateway if git_gateway is not None else getattr(agent, "git_gateway", None)

    def _validate_context_entrypoint(self, context, task, cursor):
        original = task.details.get("profile_analysis", {})
        saved = cursor.get("options", {})
        source = saved.get("source", original.get("source", {}) if isinstance(original, dict) else {})
        git_enabled = bool(context.repository and self.git_gateway
                           and (not isinstance(source, dict) or source.get("policy") != "off"))
        if not context.result_id and not git_enabled:
            raise ValueError("A Result ID is required without enabled Git MCP access")

    def _confirm_context(self, task, cursor, answer):
        if answer is None or answer.request_id != cursor.get("pending_request_id"):
            raise ValueError("The answer does not match the context confirmation")
        current, updates = effective_context(task, cursor)
        pending = cursor["pending_context_update"]
        proposal = ContextUpdateProposal.model_validate(pending["proposal"])
        accepted = answer.answer.strip().casefold() in {"yes", "y", "да"}
        saved = {**cursor, "awaiting": None}
        saved.pop("pending_context_update", None)
        saved.pop("context_reply_id", None)
        if not accepted:
            saved["context_feedback"] = "Context update declined or not confirmed. Continue with the previous context."
            return ExecutionEvent(type="progress", message="Context unchanged; resuming analysis."), saved
        try:
            changed = proposal.apply(current)
            self._validate_context_entrypoint(changed, task, cursor)
        except ValueError:
            saved["context_feedback"] = "Context update rejected because the revision or proposal is no longer valid."
            return ExecutionEvent(type="progress", message="Stale context update rejected; context unchanged."), saved
        update = AnalysisContextUpdate(
            update_id=answer.request_id, reply_to=pending["reply_to"], previous=current,
            current=changed, mode=proposal.mode,
        )
        if (current.repository, current.branch) != (changed.repository, changed.branch):
            if cursor.get("source_snapshot"):
                saved["source_snapshot_history"] = [*cursor.get("source_snapshot_history", []), {
                    "revision": current.revision, "snapshot": cursor["source_snapshot"],
                }]
            saved.pop("source_snapshot", None)
            original = task.details.get("profile_analysis", {})
            options = dict(cursor.get("options", {}))
            source = options.get("source", original.get("source", {}) if isinstance(original, dict) else {})
            options["source"] = {"policy": source.get("policy", "optional") if isinstance(source, dict) else "optional"}
            saved["options"] = options
        saved.update(context_updates=[item.model_dump() for item in [*updates, update]],
                     result_id=changed.result_id,
                     context_feedback="User confirmed the context update. Use the accepted context revision.")
        return ExecutionEvent(type="progress", message="Context update confirmed and saved."), saved

    def step(self, run_id, task, cursor, answer=None, *, deadline=None):
        analyzer_id = getattr(
            self.agent, "analyzer_id", f"{type(self.agent).__module__}.{type(self.agent).__qualname__}",
        )
        if cursor.get("analyzer_id", analyzer_id) != analyzer_id:
            return ExecutionEvent(type="result", result=PerformanceResult(
                task_id=task.task_id, status="failed",
                summary="Analyzer implementation changed. Start a new run.",
            )), cursor
        if not cursor:
            return ExecutionEvent(type="progress", message="Preparing performance analysis."), {
                "stage": "analyze", "analyzer_id": analyzer_id,
            }
        cursor = {**cursor, "analyzer_id": analyzer_id}
        if cursor.get("awaiting") == "context_confirmation":
            return self._confirm_context(task, cursor, answer)
        context, updates = effective_context(task, cursor)
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
        options["repository"] = context.repository
        options["branch"] = context.branch
        options["result_id"] = context.result_id
        options["context_revision"] = context.revision
        options["context_feedback"] = cursor.get("context_feedback", "")
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
            elif awaiting == "analysis_input":
                cursor["context_reply_id"] = answer.request_id
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
        if not options.get("result_id") and not (
            context.repository and self.git_gateway
        ):
            return ask("result_id", "Provide the profiler result_id to analyze.")
        if "source" not in options and context.repository:
            options["source"] = {}
        source_limitation = None
        validating_source = False
        try:
            data = dict(options)
            if options.get("source") is not None:
                validating_source = True
                source = SourceOptions.model_validate(options["source"])
                if not context.repository:
                    if source.policy == "required":
                        return ask(
                            "source",
                            'This run has no complete repository/branch binding for required source. '
                            'Save Repository and Branch in project settings and start a new run, '
                            'or reply {"policy":"off"} to analyze this profile without source.',
                        )
                    data["source"] = None
                    if source.policy == "optional":
                        source_limitation = (
                            "Optional source enrichment skipped: this run has no complete "
                            "repository/branch binding."
                        )
                else:
                    if (
                        source.policy != "off"
                        and source.commit
                        and (
                            source.scope_repository != context.repository
                            or source.scope_ref != (context.branch or "HEAD")
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
                        repository=context.repository or "",
                        ref=context.branch or "HEAD",
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
        saved_cursor.setdefault("analysis_context_initial", {
            "revision": 0, "result_id": request.result_id,
            "repository": request.repository, "branch": request.branch,
        })
        if request.source and "source_snapshot" not in cursor:
            with execution_deadline(deadline):
                snapshot = prepare_source(self.source_gateway, request.source)
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
        if hasattr(self.agent, "step_analysis"):
            extra = {"context_updates": updates} if getattr(self.agent, "supports_context_updates", False) else {}
            step = AnalysisStep.model_validate(self.agent.step_analysis(
                request, checkpoint=cursor.get("analyzer_state", {}),
                answer=answer.answer if awaiting == "analysis_input" and answer else None,
                deadline=deadline, source_snapshot=snapshot, **extra,
            ))
            saved_cursor["analyzer_state"] = step.checkpoint
            if step.context_update is not None:
                try:
                    if (not extra or not saved_cursor.get("context_reply_id")
                            or step.report is not None):
                        raise ValueError("Context updates require an answered analyzer question and opt-in")
                    changed = step.context_update.apply(context)
                    self._validate_context_entrypoint(changed, task, saved_cursor)
                except ValueError:
                    saved_cursor["context_feedback"] = "Context update rejected: unsupported, stale, unchanged or invalid proposal. Ask a new question if needed."
                    saved_cursor.pop("context_reply_id", None)
                    return ExecutionEvent(type="progress", message="Context update rejected; previous context retained."), {
                        **saved_cursor, "awaiting": None,
                    }
                text = ("Confirm the analysis context update (" + step.context_update.mode + ").\n\n"
                        "```json\n" + json.dumps({"from": context.model_dump(), "to": changed.model_dump()},
                                                 indent=2, ensure_ascii=True) + "\n```\n\n"
                        "Reply yes to apply. Any other reply keeps the current context.")
                return self._clarification(run_id, "context_confirmation", text, {
                    **saved_cursor, "pending_context_update": {
                        "proposal": step.context_update.model_dump(), "reply_to": saved_cursor["context_reply_id"],
                    },
                })
            if step.report is None:
                return ExecutionEvent(type="progress", message=step.message), {
                    **saved_cursor, "stage": "analyze", "awaiting": None,
                }
            report = step.report
        else:
            # Preserve integrations that supplied the original analyze() object.
            report = self.agent.analyze(request, deadline=deadline, source_snapshot=snapshot)
        if source_limitation:
            report.limitations.append(source_limitation)
        report.context_updates = updates
        if report.status == "needs_input":
            return self._clarification(
                run_id,
                report.clarification_field,
                "\n".join(report.open_questions)
                + (
                    '\nReply with source options JSON, or {"policy":"off"} to continue without source. Preparation failures require a new run to retry.'
                    if report.clarification_field == "source"
                    else '\nReply as a JSON array, for example ["cpu_samples"].'
                    if report.clarification_field == "metrics"
                    else "\nReply in chat with the requested analysis context."
                ),
                {
                    **saved_cursor,
                    "stage": "analyze",
                    "result_id": request.result_id,
                    "analysis": report.model_dump(),
                    "clarification_attempt": cursor.get("clarification_attempt", 0),
                },
            )
        analyzer_state = saved_cursor.get("analyzer_state", {})
        report.chat_report = format_chat_report(
            report, unvalidated_output=analyzer_state.get("raw_interpretation")
            if analyzer_state.get("unvalidated_interpretation") else None,
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
                summary=report.chat_report.content,
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
        ), {**cursor, "awaiting": field, "clarification_attempt": attempt,
            "pending_request_id": f"{run_id}:analysis:{field}:{attempt}"}

    def cancel(self, run_id):
        # Read-only calls are bounded; custom analyzers may release their own resources.
        cancel = getattr(self.agent, "cancel", None)
        if cancel is not None:
            cancel(run_id)
