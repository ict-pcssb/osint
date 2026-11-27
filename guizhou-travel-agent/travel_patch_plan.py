"""Apply a bounded candidate plan to an existing itinerary snapshot without side effects."""
from copy import deepcopy
from math import ceil

from travel_contract import ContractError, _validator, check, validate_reply
from travel_evidence import digest, public_url
from travel_fares import summarize_costs
from travel_rules import format_time, instant
from travel_timeline import calc_feasibility, return_lock_for, stamp

PATCH_SCHEMA=_validator("patch")


def validate_patch(kind, payload):
    validator=PATCH_SCHEMA.evolve(schema={"$defs":PATCH_SCHEMA.schema["$defs"], "$ref":"#/$defs/"+kind})
    return check(validator,payload)


def must(condition, reason="plan_scope"):
    if not condition:
        raise ContractError(reason)


def day_id(day):
    return day.get("day_id","day:"+day["date"])


def stop_text(stop):
    if stop is None:
        return "无此站"
    time="时段待查" if not stop["arrival_local"] else (
        stop["arrival_local"][11:16]+"–"+stop["departure_local"][11:16])
    return f"{stop.get('label',stop['poi_id'])} · {time}"


def change_line(line, amount, refs):
    row=dict(line,unit_fen=amount,subtotal_fen=None if amount is None else amount*line["quantity"],
             evidence_ids=refs,price_status="unknown" if amount is None else "known",
             eligibility={"status":"unknown" if amount is None else "eligible"})
    if amount is None:
        row["missing_reason"]="需核实调整后的实际费用"
    else:
        row.pop("missing_reason",None)
    return row


def scoped_operations(base, variants, allowed):
    by_day={day["date"]:day for day in base["days"]}
    must(set(allowed)<=by_day.keys())
    existing={s["stop_id"] for day in base["days"] for s in day["stops"]}
    new_ids=[o["new_stop_id"] for o in variants if "new_stop_id" in o]
    touched=[o["stop_id"] for o in variants if "stop_id" in o]
    must(len(new_ids)==len(set(new_ids)) and not set(new_ids)&existing and len(touched)==len(set(touched)))
    for op in variants:
        must(op["date"] in allowed)
        if op["op"]!="add":
            target=next((s for s in by_day[op["date"]]["stops"] if s["stop_id"]==op["stop_id"]),None)
            must(target is not None and not any(target.get(k) for k in ("completed","required","locked")),"protected_stop")


def model_cost(base, new, changed):
    # IDs present before and after are comparable. A genuinely added or removed line contributes its exact amount.
    old={x["item_id"]:x for x in base["items"]}
    final={x["item_id"]:x for x in new["items"]}
    deltas=[]
    missing=[]
    for key in sorted(changed):
        prior,after=old.get(key),final.get(key)
        before_amount=prior["subtotal_fen"] if prior is not None else 0
        next_amount=after["subtotal_fen"] if after is not None else 0
        if before_amount is None or next_amount is None:
            missing.append(key)
        else:
            deltas.append(next_amount-before_amount)
    known_delta=sum(deltas) if deltas else None
    return dict(known_delta_fen=known_delta,delta_fen=None if missing else sum(deltas),currency="CNY",
                status="unknown" if missing else "estimate",missing_item_ids=missing,
                before_total_fen=base["total_fen"],after_total_fen=new["total_fen"])


def duration_totals(result,dates):
    selected=[day for day in result["days"] if day["date"] in dates]
    durations=[s["incoming_leg"]["travel_min"] for day in selected for s in day["stops"]]
    if result["days"][-1]["date"] in dates:
        durations.append(result["return_leg"]["travel_min"])
    return sum(durations) if all(v is not None for v in durations) else None


