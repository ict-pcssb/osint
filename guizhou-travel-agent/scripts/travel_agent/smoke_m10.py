"""Read-only M10 smoke using real M7 weather MCP and optionally M5 source discovery."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[2]
TESTS=ROOT/"tests/travel_agent"
sys.path[:0]=[str(ROOT),str(TESTS),str(TESTS/"m10")]
from fixture_m10 import make_case, set_facts, previous, DAY1
from support.guards import Trace, isolated_database
from travel_recheck import RecheckService
from travel_recheck_live import configured_providers
from travel_state import BEIJING


def check_live(weather_entry, notice):
    clock=lambda:datetime.now(BEIJING)
    if DAY1<clock().date().isoformat():
        return {"status":"BLOCKED","reason":"示例行程日期已过，需要另外选择未来日期"}
    if not weather_entry.is_file():
        return {"status":"BLOCKED","reason":"未找到之前已安装的 M7 天气 MCP"}
    with TemporaryDirectory(prefix="travel-m10-live-") as folder:
        root=Path(folder)
        with isolated_database(root/"travel_m10.sqlite3",root) as db:
            app,token,_,trip=make_case(db,clock,use_qingyan=not notice)
            target = "jiaxiu-entry" if notice else "qingyan"
            facts=[dict(previous("weather"),stop_id="d1-jiaxiu",entity=target,target_date=DAY1,
                        value=None,source_url=None,origin_id=None,collected_at=None,valid_until=None,
                        published_at=None,valid_from=None,valid_to=None,excerpt="")]
            if notice:
                facts.append(dict(previous(),stop_id="d1-jiaxiu",entity="jiaxiulou",target_date=DAY1,
                                  valid_from=DAY1,valid_to=DAY1,valid_until="2026-11-26T12:00:00+08:00"))
            trip=set_facts(app,token,trip.trip_id,facts)
            service=RecheckService(app,configured_providers(clock,weather_entry=str(weather_entry),
                                                            enable_notice=notice,enable_hotel=False),item_timeout=25)
            trace=Trace("m10-real-read-only")
            try:
                opened=service.open(token,trip.trip_id,trace=trace)
                task=service.start(token,trip.trip_id,base_revision=opened["revision"],trigger="open",trace=trace)
                service.wait(task["run_id"])
                checked=service.get(token,trip.trip_id,task["run_id"])
                saved=app.read_trip(token,trip.trip_id)
                weather=next(row for row in checked["station_changes"] if row["kind"]=="weather")
                result=weather["latest"]
                weather_pass=bool(result and result["data_mode"]=="live" and result["valid_from"]==DAY1
                                  and result["origin_id"].startswith("m7-") and result["collected_at"])
                actual_calls=next(row["providers"] for row in trace.events if row["event"]=="m10_finished")
                pass_gate=weather_pass and saved==trip and actual_calls.count("open-meteo")==1 and len(actual_calls)<=6
                other=next((r for r in checked["station_changes"] if r["kind"]=="opening"),None)
                return dict(status="PASS" if pass_gate else "FAIL" if weather_pass else "BLOCKED",
                            layer="real_provider_read_only",snapshot_data="fixture",
                            provider_attempts=actual_calls,weather=dict(status="PASS" if weather_pass else "BLOCKED",finding=weather),
                            notice=dict(status="PASS" if other and other["latest"] else "BLOCKED" if other else "SKIPPED",finding=other),
                            same_saved_revision=saved==trip,revision=saved.revision,check_state=checked["check_state"],
                            trace=trace.events)
            finally:
                service.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weather-entry",type=Path,required=True)
    parser.add_argument("--notice",action="store_true")
    parser.add_argument("--report",type=Path,required=True)
    args=parser.parse_args()
    report=check_live(args.weather_entry,args.notice)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="trace"},ensure_ascii=False))
    raise SystemExit({"PASS":0,"FAIL":1,"BLOCKED":2}[report["status"]])
