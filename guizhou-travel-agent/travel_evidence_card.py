"""Project read-only evidence into the M2 verification card and existing Agent turn."""
from uuid import uuid4
import json

from travel_agent import Conversation
from travel_contract import validate_reply

LABELS = {"opening":"开放","booking":"预约","ticket_price":"成人票价","last_service":"末班车"}


def verification_packet(report, entity, *, fixture_mode, trace_id=None):
    claim = report["claim"]
    status = claim["status"]
    human = LABELS[claim["claim_type"]]
    findings = {"confirmed":"已找到适用的正式原文。","contradicted":"适用原文显示该处暂停开放。",
                "conflicted":"已读原文存在冲突，请继续核对。","unconfirmed":"没有足以确认目标日的正式原文。"}
    value = claim["value"]
    if isinstance(value,dict):
        detail = f" 成人票价 {value['amount_fen']/100:g} 元。"
    elif value is None:
        detail = ""
    else:
        label = {"open":"正常开放","closed":"暂停开放","required":"须预约","not_required":"无需预约"}.get(value,value)
        detail = f" 原文结论：{label}。"
    output = dict(kind="verification",data_mode="fixture" if fixture_mode else
                  "live" if any(e["data_mode"] == "live" for e in report["evidence"]) else "snapshot",
                  subject_id=entity.id,claim=f"{entity.label}的{human}情况",target_date=claim["target_date"],
                  status=status,checked_at=claim["checked_at"],finding=findings[status]+detail,
                  booking_entry={"state":"unverified","reason":"目标日预约状态待核实"},
                  evidence_ids=claim["evidence_ids"],action="可继续核对景区或运营方公告。",
                  official_links=report["official_links"],uncertainties=claim["uncertainties"])
    fields = ("evidence_id","source_url","publisher","source_group_id","source_kind","data_mode","provider",
              "effective_from","effective_to","published_at","expires_at","freshness","content_hash")
    evidence = [
        {**{key:row[key] for key in fields},"collected_at":row["retrieved_at"],
         "applies_to":[claim["target_date"]] if row["evidence_id"] in claim["evidence_ids"] else [],
         "excerpt":row["excerpt"] or "已读取原文，未抽取到和请求匹配的关键事实。"} for row in report["evidence"]
    ]
    packet = dict(schema_version="1.0",trace_id=trace_id or uuid4().hex,generated_at=claim["checked_at"],
                  timezone="Asia/Shanghai",run_status="completed" if status in {"confirmed","contradicted"} else "partial",
                  persistence="none",result=output,evidence=evidence)
    return validate_reply(packet,fixture_mode=fixture_mode)


async def verify_turn(service, subject_id, claim_type, target_date, *, provider="default", known_urls=(),
                      direction=None, fixture_mode=False, trace=None):
    """Explicit user verification action. Return existing Reply plus distinct audit record."""
    entity = service.catalog.get(subject_id)
    if entity is None:
        raise ValueError("请选择唯一的目录实体 ID")
    record = {}

    async def intent(*_):
        return json.dumps({"intent":"verification","confidence_band":"high",
                           "requested_outputs":["verification"],"parameters":{}})

    async def verify(arguments):
        nonlocal record
        record = await service.verify_claim(subject_id,claim_type,arguments["date"],provider=provider,
                                           known_urls=known_urls,line_direction=direction,trace=trace)
        return verification_packet(record,entity,fixture_mode=fixture_mode,trace_id=getattr(trace,"trace_id",None))

    agent = Conversation(homepage={"scenic_area":entity.label,"date":target_date,"place_id":subject_id},
                         typed_output=True,clock=service.clock,intent_model=intent,fixture_mode=fixture_mode,
                         verification_handler=verify)
    packet = await agent.ask(f"核验 {entity.label} 的{LABELS[claim_type]}情况？",trace)
    return packet, record



async def verify_timeline_stop(application, token, trip_id, stop_id, service, *,
                               known_urls=(), fixture_mode=False, trace=None):
    version = application.read_trip(token,trip_id)
    if not version.timeline:
        raise ValueError("此行程没有可核验的时间轴")
    for day in version.timeline["result"]["days"]:
        for stop in day["stops"]:
            if stop["stop_id"] == stop_id:
                entrance = service.catalog.get(stop["entrance_id"])
                if entrance is None or entrance.kind != "entrance":
                    raise ValueError("请确认该站点的具体入口")
                return await verify_turn(service,entrance.id,"opening",day["date"],
                                         known_urls=known_urls,fixture_mode=fixture_mode,trace=trace)
    raise ValueError("该站不在已保存行程中")
