"""Read-only E2/E3/E4 adapters and cards for the single Agent."""
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import math
import re
from urllib.parse import urlsplit
from uuid import uuid4

from travel_candidate_context import ToolContextStore
from travel_catalog import public_catalog
from travel_evidence import Material, digest, group_sources, public_url
from travel_fares import cost_line, summarize_costs
from travel_rules import format_time, instant
from travel_state import BEIJING
from travel_transport import ProviderFailure, Result, event, hash_payload, money_fen
from travel_transport_skills import reply

SOURCES={
    "amap-taxi":("https://lbs.amap.com/","高德打车"),
    "rollinggo":("https://www.rollinggo.cn/","RollingGo"),
    "osm":("https://www.openstreetmap.org/copyright","OpenStreetMap"),
}
VEHICLES=("经济","优享","商务","豪华","专车","特惠","出租车")
CITY_POINTS={"guiyang-north":("贵阳北站","贵阳市"),
             "qingyan-north-parking":("青岩古镇北门停车场","贵阳市"),
             "qingyan":("青岩古镇","贵阳市"),
             "jiaxiu-entry":("甲秀楼入口","贵阳市")}
HOTEL_REGIONS={"guiyang":("贵阳市","城市"),"qingyan":("青岩古镇","景点"),
               "jiaxiulou":("甲秀楼","景点")}
DISTANCE_LIMIT=700


def mode_for_reply(mode):
    return mode=="fixture"


def label(value,max_len=120):
    if not isinstance(value,str):
        return ""
    return " ".join(value.split())[:max_len]


def currency(value):
    return value if isinstance(value,str) and re.fullmatch(r"[A-Z]{3}",value) else None


def proof(provider, raw, now, day, mode, note, *, scope=''):
    url,title=SOURCES[provider]
    raw_hash=hash_payload(raw)
    return dict(evidence_id="m8-"+digest(f"{provider}|{now}|{day}|{scope}|{raw_hash}")[:24],
                source_url=url,publisher=title,source_group_id=provider,collected_at=format_time(now),
                applies_to=[day],source_kind="fixture" if mode=="fixture" else "provider",
                data_mode=mode,excerpt=note,provider=provider,content_hash=raw_hash)


def failure(now,mode,kind,status,action=None):
    return reply(dict(kind="unavailable",requested_kind=kind,reason=status,
                      action=action or "本次没有拿到有效的候选资料，请核对条件后再查询。"),
                 now,"partial",fixture=mode_for_reply(mode))


def card(kind,mode,rows,primary,basis,evidence,price_meaning,*,code="CNY",**fields):
    return dict(kind="candidates",candidate_kind=kind,data_mode=mode,evidence_ids=[e["evidence_id"] for e in evidence],
                currency=code,primary_id=primary["id"],alternative_ids=[r["id"] for r in rows if r["id"]!=primary["id"]],
                options=rows,basis=basis,price_meaning=price_meaning,booking_status="not_booked",
                inventory_status="unverified",**fields)


async def provider(call,name,args,tag,trace):
    # The exact vendor state is used on the wire and reduced to a hash for diagnostics.
    sent=dict(args)
    token=sent.pop("businessData",None)
    if token:
        sent["context_hash"]=digest(token)
    try:
        raw=await call(name,args) if call is not None else None
        if raw is None:
            result=Result("empty" if call is not None else "unbound",reason="empty" if call is not None else "unbound")
        else:
            calculated=hash_payload(raw)
            event(trace,"m08_provider",provider=tag,tool=name,arguments=sent,response_hash=calculated,attempted=True)
            return Result("success",{"raw":raw})
    except ProviderFailure as exc:
        result=Result(exc.status,reason=exc.reason)
    except TimeoutError:
        result=Result("timeout",reason="timeout")
    except (TypeError,ValueError,KeyError):
        result=Result("invalid_response",reason="invalid_response")
    except (OSError,RuntimeError):
        result=Result("provider_error",reason="connection_error")
    event(trace,"m08_provider",provider=tag,tool=name,arguments=sent,status=result.status,reason=result.reason,
          attempted=call is not None)
    return result


def taxi_entities(raw):
    chunks=raw if isinstance(raw,list) else [raw]
    for chunk in chunks:
        if isinstance(chunk,dict) and chunk.get("type")=="text":
            try:
                chunk=json.loads(chunk.get("text",""))
            except (ValueError,TypeError):
                continue
        if isinstance(chunk,dict) and isinstance(chunk.get("entities"),list):
            return [e for e in chunk["entities"] if isinstance(e,dict)]
    return []


