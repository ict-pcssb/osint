"""Bind M10 only to the registered read-only providers already used by M5/M7/M8."""
import os
import re

from travel_candidates import HotelAdapter
from travel_candidates_live import RemoteTool, setting
from travel_catalog import public_catalog
from travel_contract import FORMATS
from travel_evidence import EvidenceService
from travel_evidence_reader import PublicOriginalReader
from travel_recheck_facts import TTL
from travel_rules import format_time, instant
from travel_search_mcp import BEFORE_SEARCH_ATTEMPT, search_travel_sources
from travel_state import BEIJING
from travel_transport import WeatherAdapter
from travel_transport_live import node_tool


def observation(kind, value, source, day, *, until=None, valid_from=None, valid_to=None):
    collected = source.get("collected_at") or source["retrieved_at"]
    original,_ = instant(collected)
    expiry,_ = instant(until)
    expiry = min(expiry,original+TTL[kind]) if expiry else original+TTL[kind]
    published = source.get("published_at")
    if published and (match:=re.fullmatch(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?",published)):
        published = f"{int(match[1]):04d}-{int(match[2]):02d}-{int(match[3]):02d}"
    if published and not any(FORMATS.conforms(published,fmt) for fmt in ("date","date-time")):
        published = None
    return {"status":"success","observation":dict(
        value=value,collected_at=collected,valid_from=valid_from or day,valid_to=valid_to if valid_from else day,
        valid_until=format_time(expiry),published_at=published,origin_id=source["evidence_id"],
        source_url=source["source_url"],excerpt=source["excerpt"],data_mode=source["data_mode"])}


def configured_providers(clock, *, weather_entry=None, enable_notice=None, enable_hotel=None):
    if weather_entry is None and os.getenv("TRAVEL_M10_NETWORK")!="1":
        return {}
    clock = lambda source=clock: source().astimezone(BEIJING)
    catalog = public_catalog()
    weather_entry = weather_entry or os.getenv("TRAVEL_M07_WEATHER_ENTRY")
    weather_tool = node_tool("open-meteo",weather_entry,authorized=True)
    enable_notice = os.getenv("TRAVEL_M10_NOTICES")=="1" if enable_notice is None else enable_notice
    enable_hotel = os.getenv("TRAVEL_M10_HOTEL")=="1" if enable_hotel is None else enable_hotel
    hotel_key = setting("ROLLINGGO_HOTEL_API_KEY") if enable_hotel else ""
    hotel_tool = RemoteTool("rollinggo",hotel_key) if hotel_key else None

    async def weather(fact,budget):
        async def invoke(name,args):
            budget.take("open-meteo")
            return await weather_tool(name,args)
        # Per-task adapter keeps M7's parsing and target-day checks without sharing account caches.
        adapter = WeatherAdapter(catalog,invoke,clock,mode="live")
        day = fact["target_date"]
        result = await adapter.query(fact["entity"],day)
        if result.status!="success":
            return {"status":result.reason if result.reason in {"out_of_window","unresolved_location"} else result.status}
        data = result.data
        value = dict(low_c=data["temperature_c"][0],high_c=data["temperature_c"][1],rain_pct=data["rain_probability_pct"])
        return observation("weather",value,data["evidence"][0],day)

    async def notice(fact,budget):
        entity = catalog.get(fact["entity"])
        if not entity or not entity.authority_hosts:
            return {"status":"unbound"}
        reader = PublicOriginalReader(entity.authority_hosts,clock=clock,enabled=True,
                                      before_request=lambda:budget.take("read_original"))

        async def search(query, subject, *, limit):
            return await search_travel_sources(query,subject,limit=limit,source_domains=list(entity.authority_hosts))

        day,kind = fact["target_date"],fact["kind"]
        service = EvidenceService(catalog,clock=clock,reader=reader,search=search,timeout=23,max_reads=2)
        marker = BEFORE_SEARCH_ATTEMPT.set(budget.take)
        try:
            report = await service.verify_claim(entity.id,kind,day,line_direction=fact["scope"].get("direction"))
        finally:
            BEFORE_SEARCH_ATTEMPT.reset(marker)
        claim = report["claim"]
        if claim["status"]=="conflicted":
            return {"status":"source_conflict"}
        rows = [r for r in report["evidence"] if r["evidence_id"] in claim["evidence_ids"]]
        if not rows or claim["value"] is None:
            reasons = [row["status"] for row in report["stages"]]
            if report["evidence"]:
                return {"status":"outside_effective_period"}
            return {"status":next((s for s in ("rate_limited","timeout","auth_failed","quota_exhausted","invalid_response","provider_error","unbound")
                                   if s in reasons),"empty")}
        row = max(rows,key=lambda r:r["retrieved_at"])
        return observation(kind,claim["value"],row,day,until=row["expires_at"],
                           valid_from=row["effective_from"],valid_to=row["effective_to"])

    async def quote(fact,budget):
        scope = fact["scope"]
        keys = ("place_id","check_in","nights","adults","rooms")
        if (not hotel_tool or scope.get("quote_type")!="hotel" or not fact["entity"].startswith("hotel-")
                or not all(key in scope for key in keys) or scope["check_in"]!=fact["target_date"]):
            return {"status":"unbound"}
        # A different hotel or an ambiguous room is not a replacement for the old quote.
        if not scope.get("room_name"):
            return {"status":"unbound"}

        async def invoke(name,args):
            budget.take("rollinggo")
            return await hotel_tool(name,args)

        adapter = HotelAdapter(invoke,clock,mode="live")
        packet = await adapter.query({**{key:scope[key] for key in keys},"children":scope.get("children",0),
                                      "child_ages":scope.get("child_ages",[])})
        card = packet["result"]
        if card["kind"]!="candidates":
            return {"status":card.get("reason","invalid_response")}
        option = next((r for r in card["options"] if r["id"]==fact["entity"] and r["room_name"]==scope["room_name"]),None)
        if not option or option["stay_total_fen"] is None or option["currency"]!="CNY":
            return {"status":"empty"}
        source = next((r for r in reversed(packet["evidence"]) if r["evidence_id"] in option["evidence_ids"]),None)
        return observation("quote",dict(amount_fen=option["stay_total_fen"],currency="CNY"),
                           source,fact["target_date"]) if source else {"status":"invalid_response"}

    providers = {}
    if weather_tool:
        providers["weather"] = weather
    if enable_notice:
        providers.update(opening=notice,booking=notice,last_service=notice)
    if hotel_tool:
        providers["quote"] = quote
    return providers
