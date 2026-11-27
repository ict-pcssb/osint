"""The first scoped indoor replacement for the existing Guiyang two-day timeline."""
from pathlib import Path
import json

from travel_timeline import PLAN

ROOT=Path(__file__).resolve().parent
MAPPED_MUSEUM=ROOT/"data/travel_patch_indoor_v1.json"
FIXTURE=ROOT/"tests/travel_agent/fixtures/m09/indoor.json"


async def indoor_plan(version,dates,instruction):
    original=version.timeline["result"]
    if "室内" not in instruction or len(dates)!=1 or version.fields.get("destination")!="贵阳":
        raise ValueError("当前行程需要明确的室内替换日期")
    day=next((d for d in original["days"] if d["date"]==dates[0]),None)
    park=next((s for s in day["stops"] if s["poi_id"]=="guanshanhu"),None) if day else None
    if park is None or any(park.get(key) for key in ("completed","locked","required")):
        raise ValueError("没有可替换的户外站点")
    if original["data_mode"]=="fixture" and (park["stop_id"],dates[0])==("d2-park","2026-11-29"):
        known=json.loads(FIXTURE.read_text(encoding="utf-8"))
        return {key:known[key] for key in ("operations","points","routes","unknown_cost_ids")}
    if original["data_mode"]=="fixture":
        raise ValueError("该日期没有匹配的固定场景")
    point=json.loads(MAPPED_MUSEUM.read_text(encoding="utf-8"))
    scheduled=next((item[2:4] for stops in PLAN for item in stops if item[0]==park["stop_id"]), None)
    if park["arrival_local"] and park["departure_local"]:
        arrive,leave=park["arrival_local"][11:16],park["departure_local"][11:16]
    elif scheduled:
        arrive,leave=scheduled
    else:
        raise ValueError("原时段待查")
    index=original["days"].index(day)+1
    old_charge=f"d{index}-ride"
    unknown=[old_charge] if any(x["item_id"]==old_charge for x in original["budget"]["items"]) else []
    return dict(operations=[dict(op="replace",date=day["date"],stop_id=park["stop_id"],
                                new_stop_id=park["stop_id"]+"-indoor",point_id=point["id"],
                                arrive=arrive,leave=leave,ticket_fen=None)],
                points=[point],routes=[],unknown_cost_ids=unknown)

