"""Weather and intercity components for the existing typed agent, with no write path."""
import asyncio
from datetime import timedelta
import math
from uuid import uuid4

from travel_contract import validate_reply
from travel_fares import cost_line, nonnegative, summarize_costs
from travel_rules import POLICY_VERSION, format_time, instant, validate_itinerary
from travel_transport import Result, event, read, source


def reply(result, now, state="completed", evidence=(), *, fixture=False):
    packet=dict(schema_version="1.0",trace_id=uuid4().hex,generated_at=format_time(now),timezone="Asia/Shanghai",
                run_status=state,persistence="none",result=result,evidence=list(evidence))
    return validate_reply(packet,fixture_mode=fixture)


def gcj02(latitude, longitude):
    # Same WGS84-to-GCJ-02 projection as the reference project's city-map.js.
    x,y=longitude-105,latitude-35
    pi=math.pi
    root=math.sqrt(abs(x))
    common=(20*math.sin(6*x*pi)+20*math.sin(2*x*pi))*2/3
    lat_delta=(-100+2*x+3*y+.2*y*y+.1*x*y+.2*root+common
               +(20*math.sin(y*pi)+40*math.sin(y*pi/3)+160*math.sin(y*pi/12)+320*math.sin(y*pi/30))*2/3)
    lon_delta=(300+x+2*y+.1*x*x+.1*x*y+.1*root+common
               +(20*math.sin(x*pi)+40*math.sin(x*pi/3)+150*math.sin(x*pi/12)+300*math.sin(x*pi/30))*2/3)
    rad=math.radians(latitude)
    e2,a=.006693421622965943,6378245
    m=1-e2*math.sin(rad)**2
    return (latitude+lat_delta*180/(a*(1-e2)/(m*math.sqrt(m))*pi),
            longitude+lon_delta*180/(a/math.sqrt(m)*math.cos(rad)*pi))


def route_waypoints(origin, target, provider):
    if provider not in {"osm","amap"}:
        raise ValueError("地图供应商未登记")
    result=[]
    for entity in (origin,target):
        coords=entity.coordinates
        if not coords or coords["system"]!="WGS84":
            raise ValueError("缺少已核对的 WGS84 坐标")
        lat,lon=coords["latitude"],coords["longitude"]
        if provider=="osm":
            result.append(f"{lat},{lon}")
        else:
            if not 18 <= lat <= 54 or not 73 <= lon <= 135:
                raise ValueError("高德转换仅用于中国大陆点位")
            la,lo=gcj02(lat,lon)
            result.append(f"{lo:.6f},{la:.6f}")
    return result


def weather_card(result, place, day, now, mode):
    if result.status != "success":
        action = ("目标日超过预报窗口，请临近出发再查询。" if result.reason=="out_of_window"
                  else "暂未拿到该地区的有效预报，请临近出发时复查气象信息。")
        row=dict(kind="weather_unavailable",place_id=place.id,target_date=day,checked_at=format_time(now),
                 status=result.status,reason=result.reason or result.status,action=action)
        return reply(row,now,"partial",fixture=mode=="fixture")
    data=result.data
    risk=data["rain_probability_pct"]
    action = "出门带伞，给户外安排留些余地。" if risk>=60 else "出发前再看一次最新预报。"
    coverage=place.weather_area or "查询坐标周边预报"
    row=dict(kind="weather",data_mode=mode,evidence_ids=[e["evidence_id"] for e in data["evidence"]],
             place=dict(poi_id=place.id,label=place.label,coverage=coverage),
             target_date=day,checked_at=format_time(now),updated_at=data["updated_at"],as_of=data["as_of"],
             forecast={k:data[k] for k in ("rain_probability_pct","temperature_c","units","hourly")},
             conclusion=f"当天最大降雨概率为 {risk}%。预报覆盖该地区，景区现场天气可能有差异。",action=action)
    return reply(row,now,evidence=data["evidence"],fixture=mode=="fixture")


