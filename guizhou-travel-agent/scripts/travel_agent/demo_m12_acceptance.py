"""Run one actual Win/Chrome journey through the M11 workbench with fixed tools."""
import argparse
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from demo_m11 import NOW, ROOT, TEST, Workbench, make_case, serve
from support.guards import isolated_database
from travel_recheck import RecheckService


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


async def official_fixture(fact,budget):
    if fact["stop_id"]!="d2-museum":
        return {"status":"empty"}
    budget.take("test_announcement")
    day=fact["target_date"]
    return {"status":"success","observation":{
        "value":"closed","collected_at":NOW.isoformat(),"valid_from":day,"valid_to":day,
        "valid_until":(NOW+timedelta(minutes=30)).isoformat(),"published_at":NOW.isoformat(),
        "origin_id":"m12-fixture-station-conflict","source_url":"fixture://m12-closed",
        "excerpt":"测试场馆在出游日暂停开放。","data_mode":"fixture"}}


def fixture_audit(app):
    with app.repository.connection() as db:
        rows=db.execute("SELECT revision,payload_json FROM trip_versions ORDER BY rowid").fetchall()
        counts={name:db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("trips","trip_versions","save_keys")}
    snapshots=[]
    for row in rows:
        record=json.loads(row["payload_json"])
        days=record["timeline"]["result"]["days"]
        snapshots.append(dict(revision=row["revision"],first_day=digest(days[0]),second_day=digest(days[1]),
                              locks=digest(record["locked_items"])))
    return dict(trips=snapshots,counts=counts)


def run(browser,out,width):
    for key in ("TRAVEL_M07_NETWORK","TRAVEL_M08_NETWORK","TRAVEL_M10_NETWORK"):
        os.environ[key]="0"
    with TemporaryDirectory(prefix="travel-m12-e2e-") as temp:
        work=Path(temp)
        with isolated_database(work/"travel_m12.sqlite3",work) as db:
            app,token,_,sample=make_case(db,clock=lambda:NOW)
            workbench=Workbench(app,token,sample,live=False,testing=True,pdf_browser=browser)
            workbench.recheck.close()
            service=RecheckService(app,{"opening":official_fixture})
            app.recheck_service=workbench.recheck=service
            try:
                with serve(workbench,browser_checks=TEST/"m12/browser_checks.js",release_audit=lambda:fixture_audit(app)) as url:
                    out.mkdir(parents=True,exist_ok=True)
                    cmd=["node",str(TEST/"m08/browser_runner.mjs"),str(browser),url,str(work/"chrome"),str(out),
                         str(width),"saved,declined,reopened"]
                    received=subprocess.run(cmd,capture_output=True,text=True,encoding="utf-8",timeout=55)
                    print(received.stdout+received.stderr)
                    report=json.loads(received.stdout) if received.stdout else {"status":"FAIL","reason":"browser_no_report"}
                    report.update(width=width,providers="fixture",sqlite="temporary",database_digest=fixture_audit(app))
                    (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                    return received.returncode
            finally:
                service.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--width",type=int,choices=(390,1100),default=1100)
    args=parser.parse_args()
    raise SystemExit(run(args.browser,args.output,args.width))
