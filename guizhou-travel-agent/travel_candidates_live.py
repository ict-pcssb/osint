"""Server-only binding of explicitly enabled read-only candidate providers."""
import asyncio
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
from urllib.parse import quote, urljoin, urlsplit

import httpx2
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from travel_candidates import CandidateSkills, FoodAdapter, FoodOriginal, HotelAdapter, TaxiAdapter, distance_between
from travel_catalog import public_catalog
from travel_evidence import Material, ToolResult, parse_document, public_url
from travel_evidence_reader import PublicOriginalReader
from travel_search_mcp import search_travel_sources
from travel_state import BEIJING
from travel_transport import ProviderFailure, event
from travel_transport_live import semantic_error, stdio

ROOT=Path(__file__).resolve().parent
ALLOWED={"amap-taxi":{"get_taxi_service"},"rollinggo":{"searchHotels","getHotelDetail"}}
PUBLIC_MEDIA={"english.guiyang.gov.cn","www.gzstv.com","www.gzculture.net","www.thepaper.cn",
              "m.thepaper.cn","www.yp.gov.cn","www.chinawriter.com.cn","www.mct.gov.cn"}
CONFIG_KEYS={"AMAP_KEY","ROLLINGGO_HOTEL_API_KEY","ANYSEARCH_API_KEY","EXA_API_KEY","TRAVEL_M08_OSM_ENTRY"}


def setting(name):
    if name not in CONFIG_KEYS:
        raise ValueError("配置名未登记")
    value=os.environ.get(name)
    if value is not None:
        return value.strip()
    path=ROOT/".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if match:=re.fullmatch(r"\s*(?:export\s+)?"+name+r"\s*=\s*(.*?)\s*",line):
                return match[1].strip().strip('"').strip("'")
    return ""


def classify_response(value):
    return {401:"auth_failed",403:"auth_failed",402:"quota_exhausted",429:"rate_limited","quota":"quota_exhausted",
            "bad_json":"invalid_response","empty":"empty","timeout":"timeout"}.get(value,"provider_error")


def failure_from(error):
    if isinstance(error,BaseExceptionGroup):
        for member in error.exceptions:
            mapped=failure_from(member)
            if mapped is not None:
                return mapped
        return None
    if isinstance(error,ProviderFailure):
        return error
    if isinstance(error,httpx2.HTTPStatusError):
        code=error.response.status_code
        return ProviderFailure(classify_response(code),f"http_{code}")
    if isinstance(error,(TimeoutError,httpx2.TimeoutException)):
        return ProviderFailure("timeout")
    if isinstance(error,httpx2.TransportError):
        reason="transport_error"
        summary=str(error).casefold()
        if any(token in summary for token in ("ssl","tls","certificate","handshake")):
            reason="tls_failed"
        return ProviderFailure("provider_error",reason)
    return None


def response_payload(result,vendor):
    if result.is_error:
        text=" ".join(getattr(item,"text","") for item in result.content)
        if re.search(r"额度|quota|balance|次数.*用完",text,re.I):
            raise ProviderFailure("quota_exhausted")
        raise semantic_error(text)
    parsed=result.structured_content
    if isinstance(parsed,dict) and set(parsed)=={"result"}:
        parsed=parsed["result"]
    if isinstance(parsed,dict):
        expected={"entities"} if vendor=="amap-taxi" else {"hotelInformationList","roomRatePlans","success","code"}
        if expected.intersection(parsed):
            return parsed
    chunks=[]
    for item in result.content:
        if item.type=="text":
            try:
                value=json.loads(item.text)
            except (ValueError,TypeError):
                continue
            if isinstance(value,dict):
                if vendor=="amap-taxi" and "entities" in value:
                    return value
                if vendor=="rollinggo" and {"hotelInformationList","roomRatePlans","success","code"}.intersection(value):
                    return value
                chunks.append(value)
    if chunks and vendor=="amap-taxi":
        return chunks
    raise ProviderFailure("invalid_response","bad_json")


