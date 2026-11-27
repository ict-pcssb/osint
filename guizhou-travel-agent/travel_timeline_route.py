"""Validate the single OSM route boundary for M4."""
from dataclasses import dataclass
import asyncio
import json
from pathlib import Path
import shutil

OSM_SOURCE = "https://www.openstreetmap.org/copyright"
EXPECTED_MODES = {"walk":"foot", "car":"car"}


class RouteFailure(RuntimeError):
    def __init__(self, status):
        self.status = status


@dataclass(frozen=True)
class RouteResult:
    status: str
    mode: str
    from_id: str
    to_id: str
    checked_at: str
    data_mode: str
    distance_m: int | None = None
    duration_s: int | None = None


class RouteAdapter:
    def __init__(self, invoke, clock, *, fixture, max_attempts=5):
        self.invoke = invoke
        self.clock = clock
        self.fixture = fixture
        self.max_attempts = max_attempts

    async def route(self, start, end, mode):
        checked_at = self.clock().isoformat()
        material = dict(mode=mode, from_id=start.id, to_id=end.id, checked_at=checked_at,
                        data_mode="fixture" if self.fixture else "live")
        if (mode not in EXPECTED_MODES or start.id == end.id or
                start.coordinate_system != "WGS84" or end.coordinate_system != "WGS84" or
                not start.confirmed or not end.confirmed):
            return RouteResult("invalid_response", **material)
        profile = EXPECTED_MODES[mode]
        arguments = {"profile":profile, "waypoints":[start.waypoint, end.waypoint]}
        try:
            raw = await asyncio.wait_for(self.invoke(arguments), timeout=12)
        except TimeoutError:
            return RouteResult("timeout", **material)
        except RouteFailure as exc:
            status = exc.status if exc.status in {"empty","auth_failed","rate_limited","quota_exhausted","invalid_response"} else "provider_error"
            return RouteResult(status, **material)
        except (RuntimeError, OSError):
            return RouteResult("provider_error", **material)
        if raw is None:
            return RouteResult("empty", **material)
        if (not isinstance(raw, dict) or raw.get("profile") != profile or
                any(type(raw.get(key)) is not int or raw[key] <= 0 for key in ("distance_m", "duration_s"))):
            return RouteResult("invalid_response", **material)
        material["checked_at"] = self.clock().isoformat()
        return RouteResult("success", **material, distance_m=raw["distance_m"], duration_s=raw["duration_s"])


def make_osm_invoker(entry, *, authorized):
    """Read-only stdio client for an already installed osm-mcp package."""
    if authorized is not True:
        raise PermissionError("M4 真实路段需要单独的只读网络许可")
    entry = Path(entry).resolve()
    node = shutil.which("node")
    if not node or not entry.is_file() or entry.name != "index.js":
        raise FileNotFoundError("需要现有 Node 和 osm-mcp 的 dist/index.js")

    async def invoke(arguments):
        import os
        import mcp
        from mcp.client.stdio import stdio_client

        env = {key: value for key, value in os.environ.items() if key.upper() in
               {"PATH","SYSTEMROOT","TEMP","TMP","HTTP_PROXY","HTTPS_PROXY","NO_PROXY","SSL_CERT_FILE","NODE_EXTRA_CA_CERTS"}}
        env.update(TRANSPORT="stdio", NODE_USE_ENV_PROXY="1", OSM_USER_AGENT="GuizhouTravel/1.0")
        config = mcp.StdioServerParameters(command=node, args=[str(entry)], env=env)
        with open(os.devnull, "w") as errors:
            async with stdio_client(config, errlog=errors) as streams:
                async with mcp.ClientSession(*streams) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if "route" not in {tool.name for tool in tools.tools}:
                        raise RouteFailure("invalid_response")
                    result = await session.call_tool("route", arguments)
        if result.is_error:
            description = " ".join(part.text for part in result.content if part.type == "text").lower()
            if any(word in description for word in ("noroute","nosegment","no route","no segment")):
                raise RouteFailure("empty")
            if "429" in description:
                raise RouteFailure("rate_limited")
            raise RouteFailure("provider_error")
        if result.structured_content is not None:
            return result.structured_content
        try:
            return json.loads(next(part.text for part in result.content if part.type == "text"))
        except (ValueError, StopIteration):
            raise RouteFailure("invalid_response") from None

    return invoke

