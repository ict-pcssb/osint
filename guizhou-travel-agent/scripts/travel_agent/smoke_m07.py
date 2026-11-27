"""Run explicitly authorized, read-only M7 provider checks and record business results."""
import argparse
import asyncio
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from travel_agent import Conversation
from travel_catalog import public_catalog
from travel_state import BEIJING
from travel_transport import FlightAdapter, RailAdapter, WeatherAdapter
from travel_transport_live import backup_flights, node_tool, primary_flights
from travel_transport_skills import TransportSkills, transport_card, weather_card

PROVIDERS=("weather","rail","flight-primary","flight-backup")
ALL_PROVIDERS=(*PROVIDERS,"transfer")


def business_check(service, provider, day, now, outcome):
    if provider=="weather":
        packet=weather_card(outcome,service.catalog.get("qingyan"),day,now,"live")
        result=packet["result"]
        passed=(result.get("kind")=="weather" and result["place"]["poi_id"]=="qingyan"
                and result["target_date"]==day and result.get("as_of") and
                result["forecast"]["units"]=={"temperature":"°C","probability":"%"})
    else:
        rail=provider=="rail"
        first,last=("guiyang-north","zunyi") if rail else ("pvg","kwe")
        packet=transport_card(outcome,"train" if rail else "flight",
                              dict(date=day,from_id=first,to_id=last,adults=2),now,"live")
        result=packet["result"]
        passed=(result.get("kind")=="candidates" and result["basis"]["date"]==day
                and result["basis"]["from_id"]==first and result["basis"]["to_id"]==last
                and result["currency"]=="CNY" and result["booking_status"]=="not_booked"
                and all(o["depart_at"][:10]==day and o["arrive_at"]>o["depart_at"] for o in result["options"]))
    passed = bool(passed and packet["evidence"] and all(day in row["applies_to"] for row in packet["evidence"])
                  and all(row["data_mode"]=="live" for row in packet["evidence"]) and packet["persistence"]=="none")
    return passed,packet


async def check_transfer(catalog, route, day, clock, trace):
    fields=dict(from_id="tongren",to_id="fanjing-east",date=day,
                arrival_at=f"{day}T14:30:00+08:00",mode="bus")

    async def intent(*_):
        return json.dumps(dict(intent="transfer",confidence_band="high",
                               requested_outputs=["transfer"],parameters=fields))

    skills=TransportSkills(catalog,clock,mode="live",route=route)
    conversation=Conversation(typed_output=True,clock=clock,intent_model=intent,transport_skills=skills)
    packet=await conversation.ask("从铜仁站怎么去梵净山东门？",trace)
    card=packet["result"]
    applicable=[row for row in packet["evidence"] if row["provider"]=="osm"]
    passed=(packet["run_status"]=="completed" and packet["persistence"]=="none"
            and card["kind"]=="transfer" and card["data_mode"]=="live"
            and (card["from_id"],card["to_id"],card["date"])==("tongren","fanjing-east",day)
            and any(row["mode"]=="car" and row["status"]=="estimate" and row["travel_min"]>0
                    for row in card["segments"])
            and card["last_service"]["state"]=="unknown" and card["feasibility"]=="needs_verification"
            and len(applicable)==1 and applicable[0]["data_mode"]=="live"
            and day in applicable[0]["applies_to"])
    return passed,packet


async def run(args):
    now=datetime.now(BEIJING)
    day=(args.day or (now.date()+timedelta(days=1))).isoformat()
    trace_id="m07-live-"+uuid4().hex
    cases=[]
    selected=tuple(dict.fromkeys(args.provider or PROVIDERS))
    if args.read_only:
        catalog=public_catalog()
        live={}
        if "weather" in selected:
            live["weather"]=node_tool("open-meteo",args.weather_entry,authorized=True)
        if "rail" in selected:
            live["rail"]=node_tool("12306",args.rail_entry,authorized=True)
        if "flight-primary" in selected:
            live["flight-primary"]=primary_flights(authorized=True,timeout=25)
        if "flight-backup" in selected:
            live["flight-backup"]=backup_flights(args.backup,authorized=True,timeout=25)
        if "transfer" in selected:
            live["transfer"]=node_tool("osm",args.osm_entry,authorized=True)
        for provider,call in live.items():
            local_trace=[]
            if call is None:
                cases.append(dict(provider=provider,status="BLOCKED",reason="launch_path_unavailable",tool_calls=0))
                continue
            clock=lambda:datetime.now(BEIJING)
            if provider=="transfer":
                passed,packet=await check_transfer(catalog,call,day,clock,local_trace)
                provider_events=[row for row in local_trace if row["event"]=="m07_provider"]
                status=provider_events[0].get("status","success") if provider_events else "unbound"
                rule=next((row for row in local_trace if row["event"]=="m07_transfer_rules"),{})
                if status=="success":
                    status=rule.get("route_status","invalid_response")
                verdict="PASS" if passed else "FAIL" if status in {"success","invalid_response"} else "BLOCKED"
                cases.append(dict(provider=provider,status=verdict,
                                  reason="verified_map_estimate_and_unknown_last_service" if passed else status,
                                  tool_result=status,tool_calls=len(provider_events),trace=local_trace,packet=packet))
                continue
            if provider=="weather":
                service=WeatherAdapter(catalog,call,clock,mode="live")
                result=await service.query("qingyan",day,trace=local_trace)
            elif provider=="rail":
                service=RailAdapter(catalog,call,clock,mode="live")
                result=await service.query("guiyang-north","zunyi",day,adults=2,trace=local_trace)
            else:
                backup=provider=="flight-backup"
                service=FlightAdapter(catalog,None if backup else call,call if backup else None,clock,
                                      mode="live",allow_backup=backup)
                result=await service.query("pvg","kwe",day,adults=2,trace=local_trace,backup_only=backup)
            if result.status=="success":
                passed,packet=business_check(service,provider,day,clock(),result)
                verdict="PASS" if passed else "FAIL"
                reason="verified_read_only_fields" if passed else "business_contract_mismatch"
            else:
                packet=None
                verdict="FAIL" if result.status=="invalid_response" else "BLOCKED"
                reason=result.reason or result.status
            calls=sum(row["event"]=="m07_provider" for row in local_trace)
            cases.append(dict(provider=provider,status=verdict,reason=reason,tool_result=result.status,
                              tool_calls=calls,trace=local_trace,packet=packet))
    for provider in selected:
        if not any(row["provider"]==provider for row in cases):
            cases.append(dict(provider=provider,status="BLOCKED",tool_calls=0,
                              reason="read_only_authorization_pending" if not args.read_only else "not_run_in_this_invocation"))
    report=dict(trace_id=trace_id,data_mode="live",target_date=day,checked_at=now.isoformat(),write_count=0,
                authorization="explicit_read_only" if args.read_only else "pending",cases=cases)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps([dict(provider=c["provider"],status=c["status"],reason=c["reason"]) for c in cases],ensure_ascii=False))
    return 0 if all(row["status"]=="PASS" for row in cases) else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only",action="store_true",help="Only set following explicit approval of this live run")
    parser.add_argument("--day",type=date.fromisoformat)
    parser.add_argument("--provider",action="append",choices=ALL_PROVIDERS)
    parser.add_argument("--weather-entry")
    parser.add_argument("--rail-entry")
    parser.add_argument("--backup")
    parser.add_argument("--osm-entry")
    parser.add_argument("--output",type=Path,required=True)
    return asyncio.run(run(parser.parse_args()))


if __name__=="__main__":
    raise SystemExit(main())