class RemoteTool:
    def __init__(self,vendor,key,*,trace=None):
        if vendor not in ALLOWED:
            raise ValueError("未注册候选供应商")
        self.vendor,self.key,self.trace=vendor,key,trace
        self.counts={"initialize_attempts":0,"initialize_success":0,"list_attempts":0,"list_success":0,"business_attempts":0}
        self.last_status=None

    async def __call__(self,name,args):
        if name not in ALLOWED[self.vendor]:
            raise ProviderFailure("unbound")
        if not self.key:
            raise ProviderFailure("unbound","missing_server_key")
        if self.vendor=="amap-taxi":
            url="https://mcp.amap.com/rest/amap_car/dev/mcp?key="+quote(self.key,safe="")
            headers={}
        else:
            url="https://mcp.rollinggo.cn/mcp"
            headers={"Authorization":"Bearer "+self.key}
        try:
            async with asyncio.timeout(19):
                async with httpx2.AsyncClient(headers=headers,follow_redirects=False) as client:
                    async with streamable_http_client(url,http_client=client,terminate_on_close=False) as (r,w):
                        async with ClientSession(r,w) as session:
                            self.counts["initialize_attempts"]+=1
                            await session.initialize()
                            self.counts["initialize_success"]+=1
                            self.counts["list_attempts"]+=1
                            listing=await session.list_tools()
                            self.counts["list_success"]+=1
                            spec=next((tool for tool in listing.tools if tool.name==name),None)
                            if spec is None:
                                raise ProviderFailure("unbound","tool_not_listed")
                            if not Draft202012Validator(spec.input_schema).is_valid(args):
                                raise ProviderFailure("invalid_response","provider_schema_mismatch")
                            self.counts["business_attempts"]+=1
                            raw=await session.call_tool(name,args)
                            payload=response_payload(raw,self.vendor)
                            self.last_status=("success",None)
                            return payload
        except (ValueError,OSError,RuntimeError,httpx2.HTTPError,ExceptionGroup) as error:
            mapped=failure_from(error) or ProviderFailure("provider_error","connection_error")
            self.last_status=(mapped.status,mapped.reason)
            raise mapped from None
        finally:
            event(self.trace,"m08_remote_protocol",provider=self.vendor,tool=name,counts=self.counts.copy())


def osm_binding(entry,*,trace=None):
    if not entry:
        return None
    path=Path(entry)
    if not path.is_file() or path.parts[-3:]!=("osm-mcp","dist","index.js"):
        return None
    return stdio("osm","node",[str(path.resolve())],authorized=True,timeout=14,
                 additional_read_only={"find_nearby_pois"})


async def get_public(url, *, params=None, limit=1_500_000):
    async with asyncio.timeout(11):
        async with httpx2.AsyncClient(follow_redirects=False,headers={"User-Agent":"GuizhouTravel/1.0 read-only"}) as client:
            async with client.stream("GET",url,params=params) as response:
                if response.status_code!=200:
                    raise ProviderFailure(classify_response(response.status_code),f"http_{response.status_code}")
                payload=bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload)>limit:
                        raise ProviderFailure("invalid_response","oversize_response")
    return bytes(payload)


async def osm_map_read(tool,args):
    """Bounded fallback against the official OSM map download API when Overpass is unavailable."""
    if tool!="find_nearby_pois" or args.get("category")!="amenity=restaurant":
        raise ProviderFailure("unbound")
    try:
        lat,lon=map(float,args["near"].split(","))
        radius=args["radius_m"]
        if not (-85<=lat<=85 and -179<=lon<=179 and type(radius) is int and 50<=radius<=700):
            raise ValueError
    except (KeyError,ValueError,TypeError) as exc:
        raise ProviderFailure("invalid_response","invalid_map_area") from exc
    import math
    dy=radius/110950
    dx=dy/math.cos(math.radians(lat))
    bbox=",".join(f"{axis:.7f}" for axis in (lon-dx,lat-dy,lon+dx,lat+dy))
    try:
        response=await get_public("https://api.openstreetmap.org/api/0.6/map.json",params={"bbox":bbox})
        data=json.loads(response)
        elements=data["elements"]
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,ProviderFailure):
            raise
        raise ProviderFailure("invalid_response","bad_map_json") from exc
    nodes={row["id"]:(row["lat"],row["lon"]) for row in elements if row["type"]=="node"}
    result=[]
    for row in elements:
        meta=row.get("tags",{})
        name=meta.get("name:zh") or meta.get("name")
        if meta.get("amenity")!="restaurant" or not name:
            continue
        if row["type"]=="node":
            point=nodes.get(row["id"])
        elif row["type"]=="way":
            corners=[nodes[key] for key in row.get("nodes",[]) if key in nodes]
            point=tuple(sum(axis)/len(corners) for axis in zip(*corners)) if corners else None
        else:
            continue
        if point and distance_between(lat,lon,*point)<=radius:
            result.append(dict(id=f"{row['type']}/{row['id']}",name=name,lat=point[0],lon=point[1],
                               address=" ".join(meta.get(k,"") for k in ("addr:street","addr:housenumber")).strip(),
                               opening_hours=meta.get("opening_hours")))
    result.sort(key=lambda row:distance_between(lat,lon,row["lat"],row["lon"]))
    return {"source":"osm-map-v0.6","bbox":bbox,"results":result[:args["limit"]]}


