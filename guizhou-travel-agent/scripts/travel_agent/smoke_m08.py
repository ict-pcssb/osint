"""M8 real read-only checks. Taxi continuation requires a manual pickup selection."""
import argparse
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from travel_agent import Conversation
from travel_candidates import CandidateSkills, FoodAdapter, HotelAdapter, TaxiAdapter
from travel_candidates_live import Originals, RemoteTool, osm_binding, osm_map_read, setting
from travel_evidence import digest
from travel_state import BEIJING
from travel_state_app import TravelApplication


def redacted(packet):
    gen=deepcopy(packet)
    data=gen.get("result",{})
    if "tool_context_id" in data:
        data["tool_context_hash"]=digest(data.pop("tool_context_id"))
        for option in data["options"]:
            option["id"]=digest(option["id"])
    return gen


def count(app):
    with app.repository.connection() as db:
        return dict(zip(("trips","trip_versions","save_keys"),(
            db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("trips","trip_versions","save_keys"))))


def taxi_pass(result):
    return (result.get("candidate_kind")=="taxi" and result["booking_status"]=="not_booked"
            and all(item["unit"]=="vehicle" and item["min_fen"]<=item["max_fen"] for item in result["options"]))


async def smoke(args):
    clock=lambda:datetime.now(BEIJING)
    now=clock()
    day=(args.day or now.date()+timedelta(days=2)).isoformat()
    trace=[]
    report=dict(trace_id="m08-live-"+uuid4().hex,provider=args.provider,data_mode="live",
                checked_at=now.isoformat(),target_date=now.date().isoformat() if args.provider=="taxi" else day,status="BLOCKED",reason="read_only_required",
                writes=None,trace=trace,packets=[])
    if args.read_only:
        await run_provider(args,clock,day,report,trace)
    save(args.output,report)
    print(json.dumps({k:report[k] for k in ("provider","status","reason","writes")},ensure_ascii=False),flush=True)
    return 0 if report["status"]=="PASS" else 1


async def run_provider(args,clock,day,report,trace):
    kind=args.provider
    remote=None
    if kind=="taxi":
        remote=RemoteTool("amap-taxi",setting("AMAP_KEY"),trace=trace)
        skills=CandidateSkills(clock,mode="live",taxi=TaxiAdapter(remote,clock,mode="live"))
        fields=dict(candidate_kind="taxi",from_id="guiyang-north",to_id="qingyan-north-parking",vehicle_class="经济")
        text="请估算贵阳北站到青岩古镇北门停车场的经济车型打车价格？"
    elif kind=="hotel":
        remote=RemoteTool("rollinggo",setting("ROLLINGGO_HOTEL_API_KEY"),trace=trace)
        skills=CandidateSkills(clock,mode="live",hotel=HotelAdapter(remote,clock,mode="live",details=1))
        fields=dict(candidate_kind="hotel",place_id="guiyang",check_in=day,nights=2,adults=2,rooms=1,children=0)
        text="查贵阳两位成人住两晚一间房的住宿候选？"
    else:
        osm=osm_map_read if args.osm_map else osm_binding(args.osm_entry)
        if osm is None:
            report["reason"]="missing_osm_tool"
            return
        originals=Originals(clock,known=args.original,discover=not args.original)
        skills=CandidateSkills(clock,mode="live",food=FoodAdapter(osm,originals,clock,mode="live"))
        fields=dict(candidate_kind="dining",place_id=args.place,meal_at=f"{day}T19:00:00+08:00")
        text="查找目标区域晚餐的候选店？"

    async def decision(*_):
        return json.dumps(dict(intent="candidates",confidence_band="high",requested_outputs=["candidates"],parameters=fields))

    with TemporaryDirectory(prefix="travel-m08-live-") as wd:
        app=TravelApplication(Path(wd)/"travel_m08.sqlite3",clock=clock,candidate_skills=skills)
        app.create_account("m08_live_local","temporary-fixture-account")
        token=app.login("m08_live_local","temporary-fixture-account")
        conversation=app.open_session(token)
        packet=await app.turn(token,text,session_id=conversation,trace=trace,
                              conversation_factory=lambda:Conversation(typed_output=True,clock=clock,intent_model=decision))
        result=packet["result"]
        report["packets"].append(redacted(packet))
        report["writes"]=count(app)
        if remote:
            report["protocol"]=remote.counts.copy()
        report["reason"]=result.get("reason","business_chain_incomplete")
        if result.get("reason")=="invalid_response":
            report["status"]="FAIL"
        if remote and remote.last_status and remote.last_status[1]:
            report["reason"]=remote.last_status[1]
        if kind=="taxi" and result["kind"]=="pickup_choices":
            options=result["options"]
            report["reason"]="manual_choice_required"
            save(args.output,report)
            print(json.dumps({"choice_labels":[r["label"] for r in options],
                              "expires_at":result["expires_at"]},ensure_ascii=False),flush=True)
            if not args.pause_for_choice:
                return
            print("Enter the authorized choice number (1-based):",flush=True)
            try:
                v=await asyncio.wait_for(asyncio.to_thread(input),timeout=270)
                index=int(v)-1
            except (ValueError,EOFError,TimeoutError):
                return
            if not 0<=index<len(options):
                return
            follow=await app.choose_candidate(token,conversation,result["tool_context_id"],options[index]["id"],trace=trace)
            report["packets"].append(redacted(follow))
            report["protocol"]=remote.counts.copy()
            report["writes"]=count(app)
            chosen=follow["result"]
            if taxi_pass(chosen) and chosen["basis"]["from_label"]==options[index]["label"]:
                report.update(status="PASS",reason="actual_choice_followed_by_vehicle_quote")
            else:
                report["reason"]=remote.last_status[1] if remote.last_status and remote.last_status[1] else "selection_did_not_return_quote"
        elif kind=="taxi" and taxi_pass(result):
            report["reason"]="direct_quote_without_required_pickup_choice"
        elif kind=="hotel" and result.get("candidate_kind")=="hotel":
            calls=[i for i in trace if i["event"]=="m08_provider"]
            passed=(len(calls)>=2 and [r["tool"] for r in calls[:2]]==["searchHotels","getHotelDetail"]
                    and calls[1].get("response_hash") and result["options"][0]["room_name"]
                    and result["basis"]["check_in"]==day and result["basis"]["adults"]==2
                    and result["basis"]["rooms"]==1 and result["basis"]["nights"]==2
                    and result["booking_status"]=="not_booked")
            if passed:
                report.update(status="PASS",reason="actual_search_and_room_detail")
        elif kind=="dining" and result.get("candidate_kind")=="dining":
            observed=[e for e in packet["evidence"] if e.get("provider")=="public-original"]
            if observed and result["options"] and result["options"][0]["distance_m"]>=0:
                report.update(status="PASS",reason="actual_poi_and_matched_readable_original")
            else:
                report["reason"]="no_readable_original_matched_discovered_restaurant"
        if any(report["writes"].values()):
            report.update(status="FAIL",reason="unexpected_business_write")


def save(path,report):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider",choices=("taxi","hotel","dining"),required=True)
    parser.add_argument("--read-only",action="store_true")
    parser.add_argument("--day",type=date.fromisoformat)
    parser.add_argument("--osm-entry")
    parser.add_argument("--osm-map",action="store_true")
    parser.add_argument("--original",action="append",default=[])
    parser.add_argument("--place",default="qingyan")
    parser.add_argument("--pause-for-choice",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    return asyncio.run(smoke(parser.parse_args()))


if __name__=="__main__":
    raise SystemExit(main())