def point_role(scene):
    if scene=="start_point":
        return "pickup",None
    if scene=="end_point":
        return "dropoff",None
    if isinstance(scene,str) and (match:=re.fullmatch(r"pass_point[_-]?(\d{1,2})",scene)):
        return "waypoint",int(match[1])-1
    return None,None


def taxi_points(entities):
    rows=[]
    for item in entities:
        if item.get("cardScene")!="amap_car_point":
            continue
        role,index=point_role(item.get("cardSubScene"))
        opaque=item.get("businessData")
        if not role or not isinstance(opaque,str) or not 0 < len(opaque) <= 100000:
            continue
        data=item.get("modelInput",{})
        if not isinstance(data,dict) or not isinstance(data.get("points"),list):
            continue
        for point in data["points"][:12]:
            if not isinstance(point,dict):
                continue
            children=point.get("childNodeList")
            leaves=children if isinstance(children,list) and children else [point]
            for p in leaves[:12]:
                name=p.get("name") if isinstance(p,dict) else None
                if isinstance(name,str) and name.strip() and len(name)<=200 and not any(c in name for c in "\r\n\0"):
                    row=dict(name=name,role=role,index=index,businessData=opaque)
                    if row not in rows:
                        rows.append(row)
    return rows[:16]


def vehicle_quotes(rows,preferred):
    result=[]
    for entity in rows:
        if entity.get("cardScene")!="amap_car_plan" or not isinstance(entity.get("modelInput"),dict):
            continue
        plan=entity["modelInput"]
        groups=plan.get("transportTypeInfoList",[])
        if not isinstance(groups,list):
            continue
        for group in groups[:24]:
            if not isinstance(group,dict):
                continue
            name=label(group.get("transportTypeName") or group.get("typeName") or group.get("name")
                       or group.get("transportType"))
            if name not in VEHICLES or (preferred and name!=preferred):
                continue
            low,high=money_fen(group.get("minPrice")),money_fen(group.get("maxPrice"))
            if low is None or high is None or low>high:
                continue
            if not any(item[0]==name for item in result):
                result.append((name,low,high))
    return result


