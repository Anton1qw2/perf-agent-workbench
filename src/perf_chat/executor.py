"""Replace DemoExecutor with an idempotent adapter to external execution infrastructure."""

from .contracts import (
    ClarificationRequest,
    ExecutionEvent,
    PerformanceResult,
)


class DemoExecutor:
    def step(self, run_id, task, cursor, answer=None):
        stage = cursor.get("stage", 0)
        if stage == 0:
            return ExecutionEvent(
                type="progress", message="Demo: task handed over to the executor."
            ), {
                "stage": 1,
            }
        if stage == 1 and answer is None:
            return ExecutionEvent(
                type="clarification",
                clarification=ClarificationRequest(
                    request_id=f"{run_id}:environment",
                    question="Demo executor question: are there any additional constraints for this run?",
                ),
            ), cursor
        if stage == 1:
            return ExecutionEvent(type="progress", message="Demo: clarification received."), {
                "stage": 2
            }
        return ExecutionEvent(
            type="result",
            result=PerformanceResult(
                task_id=task.task_id,
                status="demo",
                summary="Integration scenario completed. No actual optimization was performed.",
                limitations=["Demo executor: no measurements or code changes were made."],
            ),
        ), {"stage": 3}

    def cancel(self, run_id):
        pass
