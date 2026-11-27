"""Radeon transport for explicitly enabled local, read-only intent classification."""
import asyncio
from functools import cache

import httpx2

from travel_agent import classify_intent
from travel_contract import INTENT

RADEON_MODEL = "DeepSeek-V4-Flash"
RADEON_ENDPOINT = "https://developer.amd.com.cn/radeon/api/v1/chat/completions"
RADEON_KEY_ENV = "RADEON_API_KEY"


def radeon_schema(schema):
    """xgrammar accepts a subset. The unchanged application schema validates the raw reply."""
    if isinstance(schema, list):
        return [radeon_schema(node) for node in schema]
    if not isinstance(schema, dict):
        return schema
    projected = {}
    for key, value in schema.items():
        if key == "format":
            if value == "date":
                projected["pattern"] = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
            elif value == "date-time":
                projected["pattern"] = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?(Z|[+-][0-9]{2}:[0-9]{2})$"
            continue
        if key in {"$schema", "uniqueItems", "pattern"}:
            continue
        if key == "properties":
            projected[key] = {field: radeon_schema(node) for field, node in value.items()}
        else:
            projected[key] = radeon_schema(value)
    return projected


@cache
def radeon_response_format():
    return {"type": "json_schema", "json_schema": {"name": "travel_intent", "strict": True,
                                                   "schema": radeon_schema(INTENT.schema)}}


class RadeonIntent:
    # Two attempts of 25 seconds plus at most 5 seconds of server-requested delay.
    turn_timeout = 58

    def __init__(self, key):
        self._key = key

    async def intent(self, message, now, known, repair, *, trace):
        async def complete(messages, *, structured):
            body = dict(model=RADEON_MODEL, messages=messages, stream=False, temperature=0,
                        max_tokens=384, response_format=radeon_response_format())
            headers = {"Authorization": "Bearer " + self._key}
            async with httpx2.AsyncClient(timeout=25, follow_redirects=False) as client:
                for i in (1, 2):
                    try:
                        response = await client.post(RADEON_ENDPOINT, headers=headers, json=body)
                    except httpx2.TransportError as exc:
                        trace.record("model_response", provider="radeon", attempt=i,
                                     status="timeout" if isinstance(exc, httpx2.TimeoutException) else "transport_error")
                        if i == 2:
                            raise
                        continue
                    if response.is_success:
                        reply = response.json()["choices"][0]["message"]
                        if not isinstance(reply, dict):
                            raise ValueError("invalid_model_message")
                        trace.record("model_response", provider="radeon", attempt=i, status="received")
                        return reply
                    status = {401:"auth_failed", 403:"auth_failed", 429:"rate_limited"}.get(
                        response.status_code, "provider_error")
                    trace.record("model_response", provider="radeon", attempt=i,
                                 status=status, reason=f"http_{response.status_code}")
                    if i == 1 and response.status_code == 429:
                        try:
                            delay = float(response.headers.get("Retry-After", "2"))
                        except ValueError:
                            delay = 2
                        if 0 <= delay <= 5:
                            await asyncio.sleep(max(1, delay))
                            continue
                    response.raise_for_status()

        return await classify_intent(message, now, known, repair, completion=complete)