def material_visit(item,food):
    author=re.search(r"(?:作者|撰文)\s*[：:]\s*([^\s，。；：]{1,30})",item.text)
    match=re.search(r"(20\d{2})[年\-/](\d{1,2})[月\-/](\d{1,2})日?[^。！？\n]{0,25}(?:我|我们)(?:到访|在|去过|去)[^。！？\n]{0,45}"+re.escape(food),item.text)
    day=None
    if author and match:
        try:
            day=date(*(int(v) for v in match.groups()))
        except ValueError:
            pass
    return FoodOriginal(item.url,item.text,datetime.fromisoformat(item.retrieved_at),item.canonical_url,
                        author=author[1] if day else None,visit_date=day)


async def known_original(url,clock):
    # Only the exact public URLs selected by the operator reach this binding.
    # Provider TLS and same-host validation remain active.
    try:
        raw=await get_public(url,limit=850_000)
        text,canonical,published=parse_document(raw.decode("utf-8",errors="replace"))
        canonical=public_url(urljoin(url,canonical)) if canonical else None
        if len(text)<100:
            return ToolResult("invalid_response")
        return ToolResult("success",Material(url,text,clock().isoformat(),"live",published_at=published,
                                             canonical_url=canonical,source_kind="other"))
    except (OSError,ValueError,TimeoutError,httpx2.HTTPError) as exc:
        status=exc.status if isinstance(exc,ProviderFailure) else "timeout" if isinstance(exc,TimeoutError) else "provider_error"
        return ToolResult(status)


class Originals:
    def __init__(self,clock,*,known=(),discover=True):
        self.clock,self.known,self.discover=clock,tuple(known),discover
        self.reader=PublicOriginalReader(PUBLIC_MEDIA,clock=clock,enabled=True)
        self.cache={}

    async def __call__(self,food,place,meal,trace=None):
        urls=list(self.known)
        if self.discover and not urls:
            try:
                found=await asyncio.wait_for(search_travel_sources(f"{place} {food} 餐饮 到访",
                                                      subject=food,limit=2,source_domains=sorted(PUBLIC_MEDIA)),timeout=5)
                urls.extend(item["url"] for item in found.get("sources",[]))
            except (ValueError,TimeoutError,RuntimeError,OSError):
                pass
        results=[]
        for url in dict.fromkeys(url for url in urls if public_url(url) and urlsplit(url).hostname in PUBLIC_MEDIA):
            if len(results)>=2:
                break
            if url not in self.cache:
                data=await (known_original(url,self.clock) if url in self.known else self.reader(url))
                self.cache[url]=data
                event(trace,"m08_public_original",url=url,status=data.status,call_count=1)
            record=self.cache[url]
            if record.status=="success":
                item=record.data
                results.append(material_visit(item,food))
        return results


def hotel_route(osm):
    catalog=public_catalog()
    async def route(_hotel,target,row):
        hotel=row.get("coordinates")
        destination=catalog.get(target)
        if not isinstance(hotel,dict) or hotel.get("system")!="WGS84" or not destination or not destination.coordinates:
            return None
        first=(hotel.get("latitude"),hotel.get("longitude"))
        if (not all(type(v) in (int,float) for v in first)
                or not -90<=first[0]<=90 or not -180<=first[1]<=180):
            return None
        final=destination.coordinates
        result=await osm("route",dict(profile="car",waypoints=[f"{first[0]},{first[1]}",
                                f"{final['latitude']},{final['longitude']}"]))
        seconds=result.get("duration_s") if isinstance(result,dict) and result.get("profile")=="car" else None
        if type(seconds) is int and seconds>0:
            return dict(to_id=target,travel_min=(seconds+59)//60,status="estimate",source_data=result)
        return None
    return route


def configured_skills(clock=None):
    if os.getenv("TRAVEL_M08_NETWORK")!="1":
        return None
    clock=clock or (lambda:datetime.now(BEIJING))
    taxi=setting("AMAP_KEY") if os.getenv("TRAVEL_M08_AMAP")=="1" else ""
    hotel=setting("ROLLINGGO_HOTEL_API_KEY") if os.getenv("TRAVEL_M08_HOTEL")=="1" else ""
    osm=osm_binding(setting("TRAVEL_M08_OSM_ENTRY"))
    food_poi=(osm or osm_map_read) if os.getenv("TRAVEL_M08_FOOD")=="1" else None
    return CandidateSkills(clock,mode="live",
                           taxi=TaxiAdapter(RemoteTool("amap-taxi",taxi),clock,mode="live") if taxi else None,
                           hotel=HotelAdapter(RemoteTool("rollinggo",hotel),clock,mode="live",route=hotel_route(osm) if osm else None) if hotel else None,
                           food=FoodAdapter(food_poi,Originals(clock),clock,mode="live") if food_poi else None)

