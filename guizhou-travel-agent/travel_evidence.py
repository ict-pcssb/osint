"""Read-only claim verification. Discovery, original material and conclusion are separate."""
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
import re
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit


from jsonschema import Draft202012Validator
from travel_contract import FORMATS, check

CONTRACT_PATH = Path(__file__).resolve().parent / "contracts/travel_agent/v1/evidence.schema.json"
VERIFY_REPORT = Draft202012Validator(json.loads(CONTRACT_PATH.read_text("utf-8")), format_checker=FORMATS)

MODES = {"live", "snapshot", "fixture"}
TOOL_STATES = {"success", "empty", "timeout", "auth_failed", "rate_limited", "quota_exhausted",
               "invalid_response", "provider_error"}
CLAIM_TYPES = {"opening", "booking", "ticket_price", "last_service"}
DATE = r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?"
INJECTION = re.compile(r"忽略.*规则|系统提示|发送.*(?:手机号|密码)|发到外站|分享.*身份|ignore.*instruction|system prompt", re.I)
INDEFINITE = re.compile(r"可能|据传|待定|尚未明确|暂无法|不代表|示意|原定|拟定|据.*转载|例[如子]")


@dataclass(frozen=True)
class ToolResult:
    status: str
    data: object = None
    provider: str = "public_original"

    def __post_init__(self):
        if self.status not in TOOL_STATES:
            raise ValueError("未知工具状态")
        if self.status != "success" and self.data is not None:
            raise ValueError("失败工具结果不能带业务数据")


@dataclass(frozen=True)
class Material:
    url: str
    text: str
    retrieved_at: str
    data_mode: str
    published_at: str | None = None
    canonical_url: str | None = None
    origin_url: str | None = None
    source_kind: str = "official"

    def __post_init__(self):
        if (not public_url(self.url) or self.data_mode not in MODES or not self.text.strip()
                or datetime.fromisoformat(self.retrieved_at).utcoffset() is None):
            raise ValueError("原文资料格式无效")

    @property
    def content_hash(self):
        return digest(re.sub(r"\s+", "", self.text))


def digest(value):
    return sha256(value.encode("utf-8")).hexdigest()


def public_url(raw):
    """Syntactic URL guard. The live reader additionally pins a public address."""
    if not isinstance(raw, str) or len(raw) > 1400 or any(c.isspace() or c == "\\" for c in raw):
        return None
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if (parsed.scheme == "https" and host and not parsed.username and not parsed.password
                and parsed.port in {None, 443} and "." in host
                and re.fullmatch(r"[a-z0-9.-]+",host)
                and not host.endswith((".local",".localhost",".internal"))
                and not re.fullmatch(r"[\d.]+",host)
                and not any(key.lower() in {"key","token","api_key","password","authorization","access_token","signature","sig"}
                            for key,_ in parse_qsl(parsed.query))):
            return urlunsplit(("https",host,parsed.path or "/",parsed.query,""))
    except ValueError:
        pass
    return None


