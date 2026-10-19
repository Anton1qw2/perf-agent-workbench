"""Standalone entry point: JSON analysis report on stdout."""

import argparse
import json

from ..config import Settings
from ..integrations.git_source import GitSourceGateway
from ..integrations.profiler_mcp import DemoProfilerGateway, MCPProfilerGateway
from ..llm import ChatModel
from .contracts import AnalysisRequest, AnalysisServices
from .factory import create_builtin_analyzer


def main():
    parser = argparse.ArgumentParser(description="Analyze profiler data using MCP")
    parser.add_argument("--result-id", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--repository", help="Optional local Git path, registered alias or GitLab URL")
    parser.add_argument("--branch", help="Optional source revision; local default is HEAD")
    parser.add_argument("--metrics", nargs="*", default=[])
    parser.add_argument("--asm", action="store_true", help="Also request assembly detail")
    parser.add_argument("--demo", action="store_true", help="Use explicitly synthetic data")
    parser.add_argument(
        "--source-json",
        help="SourceRequest JSON with registered repository alias, ref and source options",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    if not args.demo and not settings.profiler_mcp_url:
        parser.error("Set PROFILER_MCP_URL or use --demo")
    gateway = (
        DemoProfilerGateway()
        if args.demo
        else MCPProfilerGateway(
            settings.profiler_mcp_url,
            settings.profiler_mcp_timeout,
            allowed_tools=settings.profiler_allowed_tools,
            profile_arguments=settings.profiler_profile_arguments,
        )
    )
    agent = create_builtin_analyzer(AnalysisServices(
        profiler=gateway,
        model=ChatModel(settings) if settings.mode == "llm" else None,
        source=GitSourceGateway.for_repository(settings.git_repositories, args.repository),
        max_input_bytes=settings.model_input_bytes,
    ))
    report = agent.analyze(
        AnalysisRequest(
            result_id=args.result_id,
            goal=args.goal,
            repository=args.repository, branch=args.branch,
            metrics=args.metrics,
            include_asm=args.asm,
            source=json.loads(args.source_json) if args.source_json else None,
        )
    )
    # ASCII JSON preserves all Unicode via escapes on Windows console/pipe encodings.
    print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=True))
    return 0 if report.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