def m5_rows(packets, refs, day):
    fields=("evidence_id","source_url","publisher","source_group_id","source_kind","data_mode","provider",
            "effective_from","effective_to","published_at","expires_at","freshness","content_hash")
    return [{**{k:row[k] for k in fields if k in row},"collected_at":row["retrieved_at"],"applies_to":[day],
             "excerpt":row["excerpt"]}
            for packet in packets for row in packet.get("evidence",()) if row["evidence_id"] in refs]


def station_evidence(packets, origin):
    """M5 direction and date checks remain authoritative. Also require the boarding station in the clause."""
    return [{**packet,"evidence":[row for row in packet.get("evidence",()) if origin.label in row.get("excerpt","")]}
            for packet in packets]


def segment(from_id,from_label,to_id,to_label,mode,minutes,status,refs=()):
    return dict(from_id=from_id,from_label=from_label,to_id=to_id,to_label=to_label,mode=mode,
                travel_min=minutes,status=status,evidence_ids=list(refs))


def schedule_details(schedule, origin, target, arrival):
    keys=("exit_min","boarding_buffer_min","ride_min","transfer_min","walk_min")
    if (not schedule or arrival is None or (schedule.get("boarding_id"),schedule.get("alighting_id"))!=(origin.id,target.id)
            or not all(nonnegative(schedule.get(k)) and schedule[k]<=1440 for k in keys)):
        return None
    board=instant(schedule.get("boarding_at"))[0]
    if board is None or not timedelta(0) <= board-arrival <= timedelta(days=1):
        return None
    exit_min,buffer,ride,change,walk=(schedule[k] for k in keys)
    waiting=(board-arrival).total_seconds()/60-exit_min
    ready = waiting >= buffer
    # The boarding buffer is included in measured waiting and is explicitly broken out in the card.
    parts=dict(exit=exit_min,wait=max(0,math.floor(waiting)),ride=ride,transfer=change,walk=walk)
    eta=board+timedelta(minutes=ride+change+walk)
    return dict(board=board,parts=parts,buffer=buffer,wait=max(0,math.floor(waiting)),ready=ready,eta=eta)


