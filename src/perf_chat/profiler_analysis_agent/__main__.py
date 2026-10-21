"""Standalone entry point: JSON analysis report on stdout."""

import argparse
import json

from ..config import Settings
from ..integrations.git_mcp import MCPGitGateway
from ..integrations.git_source import GitSourceGateway
from ..integrations.profiler_mcp import DemoProfilerGateway, MCPProfilerGateway
from ..llm import ChatModel
from .contracts import AnalysisRequest, AnalysisServices
from .factory import create_builtin_analyzer


def main():
    parser = argparse.ArgumentParser(description="Analyze profiler data using MCP")
    parser.add_argument("--result-id", default="")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--repository", help="Git MCP repository reference, local Git path or registered alias")
    parser.add_argument("--branch", help="Optional source revision; local default is HEAD")
    parser.add_argument("--metrics", nargs="*", default=[])
    parser.add_argument("--asm", action="store_true", help="Also request assembly detail")
    parser.add_argument("--demo", action="store_true",
                        help="Deprecated; requires PERF_CHAT_EXECUTOR=profile-demo")
    parser.add_argument(
        "--source-json",
        help="SourceRequest JSON with registered repository alias, ref and source options",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    if settings.executor_mode not in {"profile-demo", "profile-mcp"}:
        parser.error("Set PERF_CHAT_EXECUTOR=profile-demo or profile-mcp for standalone analysis")
    demo = settings.executor_mode == "profile-demo"
    if args.demo and not demo:
        parser.error("--demo cannot override PERF_CHAT_EXECUTOR; set it to profile-demo")
    if not args.result_id and not args.repository:
        parser.error("Provide --result-id or --repository")
    gateway = (
        DemoProfilerGateway()
        if demo
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
        git=MCPGitGateway(settings.git_mcp_url, settings.git_mcp_timeout,
                          token=settings.git_mcp_token, allowed_tools=settings.git_allowed_tools)
        if settings.git_mcp_url and not demo else None,
        demo=demo,
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
