"""Stop features and switch to the frozen M11 source against one disposable SQLite database."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[2]
TEST=ROOT/"tests/travel_agent"
sys.path[:0]=[str(ROOT),str(TEST),str(TEST/"m11")]
from fixture_m11 import make_case, NOW, advance
from support.guards import isolated_database
from travel_agent import Conversation
from travel_patch import PatchError, revision_identity
from travel_recheck import RecheckError
from travel_release import RunTrace
from travel_transport_live import configured_skills as transport
from travel_candidates_live import configured_skills as candidates
from travel_recheck_live import configured_providers

SNAPSHOT=ROOT/"docs/travel_agent/evidence/m12/rollback"
FLAGS={"TRAVEL_M07_NETWORK":"0","TRAVEL_M08_NETWORK":"0","TRAVEL_M10_NETWORK":"0",
       "TRAVEL_M10_AUTO_OPEN":"0","TRAVEL_M09_COMMIT":"disabled"}

BASELINE_READ=r"""
import asyncio, json, sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
root,db,trip,instant=sys.argv[1:]
sys.path.insert(0,root)
import travel_state_app, travel_search_mcp
from travel_patch import revision_identity
app=travel_state_app.TravelApplication(db,clock=lambda:datetime.fromisoformat(instant))
token=app.login("m10_fixture_a","isolated-m10")
version=app.read_trip(token,trip)
sys.path.insert(0,str(Path(root)/"scripts/travel_agent"))
from demo_m11 import Workbench,serve
workbench=Workbench(app,token,version,live=False,testing=False,pdf_browser=None)
try:
    with serve(workbench) as url:
        request=Request(url+"api/trips/"+trip,headers={"Cookie":"m11="+token})
        with urlopen(request,timeout=10) as response:
            reopened=json.load(response)
            assert response.status==200 and reopened["revision"]==version.revision
            assert reopened["timeline"]==version.timeline
finally:
    workbench.recheck.close()
calls=[]
async def exa(*args):
    calls.append("exa")
    return []
async def anysearch(*args):
    calls.append("anysearch")
    return [{"url":"https://example.org/official","title":"甲秀楼","excerpt":"甲秀楼","published_at":None}]
with patch.object(travel_search_mcp,"search_exa",exa), patch.object(travel_search_mcp,"search_anysearch",anysearch):
    result=asyncio.run(travel_search_mcp.search_travel_sources("甲秀楼 开放","甲秀楼",limit=1))
assert calls==["exa","anysearch"] and result["status"]=="candidates"
print(json.dumps(dict(revision=version.revision,identity=revision_identity(version),search_order=calls,
                      source_is_frozen=Path(travel_state_app.__file__).resolve().parent==Path(root).resolve(),http_read=True,
                      auto_recheck=app.get_recheck_service().auto_open, pdf_bound=bool(app.get_guidebook_service().pdf))))