def transfer_packet(origin,target,day,now,*,mode,map_minutes=None,map_evidence=(),arrival_at=None,
                    schedule=None,reservation=None,last_service=None,evidence_packets=(),requested_mode="bus",
                    route_status="unbound",trace=None):
    arrival=instant(arrival_at)[0]
    if arrival and arrival.date().isoformat()!=day:
        arrival=None
    timings=schedule_details(schedule,origin,target,arrival)
    when=timings["eta"] if timings else None
    refs=[r["evidence_id"] for r in map_evidence]
    profile_mode="walk" if requested_mode=="walk" else "car"
    rows=[segment(origin.id,origin.label,target.id,target.label,profile_mode,map_minutes,
                  "unknown" if map_minutes is None else "estimate",refs)]
    checking_date=when.date().isoformat() if when else day
    leg=dict(from_id=origin.id,expected_from_id=origin.id,to_id=target.id,coordinate_system="WGS84",
             evidence_ids=refs)
    if timings and timings["ready"]:
        parts=timings["parts"]
        leg.update(starts_at=format_time(arrival),segments_min=parts)
        exit_id=f"{origin.id}:exit"
        change_id=f"{target.id}:transfer"
        rows.extend([
            segment(origin.id,origin.label,exit_id,"出站上车处","walk",parts["exit"],"known"),
            segment(exit_id,"出站上车处",change_id,"下车和换乘处",requested_mode,parts["ride"],"known"),
            segment(change_id,"下车和换乘处",target.id,target.label,"walk",parts["walk"],"known")])
    elif timings is None and map_minutes is not None and requested_mode in {"car","walk"}:
        leg.update(starts_at=arrival_at,travel_min=map_minutes,status="estimate")
        if arrival is not None:
            when=arrival+timedelta(minutes=map_minutes)
            checking_date=when.date().isoformat()
    stop=dict(stop_id="transfer",entrance_id=target.id,date=checking_date,incoming_leg=leg)
    if when is not None:
        stop["arrival_at"]=format_time(when)
    if reservation is not None:
        stop["reservation"]=reservation
    schedule_needed=requested_mode in {"bus","shuttle","rail","metro"}
    if schedule_needed:
        last=last_service or {}
        boarding = timings["board"] if timings else arrival
        stop["last_service"] = dict(boarding_id=last.get("boarding_id",origin.id),alighting_id=target.id,
                                    direction=last.get("direction"),subject_id=last.get("subject_id"),
                                    boarding_at=format_time(boarding) if boarding else None,
                                    estimate_min=map_minutes,estimate_mode=profile_mode)
    applicable=station_evidence(evidence_packets,origin) if schedule_needed else []
    validation=validate_itinerary(dict(data_mode="fixture" if mode=="fixture" else "snapshot",
                                      checked_at=format_time(now),stops=[stop]),applicable,(),POLICY_VERSION)
    failures=[row for row in validation["rule_results"] if row["status"]=="fail"]
    rule_codes=[row["reason_code"] for row in validation["rule_results"] if row["status"]!="pass"]
    conflict=bool(failures or timings and not timings["ready"])
    if timings and not timings["ready"]:
        rule_codes.insert(0,"BOARDING_BUFFER_MISSED")
    known=[row for row in validation["rule_results"] if row["reason_code"]=="LAST_SERVICE_CHECKED"]
    last_refs=known[0]["evidence_ids"] if known and mode!="fixture" else []
    last=dict(state="unknown",evidence_ids=[])
    if last_refs:
        detail=validation["time_details"]["transfer"]["last_service"]
        last=dict(state="known",direction=detail["direction"],departure_at=detail["last_departure_at"],evidence_ids=last_refs)
    m5_evidence=m5_rows(applicable,last_refs,checking_date)
    all_evidence={e["evidence_id"]:e for e in [*map_evidence,*m5_evidence]}
    reason=("已知到站时刻与班车之间没有足够的出站和候车缓冲。" if timings and not timings["ready"] else
            failures[0]["suggested_action"] if failures else
            "末班和实际候车待核实，地图估时只反映相应路段。" if schedule_needed else "到达时间为估算，预约与上下车条件待核实。")
    if route_status not in {"success","unbound"} and timings is None:
        reason="地图暂未返回有效路段。"+reason
    result=dict(kind="transfer",data_mode=mode,evidence_ids=list(all_evidence),date=day,from_id=origin.id,to_id=target.id,
                segments=rows,wait_min=timings["wait"] if timings else None,
                transfer_buffer_min=timings["buffer"] if timings else None,
                buffers=dict(exit_min=timings["parts"]["exit"] if timings else None,
                             transfer_min=timings["parts"]["transfer"] if timings else None,
                             walk_min=timings["parts"]["walk"] if timings else None),
                last_service=last,arrival_check=dict(status="unreachable" if conflict else "unknown",reason=reason,
                                                   arrival_at=format_time(when) if when else None),
                feasibility="conflict" if conflict else "needs_verification",rule_codes=list(dict.fromkeys(rule_codes)),
                alternative=dict(label="请在更早到站、换交通方式或另行确认预约之间选择。",requires_choice=True),
                next_question="你几点到达车站？" if arrival is None else None)
    event(trace,"m07_transfer_rules",from_id=origin.id,to_id=target.id,validation_report=validation,
          route_status=route_status,wait_min=result["wait_min"],write_count=0)
    state="completed" if map_minutes is not None or timings is not None else "partial"
    return reply(result,now,state,list(all_evidence.values()),fixture=mode=="fixture")


def compare_cost(option,connection,kind):
    prices=[option["reference_fen"],connection.get("access_fen"),connection.get("egress_fen")]
    if kind=="flight" and option["tax_status"]!="known":
        prices.append(None)
    lines=[cost_line(dict(item_id=f"intercity-{i}",category="transport",unit="group",unit_fen=price,
                          currency=option["currency"])) for i,price in enumerate(prices)]
    summary=summarize_costs(lines)
    known=summary["totals_by_currency"].get(option["currency"],0)
    return known,None if summary["missing_costs"] else known


