"""Per-invocation deadline shared by profiler, documentation and model calls."""

import time
from contextlib import contextmanager
from contextvars import ContextVar

_deadline = ContextVar("execution_deadline", default=None)


def remaining_timeout(timeout: float) -> float:
    deadline = _deadline.get()
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("Execution budget exhausted")
    return remaining


def check_deadline():
    remaining_timeout(float("inf"))


@contextmanager
def execution_deadline(deadline):
    parent = _deadline.get()
    effective = (
        deadline if parent is None else parent if deadline is None else min(parent, deadline)
    )
    token = _deadline.set(effective)
    try:
        check_deadline()
        yield
    finally:
        _deadline.reset(token)