class TaxiAdapter:
    def __init__(self,call,clock,*,mode,store=None):
        self.call,self.clock,self.mode=call,clock,mode
        self.store=store or ToolContextStore(clock)

    async def query(self,args,scope,trace=None):
        if not scope:
            return failure(self.clock(),self.mode,"candidates","unbound","请登录并在同一会话内查看候选。")
        first,last=(CITY_POINTS.get(args[k]) for k in ("from_id","to_id"))
        vehicle=args.get("vehicle_class")
        if not first or not last or first==last or (vehicle and vehicle not in VEHICLES):
            return failure(self.clock(),self.mode,"candidates","invalid_response","请确认打车的起终点和车型。")
        when=instant(args.get("departure_at"))[0]
        local=when.strftime("%Y-%m-%d %H:%M") if when else ""
        payload=dict(origin=first[0],destination=last[0],origin_city=first[1],destination_city=last[1],
                     agent_name="Codex",departure_time=local,arrival_time="",vehicle_type=[vehicle] if vehicle else [],
                     vehicle_brands=[],price_range=["",""],price_reference="",power_source="",vehicle_body=[],
                     incar_environment=[],driver_requirements=[],route_requirements=[],waypoints=[],businessData="")
        return await self._query(args,payload,scope,trace,turn=uuid4().hex)

    async def choose(self,scope,context_id,choice_id,trace=None):
        async def perform(cached,selected):
            payload=dict(cached["payload"])
            role=selected["role"]
            if role=="waypoint":
                points=list(payload["waypoints"])
                index=selected["index"]
                if not 0<=index<len(points):
                    return failure(self.clock(),self.mode,"pickup_choices","invalid_response")
                points[index]=selected["name"]
                payload["waypoints"]=points
            else:
                payload["origin" if role=="pickup" else "destination"]=selected["name"]
            payload["businessData"]=selected["businessData"]
            return await self._query(cached["args"],payload,scope,trace,turn=cached["turn"])

        return await self.store.choose(scope,context_id,choice_id,perform,trace)

    async def _query(self,args,payload,scope,trace,*,turn):
        result=await provider(self.call,"get_taxi_service",payload,"amap-taxi",trace)
        now=self.clock().astimezone(BEIJING)
        if result.status!="success":
            return failure(now,self.mode,"candidates",result.status)
        raw=result.data["raw"]
        entities=taxi_entities(raw)
        if not entities:
            return failure(now,self.mode,"candidates","invalid_response")
        day=(instant(args.get("departure_at"))[0] or now).date().isoformat()
        evidence=[proof("amap-taxi",raw,now,day,self.mode,"行前查询结果。报价只适用于当前端点和车型。")]
        refs=[r["evidence_id"] for r in evidence]
        if any(r.get("cardScene")=="amap_car_point" for r in entities):
            points=taxi_points(entities)
            if not points:
                return failure(now,self.mode,"pickup_choices","invalid_response")
            # Store no prior opaque token with the next card: only the token of that card's point scene.
            plain=dict(payload,businessData="")
            context_id,expires,options=self.store.issue(scope,turn,dict(args=args,payload=plain,turn=turn),points,trace)
            data=dict(kind="pickup_choices",data_mode=self.mode,evidence_ids=refs,tool_context_id=context_id,
                      expires_at=format_time(expires),options=options)
            return reply(data,now,evidence=evidence,fixture=mode_for_reply(self.mode))
        quotes=vehicle_quotes(entities,args.get("vehicle_class"))
        if not quotes:
            return failure(now,self.mode,"candidates","provider_error",
                           "这次未取得选定车型的报价，可重新确认上车点和车型。")
        rows=[dict(id=f"taxi-{i}",vehicle_class=name,min_fen=low,max_fen=high,unit="vehicle",
                   reason="此车型的整车行前估价。",evidence_ids=refs) for i,(name,low,high) in enumerate(quotes)]
        basis={key:args[key] for key in ("from_id","to_id")}
        basis.update(from_label=payload["origin"],to_label=payload["destination"],
                     from_city=payload["origin_city"],to_city=payload["destination_city"],
                     departure_at=args.get("departure_at"))
        data=card("taxi",self.mode,rows,rows[0],basis,evidence,"高德网约车整车预估区间。",quote_status="current",
                  quote_provider="amap-taxi")
        return reply(data,now,evidence=evidence,fixture=mode_for_reply(self.mode))


def provider_rows(raw,key):
    if not isinstance(raw,dict):
        return "invalid_response",[]
    if raw.get("success") is False:
        code=str(raw.get("code","")).casefold()
        mapped={"401":"auth_failed","403":"auth_failed","429":"rate_limited","402":"quota_exhausted",
                "quota_exhausted":"quota_exhausted","10044":"quota_exhausted",
                "not_found":"empty","no_results":"empty"}.get(code,"provider_error")
        return mapped,[]
    rows=raw.get(key)
    if not isinstance(rows,list):
        return "invalid_response",[]
    return ("success" if rows else "empty"),rows


def lodging_money(value,code,item_id):
    amount=money_fen(value)
    if amount is None or code is None:
        return None
    line=cost_line(dict(item_id=item_id,category="hotel",unit="group",unit_fen=amount,currency=code))
    return summarize_costs([line])["totals_by_currency"][code]


def cancellation(selected):
    if not label(selected.get("cancelPolicy"),500):
        return dict(status="unconfirmed")
    terms=label(selected["cancelPolicy"],500)
    if "不可取消" in terms or "不可退" in terms:
        status="non_refundable"
    elif "有费" in terms or "收费取消" in terms or "取消费用" in terms:
        status="fee"
    elif terms in {"免费取消","可免费取消","FREE_CANCELLATION"}:
        status="free"
    else:
        status="unconfirmed"
    return dict(status=status,terms=terms)


async def next_leg(route,hotel_id,target,hotel):
    if route is not None and target:
        try:
            found=await asyncio.wait_for(route(hotel_id,target,hotel),timeout=8)
            if (found and found.get("to_id")==target and type(found.get("travel_min")) is int
                    and found["travel_min"]>=0 and found.get("status") in {"estimate","known"}):
                return found
        except (OSError,TimeoutError,ValueError,KeyError,RuntimeError,TypeError):
            pass
    return dict(to_id=target or "next-stop-unconfirmed",travel_min=None,status="unknown")


