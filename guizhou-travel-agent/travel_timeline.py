"""The single two-day Guiyang planning slice. No provider calls except route."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
from threading import RLock
from uuid import uuid4

from travel_contract import validate_reply
from travel_fares import cost_line as rule_cost_line, summarize_costs
from travel_rules import POLICY_VERSION, validate_itinerary
from travel_state import BEIJING
from travel_timeline_route import OSM_SOURCE

FIXTURE = Path(__file__).resolve().parent / "tests/travel_agent/fixtures/m04/gz2d.json"
MESSAGE = "帮我安排贵阳两人两天的行程。"
PLAN = (
    (("d1-jiaxiu","jiaxiu-entry","10:00","11:00","walk"), ("d1-wenchang","wenchang-entry","11:25","12:30","walk")),
    (("d2-museum","museum-entry","09:30","11:30","car"), ("d2-park","park-entry","13:00","14:30","car")),
)
RETURN_BUFFER = 30
CATEGORIES = ("hotel","meal","transport","ticket")


@dataclass(frozen=True)
class Endpoint:
    id: str
    poi_id: str
    label: str
    latitude: float
    longitude: float
    coordinate_system: str
    confirmed: bool
    source_url: str

    def __post_init__(self):
        if (not self.id or not self.poi_id or not self.label or
                not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180):
            raise ValueError("入口坐标或 ID 无效")

    @property
    def waypoint(self):
        return f"{self.latitude},{self.longitude}"


@dataclass
class TimelineFixture:
    form: dict
    catalog: dict
    costs: list
    legs: dict

    async def route(self, arguments):
        points = {point.waypoint: point.id for point in self.catalog.values()}
        first, last = arguments["waypoints"]
        result = self.legs.get(f"{points[first]}/{points[last]}")
        return deepcopy(result)


def load_fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    points = {row["id"]: Endpoint(**row, coordinate_system="WGS84", confirmed=True,
                                  source_url="fixture://catalog-"+row["id"]) for row in data["catalog"]}
    return TimelineFixture(data["form"], points, data["costs"], data["legs"])


def cost_line(item, *, fixture):
    if item["category"] not in CATEGORIES:
        raise ValueError("费用分类无效")
    unit = item.get("unit_fen") if fixture else None
    line = rule_cost_line(dict(item, unit_fen=unit, evidence_ids=["m04-costs"] if unit is not None else []),
                          traveler_group="group", data_mode="fixture" if fixture else "snapshot")
    line.pop("data_mode")
    if unit is None:
        line["missing_reason"] = "需核实整项实际费用"
    return line


def budget_summary(cap, costs, *, fixture=True):
    if type(cap) is not int or cap < 0:
        raise ValueError("需要非负的全团预算")
    if set(item["category"] for item in costs) != set(CATEGORIES) or len({i["item_id"] for i in costs}) != len(costs):
        raise ValueError("费用范围不完整或重复")
    items = [cost_line(item, fixture=fixture) for item in costs]
    summary = summarize_costs(items, cap)
    return dict(scope="group", cap_fen=cap, known_lower_fen=summary["known_lower_fen"],
                total_fen=summary["total_fen"], missing_costs=summary["missing_costs"],
                budget_status=summary["budget_status"], included_categories=list(CATEGORIES),
                excluded_locked_return=True,
                items=items, reference_prices=[{"item_id":i["item_id"], "reference_fen":i["reference_fen"]}
                                               for i in costs if fixture and i["item_id"] in summary["missing_costs"]
                                               and "reference_fen" in i])


def next_stop(stops):
    return next((s["stop_id"] for s in stops if not s.get("completed") and s["plan_role"] == "primary"
                 and s["opening"]["status"] == "confirmed" and s["arrival_local"] is not None
                 and s["incoming_leg"]["status"] in {"estimate","known"}), None)


def stamp(day, wall):
    parsed = time.fromisoformat(wall)
    if parsed.tzinfo is not None:
        raise ValueError("表单时段按北京时间填写")
    return datetime.combine(date.fromisoformat(day), parsed, BEIJING).isoformat(timespec="seconds")


def return_lock_for(result, locked_items):
    return_ids = {row["lock_id"] for row in result["locked_items"] if row["kind"] in {"rail", "flight", "other"}}
    lock = next((item for item in locked_items
                 if item.stable_id in return_ids and "departure_at" in item.fields), None)
    if lock is None:
        raise ValueError("时间轴缺少对应的已锁定返程")
    return lock


def calc_feasibility(result, *, locked_items, checked_at, affected=None):
    stops = []
    for day in result["days"]:
        day_stops = day["stops"]
        prior = day["window"]["starts_at"]
        prior_endpoint = day_stops[0]["incoming_leg"]["from_id"]
        changed = affected is None or any(stop["stop_id"] in affected for stop in day_stops)
        for index, stop in enumerate(day_stops):
            leg = dict(stop["incoming_leg"], starts_at=prior, expected_from_id=prior_endpoint)
            stops.append(dict(stop_id=stop["stop_id"], date=day["date"], entrance_id=stop["entrance_id"],
                              arrival_at=stop["arrival_local"], departure_at=stop["departure_local"],
                              ends_at=day["window"]["ends_at"], incoming_leg=leg, check_opening=True, check_booking=True))
            prior = stop["departure_local"]
            prior_endpoint = stop["entrance_id"]
            if changed:
                stop["next_stop_id"] = next_stop(day_stops[index+1:]) if stop["arrival_local"] is not None else None
        if changed:
            day["next_stop_id"] = next_stop(day_stops)
    candidates = [dict(stable_id=item["lock_id"], lock_revision=item.get("lock_revision", 1), kind=item["kind"],
                       fields={"departure_at":item["departure_at"]}) for item in result["locked_items"]]
    projected_ids = {item["stable_id"] for item in candidates}
    locks = []
    for item in locked_items:
        fields = {"departure_at":item.fields.get("departure_at")} if item.stable_id in projected_ids else item.fields
        lock = dict(stable_id=item.stable_id, lock_revision=item.lock_revision, kind=item.kind, fields=fields)
        locks.append(lock)
        if item.stable_id not in projected_ids and item.kind in {"hotel", "booking"}:
            candidates.append(lock)
    returning = result["return_leg"]
    returning["leave_at"] = stops[-1]["departure_at"]
    return_inputs = dict(returning, departure_at=return_lock_for(result, locked_items).fields["departure_at"],
                         buffer_source="GZ2D fixture or preview estimate")
    inputs = dict(stops=stops, checked_at=checked_at, data_mode=result["data_mode"], locked_items=candidates,
                  return_leg=return_inputs, cost_lines=result["budget"]["items"],
                  budget={"original_budget_fen":result["budget"]["cap_fen"], "original_budget_scope":"group"})
    report = validate_itinerary(inputs, (), locks, POLICY_VERSION, affected_stop_ids=affected,
                                previous_report=result.get("validation_report"))
    returning["arrival_at"] = report["time_details"].get("_return", {}).get("arrival_at")
    result["validation_report"] = report
    result["feasibility"] = report["feasibility"]
    labels = {stop["stop_id"]:stop["label"] for day in result["days"] for stop in day["stops"]}
    messages = []
    for status in ("fail", "pending"):
        for row in report["rule_results"]:
            if row["status"] != status:
                continue
            fields = row["path"].split("/")
            label = labels.get(fields[2]) if fields[1] == "stops" else None
            action = row["suggested_action"]
            if row["rule_id"] in {"opening", "booking"} and status == "pending":
                action = "请核对当天开放时段和预约要求。"
            messages.append(f"{label}：{action}" if label else action)
    result["pending"] = list(dict.fromkeys(messages))


class TimelineWorkflow:
    """Form → current agent turn → validated unsaved draft → explicit M3 save."""

    def __init__(self, app, catalog, adapter, costs, *, reference_scaffold=False):
        self.app, self.catalog, self.adapter, self.costs = app, catalog, adapter, costs
        self.fixture_preview = adapter.fixture or reference_scaffold
        self.saved_drafts = set()
        self.mutex = RLock()

    def _reply(self, result, state, trace_id, evidence=()):
        now = self.app.clock().astimezone(BEIJING).isoformat()
        packet = dict(schema_version="1.0", trace_id=trace_id, generated_at=now, timezone="Asia/Shanghai",
                      run_status=state, persistence="none", result=result, evidence=list(evidence))
        return validate_reply(packet, fixture_mode=self.fixture_preview)

    async def preview(self, token, form, *, trace=None):
        from travel_agent import Conversation

        trace_id = getattr(trace, "trace_id", None) or uuid4().hex

        def ask(field, question):
            return self._reply(dict(kind="clarification", field=field, question=question, known={}),
                               "needs_input", trace_id)

        if not isinstance(form, dict):
            raise ValueError("首页表单无效")
        current = {k: form[k] for k in ("destination","start_date","end_date","adults","days","budget_scope","budget_fen") if k in form}
        if current.get("destination") != "贵阳" or current.get("adults") != 2 or current.get("days") != 2:
            raise ValueError("本入口只接受贵阳两人两天试行方案")
        if current.get("budget_scope") != "group":
            raise ValueError("请输入全团预算")
        draft = self.app.preview_trip(token, current, store_draft=False)
        if draft.next_question:
            # M3 provides the trusted first missing field.
            return ask(draft.missing_fields[0], draft.next_question)
        starts = form.get("day_starts", [])
        ret = form.get("return")
        if not isinstance(starts, list) or len(starts) != 2 or not all(isinstance(s, dict) for s in starts):
            return ask("arrival_at", "两天分别从几点开始游览？")
        if (not isinstance(ret, dict) or ret.get("confirmed") is not True or
                not isinstance(ret.get("departure_at"), str)):
            return ask("arrival_at", "请确认贵阳北站返程的发车时间？")
        calendar = (draft.fields["start_date"], draft.fields["end_date"])
        ids = {row[1] for day in PLAN for row in day} | {s.get("entrance_id") for s in starts} | {ret.get("entrance_id")}
        if any(key not in self.catalog or not self.catalog[key].confirmed for key in ids):
            return ask("place_id", "请确认本次出发、到访或返程的具体入口？")
        if ret["entrance_id"] != "guiyang-north-entry":
            raise ValueError("本方案的已确认返程站点为贵阳北站进站口")
        ret_dt = datetime.fromisoformat(ret["departure_at"])
        if ret_dt.utcoffset() is None or ret_dt.astimezone(BEIJING).date().isoformat() != calendar[1]:
            raise ValueError("返程必须为第二天带时区的时刻")
        windows = []
        for day, source in zip(calendar, starts):
            if not all(isinstance(source.get(key),str) for key in ("start","end")):
                return ask("arrival_at", "请补充当天的起止时段？")
            first, last = stamp(day,source["start"]),stamp(day,source["end"])
            if first >= last:
                raise ValueError("当天结束时刻须晚于开始时刻")
            windows.append(dict(starts_at=first, ends_at=last))
        arrangement = {"kind":"rail","fields":{"purpose":"return","departure_at":ret_dt.astimezone(BEIJING).isoformat(),
                                               "to_label":self.catalog[ret["entrance_id"]].label}}
        draft = self.app.preview_trip(token, current, arrangements=(arrangement,), confirm_arrangements=True)
        candidate = json.dumps({"intent":"itinerary","confidence_band":"high","requested_outputs":["itinerary"],"parameters":{}})

        async def explicit_intent(*_):
            # The user explicitly pressed the planning action. No model inference is required.
            return candidate

        async def timeline_skill(_):
            return await self.build(draft, starts, windows, ret, calendar, trace_id, trace)

        agent = Conversation(typed_output=True, clock=self.app.clock, intent_model=explicit_intent,
                             fixture_mode=self.fixture_preview, timeline_handler=timeline_skill)
        try:
            response = await self.app.turn(token, MESSAGE, draft.fields, conversation_factory=lambda: agent, trace=trace)
            if response["result"]["kind"] == "itinerary":
                self.app.attach_timeline(token, draft.draft_id, response, fixture_mode=self.fixture_preview)
            else:
                self.app.discard_draft(token, draft.draft_id)
        except BaseException:
            self.app.discard_draft(token, draft.draft_id)
            raise
        return response

    async def build(self, draft, starts, windows, ret, calendar, trace_id, trace):
        fixture = self.adapter.fixture
        data_mode = "fixture" if self.fixture_preview else "snapshot"
        evidence = {}
        now = self.app.clock().astimezone(BEIJING).isoformat()
        attempts = 0
        cutoff = asyncio.get_running_loop().time() + 12

        def source(key, url, publisher, days, excerpt, mode=data_mode, checked=now):
            evidence[key] = dict(evidence_id=key, source_url=url, publisher=publisher, source_group_id=key,
                                 collected_at=checked, applies_to=list(days), source_kind="fixture" if mode == "fixture" else "provider",
                                 data_mode=mode, excerpt=excerpt)
            return key

        def point_source(point):
            simulated = point.source_url.startswith("fixture://")
            return source("endpoint-"+point.id, point.source_url, "模拟入口目录" if simulated else "已确认入口目录",
                          calendar, point.label, "fixture" if simulated else "snapshot")

        async def read_route(start, end, mode, day):
            nonlocal attempts
            keys = [point_source(start), point_source(end)]
            result = None
            remaining = cutoff-asyncio.get_running_loop().time()
            if attempts < self.adapter.max_attempts and remaining > 0:
                attempts += 1
                try:
                    result = await asyncio.wait_for(self.adapter.route(start,end,mode), timeout=remaining)
                except TimeoutError:
                    pass
            status = result.status if result else ("unbound" if attempts >= self.adapter.max_attempts and remaining > 0 else "timeout")
            if result and (result.from_id, result.to_id) != (start.id, end.id):
                status = "endpoint_mismatch"
            known = status == "success"
            if known:
                keys.append(source("route-"+start.id+"-"+end.id,
                                   "fixture://route-"+start.id+"-"+end.id if fixture else OSM_SOURCE,
                                   "模拟路段" if fixture else "OpenStreetMap 路由", (day,),
                                   f"{result.distance_m} 米，{result.duration_s} 秒。路段估算不包含运营班次。",
                                   result.data_mode, result.checked_at))
            if trace is not None and hasattr(trace,"record"):
                trace.record("timeline_route", from_id=start.id, to_id=end.id, profile=mode, status=status,
                             data_mode=data_mode, attempts=attempts)
            return dict(from_id=start.id,to_id=end.id,mode=mode,profile="foot" if mode=="walk" else "car",
                        coordinate_system="WGS84", distance_m=result.distance_m if known else None,
                        duration_s=result.duration_s if known else None, checked_at=result.checked_at if result else now,
                        travel_min=math.ceil(result.duration_s/60) if known else None,
                        status="estimate" if known else "unknown", reason=None if known else status,
                        data_mode=result.data_mode if known else data_mode,
                        evidence_ids=list(dict.fromkeys(keys)))

        budget = budget_summary(draft.fields["group_cap_fen"], self.costs, fixture=fixture)
        if fixture and any(i["evidence_ids"] for i in budget["items"]):
            source("m04-costs","fixture://m04-costs","GZ2D 模拟费用",calendar,"模拟金额，不能用于真实报价或出行。")
        days = []
        for day, plan, start, window in zip(calendar, PLAN, starts, windows):
            stops = []
            previous = self.catalog[start["entrance_id"]]
            for stop_id, point_id, arrive, leave, mode in plan:
                point = self.catalog[point_id]
                leg = await read_route(previous,point,mode,day)
                per_stop = next(line for line in budget["items"] if line["item_id"] == f"{stop_id}-ticket")
                cost_total = per_stop["subtotal_fen"]
                usable = leg["status"] == "estimate"
                stops.append(dict(stop_id=stop_id, poi_id=point.poi_id, entrance_id=point.id, label=point.label,
                                  plan_role="alternative", completed=False,
                                  arrival_local=stamp(day,arrive) if usable else None,
                                  departure_local=stamp(day,leave) if usable else None,
                                  next_stop_id=None,incoming_leg=leg, opening={"status":"unconfirmed","applies_to":day},
                                  cost={"status":"known" if cost_total is not None else "unknown","currency":"CNY",
                                        "total_fen":cost_total,"items":[per_stop],"missing_costs":[] if cost_total is not None else ["站点费用待查"]},
                                  evidence_ids=list(dict.fromkeys([*leg["evidence_ids"],*per_stop["evidence_ids"]]))))
                previous = point
            days.append(dict(date=day, window=window, stops=stops, next_stop_id=None))
        returning = await read_route(previous,self.catalog[ret["entrance_id"]],"car",calendar[-1])
        returning.update(leave_at=None,arrival_at=None,buffer_min=RETURN_BUFFER)
        lock = draft.locked_items[0]
        points = {key for day in days for stop in day["stops"] for key in
                  (stop["incoming_leg"]["from_id"],stop["entrance_id"])} | {returning["to_id"]}
        result = dict(kind="itinerary",data_mode=data_mode,draft_id=draft.draft_id,save_status="draft",
                      feasibility="needs_verification", party={"adults":draft.fields["adults"]},budget=budget,days=days,
                      locked_items=[dict(lock_id=lock.stable_id,kind="rail",locked=True,
                                         departure_at=lock.fields["departure_at"],confirmation="user_confirmed",
                                         lock_revision=lock.lock_revision)],
                      return_leg=returning,pending=[],
                      map_points=[dict(id=point.id,label=point.label,latitude=point.latitude,longitude=point.longitude)
                                  for point in (self.catalog[key] for key in sorted(points))])
        calc_feasibility(result, locked_items=draft.locked_items, checked_at=now)
        reasons = [returning["reason"], *(stop["incoming_leg"]["reason"] for day in days for stop in day["stops"])]
        state = "partial" if any(reason not in {None, "endpoint_mismatch"} for reason in reasons) else "completed"
        if trace is not None and hasattr(trace, "record"):
            trace.record("timeline_preview", route_attempts=attempts, write_count=0, data_mode=data_mode,
                         known_lower_fen=budget["known_lower_fen"], total_fen=budget["total_fen"],
                         next_stop_ids=[day["next_stop_id"] for day in days], feasibility=result["feasibility"])
        return self._reply(result,state,trace_id,evidence.values())

    def edit(self, token, draft_id, stop_id, arrival, departure):
        with self.mutex:
            draft = self.app.get_draft(token,draft_id)
            if draft_id in self.saved_drafts:
                raise ValueError("已保存行程不能在 M4 修改")
            if not draft.timeline:
                raise ValueError("该草稿没有时间轴")
            packet = deepcopy(draft.timeline)
            for day in packet["result"]["days"]:
                for index, stop in enumerate(day["stops"]):
                    if stop["stop_id"] == stop_id:
                        if stop["incoming_leg"]["travel_min"] is None:
                            raise ValueError("缺少有效路段，计划时间仍需核验")
                        before, after = stamp(day["date"],arrival), stamp(day["date"],departure)
                        if after <= before:
                            raise ValueError("离开时间须晚于到达时间")
                        stop.update(arrival_local=before,departure_local=after)
                        result = packet["result"]
                        prior_report = result.get("validation_report")
                        affected = [row["stop_id"] for row in day["stops"][index:index+2]]
                        calc_feasibility(result, locked_items=draft.locked_items, checked_at=packet["generated_at"],
                                         affected=affected if prior_report else None)
                        return self.app.attach_timeline(token,draft_id,packet,clone=True,fixture_mode=self.fixture_preview)
            raise ValueError("未知行程站点")

    def cancel(self, token, draft_id):
        with self.mutex:
            self.app.get_draft(token,draft_id)
            if draft_id in self.saved_drafts:
                raise ValueError("已保存行程不能取消")
            self.app.discard_draft(token,draft_id)

    def save(self, token, draft_id, key, *, confirmed):
        if confirmed is not True:
            raise ValueError("请明确确认保存")
        with self.mutex:
            trip = self.app.create_trip(token,draft_id,save=True,idempotency_key=key,require_timeline=True)
            self.saved_drafts.add(draft_id)
            return trip