class PublicHTML(HTMLParser):
    IGNORED = {"script","style","noscript","template","svg","iframe","form","header","footer","nav"}
    BLOCKS = {"br","p","section","div","li","article","main","tr","h1","h2","h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignore = []
        self.text = []
        self.canonical = None
        self.published_at = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")
        if tag == "meta" and attrs.get("property") == "article:published_time":
            self.published_at = attrs.get("content")
        if tag in self.IGNORED:
            self.ignore.append(tag)
        if not self.ignore and tag in self.BLOCKS:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if self.ignore and tag == self.ignore[-1]:
            self.ignore.pop()
        if not self.ignore and tag in self.BLOCKS:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.ignore:
            self.text.append(data)


def parse_document(text):
    parser = PublicHTML()
    parser.feed(text[:800_000])
    body = "\n".join(line.strip() for line in "".join(parser.text).splitlines() if line.strip())
    return body, parser.canonical, parser.published_at


def parse_html(text):
    return parse_document(text)[0]


def group_sources(materials):
    parents = list(range(len(materials)))
    index = {}

    def root(i):
        while parents[i] != i:
            i = parents[i]
        return i

    for i, material in enumerate(materials):
        urls = [public_url(link) for link in (material.url, material.canonical_url, material.origin_url)]
        for key in [f"hash:{material.content_hash}", *(f"url:{link}" for link in urls if link)]:
            if key in index:
                parents[root(i)] = root(index[key])
            else:
                index[key] = i
    return ["source-"+digest(materials[root(i)].url)[:16] for i in range(len(materials))]


class EvidenceCache:
    """In-memory cache. Private material requires an owner key."""
    def __init__(self):
        self._entries = {}

    def get(self, url, *, owner=None):
        return self._entries.get((owner,public_url(url)))

    def put(self, material, expires_at, *, owner=None):
        expiry = datetime.fromisoformat(expires_at)
        if expiry.utcoffset() is None:
            raise ValueError("缓存时间缺少时区")
        self._entries[(owner,public_url(material.url))] = (material, expires_at)


def date_value(match):
    try:
        return date(*(int(part) for part in match[:3])).isoformat()
    except ValueError:
        return None


def effective_period(sentence):
    # Publication labels are excluded even when an adjacent sentence names the venue.
    sentence = re.sub(r"(?:发布日期|发布时间|发布于|发稿时间)\s*[:：]?\s*"+DATE, "", sentence)
    matches = list(re.finditer(DATE,sentence))
    if not matches:
        return None, None
    first = matches[0]
    start = date_value(first.groups())
    following = sentence[first.end():]
    if len(matches) == 2 and re.match(r"\s*(?:起?\s*[至到~～–—-]|起，至)", following):
        end = date_value(matches[1].groups())
        return (start,end) if start and end and start<=end else (None,None)
    if len(matches) != 1 or not start:
        return None, None
    if re.search(r"(?:自|从|生效日期[:：]?)\s*$", sentence[:first.start()]) or re.match(r"\s*起",following):
        return start,None
    return start,start


def extract_fact(material, entity, kind, direction, target):
    """Only use a clause that itself contains the object, rule and effective date."""
    facts = []
    for sentence in re.split(r"[。！？；\n]+",material.text):
        if (entity.label not in sentence or (direction and direction not in sentence)
                or INJECTION.search(sentence) or INDEFINITE.search(sentence) or len(sentence)>400):
            continue
        start, end = effective_period(sentence)
        value = None
        if kind == "opening":
            if re.search(r"暂停开放|停止开放|不开放|关闭|闭馆",sentence):
                value = "closed"
            elif re.search(r"(?:正常开放|对外开放|开放|营业)(?:[，,\s]|$)|开放时间\s*(?:为|[:：])\s*\d{1,2}:\d{2}", sentence):
                value = "open"
            elif re.match(re.escape(entity.label) + r"\s*[:：]", sentence.strip()) and re.search(
                    r"周[一二三四五六日天].*?\d{1,2}:\d{2}\s*(?:[—–至~-])+\s*\d{1,2}:\d{2}", sentence):
                # A separate schedule row establishes that the document lists hours.
                # Its scope stays unknown unless this same clause states effective dates.
                value = "published_opening_hours"
        elif kind == "booking":
            if re.search(r"无需预约|不需预约|免预约",sentence):
                value = "not_required"
            elif re.search(r"须预约|需预约|需要预约|实行(?:分时|实名制)?预约",sentence):
                value = "required"
        elif kind == "ticket_price":
            found = re.search(r"(?:成人门票|普通成人票|票价)\s*(?:为|[:：])?\s*(\d{1,5})(?:\.(\d{1,2}))?\s*元",sentence)
            if found:
                value = {"amount_fen":int(found[1])*100+int((found[2] or "").ljust(2,"0")), "currency":"CNY"}
        elif kind == "last_service":
            found = re.search(r"末班(?:车)?\s*(?:为|[:：])?\s*((?:[01]\d|2[0-3]):[0-5]\d)", sentence)
            if found:
                value = found[1]
        if value is not None:
            facts.append({"effective_from":start,"effective_to":end,"value":value,"excerpt":sentence.strip(),
                          "applicable":bool(start and start <= target and (end is None or target <= end))})
    applicable = [fact for fact in facts if fact["applicable"]]
    return applicable[:6] or facts[:1] or [{"effective_from":None,"effective_to":None,"value":None,
                                          "excerpt":"","applicable":False}]



class EvidenceService:
    def __init__(self, catalog, *, clock, reader=None, search=None, parallel_search=None, parallel_fetch=None,
                 kb=None, notice_reader=None, notice_auth_required=False, notice_credentials_available=False,
                 cache=None, timeout=45, max_reads=3):
        self.catalog = catalog
        self.clock = clock
        self.reader = reader
        self.search = search
        self.parallel_search = parallel_search
        self.parallel_fetch = parallel_fetch
        self.kb = kb
        self.notice_reader = notice_reader
        self.notice_auth_required = notice_auth_required
        self.notice_credentials_available = notice_credentials_available
        self.cache = cache if cache is not None else EvidenceCache()
        self.timeout = timeout
        self.max_reads = max_reads

    async def discover_sources(self, subject, *, query=None):
        if self.search is None:
            return {"status":"unbound","sources":[],"attempts":[]}
        return await self.search(query or subject,subject,limit=3)

    async def verify_claim(self, subject_id, claim_type, target_date, *, line_direction=None, known_urls=(),
                           provider="default", owner=None, trace=None):
        entity = self.catalog.get(subject_id)
        if entity is None or claim_type not in CLAIM_TYPES or provider not in {"default","parallel","kb","notice"}:
            raise ValueError("核验对象、类型或来源未声明")
        day = date.fromisoformat(target_date).isoformat()
        direction = entity.directions.get(line_direction) if line_direction else None
        now = self.clock()
        cutoff = asyncio.get_running_loop().time() + self.timeout
        stages, candidates, originals = [], [], []
        tool_calls = 0

        def event(stage,tool,status,**details):
            row = dict(stage=stage,tool=tool,status=status,**details)
            stages.append(row)
            if trace is not None and hasattr(trace,"record"):
                trace.record("evidence_stage",**row)

        async def tool(fn,args,stage,name):
            nonlocal tool_calls
            if fn is None:
                event(stage,name,"unbound")
                return None
            remaining = cutoff-asyncio.get_running_loop().time()
            if remaining <= 0:
                event(stage,name,"timeout",attempted=False)
                return None
            tool_calls += 1
            try:
                result = await asyncio.wait_for(fn(*args),timeout=min(12,remaining))
                if not isinstance(result,ToolResult):
                    result = ToolResult("invalid_response")
            except TimeoutError:
                result = ToolResult("timeout")
            except (TypeError,ValueError):
                result = ToolResult("invalid_response")
            except (OSError,RuntimeError):
                result = ToolResult("provider_error")
            event(stage,name,result.status,provider=result.provider,attempted=True)
            return result

        urls = list(dict.fromkeys(link for raw in known_urls if (link:=public_url(raw))))
        uncertainties = []
        explicit_unbound = False
        if (line_direction and not direction) or (claim_type=="last_service" and not direction):
            uncertainties.append("请确认要核对的线路方向？")
            explicit_unbound = True
        elif provider == "notice":
            if self.notice_reader is None:
                event("registry","notice.lookup","unbound")
                explicit_unbound = True
            elif self.notice_auth_required and not self.notice_credentials_available:
                event("read","notice.lookup","auth_failed",attempted=False)
                explicit_unbound = True
            else:
                ret = await tool(self.notice_reader,(entity.id,claim_type,day,direction),"read","notice.lookup")
                if ret and isinstance(ret.data,Material):
                    originals.append((ret.data,ret.provider,"current",None))
                explicit_unbound = True
        elif provider == "kb":
            ret = await tool(self.kb,({"query":entity.label,"poi_ids":[entity.id],"trip_dates":[day,day],"k":3},),"discover","retrieve")
            if ret and ret.status=="success" and isinstance(ret.data,dict) and isinstance(ret.data.get("hits"),list):
                rows = ret.data["hits"][:3]
                candidates.extend({"kind":"summary_candidate","url":public_url(row.get("source_url")),
                                   "title":str(row.get("title",""))[:120],"provider":"kb",
                                   "data_mode":ret.data.get("data_mode","snapshot")}
                                  for row in rows if isinstance(row,dict))
                urls.extend(c["url"] for c in candidates if c["url"] and c["data_mode"]!="fixture")
        elif not urls:
            query = " ".join(filter(None,(entity.label,direction,day,{"opening":"开放公告","booking":"预约公告",
                             "ticket_price":"成人门票公告","last_service":"末班车公告"}[claim_type])))
            rows = []
            if provider == "parallel":
                ret = await tool(self.parallel_search,(query,),"discover","web_search")
                if ret and ret.status=="success" and isinstance(ret.data,list):
                    rows = [{**row,"provider":"parallel"} for row in ret.data if isinstance(row,dict)]
            else:
                try:
                    remaining = cutoff-asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError
                    bundle = await asyncio.wait_for(self.discover_sources(entity.label,query=query),timeout=remaining)
                    for attempt in bundle["attempts"]:
                        tool_calls += 1
                        reason = attempt.get("reason")
                        normal = {"timeout":"timeout","http_429":"rate_limited","http_401":"auth_failed",
                                  "http_402":"quota_exhausted","missing_api_key":"auth_failed"}
                        status = {"matched":"success","no_match":"empty"}.get(attempt["status"],normal.get(reason,"provider_error"))
                        event("discover","search_travel_sources",status,provider=attempt["provider"],reason=reason)
                    rows = bundle["sources"]
                    if bundle["status"] == "unbound":
                        event("registry","search_travel_sources","unbound")
                except TimeoutError:
                    event("discover","search_travel_sources","timeout")
            for row in rows[:6]:
                url = public_url(row.get("url"))
                if url:
                    candidates.append({"kind":"summary_candidate","url":url,"title":str(row.get("title",""))[:120],
                                       "provider":row.get("provider","unknown"),"published_at":row.get("published_at"),
                                       "excerpt":str(row.get("excerpt",""))[:200]})
                    urls.append(url)

        if not explicit_unbound:
            for url in list(dict.fromkeys(urls))[:self.max_reads]:
                cached = self.cache.get(url,owner=owner)
                if cached is not None and now < datetime.fromisoformat(cached[1]):
                    originals.append((cached[0],"cache","current",cached[1]))
                    event("read","original_cache","success",attempted=False)
                    continue
                fn,name = (self.parallel_fetch,"web_fetch") if provider == "parallel" else (self.reader,"read_original")
                ret = await tool(fn,(url,),"read",name)
                if ret and ret.status=="success" and isinstance(ret.data,Material) and url in {public_url(ret.data.url),public_url(ret.data.origin_url)}:
                    collected = datetime.fromisoformat(ret.data.retrieved_at)
                    expiry = (collected + timedelta(minutes=30)).isoformat()
                    self.cache.put(ret.data,expiry,owner=owner)
                    freshness = "current" if collected <= self.clock() < datetime.fromisoformat(expiry) else "expired"
                    originals.append((ret.data,ret.provider,freshness,expiry))
                elif cached is not None:
                    originals.append((cached[0],"cache","expired",cached[1]))
                    uncertainties.append("原文缓存过期，刷新未成功。")

        groups = group_sources([row[0] for row in originals])
        evidence, supporting, values = [], [], {}
        for (material, origin, freshness, expiry), group in zip(originals,groups):
            facts_found = extract_fact(material,entity,claim_type,direction,day)
            trusted = urlsplit(material.url).hostname in entity.authority_hosts and material.source_kind in {"official","operator"}
            for facts in facts_found:
                excerpt = facts["excerpt"]
                identifier = "e-"+digest(material.url+" "+material.content_hash+" "+excerpt)[:18]
                if any(row["evidence_id"] == identifier for row in evidence):
                    continue
                row = dict(evidence_id=identifier,source_url=material.url,publisher=urlsplit(material.url).hostname,
                           source_group_id=group,retrieved_at=material.retrieved_at,published_at=material.published_at,
                           effective_from=facts["effective_from"],effective_to=facts["effective_to"],
                           source_kind=material.source_kind,data_mode=material.data_mode,content_hash=material.content_hash,
                           excerpt=excerpt,provider=origin,freshness=freshness,expires_at=expiry)
                evidence.append(row)
                applies = facts["applicable"] and trusted and freshness=="current"
                event("extract","fact_extractor","success" if facts["value"] is not None else "empty",
                      evidence_id=identifier)
                event("applicability","check_applicability","success",evidence_id=identifier,applicable=bool(applies),
                      confidence_basis="official_original" if trusted else "unverified")
                if applies:
                    supporting.append(row)
                    values[identifier] = facts["value"]
        live = [row for row in supporting if row["data_mode"]!="fixture"]
        if live:
            supporting = live
            values = {row["evidence_id"]:values[row["evidence_id"]] for row in live}
        distinct = {repr(value) for value in values.values()}
        outcome = "unconfirmed"
        simulation = None
        if len(distinct)>1:
            outcome = "conflicted"
        elif live:
            outcome = "contradicted" if next(iter(values.values()))=="closed" else "confirmed"
        elif supporting:
            simulation = "contradicted" if next(iter(values.values()))=="closed" else "confirmed"
        if not live or outcome=="conflicted":
            uncertainties.append("请以覆盖目标日的正式原文核实，当前材料不足以确认。")
        claim = dict(claim_id="c-"+digest(f"{subject_id}/{claim_type}/{day}/{line_direction}")[:20],
                     subject_id=subject_id,target_date=day,line_direction=line_direction,claim_type=claim_type,
                     status=outcome,checked_at=now.isoformat(),uncertainties=list(dict.fromkeys(uncertainties)),
                     evidence_ids=list(values),independent_source_count=len({r["source_group_id"] for r in supporting}),
                     simulation_status=simulation,confidence_basis="official_original" if live else "unconfirmed",
                     value=next(iter(values.values())) if live and len(distinct)==1 else None)
        report = dict(claim=claim,evidence=evidence,summary_candidates=candidates,official_links=list(entity.links),
                      stages=stages,tool_calls=tool_calls,write_count=0)
        return check(VERIFY_REPORT, report)


def import_legacy_evidence(evidence_id, url=None):
    return dict(evidence_id=evidence_id,source_url=public_url(url),retrieved_at=None,effective_from=None,
                effective_to=None,expires_at=None,status="legacy_unverified")

