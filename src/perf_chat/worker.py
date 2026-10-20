import logging
import time

from .config import Settings
from .contracts import ClarificationAnswer, ExecutionEvent, PerformanceResult, PerformanceTask
from .executor import DemoExecutor
from .integrations.git_mcp import MCPGitGateway, git_token_destination
from .integrations.git_source import GitSourceGateway
from .integrations.profiler_mcp import DemoProfilerGateway, MCPProfilerGateway
from .llm import ChatModel
from .orchestrator.agent import OrchestratorAgent
from .orchestrator.graph import build_intake_graph
from .profiler_analysis_agent.context_updates import effective_context
from .profiler_analysis_agent.contracts import AnalysisServices
from .profiler_analysis_agent.executor import ProfileAnalysisExecutor
from .profiler_analysis_agent.factory import create_builtin_analyzer
from .profiler_docs_agent import ProfilerDocsAgent
from .store import Store
from .task_agent.agent import TaskAgent

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, executor=None, *, analyzer_factory=None):
        self.store = Store(settings.db_path)
        self.settings = settings
        self._configured_executor = executor is None
        self.analyzer_factory = analyzer_factory or create_builtin_analyzer
        model = ChatModel(settings) if settings.mode == "llm" else None
        self.model = model
        self.graph = build_intake_graph(
            TaskAgent(model),
            OrchestratorAgent(model),
            ProfilerDocsAgent(model),
        )
        if executor is not None:
            self.executor = executor
        elif settings.executor_mode in {"profile-demo", "profile-mcp"}:
            # Instantiate only once the immutable run and credential references are known.
            self.executor = None
        else:
            self.executor = DemoExecutor()

    def _profile_executor(self, run):
        settings = self.settings
        context, _ = effective_context(PerformanceTask.model_validate(run["task"]), run["cursor"])
        repository = context.repository or ""
        profiler_token = self.store.run_token(run, "profiler", settings.profiler_mcp_url)
        gateway = (
            DemoProfilerGateway()
            if settings.executor_mode == "profile-demo"
            else MCPProfilerGateway(
                settings.profiler_mcp_url,
                settings.profiler_mcp_timeout,
                token=profiler_token,
                allowed_tools=settings.profiler_allowed_tools,
                profile_arguments=settings.profiler_profile_arguments,
            )
        )
        source = GitSourceGateway.for_repository(settings.git_repositories, repository)
        git_token = (self.store.run_token(run, "git_mcp", git_token_destination(
            settings.git_mcp_url, repository,
        )) if settings.git_mcp_url and settings.executor_mode == "profile-mcp" else "")
        git = (MCPGitGateway(
            settings.git_mcp_url, settings.git_mcp_timeout,
            token=git_token or settings.git_mcp_token, allowed_tools=settings.git_allowed_tools,
        ) if settings.git_mcp_url and settings.executor_mode == "profile-mcp" else None)
        return ProfileAnalysisExecutor(
            self.analyzer_factory(AnalysisServices(
                profiler=gateway, model=self.model, source=source,
                analysis_id=run["id"], max_input_bytes=settings.model_input_bytes,
                git=git,
            )), source_gateway=source, git_gateway=git,
        )

    def tick(self):
        job = self.store.claim()
        if job is None:
            return False
        try:
            if job["kind"] == "intake":
                self._intake(job)
            else:
                self._execute(job)
        except Exception as exc:
            # Exception text may contain provider response content: log only its class and job id.
            log.error("job=%s error=%s", job["id"], type(exc).__name__)
            self.store.fail(job)
        return True

    def _intake(self, job):
        conv = self.store.conversation(job["conversation_id"])
        boundary = job["payload"].get("message_count", len(conv["messages"]))
        output = self.graph.invoke({
            "task": conv["spec"],
            "messages": conv["messages"][:boundary],
        })
        self.store.finish_intake(job, output)

    def _execute(self, job):
        rid = job["id"] if job["kind"] == "execute" else job["payload"]["run_id"]
        run = self.store.run(rid)
        execution = (
            self._profile_executor(run)
            if self._configured_executor
            and self.executor is None
            else self.executor
        )
        if run["status"] == "cancelling":
            execution.cancel(rid)
            self.store.finish_cancel(job, rid)
        else:
            task = PerformanceTask.model_validate(run["task"])
            if (
                run["steps"] >= 100
                or run["runtime_seconds"] >= task.budget.max_runtime_seconds
            ):
                raise RuntimeError("Execution budget exhausted")
            answer = (
                ClarificationAnswer.model_validate(run["answer"]) if run["answer"] else None
            )
            started = time.monotonic()
            remaining = task.budget.max_runtime_seconds - run["runtime_seconds"]
            if isinstance(execution, ProfileAnalysisExecutor):
                event, cursor = execution.step(
                    rid,
                    task,
                    run["cursor"],
                    answer,
                    deadline=started + remaining,
                )
            else:
                event, cursor = execution.step(rid, task, run["cursor"], answer)
            elapsed = time.monotonic() - started
            if elapsed >= remaining:
                event = ExecutionEvent(
                    type="result",
                    result=PerformanceResult(
                        task_id=task.task_id,
                        status="failed",
                        summary="Execution budget exhausted; no successful result was accepted.",
                        limitations=[
                            "The current synchronous call may return after its deadline."
                        ],
                    ),
                )
            self.store.finish_step(job, rid, event, cursor, elapsed)


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    worker = Worker(settings)
    log.info("Worker started; executor=%s", settings.executor_mode)
    try:
        while True:
            if not worker.tick():
                time.sleep(0.3)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
