"""Read-only source discovery for the Guizhou travel assistant (MCP over stdio)."""
import asyncio
from contextvars import ContextVar
from datetime import date, datetime, timezone
import os
import re
from typing import Annotated
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from pydantic import Field

server = MCPServer("guizhou-travel-search", log_level="WARNING")
Short = Annotated[str, Field(min_length=1, max_length=120)]
TextList = Annotated[list[Short], Field(max_length=12)]
UrlList = Annotated[list[str], Field(max_length=24)]
TIMEOUT = 20
BEFORE_SEARCH_ATTEMPT = ContextVar("before_search_attempt", default=None)


def host(url):
    try:
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and not parts.username and not parts.password:
            return (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        pass
    return ""


def date_in_text(text, day):
    # Require the year. A page's publication date is deliberately kept out of text.
    y, m, d = day.year, day.month, day.day
    return bool(re.search(
        rf"(?<!\d){y}(?:年0?{m}月0?{d}(?:日|号)?|[-/.]0?{m}[-/.]0?{d})(?!\d)",
        text,
    ))


def exa_rows(result):
    if result.is_error:
        raise ValueError("provider_tool_error")
    rows = []
    for part in result.content:
        if part.type != "text":
            continue
        for section in re.split(r"(?m)^Title:[ \t]*", part.text)[1:]:
            title = section.splitlines()[0] if section.splitlines() else ""
            url = re.search(r"(?m)^URL:[ \t]*(https?://\S+)", section)
            if not url:
                continue
            published = re.search(r"(?m)^Published:[ \t]*(.+)$", section)
            excerpt = re.split(r"(?m)^(?:Highlights|Text|Summary|Snippet):[ \t]*", section, maxsplit=1)
            rows.append({
                "title": title.strip(), "url": url[1],
                "excerpt": excerpt[1].strip() if len(excerpt) == 2 else "",
                "published_at": published[1].strip() if published and published[1].strip() != "N/A" else None,
            })
    return rows


async def search_exa(query, count):
    async with httpx2.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as http:
        async with streamable_http_client("https://mcp.exa.ai/mcp", http_client=http) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool("web_search_exa", {"query": query, "numResults": count})
                return exa_rows(result)


async def search_anysearch(query, count):
    headers = {}
    if os.getenv("ANYSEARCH_AUTH_MODE", "").lower() == "key":
        key = os.getenv("ANYSEARCH_API_KEY")
        if not key:
            raise ValueError("missing_api_key")
        headers["Authorization"] = f"Bearer {key}"
    async with httpx2.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as http:
        reply = await http.post("https://api.anysearch.com/v1/search", headers=headers,
                                json={"query": query, "max_results": count})
        reply.raise_for_status()
        payload = reply.json()
        if payload.get("code") != 0 or not isinstance((payload.get("data") or {}).get("results"), list):
            raise ValueError("invalid_provider_response")
        return [{"title": str(r.get("title") or ""), "url": str(r.get("url") or ""),
                 "excerpt": str(r.get("snippet") or ""), "published_at": r.get("published_at")}
                for r in payload["data"]["results"] if isinstance(r, dict)]


def failure_reason(error):
    if isinstance(error, BaseExceptionGroup):
        return failure_reason(error.exceptions[0])
    if isinstance(error, (TimeoutError, httpx2.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx2.HTTPStatusError):
        return f"http_{error.response.status_code}"
    if isinstance(error, ValueError) and str(error) == "missing_api_key":
        return "missing_api_key"
    return "provider_error"


@server.tool()
async def search_travel_sources(
    query: Annotated[str, Field(min_length=2, max_length=600)],
    subject: Short,
    required_terms: TextList | None = None,
    source_domains: TextList | None = None,
    target_date: date | None = None,
    rejected_urls: UrlList | None = None,
    limit: Annotated[int, Field(ge=1, le=5)] = 3,
) -> dict:
    """Search Exa first, then AnySearch only if no suitable leads or on error.
    Give one literal place/shop/route anchor in subject. All required_terms must occur;
    use trusted source_domains for official sources and target_date for date-specific
    notices. Omit target_date for standing fare policies. Pass rejected_urls when
    prior articles failed review. Returned leads still require checking the original.
    """
    query = query.strip()
    subject = subject.strip()
    if not subject or len(query) < 2:
        raise ValueError("请提供检索问题和明确地点或线路")
    domains = {s.lower().strip().strip(".") for s in source_domains or []}
    if any(not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z0-9-]+", d) for d in domains):
        raise ValueError("source_domains 需要填写域名，例如 fanjingshan.gz.cn")
    terms = [subject.casefold(), *(s.strip().casefold() for s in required_terms or [] if s.strip())]
    rejected = {u.split("#")[0].rstrip("/") for u in rejected_urls or []}
    attempts = []
    matches = []
    for provider, search in (("exa", search_exa), ("anysearch", search_anysearch)):
        if before := BEFORE_SEARCH_ATTEMPT.get():
            before(provider)
        try:
            async with asyncio.timeout(TIMEOUT):
                rows = await search(query, min(limit + 2, 7))
        except Exception as exc:
            attempts.append({"provider": provider, "status": "error", "reason": failure_reason(exc)})
            continue

        seen = set()
        for row in rows:
            url = row["url"]
            hostname = host(url)
            canonical = url.split("#")[0].rstrip("/")
            if not hostname or canonical in rejected or canonical in seen:
                continue
            if hostname in {"exa.ai", "www.exa.ai"} and urlsplit(url).path.startswith("/library/places"):
                continue
            if domains and not any(hostname == d or hostname.endswith("." + d) for d in domains):
                continue
            excerpt = row["excerpt"][:2800]
            text = (row["title"] + "\n" + excerpt).casefold()
            if not all(t in text for t in terms) or (target_date and not date_in_text(text, target_date)):
                continue
            seen.add(canonical)
            matches.append({**row, "excerpt": excerpt, "provider": provider})
            if len(matches) == limit:
                break
        attempts.append({"provider": provider, "status": "matched" if matches else "no_match",
                         "returned": len(rows), "accepted": len(matches)})
        if matches:
            break
    status = "candidates" if matches else "unavailable" if all(a["status"] == "error" for a in attempts) else "unconfirmed"
    return {"status": status, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": matches, "attempts": attempts}


if __name__ == "__main__":
    server.run(transport="stdio")
