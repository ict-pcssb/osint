"""M12 fixed safety/trace probes using actual authenticated services and a disposable database."""
import argparse
import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from demo_m11 import NOW, ROOT, TEST, Workbench, make_case
from fixture_m10 import previous, set_facts
from support.guards import isolated_database, offline_guard
from travel_agent import Conversation
from travel_patch import PatchError, revision_identity
from travel_recheck import RecheckService
from travel_release import RunTrace
from travel_state_app import NotFound


def report_one(name,trace,result):
    return dict(case=name,trace_id=trace.trace_id,trace=trace.events,result=result)


async def run(app,a,b,trip):
    ui=Workbench(app,a,trip,live=False,testing=False,pdf_browser=None)
    ui.recheck.close()
    checks=[]
    traces=[]
    try:
        ok=RunTrace()
        packet=await asyncio.to_thread(ui.turn,dict(mode="weather",message="明天青岩下雨吗？",trip_id=None),ok)
        assert packet["result"]["kind"]=="weather" and packet["persistence"]=="none"
        assert any(row.get("tool")=="get_destination_weather" for row in ok.events)
        assert any(row.get("evidence_ids") for row in ok.events)
        traces.append(report_one("successful_weather",ok,dict(kind="weather",evidence_ids=packet["result"]["evidence_ids"])))

        protected=RunTrace()
        async def forbidden(*_):
            return '{"intent":"booking","confidence_band":"high","requested_outputs":["book_ticket"],"parameters":{}}'
        def blocked_tool(_):
            raise AssertionError("unregistered tool was invoked")
        result=await Conversation(typed_output=True,clock=app.clock,fixture_mode=True,
                             handlers={"book_ticket":blocked_tool},intent_model=forbidden).ask("替我直接买票并取消旧订单",protected)
        assert result["result"]["kind"]=="unavailable" and not any(row["event"]=="tool_call" for row in protected.events)
        checks.append("Unregistered purchasing/cancellation never runs")

        app.recheck_service=None
        target_id=trip.trip_id
        seed=[dict(previous(),valid_until=(NOW-timedelta(minutes=1)).isoformat()),previous("quote")]
        trip=set_facts(app,a,target_id,seed)
        identity=revision_identity(trip)
        partial=RunTrace()
        async def opening(fact,budget):
            budget.take("fixture")
            value=deepcopy(fact["previous"])
            value.update(value="closed",collected_at=NOW.isoformat(),valid_until=(NOW+timedelta(minutes=30)).isoformat(),
                         origin_id="m12-safe-notice")
            return {"status":"success","observation":value}
        async def quote(_fact,budget):
            budget.take("fixture")
            return {"status":"rate_limited"}
        recheck=RecheckService(app,{"opening":opening,"quote":quote})
        try:
            active=recheck.start(a,target_id,base_revision=8,trigger="open",trace=partial)
            await asyncio.to_thread(recheck.wait,active["run_id"])
            found=recheck.get(a,target_id,active["run_id"])
            assert found["check_state"]=="partial" and found["provider_calls"]==2
            assert any(row["status"]=="conflict" for row in found["station_changes"])
            assert found["pending_checks"][0]["reason"]=="rate_limited"
        finally:
            recheck.close()
        traces.append(report_one("partial_recheck",partial,dict(job_id=found["run_id"],revision=8)))

        rules=RunTrace()
        patch=app.get_patch_service()
        rule=await patch.preview_retime(a,target_id,date="2026-11-29",stop_id="d2-park",
                                        arrival="18:20",departure="18:25",base_revision=8,trace=rules)
        assert rule["result"]["rule_status"]=="conflict"
        details=dict(proposal_id=rule["result"]["proposal_id"],base_revision=8,idempotency_key="m12-rule-test")
        try:
            patch.commit(a,target_id,confirmed=True,trace=rules,**details)
            raise AssertionError("rule conflict was committed")
        except PatchError as exc:
            assert exc.status==409 and exc.code=="rule_conflict"
            rules.record("rule_rejected",status="conflict",reason=exc.code,rule_codes=rule["result"]["rule_codes"],trip_writes=0)
        traces.append(report_one("hard_conflict",rules,dict(revision=8,rule_codes=rule["result"]["rule_codes"])))

        try:
            patch.commit(a,target_id,confirmed="true",**details)
            raise AssertionError("forged confirmation accepted")
        except PatchError as exc:
            assert exc.code=="confirmation_required"
        try:
            patch.commit(b,target_id,confirmed=True,**details)
            raise AssertionError("cross-account commit")
        except NotFound:
            pass
        asset=ui.exports.create(a,target_id,revision=8,format="html")["result"]
        try:
            ui.exports.download(b,asset["artifact_id"])
            raise AssertionError("cross-account download")
        except NotFound:
            pass
        assert revision_identity(app.read_trip(a,target_id))==identity
        checks.extend(["Forged confirmation rejected","Cross-account patch and export denied",
                       "Partial and conflict paths keep version and locks","Evidence IDs and per-request trace retained"])
        return dict(status="PASS",checks=checks,traces=traces,forbidden_trace=protected.events)
    finally:
        ui.recheck.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",type=Path,required=True)
    args=parser.parse_args()
    with TemporaryDirectory(prefix="travel-m12-safety-") as folder:
        work=Path(folder)
        with isolated_database(work/"travel_m12.sqlite3",work) as db, offline_guard(RunTrace()):
            app,a,b,trip=make_case(db,clock=lambda:NOW)
            result=asyncio.run(run(app,a,b,trip))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(dict(status=result["status"],checks=result["checks"],trace_count=len(result["traces"])),ensure_ascii=False))


if __name__=="__main__":
    main()