class HotelAdapter:
    def __init__(self,call,clock,*,mode,route=None,details=2,children_supported=True):
        self.call,self.clock,self.mode,self.route=call,clock,mode,route
        self.details=max(1,min(details,2))
        self.children_supported=children_supported

    async def query(self,args,trace=None):
        now=self.clock().astimezone(BEIJING)
        location=HOTEL_REGIONS.get(args["place_id"])
        if not location:
            return failure(now,self.mode,"candidates","invalid_response")
        children=args.get("children",0)
        ages=args.get("child_ages",[])
        if children and (not self.children_supported or len(ages)!=children
                         or any(type(age) is not int or not 0<=age<=17 for age in ages)):
            return failure(now,self.mode,"candidates","unbound","请补齐每位儿童的年龄后再查询家庭入住价格。")
        check_in=date.fromisoformat(args["check_in"])
        if not 0<= (check_in-now.date()).days<=365:
            return failure(now,self.mode,"candidates","provider_error","请确认可查询的未来入住日期。")
        end=(check_in+timedelta(days=args["nights"])).isoformat()
        start=check_in.isoformat()
        criteria=dict(checkInDate=start,stayNights=args["nights"],adultCount=args["adults"])
        occupancy=dict(adultCount=args["adults"],roomCount=args["rooms"])
        if children:
            occupancy.update(childCount=children,childAgeDetails=ages)
        request=dict(originQuery=location[0]+"附近住宿",place=location[0],placeType=location[1],
                     checkInParam=criteria,size=2)
        search=await provider(self.call,"searchHotels",request,"rollinggo",trace)
        if search.status!="success":
            return failure(self.clock(),self.mode,"candidates",search.status)
        raw=search.data["raw"]
        status,items=provider_rows(raw,"hotelInformationList")
        if status!="success":
            return failure(self.clock(),self.mode,"candidates",status)
        places={}
        for row in items[:8]:
            if (isinstance(row,dict) and type(row.get("hotelId")) is int and 0<row["hotelId"]<=2**53-1
                    and label(row.get("name"))):
                places.setdefault(row["hotelId"],row)
        if not places:
            return failure(self.clock(),self.mode,"candidates","invalid_response")
        evidence=[proof("rollinggo",raw,self.clock(),start,self.mode,"本次住宿区域和入住条件的搜索结果。")]
        rows=[]
        problems=[]
        stop_detail=False
        target=args.get("next_stop_id")
        for i,(hotel_id,hotel) in enumerate(places.items()):
            if i>=2:
                break
            rate=None
            refs=[evidence[0]["evidence_id"]]
            if not stop_detail and i<self.details:
                detail_args=dict(hotelId=hotel_id,dateParam={"checkInDate":start,"checkOutDate":end},
                                 occupancyParam=occupancy)
                detail=await provider(self.call,"getHotelDetail",detail_args,"rollinggo",trace)
                if detail.status=="success":
                    detail_raw=detail.data["raw"]
                    status,rates=provider_rows(detail_raw,"roomRatePlans")
                    if status=="success" and detail_raw.get("hotelId",hotel_id)!=hotel_id:
                        status="invalid_response"
                    if status=="success":
                        rate=next((k for k in rates[:40] if isinstance(k,dict) and label(k.get("roomName"))),None)
                        if rate:
                            record=proof("rollinggo",detail_raw,self.clock(),start,self.mode,"来自本次已搜索酒店的房型查询。",
                                         scope=str(hotel_id))
                            evidence.append(record)
                            refs.append(record["evidence_id"])
                        else:
                            status="invalid_response"
                else:
                    status=detail.status
                if status!="success":
                    problems.append(status)
                if status in {"auth_failed","quota_exhausted","rate_limited"}:
                    stop_detail=True
            price=hotel.get("price")
            price=price if isinstance(price,dict) and price.get("hasPrice") is True else {}
            rate=rate or {}
            if children or args["rooms"]!=1:
                price={}
            search_code=currency(price.get("currency"))
            rate_code=currency(rate.get("currency"))
            code=rate_code or search_code or "CNY"
            nightly=lodging_money(rate.get("averagePrice") if rate.get("hasPrice") is not False else None,rate_code,"nightly")
            if nightly is None and code==search_code:
                nightly=lodging_money(price.get("lowestPrice"),search_code,"search-nightly")
            total=lodging_money(rate.get("totalPrice") if rate.get("hasPrice") is not False else None,rate_code,"stay")
            leg=dict(await next_leg(self.route,hotel_id,target,hotel))
            if route_data:=leg.pop("source_data",None):
                route_proof=proof("osm",route_data,self.clock(),end,self.mode,"酒店到次日首站的驾车估算。",
                                  scope=str(hotel_id))
                evidence.append(route_proof)
                refs.append(route_proof["evidence_id"])
            rows.append(dict(id=f"hotel-{hotel_id}",label=label(hotel["name"]),room_name=label(rate.get("roomName")) or None,
                             address=label(hotel.get("address"),250) or None,currency=code,
                             nightly_reference_fen=nightly,stay_total_fen=total,cancellation=cancellation(rate),
                             next_stop_leg=leg,missing_costs=["入住总价"] if total is None else [],
                             inventory_detail="on_request" if rate.get("isOnRequest") is True else "unverified",
                             evidence_ids=refs,reason="先核对房型、价格和退改再选择。"))
        primary=describe_hotels(rows)
        basis={name:args[name] for name in ("check_in","nights","adults","rooms")}
        basis.update(check_out=end,children=children,place_id=args["place_id"])
        data=card("hotel",self.mode,rows,primary,basis,evidence,
                  "每晚参考和整段入住总价分列。不同币种分别展示，库存须另行确认。",
                  code=primary["currency"],quote_status="partial" if problems else "current")
        event(trace,"m08_hotel_result",stay_nights=args["nights"],adults=args["adults"],children=children,
              rooms=args["rooms"],candidate_ids=[r["id"] for r in rows],detail_statuses=problems,write_count=0)
        return reply(data,self.clock(),"partial" if problems else "completed",evidence,fixture=mode_for_reply(self.mode))