"""


def load_baseline(to):
    record=json.loads((SNAPSHOT/"baseline-manifest.json").read_text(encoding="utf-8"))
    archive=SNAPSHOT/"m11-reviewed-source.zip"
    assert sha256(archive.read_bytes()).hexdigest()==record["archive_sha256"]
    with ZipFile(archive) as z:
        assert set(z.namelist())==set(record["files"])
        for name,value in record["files"].items():
            target=(to/name).resolve()
            assert target.is_relative_to(to.resolve())
            data=z.read(name)
            assert sha256(data).hexdigest()==value
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(data)
    return record["archive_sha256"]


def read_baseline(root,db,id_):
    env={key:value for key,value in os.environ.items() if not key.upper().endswith(("_KEY","_TOKEN","_SECRET"))}
    env.update(FLAGS)
    env.pop("TRAVEL_M11_PDF_BROWSER",None)
    line=[sys.executable,"-B","-c",BASELINE_READ,str(root),str(db),id_,NOW.isoformat()]
    run=subprocess.run(line,cwd=root,env=env,capture_output=True,text=True,encoding="utf-8",timeout=40)
    if run.returncode:
        raise RuntimeError("frozen process returned "+str(run.returncode)+": "+run.stderr)
    return json.loads(run.stdout)


def drill():
    start=monotonic()
    started=datetime.now(timezone.utc).isoformat()
    stage={}
    original=os.environ.copy()
    try:
        with TemporaryDirectory(prefix="travel-m12-rollback-") as folder:
            work=Path(folder)
            baseline=work/"m11"
            digest=load_baseline(baseline)
            path=work/"travel_m12.sqlite3"
            with isolated_database(path,work) as db:
                os.environ.update({**FLAGS,"TRAVEL_M09_COMMIT":"enabled"})
                os.environ.pop("TRAVEL_M11_PDF_BROWSER",None)
                app,token,_,old=make_case(db,clock=lambda:NOW)
                newest=advance(app,token,old)
                assert newest.revision==9
                identity=revision_identity(newest)
                with app.repository.connection() as live:
                    before=[tuple(row) for row in live.execute("SELECT trip_id,revision,payload_json FROM trip_versions ORDER BY revision")]
                    backup=work/"rollback-backup.sqlite3"
                    with closing(sqlite3.connect(backup)) as copy_db:
                        live.backup(copy_db)
                        assert copy_db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
                stage["backup_ms"]=round((monotonic()-start)*1000)
                trace=RunTrace()
                patcher=app.get_patch_service()
                preview=asyncio.run(patcher.preview_retime(token,newest.trip_id,date="2026-11-29",
                    stop_id="d2-indoor",arrival="13:15",departure="14:45",base_revision=9,trace=trace))
                assert preview["result"]["kind"]=="patch_preview"
                switched=monotonic()
                os.environ.update(FLAGS)
                try:
                    patcher.commit(token,newest.trip_id,proposal_id=preview["result"]["proposal_id"],
                                   base_revision=9,idempotency_key="m12-local-drill",confirmed=True,trace=trace)
                    raise AssertionError("commit toggle did not close")
                except PatchError as exc:
                    assert exc.status==503 and exc.code=="commit_disabled"
                service=app.get_recheck_service()
                try:
                    service.start(token,newest.trip_id,base_revision=9,trigger="open")
                    raise AssertionError("auto recheck did not close")
                except RecheckError as exc:
                    assert exc.status==503
                assert transport() is None and candidates() is None and configured_providers(app.clock)=={}
                assert Conversation(typed_output=False).typed is None
                book=app.get_guidebook_service().create(token,newest.trip_id,revision=9,format="pdf")["result"]
                assert book["pdf_status"]=="failed" and book["reason"]=="pdf_disabled"
                service.close()
                stage["flags_ms"]=round((monotonic()-switched)*1000)
                switch=monotonic()
                observed=read_baseline(baseline,path,newest.trip_id)
                assert observed==dict(revision=9,identity=identity,source_is_frozen=True,search_order=["exa","anysearch"],
                                     auto_recheck=False,pdf_bound=False,http_read=True)
                stage["restart_previous_ms"]=round((monotonic()-switch)*1000)
                with app.repository.connection() as live:
                    after=[tuple(row) for row in live.execute("SELECT trip_id,revision,payload_json FROM trip_versions ORDER BY revision")]
                    assert before==after
                    assert live.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
                restored=work/"recovered.sqlite3"
                with closing(sqlite3.connect(backup)) as source,closing(sqlite3.connect(restored)) as copy_db:
                    source.backup(copy_db)
                    assert copy_db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
                restored_version=read_baseline(baseline,restored,newest.trip_id)
                assert restored_version==observed
        return dict(status="PASS",started_at=started,finished_at=datetime.now(timezone.utc).isoformat(),
               elapsed_ms=round((monotonic()-start)*1000),stages_ms=stage,flags=FLAGS,pdf_disabled=True,
               typed_plain_constructor=True,previous_source_sha256=digest,returned=observed,
               restored_copy_matches=True,production_operations=0)
    finally:
        for k in FLAGS.keys()|{"TRAVEL_M11_PDF_BROWSER"}:
            if k in original:
                os.environ[k]=original[k]
            else:
                os.environ.pop(k,None)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",type=Path,required=True)
    args=parser.parse_args()
    try:
        result=drill()
    except (AssertionError,RuntimeError,ValueError,OSError,sqlite3.Error) as error:
        result=dict(status="FAIL",error_type=type(error).__name__,reason=str(error)[:800])
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result["status"]=="PASS" else 1)
