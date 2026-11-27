"""M2 read-only routing for the existing single Conversation."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
import json
import re
from time import monotonic
from uuid import uuid4

from jsonschema import Draft202012Validator
from travel_catalog import public_catalog
from travel_contract import ContractError, FORMATS, INTENT, REPLY, TOOLS, check, decode_intent, validate_reply
from travel_state import SessionContext, build_context

PARAMETERS = INTENT.schema["properties"]["parameters"]
HOTEL_FIELDS = ("check_in", "nights", "adults", "rooms")
CANDIDATE_FIELDS = {
    "hotel": HOTEL_FIELDS, "dining": ("meal_at", "place_id"), "train": ("date", "from_id", "to_id", "adults"),
    "flight": ("date", "from_id", "to_id", "adults"),
    "intercity": ("date", "from_id", "to_id", "adults"), "taxi": ("from_id", "to_id"),
}
QUESTIONS = {
    "place_id": "你想查哪个景点？", "scenic_area": "你指的是哪个景区的水帘洞？",
    "date": "你要查哪一天？", "start_date": "你计划哪天开始？", "from_id": "你从哪个站出发？",
    "to_id": "你要到哪个入口？", "trip_id": "你想修改或查看哪份行程？",
    "base_revision": "请先打开要查看的行程版本？", "locked_ids": "有哪些安排需要保留？",
    "adults": "有几位成人同行？", "days": "计划游玩几天？", "group_cap_fen": "全团预算是多少？",
    "destination": "你想去哪座城市？", "candidate_kind": "你要比较住宿、餐饮还是交通？",
    "check_in": "你哪天入住？", "nights": "你住几晚？", "rooms": "你需要几间房？",
    "meal_at": "你打算什么时候用餐？", "format": "你要 HTML 还是 PDF？",
    "scope_date": "你想修改哪一天？",
}


@dataclass(frozen=True)
class Skill:
    skill_id: str
    tool: str
    required: tuple
    output_kind: str
    version: str = "1.0"


SKILLS = {
    kind: Skill(skill, tool, fields, kind)
    for kind, skill, tool, fields in (
        ("weather", "S5", "get_destination_weather", ("place_id", "date")),
        ("verification", "S6", "search_travel_sources", ("scenic_area", "date")),
        ("transfer", "S8", "fixture_transfer", ("from_id", "to_id", "date")),
        ("cost_breakdown", "E6", "fixture_cost", ("place_id", "date", "adults")),
        ("candidates", "E3/E4/S3/E2", "fixture_candidates", ("candidate_kind",)),
        ("itinerary", "S1", "fixture_itinerary", ("destination", "start_date", "adults", "days", "group_cap_fen")),
        ("patch_preview", "S1", "fixture_patch_preview", ("trip_id", "base_revision", "locked_ids")),
        ("pickup_choices", "E2", "fixture_pickup_choices", ("from_id", "to_id")),
        ("recheck_report", "E7", "fixture_recheck", ("trip_id", "base_revision")),
        ("export_result", "S9", "fixture_export", ("trip_id", "base_revision", "format")),
    )
}
STATEMENT = re.compile(r"^(?:我(?:们)?(?:两个人|三个人|一家三口)?[，,\s]*)?(今天|明天|这周六|周六)?\s*(?:准备|计划|打算|要|会)?去([^？?。！!，,；;、\n]{1,25})[。！!]?$")
REQUEST_MARKER = re.compile("吗|么|怎么|帮|请|多少|如何|查|推荐|安排|值不值|好不好|可以|能否|有没有|下雨|改成|开门|门票|预约|住哪|吃哪")
LOCK_CHANGE = re.compile("晚点走|早点走|推迟出发|提前出发|(?:改动?|换|取消)(?:一下|我的|已订|已确认)?(?:返程|酒店|预约|车票)|(?:返程|酒店|预约|车票)[^，,。；;！？!?]{0,6}(?:改到|改成|取消)")
TRUSTED_ONLY = {"trip_id", "base_revision", "locked_ids", "scenic_area"}
FIXTURE_PLACES = {"青岩": "qingyan", "青岩古镇": "qingyan"}
ROUTE_CATALOG = public_catalog()
PARTY_DIGITS = {char: value for value, char in enumerate("零一二三四五六七八九")} | {"两": 2}
HOTEL_CHECK_IN = re.compile(r"(今天|明天|这周六|周六|20[0-9]{2}-[0-9]{2}-[0-9]{2})\s*(?:当天)?入住")
HOTEL_COUNTS = {
    "adults": re.compile(r"(?<![0-9零一二两三四五六七八九十百])([1-9][0-9]?|[一二两三四五六七八九])\s*(?:位|名|个)?\s*成人"),
    "rooms": re.compile(r"(?<![0-9零一二两三四五六七八九十百])([1-9][0-9]?|[一二两三四五六七八九])\s*间\s*(?:客房|房)"),
    "nights": re.compile(r"(?<![0-9零一二两三四五六七八九十百])([1-9][0-9]?|[一二两三四五六七八九])\s*晚"),
}
PARTY_COUNTS = re.compile(r"(?<![0-9零一二两三四五六七八九十百不])([1-9][0-9]?|[一二两三四五六七八九])\s*大\s*([0-9]|[零一二两三四五六七八九])\s*小(?!时)")
TRANSPORT_TOOLS = {"weather":"get_destination_weather","transfer":"m07_transfer","candidates":"m07_intercity"}
TRANSPORT_FIELDS = {"weather":("hourly",), "transfer":("arrival_at","mode","reservation_cutoff_at"),
                    "candidates":("children",)}
CANDIDATE_OPTIONS=("candidate_kind","place_id","vehicle_class","departure_at","children","child_ages","next_stop_id")
ROUTE_FIELDS = {
    "weather": (), "verification": ("place_id",), "transfer": ("arrival_at",),
    "cost_breakdown": ("children", "scenic_area"), "candidates": ("children",),
    "itinerary": ("children", "locked_ids"), "patch_preview": ("scope_date",),
}


def travel_statement(message):
    match = STATEMENT.fullmatch(message.strip())
    return match.groups() if match and not REQUEST_MARKER.search(match[2]) else None


def calendar_day(raw, today):
    if raw in {"今天", "明天", "周六", "这周六"}:
        offset = {"今天": 0, "明天": 1, "周六": (5-today.weekday()) % 7,
                  "这周六": (5-today.weekday()) % 7}[raw]
        return (today + timedelta(days=offset)).isoformat()
    return date.fromisoformat(raw).isoformat()


def resolve_parameters(explicit, kinds, message, now, *, scenic_hint=None):
    fields = {"place_id": {"poi", "entrance"}}
    if "transfer" in kinds:
        fields.update(from_id={"station", "airport"}, to_id={"entrance", "parking", "poi"})

    if "cost_breakdown" in kinds:
        destination = explicit.get("destination") or scenic_hint
        if "place_id" not in explicit and destination and destination in message:
            places = ROUTE_CATALOG.resolve(destination, kind="poi")
            if len(places) == 1:
                explicit["place_id"] = places[0].id
        parties = set(PARTY_COUNTS.findall(message))
        if len(parties) == 1:
            for field, raw in zip(("adults", "children"), next(iter(parties))):
                explicit.setdefault(field, PARTY_DIGITS[raw] if raw in PARTY_DIGITS else int(raw))

    if "candidates" in kinds and explicit.get("candidate_kind") == "hotel":
        dates = set(HOTEL_CHECK_IN.findall(message))
        if "check_in" not in explicit and len(dates) == 1:
            try:
                explicit["check_in"] = calendar_day(next(iter(dates)), now.date())
            except ValueError:
                pass
        for field, pattern in HOTEL_COUNTS.items():
            matches = {PARTY_DIGITS[raw] if raw in PARTY_DIGITS else int(raw) for raw in pattern.findall(message)}
            if len(matches) == 1:
                explicit.setdefault(field, next(iter(matches)))

    for field, allowed_kinds in fields.items():
        if field not in explicit or ROUTE_CATALOG.get(explicit[field]):
            continue
        matches = [item for item in ROUTE_CATALOG.resolve(explicit[field]) if item.kind in allowed_kinds]
        if len(matches) == 1:
            explicit[field] = matches[0].id

    if "transfer" in kinds:
        unresolved = [field for field in ("from_id", "to_id")
                      if not ROUTE_CATALOG.get(explicit.get(field))]
        if unresolved:
            # Recover only a single route explicitly spelled out with registered labels.
            entities = ROUTE_CATALOG.entities.values()
            origins = [item for item in entities if item.kind in {"station", "airport"}]
            destinations = [item for item in entities if item.kind == "entrance"]
            pairs = {(origin.id, target.id) for origin in origins for target in destinations
                     if re.search(re.escape(origin.label) + r"\s*(?:出发\s*[,，]?\s*)?(?:到|去|前往)\s*"
                                  + re.escape(target.label), message)}
            if len(pairs) == 1:
                route = dict(zip(("from_id", "to_id"), next(iter(pairs))))
                if all(field in unresolved or explicit[field] == route[field] for field in route):
                    explicit.update({field: route[field] for field in unresolved})


def safe_context(context):
    values = {}
    checks = REPLY.schema["$defs"]["clarification"]["properties"]["known"]["properties"]
    for key, value in context.items():
        if key in checks and Draft202012Validator(checks[key], format_checker=FORMATS).is_valid(value):
            values[key] = value
    return values


def failure(kind, context, now, reason):
    action = "这项信息暂时无法核实，请以官方渠道确认。"
    if kind == "weather" and {"place_id", "date"} <= context.keys():
        return dict(kind="weather_unavailable", place_id=context["place_id"], target_date=context["date"],
                    reason=reason, checked_at=now.isoformat(), action=action)
    return dict(kind="unavailable", requested_kind=kind, reason=reason, action=action)


def match_request(result, kind, context):
    if kind == "weather":
        return (result["target_date"], result["place"]["poi_id"]) == (context["date"], context["place_id"])
    if kind == "verification":
        area = {"黄果树": "huangguoshu-water-curtain"}.get(context["scenic_area"])
        return result["target_date"] == context["date"] and (area is None or result["subject_id"] == area)
    if kind == "transfer":
        return (result["date"], result["from_id"], result["to_id"]) == tuple(context[k] for k in ("date", "from_id", "to_id"))
    if kind == "cost_breakdown":
        return all(result["party"].get(key, 0) == context[key] for key in ("adults", "children") if key in context)
    if kind == "candidates":
        return result["candidate_kind"] == context["candidate_kind"] and all(
            result["basis"][key] == context[key] for key in CANDIDATE_FIELDS[result["candidate_kind"]])
    if kind == "itinerary":
        return (result["days"][0]["date"] == context["start_date"] and len(result["days"]) == context["days"]
                and result["party"]["adults"] == context["adults"]
                and result["budget"]["scope"] == "group" and result["budget"]["cap_fen"] == context["group_cap_fen"])
    if kind == "patch_preview":
        return (result["trip_id"] == context["trip_id"] and result["base_revision"] == context["base_revision"]
                and set(context["locked_ids"]).issubset(result["unchanged"])
                and ("scope_date" not in context or result["scope"]["dates"] == [context["scope_date"]]))
    if kind in {"recheck_report", "export_result"}:
        revision = "revision" if kind == "export_result" else "base_revision"
        return (result["trip_id"] == context["trip_id"] and result[revision] == context["base_revision"]
                and (kind != "export_result" or result["format"] == context["format"]))
    return True


class TypedTurn:
    """One model decision followed by bounded server-owned read-only calls."""

    def __init__(self, model, handlers, fixture_mode, *, timeline_handler=None, verification_handler=None,
                 transport_skills=None, intent_timeout=15):
        self.model = model
        self.intent_timeout = intent_timeout
        self.handlers = dict(handlers or {}) if fixture_mode else {}
        self.fixture_mode = fixture_mode
        self.timeline_handler = timeline_handler
        self.verification_handler = verification_handler
        self.transport_skills = transport_skills
        self.session = SessionContext()
        self.last_context = None

    async def run(self, message, form, now, trace=None, *, candidate_skills=None, candidate_scope=None, patch_handler=None, recheck_handler=None, export_handler=None):
        trace_id = getattr(trace, "trace_id", None) or uuid4().hex

        def record(event, **fields):
            row = dict(event=event, trace_id=trace_id, **fields)
            if hasattr(trace, "record"):
                trace.record(event, **fields)
            elif trace is not None:
                trace.append(row)

        def reply(result, state="completed", evidence=None):
            output = dict(schema_version="1.0", trace_id=trace_id, generated_at=now.isoformat(timespec="seconds"),
                          timezone="Asia/Shanghai", run_status=state, persistence="none",
                          result=result, evidence=evidence or [])
            validate_reply(output, fixture_mode=self.fixture_mode)
            record("turn_result", status=state, kind=result["kind"], write_count=0,
                   evidence_ids=[item["evidence_id"] for item in output["evidence"]])
            return output

        try:
            known = build_context({}, form, self.session, now=now, required=(), allow_refs=True).known_fields
        except ValueError:
            return reply(failure("ack", {}, now, "invalid_response"), "failed")
        statement = travel_statement(message)
        if statement:
            day, place = statement
            if day:
                target = date.fromisoformat(calendar_day(day, now.date()))
                self.session.fields["date"] = target.isoformat()
                self.session.recent_explicit_date = target.isoformat()
                day = f"{target.month}月{target.day}日"
            if self.fixture_mode and place in FIXTURE_PLACES:
                self.session.fields["place_id"] = FIXTURE_PLACES[place]
            record("decision", intent="ack", allowed_tools=[], write_policy="none", tool_budget=0)
            return reply(dict(kind="ack", text=f"收到，你计划{day or ''}去{place}。"))

        if known.get("locked_ids") and LOCK_CHANGE.search(message):
            record("lock_change_needs_confirmation", write_policy="none", tool_budget=0)
            return reply(dict(kind="clarification", field="locked_ids",
                              question="你是要变更已经确认的安排吗？",
                              known=safe_context({key: value for key, value in known.items() if key not in TRUSTED_ONLY})),
                         "needs_input")

        decision = None
        for attempt in range(2):
            try:
                model_context = {key: item for key, item in known.items() if key not in {"trip_id", "base_revision", "locked_ids"}}
                value = decode_intent(await asyncio.wait_for(self.model(message, now, model_context, bool(attempt)), timeout=self.intent_timeout))
                if value["requested_outputs"][0] != value["intent"] or (
                        len(value["requested_outputs"]) > 1 and "ack" in value["requested_outputs"]):
                    raise ContractError("invalid_intent_outputs")
                decision = value
                break
            except ContractError as error:
                record("invalid_intent", attempt=attempt + 1, reason=str(error))
            except (OSError, TimeoutError, RuntimeError):
                record("intent_unavailable", attempt=attempt + 1)
                break
        if decision is None:
            return reply(failure("ack", {}, now, "invalid_response"), "failed")
        kinds = decision["requested_outputs"]
        if kinds == ["ack"]:
            record("decision", intent="ack", allowed_tools=[], write_policy="none", tool_budget=0)
            return reply(dict(kind="ack", text="收到你的出行安排。"))

        explicit = {key: value for key, value in decision["parameters"].items() if key not in TRUSTED_ONLY}
        if "黄果树" in message and "水帘洞" in message:
            explicit["scenic_area"] = "黄果树"
        for text in ("明天", "周六", "今天", "那天"):
            if text in message:
                key = "start_date" if decision["intent"] == "itinerary" else "date"
                explicit[key] = text
                break
        if self.fixture_mode and "青岩" in message:
            explicit["place_id"] = "qingyan"
        resolve_parameters(explicit, kinds, message, now, scenic_hint=decision["parameters"].get("scenic_area"))
        try:
            self.last_context = build_context(explicit, form, self.session, now=now, required=(), allow_refs=True)
        except ValueError:
            return reply(failure(decision["intent"], {}, now, "invalid_response"), "failed")
        context = dict(self.last_context.known_fields)
        if explicit.get("date") != "那天" and "date" in explicit and "date" in context:
            self.session.recent_explicit_date = context["date"]
        second_day_patch = "patch_preview" in kinds and bool(re.search(r"第二天|第2天",message))
        if second_day_patch:
            if "start_date" in known:
                start = date.fromisoformat(calendar_day(known["start_date"], now.date()))
                context["scope_date"] = (start + timedelta(days=1)).isoformat()
            else:
                context.pop("start_date", None)
                context.pop("scope_date", None)
        if "export_result" in kinds:
            context.setdefault("format", "html")
        reusable=("place_id","date","scenic_area")
        if self.transport_skills is not None and any(self.transport_skills.supports(k,context) for k in kinds):
            reusable+=("from_id","to_id","arrival_at","adults","candidate_kind","mode")
        for key in reusable:
            if key in context:
                self.session.fields[key] = context[key]
                self.session.provenance[key] = self.last_context.provenance[key]
        required = {}
        for kind in kinds:
            required[kind] = SKILLS[kind].required
            if kind == "patch_preview" and patch_handler is not None:
                required[kind] += ("scope_date",)
            elif kind == "patch_preview" and second_day_patch and "scope_date" not in context:
                required[kind] += ("start_date",)
            if kind == "candidates":
                required[kind] += CANDIDATE_FIELDS.get(context.get("candidate_kind"), ())
        transport = {kind for kind in kinds if self.transport_skills and self.transport_skills.supports(kind,context)}
        m08 = {kind for kind in kinds if candidate_skills and candidate_skills.supports(kind,context)}
        if "candidates" in m08:
            for key in ("candidate_kind", "place_id", *CANDIDATE_FIELDS.get(context.get("candidate_kind"), ())):
                if key in self.last_context.known_fields:
                    self.session.fields[key] = self.last_context.known_fields[key]
                    self.session.provenance[key] = self.last_context.provenance[key]
        if "candidates" in m08 and context.get("candidate_kind")=="hotel":
            if "place_id" not in context and context.get("destination"):
                context["place_id"]=context["destination"]
            required["candidates"]+=("place_id",)
        record("decision", intent=decision["intent"], confidence_band=decision["confidence_band"],
               requested_outputs=kinds, required_fields=required,
               allowed_tools=["m08_candidates" if k in m08 else TRANSPORT_TOOLS[k] if k in transport else SKILLS[k].tool for k in kinds],
               write_policy="none", tool_budget=len(kinds))

        results, evidence = [], {}
        question = None
        failed = False
        cutoff = monotonic() + 35
        for index, kind in enumerate(kinds):
            skill = SKILLS[kind]
            missing = next((key for key in required[kind] if key not in context), None)
            if missing:
                record("missing_route_parameter", intent=kind, reason=missing)
                if index == 0:
                    return reply(dict(kind="clarification", field=missing, question=QUESTIONS[missing],
                                      known=safe_context({k: v for k, v in context.items() if k not in TRUSTED_ONLY})), "needs_input")
                question = question or QUESTIONS[missing]
                failed = True
                continue
            if kind in transport or kind in m08:
                new_candidate=kind in m08
                component=candidate_skills if new_candidate else self.transport_skills
                scoped, unclear = component.resolve(kind,context)
                if unclear:
                    prompt = {"from_id":"请确认具体的出发站点、机场或城市？",
                              "to_id":"请确认具体的到达站点、机场或景区检票入口？",
                              "place_id":"你想查哪个地点？" if new_candidate else "你想查哪个有预报位置的景点？",
                              "child_ages":"同行儿童分别几岁？"}[unclear]
                    if index == 0:
                        return reply(dict(kind="clarification",field=unclear,question=prompt,known=safe_context(scoped)),
                                     "needs_input")
                    question = question or prompt
                    failed = True
                    continue
                if new_candidate and scoped.get("place_id") is not None and scoped.get("candidate_kind") in {"hotel", "dining"}:
                    self.session.fields["place_id"]=scoped["place_id"]
                    self.session.provenance.pop("place_id", None)
                fields=CANDIDATE_OPTIONS if new_candidate else TRANSPORT_FIELDS[kind]
                arguments = {key:scoped[key] for key in (*required[kind],*fields) if key in scoped}
                tool="m08_candidates" if new_candidate else TRANSPORT_TOOLS[kind]
                try:
                    check(TOOLS,{"tool":tool,"arguments":arguments})
                    remaining=cutoff-monotonic()
                    if remaining<=0:
                        raise TimeoutError
                    record("tool_call",tool=tool,skill="E2/E3/E4" if new_candidate else
                           "S3" if kind=="candidates" else skill.skill_id,
                           version="m08.1" if new_candidate else "m07.1",
                           provider="local_candidates" if new_candidate else "local_transport",retry=0)
                    operation=(component.handle(kind,arguments,candidate_scope,trace=trace) if new_candidate else
                               component.handle(kind,arguments,trace=trace))
                    packet=await asyncio.wait_for(operation,timeout=min(30,remaining))
                    validate_reply(packet,fixture_mode=self.fixture_mode)
                    card=packet["result"]
                    if (new_candidate and card["kind"] not in {"pickup_choices","candidates","unavailable"}
                            or card["kind"]==kind and not match_request(card,kind,scoped)):
                        raise ContractError("candidate_response_mismatch")
                    if new_candidate and card["kind"]=="candidates" and not match_request(card,"candidates",scoped):
                        raise ContractError("response_mismatch")
                    if any(item["evidence_id"] in evidence and evidence[item["evidence_id"]]!=item for item in packet["evidence"]):
                        raise ContractError("conflicting_evidence")
                    if card.get("next_question") and len(kinds)>1:
                        question = question or card["next_question"]
                        card = dict(card,next_question=None)
                    if card["kind"]=="bundle" and len(kinds)>1:
                        results.extend([card["primary"],*card["supplements"]])
                    else:
                        results.append(card)
                    evidence.update((item["evidence_id"],item) for item in packet["evidence"])
                    failed |= packet["run_status"]!="completed"
                    if kind=="weather":
                        self.session.fields["place_id"]=scoped["place_id"]
                    continue
                except TimeoutError:
                    reason="timeout"
                except (ContractError,KeyError,TypeError,ValueError):
                    reason="invalid_response"
                except (OSError,RuntimeError):
                    reason="provider_error"
                record("route_unavailable",tool=tool,reason=reason)
                results.append(failure(kind,scoped,now,reason))
                failed=True
                continue
            local_planner = kind == "itinerary" and self.timeline_handler is not None
            local_evidence = kind == "verification" and self.verification_handler is not None
            local_patch = kind=="patch_preview" and patch_handler is not None
            local_recheck = kind=="recheck_report" and recheck_handler is not None
            local_export = kind=="export_result" and export_handler is not None
            handler = export_handler if local_export else recheck_handler if local_recheck else (self.timeline_handler if local_planner else self.verification_handler if local_evidence
                       else patch_handler if local_patch else self.handlers.get(skill.tool))
            reason = "unbound"
            if handler is not None:
                keys = (*required[kind], *ROUTE_FIELDS.get(kind, ()))
                arguments = {key: context[key] for key in keys if key in context}
                started = monotonic()
                try:
                    check(TOOLS, {"tool": skill.tool, "arguments": arguments})
                    remaining = cutoff - started
                    if remaining <= 0:
                        raise TimeoutError
                    record("tool_call", skill=skill.skill_id, version=skill.version, tool=skill.tool,
                           provider="local_timeline" if local_planner else "local_evidence" if local_evidence else "local_patch" if local_patch else "local_recheck" if local_recheck else "local_export" if local_export else "fixture", attempt_id=f"{trace_id}-{index}", retry=0)
                    packet = deepcopy(await asyncio.wait_for(handler(arguments), timeout=min(15, remaining)))
                    validate_reply(packet, fixture_mode=self.fixture_mode)
                    result = packet["result"]
                    partial_patch=local_patch and result.get("requested_kind")=="patch_preview" and result["kind"]=="unavailable"
                    if not partial_patch and (result["kind"] != kind or not match_request(result, kind, context)):
                        raise ContractError("response_mismatch")
                    if any(item["evidence_id"] in evidence and evidence[item["evidence_id"]] != item for item in packet["evidence"]):
                        raise ContractError("conflicting_evidence")
                    if result.get("next_question"):
                        if kind == "transfer" and "arrival_at" in context:
                            result["next_question"] = None
                            if result["last_service"]["state"] == "unknown":
                                result["arrival_check"]["reason"] = "已知到站时刻，末班依据仍待核实。"
                        elif len(kinds) > 1:
                            question = question or result["next_question"]
                            result["next_question"] = None
                    evidence.update((item["evidence_id"], item) for item in packet["evidence"])
                    results.append(result)
                    failed |= packet["run_status"] != "completed"
                    continue
                except TimeoutError:
                    reason = "timeout"
                except (ContractError, TypeError, KeyError, ValueError):
                    reason = "invalid_response"
                except (OSError, RuntimeError):
                    reason = "provider_error"
                finally:
                    record("tool_finished", tool=skill.tool, duration_ms=round((monotonic()-started)*1000))
            record("route_unavailable", tool=skill.tool, reason=reason)
            results.append(failure(kind, context, now, reason))
            failed = True
        if not results:
            return reply(failure(kinds[0],context,now,"unbound"),"partial")
        result = results[0]
        if len(results) > 1:
            result = dict(kind="bundle", primary=result, supplements=results[1:], next_question=question)
        return reply(result, "partial" if failed else "completed", list(evidence.values()))