def describe_hotels(rows):
    eligible=[row for row in rows if row["next_stop_leg"]["travel_min"] is not None]
    primary=min(eligible,key=lambda x:x["next_stop_leg"]["travel_min"]) if eligible else rows[0]
    for row in rows:
        other=next((item for item in rows if item is not row),None)
        minutes=row["next_stop_leg"]["travel_min"]
        text=[f"到次日首站约 {minutes} 分钟。" if minutes is not None else "次日接驳待查。"]
        if other and minutes is not None and other["next_stop_leg"]["travel_min"] is not None:
            delta=minutes-other["next_stop_leg"]["travel_min"]
            if delta:
                text.append(f"比另一家{'多' if delta>0 else '少'}约 {abs(delta)} 分钟。")
        if other and row["currency"]==other["currency"] and all(
                item["stay_total_fen"] is not None for item in (row,other)):
            delta=row["stay_total_fen"]-other["stay_total_fen"]
            if delta:
                text.append(f"同币种入住总价{'高' if delta>0 else '低'} {abs(delta)/100:g} {row['currency']}。请自行选择。")
        elif row["stay_total_fen"] is None:
            text.append("入住总价待查。")
        row["reason"]="".join(text)
    return primary




@dataclass(frozen=True)
class FoodOriginal:
    url: str
    text: str
    collected_at: datetime
    canonical_url: str | None = None
    author: str | None = None
    visit_date: date | None = None


def distance_between(lat,lon,other_lat,other_lon):
    angle=math.sin(math.radians(other_lat-lat)/2)**2
    angle+=math.cos(math.radians(lat))*math.cos(math.radians(other_lat))*math.sin(math.radians(other_lon-lon)/2)**2
    return round(12_742_000*math.asin(math.sqrt(min(1,angle))))


def poi_reference(row):
    key=row.get("id") or row.get("osm_id") or row.get("osm")
    if isinstance(key,dict):
        key=f"{key.get('type')}/{key.get('id')}"
    return key if isinstance(key,str) and re.fullmatch(r"(node|way|relation)/[0-9]{1,16}", key) else None


