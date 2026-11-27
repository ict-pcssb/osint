import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"m09"))
from fixture_m09 import make_trip, planner, DAY1, DAY2
from travel_patch import PatchService
from travel_state_app import TravelApplication

NOW = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
DAY = DAY2


def previous(kind="opening"):
    values = {"opening":"open", "weather":{"low_c":5,"high_c":13,"rain_pct":10},
              "last_service":"17:00", "quote":{"amount_fen":12000,"currency":"CNY"}}
    return dict(kind=kind, stop_id="d2-park", entity="guanshanhu", target_date=DAY,
                scope={"direction":"到贵阳北站"} if kind=="last_service" else (
                       {"quote_type":"hotel","check_in":DAY,"nights":2,"adults":2,"rooms":1} if kind=="quote" else {}),
                value=values[kind], collected_at="2026-11-25T09:00:00+08:00",
                valid_from=DAY, valid_to=DAY, published_at="2026-11-25T08:00:00+08:00",
                valid_until="2026-11-25T09:05:00+08:00" if kind=="quote" else "2026-11-27T12:00:00+08:00",
                origin_id=f"fixture-{kind}-original", source_url="fixture://m10-old", excerpt="原公告（模拟）",
                data_mode="fixture")


def observed(value, *, start=DAY,end=DAY):
    return {"status":"success","observation":dict(value=value, collected_at="2026-11-28T08:00:00+08:00",
            valid_from=start, valid_to=end, valid_until="2026-12-02T09:00:00+08:00",
            published_at="2026-11-28T07:00:00+08:00", origin_id="fixture-new-"+str(value),
            source_url="fixture://m10-new", excerpt="新公告（模拟）", data_mode="fixture")}


def set_facts(app, token, trip_id, facts):
    with app.repository.transaction() as db:
        payload = json.loads(db.execute("SELECT payload_json FROM trip_versions WHERE trip_id=? AND revision=8",
                                       (trip_id,)).fetchone()[0])
        payload["timeline"]["result"]["recheck_facts"] = facts
        db.execute("UPDATE trip_versions SET payload_json=? WHERE trip_id=? AND revision=8",
                   (json.dumps(payload,ensure_ascii=False),trip_id))
    return app.read_trip(token, trip_id)


def make_case(db, clock, *, use_qingyan=True):
    app = TravelApplication(db,clock=clock)
    app.create_account("m10_fixture_a","isolated-m10")
    app.create_account("m10_fixture_b","isolated-m10")
    a,b = (app.login(name,"isolated-m10") for name in ("m10_fixture_a","m10_fixture_b"))
    r7 = asyncio.run(make_trip(app,a,use_qingyan=use_qingyan))
    # Keep the original outdoor stop for the M10 fixed scenario.
    with app.repository.transaction() as conn:
        app.repository.insert_version(conn,r7,8,timeline=r7.timeline)
        conn.execute("INSERT INTO save_keys VALUES (?,?,?,?,?)",(r7.owner_id,"m10-fixture-r8","fixture",r7.trip_id,8))
    trip = set_facts(app,a,r7.trip_id,[previous()])
    app.patch_service = PatchService(app,planner)
    return app,a,b,trip


def advance(app, token, original):
    service = app.get_patch_service()
    packet = asyncio.run(service.preview(token,original.trip_id,scope_dates=[DAY],
                                         instruction="第二天改成室内，返程票不变",base_revision=8))
    service.commit(token,original.trip_id,proposal_id=packet["result"]["proposal_id"],base_revision=8,
                   idempotency_key="m10-other-tab",confirmed=True)
    return app.read_trip(token,original.trip_id)
