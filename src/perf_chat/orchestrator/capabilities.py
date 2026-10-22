"""Host-owned descriptions of supported functions and configured execution paths."""

import re

from .catalog import work_catalog


def _label(value, limit):
    text = " ".join(value.split())[:limit]
    return re.sub(r"([\\`*{}_\[\]()<>#!|])", r"\\\1", text)


def capabilities_reply(registry, execution=None):
    lines = [
        "I can help define a performance optimization or analysis task, clarify its "
        "goal and constraints, and hand it to the configured executor.",
        "I can also explain previous agent errors and results, and reformat existing "
        "reports in LLM mode without launching another analysis. Explanations depend "
        "on the available error details; they do not verify a fix.",
        "The built-in profile analyzer can investigate profiler data and repository "
        "source evidence through configured read-only services and report findings "
        "with evidence and limitations. Actual optimization requires an external executor.",
    ]
    mode = (execution or {}).get("mode")
    if mode in {"demo", "profile-demo"}:
        lines.append(f"Current execution mode: {mode}. Execution uses synthetic data; "
                     "it does not establish real measurements or performance improvements.")
    elif mode == "profile-mcp":
        lines.append("Current execution mode: profile-mcp. "
                     "Configured and allowlisted query paths: "
                     f"Profiler MCP: {'yes' if execution.get('profiler') else 'no'}; "
                     f"Git MCP: {'yes' if execution.get('git') else 'no'}. "
                     "Configuration does not verify service connectivity or data availability.")
    elif mode == "external":
        lines.append("Execution uses a supplied external executor; its capabilities "
                     "and connectivity are not verified by this description.")
    else:
        lines.append("Execution availability depends on the worker configuration; "
                     "service connectivity is not verified by this description.")
    catalog = work_catalog(registry, execution)
    analysis = next(item for item in catalog if item["intent"] == "task")
    lines.append(f"Analysis direction: {analysis['name']}. "
                 "DynamicProfilerAnalyzer is the built-in analyzer for profile-demo and profile-mcp. "
                 "Analysis uses the separate execution flow; it is not a consultation expert.")
    catalog = [item for item in catalog if item["intent"] == "consultation"]
    if catalog:
        lines.append("Registered consultation experts (registration does not verify "
                     "their reference data or backend availability):\n\n" + "\n".join(
                         f"- {item['id']}: {_label(item['name'], 40)} — "
                         f"{_label(item['description'], 120)}"
                         for item in catalog
                     ))
        lines.append("Use /ask <expert_id> <question> for a specific expert, "
                     "or /task <description> to define or change a task.")
    else:
        lines.append("No consultation experts are registered. "
                     "Use /task <description> to define or change a task.")
    return "\n\n".join(lines)
