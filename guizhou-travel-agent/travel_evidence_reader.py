"""HTTPS original reader. Constrain the host and the IP actually connected."""
import asyncio
import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import urljoin, urlsplit

from time import monotonic

from travel_evidence import Material, ToolResult, parse_document, public_url

MAX_ORIGINAL = 600_000


def public_addresses(host):
    addresses = {item[4][0] for item in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)}
    if not addresses or any(not address_allowed(value) for value in addresses):
        raise ValueError("正文地址不是公开地址")
    return sorted(addresses, key=lambda value: (":" in value,value))


def address_allowed(value):
    ip = ipaddress.ip_address(value)
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=5, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address,443),self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw,server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class PublicOriginalReader:
    def __init__(self, hosts, *, clock, enabled=False, before_request=None):
        self.hosts = frozenset(hosts)
        self.clock = clock
        self.enabled = enabled
        self.before_request = before_request

    async def __call__(self, url):
        if not self.enabled:
            return ToolResult("provider_error")
        return await asyncio.to_thread(self.read,url)

    def read(self, raw):
        url = public_url(raw)
        request_url = url
        cutoff = monotonic()+10
        for hop in range(2):
            if not url or urlsplit(url).hostname not in self.hosts:
                return ToolResult("invalid_response")
            parsed = urlsplit(url)
            try:
                address = public_addresses(parsed.hostname)[0]
                connection = PinnedHTTPS(parsed.hostname,address)
                try:
                    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                    if self.before_request:
                        self.before_request()
                    connection.request("GET",path,headers={"Accept":"text/html,text/plain","User-Agent":"GuizhouTravelEvidence/1.0"})
                    response = connection.getresponse()
                    if response.status in {301,302,303,307,308}:
                        location = response.getheader("Location")
                        if hop or not location:
                            return ToolResult("invalid_response")
                        url = public_url(urljoin(url,location))
                        continue
                    if response.status in {401,403}:
                        return ToolResult("auth_failed" if response.status == 401 else "provider_error")
                    if response.status in {402,429}:
                        return ToolResult("quota_exhausted" if response.status == 402 else "rate_limited")
                    if response.status in {404,410}:
                        return ToolResult("empty")
                    if response.status != 200:
                        return ToolResult("provider_error")
                    kind = response.headers.get_content_type()
                    if kind not in {"text/html","text/plain","application/xhtml+xml"}:
                        return ToolResult("invalid_response")
                    content = bytearray()
                    while len(content) <= MAX_ORIGINAL:
                        if monotonic() >= cutoff:
                            return ToolResult("timeout")
                        chunk = response.read1(min(16_384,MAX_ORIGINAL+1-len(content)))
                        if not chunk:
                            break
                        content.extend(chunk)
                    if len(content)>MAX_ORIGINAL:
                        return ToolResult("invalid_response")
                    encoding = response.headers.get_content_charset()
                    if not encoding:
                        meta = re.search(rb'charset\s*=\s*["\']?([A-Za-z0-9_-]+)',content[:2048],re.I)
                        encoding = meta[1].decode("ascii") if meta else "utf-8"
                    raw_text = content.decode(encoding,errors="replace")
                    text, canonical, meta_date = parse_document(raw_text) if kind != "text/plain" else (raw_text.strip(),None,None)
                    canonical = public_url(urljoin(url,canonical)) if canonical else None
                    meta_date = meta_date[:40] if isinstance(meta_date,str) else None
                    if not text:
                        return ToolResult("empty")
                    if re.match(r"^\s*(?:请登录|登录后查看|Just a moment|Access Denied)",text,re.I):
                        return ToolResult("provider_error")
                    published = re.search(r"(?:发布日期|发布时间|发稿时间)\s*[:：]?\s*(20\d{2}[-年]\d{1,2}[-月]\d{1,2}日?)",text)
                    return ToolResult("success",Material(url,text,self.clock().isoformat(),"live",
                                      published_at=published[1] if published else meta_date,canonical_url=canonical,
                                      origin_url=request_url if request_url != url else None))
                finally:
                    connection.close()
            except TimeoutError:
                return ToolResult("timeout")
            except (LookupError, ValueError):
                return ToolResult("invalid_response")
            except (OSError, http.client.HTTPException):
                return ToolResult("provider_error")
        return ToolResult("invalid_response")


class KnowledgeBaseAdapter:
    """Old /v1/retrieve contract. An operator-supplied transport controls access."""
    def __init__(self, post):
        self.post = post

    async def __call__(self, query):
        result = await self.post("/v1/retrieve", {**query,"region":"贵州"})
        if not isinstance(result, ToolResult) or result.status != "success":
            return result
        payload = result.data
        if (not isinstance(payload,dict) or not isinstance(payload.get("corpus_version"), str)
                or not isinstance(payload.get("hits"),list)):
            return ToolResult("invalid_response",provider="kb")
        if not payload["hits"]:
            return ToolResult("empty",provider="kb")
        return ToolResult("success",payload,provider="kb")



class ParallelOriginalReader:
    """Opt-in independent original fetch for exact registered public pages."""
    def __init__(self, allowed_urls, *, clock):
        self.allowed_urls = {public_url(url) for url in allowed_urls}
        self.clock = clock

    async def __call__(self, url):
        import json
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        if public_url(url) not in self.allowed_urls or not public_url(url):
            return ToolResult("invalid_response",provider="parallel")
        try:
            async with httpx2.AsyncClient(timeout=10,follow_redirects=False) as http:
                async with streamable_http_client("https://search.parallel.ai/mcp",http_client=http) as channels:
                    async with ClientSession(*channels) as session:
                        await session.initialize()
                        result=await session.call_tool("web_fetch",{"urls":[url]})
            if result.is_error:
                return ToolResult("provider_error",provider="parallel")
            packet=result.structured_content
            if not isinstance(packet,dict):
                blocks=[part.text for part in result.content if part.type=="text"]
                packet=json.loads("\n".join(blocks))
            rows=packet.get("results",[]) if isinstance(packet,dict) else []
            for row in rows:
                if not isinstance(row,dict) or public_url(row.get("url")) != public_url(url):
                    continue
                excerpts=row.get("excerpts",[])
                body="\n".join(x for x in excerpts if isinstance(x,str) and x.strip())
                if body:
                    published=row.get("publish_date")
                    return ToolResult("success",Material(url,body[:12000],self.clock().isoformat(),
                                      "live",published_at=published if isinstance(published,str) else None),
                                      provider="parallel")
            return ToolResult("empty",provider="parallel")
        except (TypeError,ValueError):
            return ToolResult("invalid_response",provider="parallel")
        except TimeoutError:
            return ToolResult("timeout",provider="parallel")
        except (OSError,RuntimeError,ExceptionGroup,httpx2.HTTPError):
            return ToolResult("provider_error",provider="parallel")
