import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
from unittest.mock import patch

from travel_agent import Conversation
from travel_evidence import digest
from travel_state import lock_arrangement
from travel_timeline import PLAN, TimelineWorkflow, load_fixture
from travel_timeline_route import RouteAdapter

FX=json.loads((Path(__file__).parent.parent/"fixtures/m09/indoor.json").read_text(encoding="utf-8"))
NOW=datetime.fromisoformat(FX["clock"])
DAY1="2026-11-28"
DAY2=FX["scope"]
MESSAGE=FX["instruction"]


def count(app):
    with app.repository.connection() as db:
        return [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("trips","trip_versions","save_keys")]


def fields_digest(locked):
    serialized=json.dumps([asdict(item) for item in locked],ensure_ascii=False,sort_keys=True)
    return digest(serialized)


async def make_trip(app,token,*,use_qingyan=True):
    mock=load_fixture()
    source=mock.catalog["jiaxiu-entry"]
    mock.catalog["qingyan-entry"]=replace(source,id="qingyan-entry",poi_id="qingyan",label="青岩古镇入口（模拟）")
    mock.legs["hotel-demo-entry/qingyan-entry"]=mock.legs["hotel-demo-entry/jiaxiu-entry"]
    mock.legs["qingyan-entry/wenchang-entry"]=mock.legs["jiaxiu-entry/wenchang-entry"]
    first=tuple((key,"qingyan-entry" if key=="d1-jiaxiu" else point,arrive,leave,mode)
                for key,point,arrive,leave,mode in PLAN[0])
    workflow=TimelineWorkflow(app,mock.catalog,RouteAdapter(mock.route,app.clock,fixture=True),mock.costs)
    with patch("travel_timeline.PLAN",(first,PLAN[1]) if use_qingyan else PLAN):
        packet=await workflow.preview(token,mock.form)
    version=workflow.save(token,packet["result"]["draft_id"],"m09-fixture-r1",confirmed=True)
    # Seed the public test scenario at r7. No migrations or user data are involved.
    with app.repository.transaction() as db:
        raw=db.execute("SELECT payload_json FROM trip_versions WHERE trip_id=? AND revision=1",
                       (version.trip_id,)).fetchone()
        payload=json.loads(raw[0])
        second=payload["timeline"]["result"]["days"][1]
        second["next_stop_id"]="d2-park"
        second["stops"][0]["next_stop_id"]="d2-park"
        rail=payload["locked_items"][0]
        rail["fields"].update(train_number="D-FIXTURE",source="fixture-rail-confirmed")
        rail["fields_hash"]=digest(json.dumps({"kind":rail["kind"],"fields":rail["fields"]},ensure_ascii=False,
                                               sort_keys=True,separators=(",",":")))
        hotel=lock_arrangement({"kind":"hotel","fields":{"place":"贵阳测试酒店","check_in":DAY1,"check_out":DAY2}},
                               confirmed=True,now=NOW)
        payload["locked_items"].insert(0,asdict(hotel))
        for revision in range(2,8):
            db.execute("INSERT INTO trip_versions VALUES (?,?,?,?)",
                       (version.trip_id,revision,version.schema_version,json.dumps(payload,ensure_ascii=False)))
    return app.read_trip(token,version.trip_id)


async def planner(*_):
    return {key:deepcopy(FX[key]) for key in ("operations","points","routes","unknown_cost_ids")}


def model_factory(clock):
    async def model(*_):
        return json.dumps(dict(intent="patch_preview",confidence_band="high",requested_outputs=["patch_preview"],parameters={}))
    return lambda:Conversation(typed_output=True,clock=clock,intent_model=model,fixture_mode=True)

