import logging
import time

from .config import Settings
from .contracts import ClarificationAnswer, ExecutionEvent, PerformanceResult, PerformanceTask
from .executor import DemoExecutor
from .integrations.git_source import GitSourceGateway
from .integrations.gitlab_source import RepositorySourceGateway
from .integrations.profiler_mcp import DemoProfilerGateway, MCPProfilerGateway
from .llm import ChatModel
from .orchestrator.agent import OrchestratorAgent
from .orchestrator.graph import build_intake_graph
from .profiler_analysis_agent import ProfileAnalysisAgent
from .profiler_analysis_agent.executor import ProfileAnalysisExecutor
from .profiler_docs_agent import ProfilerDocsAgent
from .store import Store
from .task_agent.agent import TaskAgent

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, executor=None):
        self.store = Store(settings.db_path)
        self.settings = settings
        self._configured_executor = executor is None
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
            gateway = (
                DemoProfilerGateway()
                if settings.executor_mode == "profile-demo"
                else (MCPProfilerGateway(settings.profiler_mcp_url, settings.profiler_mcp_timeout))
            )
            self.executor = ProfileAnalysisExecutor(
                ProfileAnalysisAgent(
                    gateway,
                    model,
                    source_gateway=GitSourceGateway(settings.git_repositories),
                    max_input_bytes=settings.model_input_bytes,
                )
            )
        else:
            self.executor = DemoExecutor()

    def _profile_executor(self, run):
        settings = self.settings
        repository = run["task"]["target"].get("repository") or ""
        profiler_token = self.store.run_token(run, "profiler", settings.profiler_mcp_url)
        git_token = self.store.run_token(run, "git", repository)
        gateway = (
            DemoProfilerGateway()
            if settings.executor_mode == "profile-demo"
            else MCPProfilerGateway(
                settings.profiler_mcp_url,
                settings.profiler_mcp_timeout,
                token=profiler_token,
            )
        )
        source = RepositorySourceGateway(
            GitSourceGateway(settings.git_repositories), repository=repository, token=git_token
        )
        return ProfileAnalysisExecutor(
            ProfileAnalysisAgent(
                gateway,
                self.model,
                source_gateway=source,
                max_input_bytes=settings.model_input_bytes,
            )
        )

    def tick(self):
        job = self.store.claim()
        if job is None:
            return False
        try:
            if job["kind"] == "intake":
                conv = self.store.conversation(job["conversation_id"])
                boundary = job["payload"].get("message_count", len(conv["messages"]))
                output = self.graph.invoke(
                    {
                        "task": conv["spec"],
                        "messages": conv["messages"][:boundary],
                    }
                )
                self.store.finish_intake(job, output)
            else:
                rid = job["id"] if job["kind"] == "execute" else job["payload"]["run_id"]
                run = self.store.run(rid)
                execution = (
                    self._profile_executor(run)
                    if self._configured_executor
                    and isinstance(self.executor, ProfileAnalysisExecutor)
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
        except Exception as exc:
            # Exception text may contain provider response content: log only its class and job id.
            log.error("job=%s error=%s", job["id"], type(exc).__name__)
            self.store.fail(job)
        return True


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