def clean_food_points(raw,coords):
    if not isinstance(raw,dict) or not isinstance(raw.get("results"),list):
        return "invalid_response",[]
    points={}
    for row in raw["results"][:18]:
        if not isinstance(row,dict):
            continue
        name=label(row.get("name"))
        key=poi_reference(row)
        lat,lon=row.get("lat"),row.get("lon")
        if not name or not key or not all(type(v) in (int,float) and math.isfinite(v) for v in (lat,lon)):
            continue
        if not -90<=lat<=90 or not -180<=lon<=180:
            continue
        distance=distance_between(coords["latitude"],coords["longitude"],lat,lon)
        if distance>DISTANCE_LIMIT:
            continue
        opening=row.get("opening_hours") or row.get("openingHours")
        points.setdefault(key,dict(id="food-"+key.replace("/","-"),osm_id=key,label=name,
                                  address=label(row.get("address"),240) or None,distance_m=distance,
                                  snapshot_hours=label(opening,240) or None,collected_at=row.get("collected_at")))
    if not points:
        return ("invalid_response" if raw["results"] else "empty"),[]
    return "success",sorted(points.values(),key=lambda item:item["distance_m"])


def source_excerpt(body,name):
    sentences=re.split(r"[。！？；\n]+",body)
    return next((label(s,200) for s in sentences if name in s),"")


def food_evidence(originals,point,meal,now,mode):
    checked=[]
    for original in originals[:6]:
        if not isinstance(original,FoodOriginal) or not public_url(original.url) or original.collected_at>now:
            continue
        excerpt=source_excerpt(original.text,point["label"])
        if not excerpt:
            continue
        material=Material(original.url,original.text,format_time(original.collected_at),mode,
                          canonical_url=original.canonical_url,source_kind="other")
        checked.append((original,material,excerpt))
    groups=group_sources([entry[1] for entry in checked])
    rows={}
    for (original,material,excerpt),group in zip(checked,groups,strict=True):
        promo=bool(re.search(r"广告|赞助|推广合作|商业合作|推广|sponsored",material.text,re.I))
        recent=(original.author and original.visit_date and 0 <= (now.date()-original.visit_date).days<=90)
        first_person=bool(re.search(r"(我|我们)(?:到访|在|去过|去|点了|吃了)|I (?:visited|ate)",excerpt,re.I))
        role="sponsored_claims" if promo else "independent_reviews" if recent and first_person else "public_original"
        row=dict(evidence_id="m8-"+digest(group+"|"+point["id"])[:24],source_url=public_url(original.url),
                 publisher=urlsplit(original.url).hostname,source_group_id=group,collected_at=material.retrieved_at,
                 applies_to=[],source_kind="fixture" if mode=="fixture" else "other",provider="public-original",
                 data_mode=mode,excerpt=excerpt,content_hash=material.content_hash)
        previous=rows.get(group)
        if previous is None or role=="sponsored_claims":
            rows[group]=(row,role)
    return list(rows.values())


