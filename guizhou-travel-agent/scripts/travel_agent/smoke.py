"""M4 one-leg live route smoke behind an explicit authorization gate."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from travel_state import BEIJING
from travel_state_app import TravelApplication
from travel_timeline import Endpoint, TimelineWorkflow, load_fixture
from travel_timeline_route import RouteAdapter, make_osm_invoker

DEFAULT_OSM = ROOT.parent / "samples-1124/scripts/travel_eval/mcp/node_modules/osm-mcp/dist/index.js"


def read_inputs(path, form_path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    form = json.loads(form_path.read_text(encoding="utf-8"))
    points = {row["id"]:Endpoint(**row) for row in raw}
    if len(points) != len(raw) or len({p.waypoint for p in points.values()}) != len(points):
        raise ValueError("入口重复，须提供独立的入口 ID 与坐标")
    if any(point.confirmed is not True or point.coordinate_system != "WGS84" or
           not (26.2 < point.latitude < 27.1 and 106.2 < point.longitude < 107.25) or
           urlsplit(point.source_url).scheme != "https" or not urlsplit(point.source_url).hostname
           for point in points.values()):
        raise ValueError("需要经核对的贵阳 WGS84 入口坐标及 HTTPS 来源")
    if len(form.get("day_starts", [])) != 2:
        raise ValueError("行前表单需要两天的起止安排")
    if not all(day in form for day in ("start_date","end_date")):
        raise ValueError("需提供实际起止日期")
    date = datetime.fromisoformat(form["start_date"]).date()
    if date < datetime.now(BEIJING).date():
        raise ValueError("实测日期不能在过去")
    return points, form


async def smoke(points, form, invoker, *, preview_path=None):
    attempts = 0
    original = load_fixture()
    from_id = form["day_starts"][0]["entrance_id"]
    target = "jiaxiu-entry"
    required = {from_id, target}
    if not required <= points.keys():
        return {"status":"BLOCKED","attempts":0,"reason":"缺首段两端已经核对的真实入口"}
    reference_scaffold = len(points) == 2
    catalog = {**original.catalog, **points} if reference_scaffold else points

    async def counted(arguments):
        nonlocal attempts
        attempts += 1
        if attempts != 1 or arguments["waypoints"] != [points[from_id].waypoint,points[target].waypoint]:
            raise ValueError("只允许已核对的这一段")
        return await invoker(arguments)

    try:
        with TemporaryDirectory(prefix="travel-m04-live-") as folder:
            app = TravelApplication(Path(folder)/"travel_m04.sqlite3")
            app.create_account("m04_smoke","local-smoke-only")
            token = app.login("m04_smoke","local-smoke-only")
            logic = TimelineWorkflow(app,catalog,RouteAdapter(counted,app.clock,fixture=False,max_attempts=1),original.costs,
                                     reference_scaffold=reference_scaffold)
            packet = await logic.preview(token,form)
    except (ValueError,OSError,RuntimeError,ExceptionGroup) as exc:
        return {"status":"FAIL" if attempts else "BLOCKED","attempts":attempts,"reason":type(exc).__name__}
    if packet["result"]["kind"] != "itinerary":
        return {"status":"BLOCKED","attempts":attempts,"reason":"入口或首页事实缺少必要信息"}
    stop = packet["result"]["days"][0]["stops"][0]
    leg = stop["incoming_leg"]
    evidence = [row for row in packet["evidence"] if row["evidence_id"] in leg["evidence_ids"] and row["data_mode"]=="live"]
    if leg["status"] != "estimate":
        status = "FAIL" if leg["reason"] == "invalid_response" else "BLOCKED"
        return {"status":status,"attempts":attempts,"reason":leg["reason"],"route":leg}
    if not (attempts == 1 and leg["profile"] == "foot" and leg["coordinate_system"]=="WGS84"
            and leg["distance_m"] > 0 and leg["duration_s"] > 0
            and leg["from_id"]==form["day_starts"][0]["entrance_id"] and leg["to_id"]==stop["entrance_id"]
            and evidence and leg["checked_at"]):
        return {"status":"FAIL","attempts":attempts,"reason":"真实路段未满足业务语义"}
    if preview_path is not None:
        save_preview(preview_path, packet)
    return {"status":"PASS","attempts":attempts,"date":packet["result"]["days"][0]["date"],
            "route":leg,"evidence":evidence,"target_stop":stop["stop_id"]}


def save_preview(folder, packet):
    from travel_contract import REPLY
    folder.mkdir(parents=True, exist_ok=True)
    (folder/"timeline.json").write_text(json.dumps(packet,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    css=(ROOT/"frontend/travel_result.css").read_text(encoding="utf-8")
    renderer=(ROOT/"frontend/travel_result.js").read_text(encoding="utf-8")
    payload=json.dumps([packet,REPLY.schema],ensure_ascii=False).replace("<","\\u003c")
    html=("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
          "<title>M4 真实步行路段验收</title><style>"+css+"</style></head><body><main>"
          "<h1>M4 真实步行路段验收</h1><p>只有标为 OSM 实测的一段使用真实路由。其余地点是联调样例，开放和费用均待核实。</p>"
          "<section id='timeline'></section><script>"+renderer+"\nconst [packet,schema]="+payload+
          ";document.querySelector('#timeline').append(renderResult(packet,schema));</script></main></body></html>")
    (folder/"timeline.html").write_text(html,encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module",choices=("M4","M04"),required=True)
    parser.add_argument("--read-only",action="store_true",required=True)
    parser.add_argument("--allow-network",action="store_true")
    parser.add_argument("--catalog",type=Path)
    parser.add_argument("--form",type=Path)
    parser.add_argument("--osm-entry",type=Path,default=DEFAULT_OSM)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    reasons=[]
    if not args.allow_network:
        reasons.append("本轮未获取实际 OSM 只读外呼许可")
    if args.catalog is None or args.form is None:
        reasons.append("缺已核对的真实入口目录和实际日期首页表单")
    if not shutil.which("node") or not args.osm_entry.is_file():
        reasons.append("未找到可用的 Node 或已安装 osm-mcp dist/index.js")
    if reasons:
        result={"status":"BLOCKED","attempts":0,"reasons":reasons}
    else:
        try:
            points, form=read_inputs(args.catalog,args.form)
            invoke=make_osm_invoker(args.osm_entry,authorized=True)
            result=asyncio.run(smoke(points,form,invoke,preview_path=args.output.parent))
        except (ValueError,OSError,RuntimeError) as exc:
            result={"status":"BLOCKED","reason":type(exc).__name__,"attempts":0}
    result["case_id"]="M4-08"
    result["data_mode"]="real_provider_read_only"
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))
    return {"PASS":0,"FAIL":1,"BLOCKED":2}[result["status"]]


if __name__=="__main__":
    raise SystemExit(main())

