"""Routing directions share a catalog, not an execution lifecycle."""


def work_catalog(registry, execution=None, *, include_schemas=False):
    return [
        {
            "id": "performance_analysis", "intent": "task", "execution": "run",
            "name": "Performance analysis",
            "description": "Prepare analysis or optimization of the selected profile or repository. "
                           "Clarify missing inputs; execution requires an explicit launch.",
            "executor_mode": (execution or {}).get("mode", "unknown"),
        },
        {
            "id": "capabilities", "intent": "capabilities", "execution": "intake",
            "name": "Assistant capabilities",
            "description": "Describe supported functions and registered experts without changing the task.",
        },
        *({**expert, "intent": "consultation", "execution": "intake"}
          for expert in registry.catalog(include_schemas=include_schemas)),
    ]