class FoodAdapter:
    def __init__(self,poi,originals,clock,*,mode):
        self.poi,self.originals,self.clock,self.mode=poi,originals,clock,mode
        self.catalog=public_catalog()

    async def query(self,args,trace=None):
        now=self.clock().astimezone(BEIJING)
        meal=instant(args["meal_at"])[0]
        place=self.catalog.get(args["place_id"])
        if not meal or not place or not place.coordinates:
            return failure(now,self.mode,"candidates","invalid_response","请确认用餐地点和时间。")
        coords=place.coordinates
        req=dict(near=f"{coords['latitude']},{coords['longitude']}",category="amenity=restaurant",
                 radius_m=DISTANCE_LIMIT,limit=6,language="zh")
        found=await provider(self.poi,"find_nearby_pois",req,"osm",trace)
        if found.status!="success":
            return failure(self.clock(),self.mode,"candidates",found.status)
        raw=found.data["raw"]
        status,points=clean_food_points(raw,coords)
        if status!="success":
            return failure(self.clock(),self.mode,"candidates",status)
        day=meal.date().isoformat()
        location_proof=proof("osm",raw,self.clock(),day,self.mode,"餐馆地图点位与直线距离。营业须另行核实。")
        evidence={location_proof["evidence_id"]:location_proof}
        rows=[]
        for i,point in enumerate(points[:2]):
            try:
                originals=await asyncio.wait_for(self.originals(point["label"],place.label,meal,trace),timeout=10) if self.originals else ()
            except (OSError,TimeoutError,ValueError,RuntimeError):
                originals=()
            inspected=food_evidence(list(originals or ()),point,meal,self.clock(),self.mode)
            refs=[location_proof["evidence_id"]]
            review_refs=[]
            promo_refs=[]
            for item,role in inspected:
                key=item["evidence_id"]
                evidence[key]=item
                refs.append(key)
                if role=="independent_reviews":
                    review_refs.append(key)
                elif role=="sponsored_claims":
                    promo_refs.append(key)
            qualifiers="首选按直线距离较近。" if i==0 else f"备选比首选远约 {point['distance_m']-points[0]['distance_m']} 米。"
            facts=[f"地图直线距离约 {point['distance_m']} 米。"]
            if point["address"]:
                facts.append(f"地图记录地址 {point['address']}。")
            if point['snapshot_hours']:
                facts.append("地图营业标签为查询时看到的快照，实际时段待核实。")
            observed, _ = instant(point["collected_at"])
            collected=(format_time(observed) if observed and observed<=self.clock() else location_proof["collected_at"])
            rows.append(dict(id=point["id"],label=point["label"],place_id=point["id"],address=point["address"],
                             reason=qualifiers+"请自行核对口味、排队和当晚营业。",
                             distance_m=point["distance_m"],price_per_person_fen=None,evidence_ids=refs,
                             visit_evidence_ids=review_refs,verified_facts=facts,independent_reviews=review_refs,
                             sponsored_claims=promo_refs,uncertainties=["口味","排队","当晚营业","人均费用"],
                             opening_status="unconfirmed",snapshot_hours=point["snapshot_hours"],
                             snapshot_at=collected,promotion_status="declared" if promo_refs else "unconfirmed"))
            event(trace,"m08_food_originals",candidate_id=point["id"],reference_ids=refs,
                  independent_count=len(review_refs),sponsored_count=len(promo_refs),write_count=0)
        rows.sort(key=lambda row:row["distance_m"])
        result=card("dining",self.mode,rows,rows[0],{"meal_at":format_time(meal),"place_id":place.id},
                    list(evidence.values()),"按附近距离比较，人均费用与实际营业待查。")
        return reply(result,self.clock(),evidence=evidence.values(),fixture=mode_for_reply(self.mode))


class CandidateSkills:
    def __init__(self,clock,*,mode,taxi=None,hotel=None,food=None):
        self.clock,self.mode=clock,mode
        self.taxi,self.hotel,self.food=taxi,hotel,food

    def supports(self,kind,context):
        return kind=="pickup_choices" or (kind=="candidates" and context.get("candidate_kind") in {"taxi","hotel","dining"})

    def resolve(self,kind,context):
        args=dict(context)
        style="taxi" if kind=="pickup_choices" else args["candidate_kind"]
        args["candidate_kind"]=style
        aliases={"贵阳北站":"guiyang-north","贵阳北":"guiyang-north","青岩古镇北门停车场":"qingyan-north-parking", "青岩北门停车场":"qingyan-north-parking",
                 "青岩古镇":"qingyan","青岩":"qingyan","贵阳":"guiyang","贵阳市":"guiyang","甲秀楼":"jiaxiulou"}
        keys=("from_id","to_id") if style=="taxi" else ("place_id",)
        for key in keys:
            value=aliases.get(args.get(key),args.get(key))
            if (style=="taxi" and value not in CITY_POINTS or style=="hotel" and value not in HOTEL_REGIONS
                    or style=="dining" and not (self.food and self.food.catalog.get(value) and self.food.catalog.get(value).coordinates)):
                return args,key
            args[key]=value
        if style=="hotel" and args.get("children",0)>0 and len(args.get("child_ages",[]))!=args["children"]:
            return args,"child_ages"
        if style=="taxi" and args["from_id"]==args["to_id"]:
            return args,"to_id"
        return args,None

    async def handle(self,kind,args,scope,trace=None):
        if kind=="pickup_choices" or args["candidate_kind"]=="taxi":
            if self.taxi is not None:
                return await self.taxi.query(args,scope,trace)
        elif args["candidate_kind"]=="hotel" and self.hotel is not None:
            return await self.hotel.query(args,trace)
        elif args["candidate_kind"]=="dining" and self.food is not None:
            return await self.food.query(args,trace)
        return failure(self.clock(),self.mode,kind,"unbound")

    async def choose(self,scope,context_id,choice_id,trace=None):
        from travel_candidate_context import ChoiceError

        if self.taxi is None:
            raise ChoiceError("forbidden")
        return await self.taxi.choose(scope,context_id,choice_id,trace)

