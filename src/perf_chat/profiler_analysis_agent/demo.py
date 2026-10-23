"""Deterministic model fixture for the dynamic graph, never real LLM analysis."""

import json


class DemoAnalysisModel:
    def complete(self, system, payload, **kwargs):
        if payload["schema"]["title"] == "KnowledgeSelection":
            return json.dumps({"paths": []})
        if "hotspot_findings" in payload:
            return json.dumps({
                "observations": [observation for finding in payload["hotspot_findings"]
                                 for observation in finding["interpretation"]["observations"]],
                "recommendations": ["Demo only; validate real profiler and model deployments separately."],
            })
        if payload["schema"]["title"] == "ToolDecision":
            if not payload["evidence"]:
                return json.dumps({"action": "call", "reason": "Read synthetic fixture",
                                   "tool": payload["tools"][0]["name"], "arguments": {}})
            return json.dumps({"action": "finish", "reason": "Synthetic fixture collected"})
        return json.dumps({
            "observations": [{"text": "Synthetic profiler evidence: " + json.dumps(item["response"]),
                              "evidence_ids": [item["id"]]} for item in payload["evidence"]],
            "recommendations": ["Demo only; validate real profiler and model deployments separately."],
        })
