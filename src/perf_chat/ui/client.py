import json
from uuid import uuid4

import httpx


class ApiClient:
    def __init__(self, url):
        self.url = url.rstrip("/")

    async def call(self, method, path, body=None):
        async with httpx.AsyncClient(base_url=self.url, timeout=20) as client:
            response = await client.request(method, "/api" + path, json=body)
            if response.is_error:
                raise ValueError(str(response.json().get("detail", response.status_code)))
            return response.json()

    async def events(self, cid, after=0):
        async with httpx.AsyncClient(base_url=self.url, timeout=None) as client:
            async with client.stream(
                "GET", f"/api/conversations/{cid}/events", params={"after": after}
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield json.loads(line[6:])


def request_id():
    return uuid4().hex