def preview_copy(base, result, initial_changes, dates, changed_costs, locks):
    old_days={x["date"]:x for x in base["days"]}
    new_days={x["date"]:x for x in result["days"]}
    lookup={s["stop_id"]:s for day in new_days.values() for s in day["stops"]}
    rows=[]
    represented=set()
    for item in initial_changes:
        before,after=item["before"],lookup.get(item.get("after_id"))
        stop_id=(after or before)["stop_id"]
        represented.add(stop_id)
        rows.append(dict(stop_id=stop_id,before=stop_text(before),after=stop_text(after),date=item["date"],
                         action=item["op"]))
    for day in dates:
        before={s["stop_id"]:s for s in old_days[day]["stops"]}
        for stop in new_days[day]["stops"]:
            old=before.get(stop["stop_id"])
            if old and old["incoming_leg"]!=stop["incoming_leg"] and stop["stop_id"] not in represented:
                rows.append(dict(stop_id=stop["stop_id"],before=stop_text(old),after=stop_text(stop),date=day,action="route"))
    minutes_before=duration_totals(base,dates)
    minutes_after=duration_totals(result,dates)
    ret=result["return_leg"]
    locked_time=instant(return_lock_for(base, locks).fields["departure_at"])[0]
    at=instant(ret["arrival_at"])[0]
    margin=None if at is None else max(0,int((locked_time-at).total_seconds()//60)-ret["buffer_min"])
    routes=[]
    replacements={item["after_id"]:item["before"] for item in initial_changes if item["op"]=="replace"}
    for date in dates:
        old_stops={s["stop_id"]:s for s in old_days[date]["stops"]}
        for stop in new_days[date]["stops"]:
            old=old_stops.get(stop["stop_id"],replacements.get(stop["stop_id"]))
            saved=old["incoming_leg"] if old else None
            current=stop["incoming_leg"]
            if saved!=current:
                routes.append(dict(stop_id=stop["stop_id"],from_id=current["from_id"],to_id=current["to_id"],
                                   before_min=saved["travel_min"] if saved else None,after_min=current["travel_min"]))
    prev,end=base["return_leg"],result["return_leg"]
    if (prev["from_id"],prev["travel_min"])!=(end["from_id"],end["travel_min"]):
        routes.append(dict(stop_id="locked-return-connection",from_id=end["from_id"],to_id=end["to_id"],
                           before_min=prev["travel_min"],after_min=end["travel_min"]))
    unchanged=[day_id(day) for day in old_days.values() if day["date"] not in dates]
    unchanged += [lock.stable_id for lock in locks]
    next_stops=[dict(date=day,before=old_days[day]["next_stop_id"],after=new_days[day]["next_stop_id"]) for day in dates]
    codes=list(dict.fromkeys(row["reason_code"] for row in result["validation_report"]["rule_results"] if row["status"]!="pass"))
    return dict(changes=rows,time_impact=dict(delta_min=None if None in (minutes_before,minutes_after)
                                             else minutes_after-minutes_before,return_buffer_min=margin),
                cost_impact=model_cost(base["budget"],result["budget"],changed_costs),
                route_changes=routes,next_stop_impact=next_stops,unchanged=unchanged,rule_codes=codes,
                pending=result["pending"],rule_status=result["feasibility"])


class ApplyPlan:
    def __init__(self, version, plan, dates, checked_at):
        self.base=version.timeline["result"]
        self.packet=deepcopy(version.timeline)
        self.result=self.packet["result"]
        self.version,self.plan,self.dates,self.checked_at=version,plan,dates,checked_at
        self.mode=self.result["data_mode"]
        self.evidence={x["evidence_id"]:x for x in self.packet["evidence"]}
        self.original_evidence=set(self.evidence)
        self.points={x["id"]:x for x in self.result["map_points"]}
        self.candidates={x["id"]:x for x in plan["points"]}
        self.lines={x["item_id"]:x for x in self.result["budget"]["items"]}
        self.routes={(x["from_id"],x["to_id"],x["mode"]):x for x in plan["routes"]}
        self.changed_costs=set()
        self.changes=[]

    def proof(self,url,excerpt,day,source_kind="provider",mode=None,collected=None):
        mode=mode or self.mode
        if mode=="fixture":
            must(isinstance(url,str) and url.startswith("fixture://"),"fixture_source_required")
        else:
            must(public_url(url),"public_source_required")
        at=collected or self.checked_at
        key="m9-"+digest(f"{url}|{at}|{day}|{excerpt}")[:22]
        self.evidence.setdefault(key,dict(evidence_id=key,source_url=url,publisher="固定回放" if mode=="fixture" else "地方资料或地图",
                 source_group_id=key,collected_at=at,applies_to=[day],
                 source_kind="fixture" if mode=="fixture" else source_kind,data_mode=mode,excerpt=excerpt))
        return key

    def existing_point_ref(self,key,day):
        candidate=self.candidates.get(key)
        if candidate:
            default="fixture://m09-"+key if self.mode=="fixture" else None
            collected=candidate.get("collected_at")
            if self.mode!="fixture":
                checked,_=instant(self.checked_at)
                mapped_at,_=instant(collected)
                must(mapped_at is not None and mapped_at<=checked,"point_source_time")
            return self.proof(candidate.get("source_url",default),candidate["label"]+"。开放与预约待查。",day,
                              mode="fixture" if self.mode=="fixture" else "snapshot",collected=collected,source_kind="other")
        known=self.evidence.get("endpoint-"+key)
        if known:
            return known["evidence_id"]
        for item in self.base["days"]:
            for stop in item["stops"]:
                old=stop["incoming_leg"]
                if key in (old["from_id"],old["to_id"]) and old["evidence_ids"]:
                    return old["evidence_ids"][0]
        if self.base["return_leg"]["to_id"]==key and self.base["return_leg"]["evidence_ids"]:
            return self.base["return_leg"]["evidence_ids"][0]
        raise ContractError("unknown_endpoint")

    def new_leg(self,start,end,mode,day,existing):
        if existing and (existing["from_id"],existing["to_id"],existing["mode"])==(start,end,mode):
            return existing
        must(start in self.points and end in self.points and start!=end,"invalid_endpoint")
        refs=[self.existing_point_ref(key,day) for key in (start,end)]
        route=self.routes.get((start,end,mode))
        route_mode=self.mode if not route else ("fixture" if self.mode=="fixture" else "live")
        time=self.checked_at
        if route:
            time=route.get("checked_at",time)
            url=route.get("source_url","fixture://m09-"+start+"-"+end if self.mode=="fixture" else None)
            refs.append(self.proof(url,f"{start} → {end}，{route['distance_m']} 米、{route['duration_s']} 秒。路段估算。",
                                   day,mode=route_mode,collected=time))
        return dict(from_id=start,to_id=end,mode=mode,profile="foot" if mode=="walk" else "car",coordinate_system="WGS84",
                    travel_min=ceil(route["duration_s"]/60) if route else None,
                    distance_m=route["distance_m"] if route else None,duration_s=route["duration_s"] if route else None,
                    checked_at=time,status="estimate" if route else "unknown",reason=None if route else "unbound",
                    data_mode=route_mode,evidence_ids=list(dict.fromkeys(refs)))

    def remove_costs(self,stop):
        for item in stop["cost"]["items"]:
            key=item["item_id"]
            must(item["category"]=="ticket" and key==stop["stop_id"]+"-ticket","shared_cost_locked")
            del self.lines[key]
            self.changed_costs.add(key)

    def new_stop(self,operation,mode):
        candidate=self.candidates.get(operation["point_id"])
        must(candidate is not None)
        point_id=candidate["id"]
        self.points[point_id]={key:candidate[key] for key in ("id","label","latitude","longitude")}
        day=operation["date"]
        point=self.existing_point_ref(point_id,day)
        stop_id=operation["new_stop_id"]
        price=operation["ticket_fen"]
        must(self.mode=="fixture" or price is None,"live_price_unverified")
        evidence=[] if price is None else [self.proof("fixture://m09-fare-"+stop_id,"全团门票的固定模拟单价。",day)]
        ticket=change_line(dict(item_id=stop_id+"-ticket",category="ticket",traveler_group="group",unit="person",
                                currency="CNY",mandatory=True,quantity=self.result["party"]["adults"]),price,evidence)
        self.lines[ticket["item_id"]]=ticket
        self.changed_costs.add(ticket["item_id"])
        return dict(stop_id=stop_id,label=candidate["label"],poi_id=candidate["poi_id"],entrance_id=point_id,
                    plan_role="alternative",completed=False,arrival_local=stamp(day,operation["arrive"]),
                    departure_local=stamp(day,operation["leave"]),incoming_leg={"mode":mode},next_stop_id=None,
                    opening={"status":"unconfirmed","applies_to":day},evidence_ids=[point,*evidence],
                    cost=dict(currency="CNY",status="unknown" if price is None else "known",
                              total_fen=ticket["subtotal_fen"],items=[ticket],missing_costs=["门票"] if price is None else []))

    def edit_day(self,day,operations):
        stops=day["stops"]
        starting=stops[0]["incoming_leg"]["from_id"]
        for operation in operations:
            kind=operation["op"]
            indexes={s["stop_id"]:index for index,s in enumerate(stops)}
            before=None
            after_id=None
            if kind=="add":
                after=operation["after_id"]
                must(after is None or after in indexes)
                index=indexes[after]+1 if after else 0
                mode=stops[index-1 if index else 0]["incoming_leg"]["mode"] if stops else "car"
                added=self.new_stop(operation,mode)
                after_id=added["stop_id"]
                stops.insert(index,added)
            else:
                index=indexes[operation["stop_id"]]
                before=deepcopy(stops[index])
                if kind=="retime":
                    stops[index]["arrival_local"]=stamp(day["date"],operation["arrive"])
                    stops[index]["departure_local"]=stamp(day["date"],operation["leave"])
                    after_id=before["stop_id"]
                else:
                    self.remove_costs(before)
                    if kind=="remove":
                        del stops[index]
                    else:
                        new=self.new_stop(operation,before["incoming_leg"]["mode"])
                        after_id=new["stop_id"]
                        stops[index]=new
            self.changes.append(dict(date=day["date"],op=kind,before=before,after_id=after_id))
        must(bool(stops),"empty_day")
        day["day_id"]=day_id(day)
        previous=starting
        for stop in stops:
            prior=stop["incoming_leg"]
            leg=self.new_leg(previous,stop["entrance_id"],prior["mode"],day["date"],prior if "from_id" in prior else None)
            stop["incoming_leg"]=leg
            if prior!=leg:
                refs=[ref for ref in stop["evidence_ids"] if ref not in prior.get("evidence_ids",[])]
                stop["evidence_ids"]=list(dict.fromkeys([*leg["evidence_ids"],*refs]))
            if leg["status"]=="unknown":
                must(not any(stop.get(k) for k in ("completed","required","locked")) or prior==leg,"protected_route")
                stop.update(arrival_local=None,departure_local=None,plan_role="alternative")
            previous=stop["entrance_id"]

    def build(self):
        plan=self.plan
        variants=plan["operations"]
        scoped_operations(self.base,variants,self.dates)
        must(len(self.candidates)==len(plan["points"]) and len(self.routes)==len(plan["routes"]))
        must(not set(self.candidates)&self.points.keys() and
             set(self.candidates)=={op["point_id"] for op in variants if "point_id" in op})
        selected={index+1 for index,day in enumerate(self.result["days"]) if day["date"] in self.dates}
        costs=plan["unknown_cost_ids"]
        must(set(costs)<={f"d{number}-ride" for number in selected},"cost_outside_scope")
        for key in costs:
            must(key in self.lines and self.lines[key]["category"]=="transport")
            self.lines[key]=change_line(self.lines[key],None,[])
            self.changed_costs.add(key)
        for day in self.result["days"]:
            ops=[row for row in variants if row["date"]==day["date"]]
            if ops:
                self.edit_day(day,ops)
        budget=self.result["budget"]
        summary=summarize_costs(list(self.lines.values()),budget["cap_fen"])
        for key in ("known_lower_fen","total_fen","missing_costs","budget_status","items"):
            budget[key]=summary[key]
        budget["reference_prices"]=[row for row in budget.get("reference_prices",[]) if row["item_id"] not in self.changed_costs]
        ret=self.result["return_leg"]
        last=self.result["days"][-1]
        if last["date"] in self.dates:
            leg=self.new_leg(last["stops"][-1]["entrance_id"],ret["to_id"],ret["mode"],last["date"],ret)
            self.result["return_leg"]=dict(leg,leave_at=ret["leave_at"],arrival_at=ret["arrival_at"],buffer_min=ret["buffer_min"])
        if "recheck_facts" in self.result:
            active = {stop["stop_id"] for day in self.result["days"] for stop in day["stops"]}
            self.result["recheck_facts"] = [fact for fact in self.result["recheck_facts"] if fact["stop_id"] in active]
        self.result["map_points"]=list(self.points.values())
        affected=[s["stop_id"] for day in self.result["days"] if day["date"] in self.dates for s in day["stops"]]
        calc_feasibility(self.result,locked_items=self.version.locked_items,checked_at=self.checked_at,
                         affected=affected if self.result.get("validation_report") else None)
        must(self.result["locked_items"]==self.base["locked_items"],"locked_projection_changed")
        for new, old in zip(self.result["days"], self.base["days"], strict=True):
            if old["date"] not in self.dates:
                must(new==old,"changed_outside_scope")
                continue
            after={stop["stop_id"]:stop for stop in new["stops"]}
            for stop in old["stops"]:
                if any(stop.get(key) for key in ("completed","required","locked")):
                    must(after.get(stop["stop_id"])==stop,"protected_stop")
        self.packet.update(evidence=list(self.evidence.values()),generated_at=self.checked_at)
        self.packet["run_status"]="partial" if self.result["return_leg"]["status"]=="unknown" or any(
            s["incoming_leg"]["status"]=="unknown" for day in self.result["days"] if day["date"] in self.dates for s in day["stops"]) else "completed"
        validate_reply(self.packet,fixture_mode=self.mode=="fixture")
        output=preview_copy(self.base,self.result,self.changes,self.dates,self.changed_costs,self.version.locked_items)
        return self.packet,output,[row for key,row in self.evidence.items() if key not in self.original_evidence]