def transport_card(result, kind, args, now, mode, *, connection=None):
    if result.status!="success":
        failure=dict(kind="unavailable",requested_kind="candidates",reason=result.status,
                     action="暂未返回可用的指定线路结果，请核对车站或机场、出发日期和官方查询页。")
        return reply(failure,now,"partial",fixture=mode=="fixture")
    connection=connection or {}
    options=result.data["options"]
    evidence=result.data["evidence"]
    refs=[row["evidence_id"] for row in evidence]
    rows=[]
    for item in options[:8]:
        depart,arrive=(instant(item[k])[0] for k in ("depart_at","arrive_at"))
        access,egress,buffer=(connection.get(k) for k in ("access_min","egress_min","boarding_buffer_min"))
        door=(int((arrive-depart).total_seconds()//60)+access+egress+buffer
              if all(nonnegative(v) for v in (access,egress,buffer)) else None)
        lower,total=compare_cost(item,connection,kind)
        row=dict(id=item["id"],reason="门到门费用或耗时待核实。" if total is None or door is None else "已分列交通报价和接驳费用。",
                 evidence_ids=refs,depart_at=item["depart_at"],arrive_at=item["arrive_at"],
                 door_to_door_min=door,reference_fen=item["reference_fen"],known_lower_fen=lower,total_fen=total,
                 connection=dict(access_min=access,egress_min=egress,boarding_buffer_min=buffer,
                                 access_fen=connection.get("access_fen"),egress_fen=connection.get("egress_fen")))
        row["flight_number" if kind=="flight" else "train_number"]=item["number"]
        if kind=="flight":
            row.update(legs=item["legs"],party_status=item["party_status"],tax_status=item["tax_status"])
        else:
            row.update(start_date=item["start_date"],arrive_date=item["arrive_date"],seats=item["seats"][:12])
        rows.append(row)
    head,*rest=rows
    currency=options[0]["currency"]
    basis={k:args[k] for k in ("date","from_id","to_id","adults","children","origin_label","destination_label") if k in args}
    result=dict(kind="candidates",candidate_kind=kind,data_mode=mode,evidence_ids=refs,currency=currency,
                basis=basis,primary_id=head["id"],alternative_ids=[row["id"] for row in rest],options=rows,
                price_meaning="首条为查询结果中的一项。全团门到门费用、税费或余票仍可待核实。",
                inventory_status="unverified",booking_status="not_booked",provider_status=result.data["provider_status"])
    return reply(result,now,evidence=evidence,fixture=mode=="fixture")


class TransportSkills:
    """Only explicit bindings can reach providers. M5 evidence and route facts are server-owned."""
    def __init__(self,catalog,clock,*,mode,weather=None,route=None,rail=None,flight=None,
                 schedules=None,connections=None):
        self.catalog,self.clock,self.mode=catalog,clock,mode
        self.weather,self.route,self.rail,self.flight=weather,route,rail,flight
        self.schedules,self.connections=schedules or {},connections or {}

    def supports(self,kind,context):
        return kind in {"weather","transfer"} or kind=="candidates" and context.get("candidate_kind") in {"train","flight","intercity"}

    def resolve(self,kind,context):
        c=dict(context)
        if kind=="weather":
            mapping={"place_id":{"poi","entrance"}}
        elif kind=="transfer":
            mapping={"from_id":{"station","airport"},"to_id":{"entrance"}}
        elif c.get("candidate_kind")=="intercity":
            mapping={"from_id":{"city"},"to_id":{"city"}}
        else:
            entity_kind="station" if c.get("candidate_kind")=="train" else "airport"
            mapping={"from_id":{entity_kind},"to_id":{entity_kind}}
        for key,allowed in mapping.items():
            raw=c.get(key)
            entities=[e for e in self.catalog.resolve(raw) if e.kind in allowed] if isinstance(raw,str) else []
            if kind=="weather":
                entities=[e for e in entities if e.coordinates]
            if kind=="transfer" and key=="to_id":
                entities=[e for e in entities if "停车场" not in e.label]
            if len(entities)!=1:
                return c,key
            c[key]=entities[0].id
        return (c,"to_id") if c.get("from_id") is not None and c.get("from_id")==c.get("to_id") else (c,None)

    async def handle(self,kind,args,*,trace=None):
        now=self.clock()
        day=args["date"]
        if kind=="weather":
            found=await self.weather.query(args["place_id"],day,hourly=args.get("hourly",False),trace=trace) if self.weather else Result("unbound",reason="unbound")
            return weather_card(found,self.catalog.get(args["place_id"]),day,now,self.mode)
        if kind=="transfer":
            first,last=(self.catalog.get(args[k]) for k in ("from_id","to_id"))
            details=dict(self.schedules.get((first.id,last.id,day),{}))
            mode=args.get("mode","bus")
            profile="foot" if mode=="walk" else "car"
            evidence,minutes=[],None
            route_state="unbound"
            if self.route is not None and first.coordinates and last.coordinates:
                route_args={"profile":profile,"waypoints":route_waypoints(first,last,"osm")}
                value=await read(self.route,"route",route_args,"osm",trace)
                route_state=value.status
                raw=value.data["raw"] if value.data else None
                if (isinstance(raw,dict) and raw.get("profile")==profile and
                        all(type(raw.get(k)) is int and raw[k]>0 for k in ("distance_m","duration_s"))):
                    minutes=math.ceil(raw["duration_s"]/60)
                    note=(f"地图返回 {raw['distance_m']} 米、{raw['duration_s']} 秒的路段估算。"
                          "公交时刻须另查运营方资料。")
                    evidence=[source("osm",day,format_time(self.clock()),self.mode,raw,note,
                                     scope=f"{first.id}/{last.id}/{profile}")]
                elif value.status=="success":
                    route_state="invalid_response"
            if "reservation_cutoff_at" in args and "reservation" not in details:
                cutoff=instant(args["reservation_cutoff_at"])[0]
                if cutoff:
                    details["reservation"]=dict(entrance_id=last.id,cutoff_at=format_time(cutoff),
                                                cutoff_confirmed=True,buffer_min=None)
            return transfer_packet(first,last,day,now,mode=self.mode,map_minutes=minutes,map_evidence=evidence,
                                   arrival_at=args.get("arrival_at"),requested_mode=mode,route_status=route_state,
                                   trace=trace,**details)
        if args["candidate_kind"]=="intercity":
            return await self.intercity(args,now,trace)
        return await self.candidate(args,now,trace)

    async def candidate(self,args,now,trace):
        kind=args["candidate_kind"]
        adapter=self.rail if kind=="train" else self.flight
        values=[args[k] for k in ("from_id","to_id","date")]
        opts={"adults":args["adults"],"children":args.get("children",0),"trace":trace}
        if adapter is None:
            result=Result("unbound",reason="unbound")
        else:
            result=await adapter.query(*values,**opts)
        event(trace,"m07_candidate_status",candidate_kind=kind,date=args["date"],status=result.status,
              reason=result.reason,provider_status=result.data.get("provider_status") if result.data else None)
        connection=self.connections.get((kind,*values,args["adults"],args.get("children",0)),{})
        labels=[self.catalog.get(key).label for key in values[:2]]
        return transport_card(result,kind,args|dict(origin_label=labels[0],destination_label=labels[1]),
                              now,self.mode,connection=connection)

    async def intercity(self,args,now,trace):
        first,last=(self.catalog.get(args[k]) for k in ("from_id","to_id"))
        async def query(kind, field):
            origin,destination=(getattr(city,field) for city in (first,last))
            if origin and destination:
                return await self.candidate(args|dict(candidate_kind=kind,from_id=origin,to_id=destination),now,trace)
            return transport_card(Result("unbound",reason="unresolved_endpoint"),kind,args,now,self.mode)
        packets=await asyncio.gather(query("train","station_id"),query("flight","airport_id"))
        primary,*other=sorted(packets,key=lambda item:item["run_status"]!="completed")
        evidence={row["evidence_id"]:row for packet in packets for row in packet["evidence"]}
        result=dict(kind="bundle",primary=primary["result"],supplements=[item["result"] for item in other],next_question=None)
        return reply(result,now,"completed" if all(p["run_status"]=="completed" for p in packets) else "partial",
                     evidence.values(),fixture=self.mode=="fixture")

