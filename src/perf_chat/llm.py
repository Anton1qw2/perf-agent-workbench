"""Small provider-neutral Chat Completions adapter; no provider SDK required."""

import asyncio
import json

import httpx

from .config import Settings
from .runtime import check_deadline, remaining_timeout


class ChatModel:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build_request(self, system: str, payload: dict, *, json_output=False) -> dict:
        body = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        if json_output:
            body["response_format"] = {"type": "json_object"}
        return body

    def complete(self, system: str, payload: dict, *, json_output: bool = False) -> str:
        headers = {}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        body = self.build_request(system, payload, json_output=json_output)
        timeout = remaining_timeout(self.settings.timeout)

        async def request():
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.settings.base_url}/chat/completions",
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]

        async def bounded():
            return await asyncio.wait_for(request(), timeout=timeout)

        content = asyncio.run(bounded())
        check_deadline()
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Model returned an empty response")
        return content
