import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch
import sqlite3

from fixture_m09 import DAY1, DAY2, MESSAGE, NOW, count, fields_digest, make_trip, model_factory, planner
from support.guards import Trace, isolated_database
from travel_contract import validate_reply
from travel_patch import PatchError, PatchService
from travel_state_app import NotFound, TravelApplication
from travel_patch_indoor import MAPPED_MUSEUM
from travel_timeline import TimelineWorkflow, load_fixture
from travel_timeline_route import RouteAdapter

TRACE=Trace("m09-fixture")
BEFORE=[1,7,1]
AFTER=[1,8,2]


class Module09(unittest.TestCase):
    def setUp(self):
        self.now=NOW
        self.tmp=self.enterContext(TemporaryDirectory(prefix="travel-m09-"))
        root=Path(self.tmp)
        self.db=self.enterContext(isolated_database(root/"travel_m09.sqlite3",root))
        self.app=TravelApplication(self.db,clock=lambda:self.now)
        for name in ("m9_fixture_a","m9_fixture_b"):
            self.app.create_account(name,"m09-fixture-only")
        self.a=self.app.login("m9_fixture_a","m09-fixture-only")
        self.b=self.app.login("m9_fixture_b","m09-fixture-only")
        self.base=asyncio.run(make_trip(self.app,self.a))
        self.trip_id=self.base.trip_id
        self.service=PatchService(self.app,planner)
        self.app.patch_service=self.service

    def preview(self,plan_service=None,*,token=None,date=DAY2):
        service=plan_service or self.service
        return asyncio.run(service.preview(token or self.a,self.trip_id,scope_dates=[date],
                                           instruction=MESSAGE,base_revision=7,trace=TRACE))

    def confirm(self,packet,key="m09-commit",*,confirmed=True,token=None):
        return self.service.commit(token or self.a,self.trip_id,proposal_id=packet["result"]["proposal_id"],
                                   base_revision=7,idempotency_key=key,confirmed=confirmed,trace=TRACE)

    def assert_original(self):
        self.assertEqual(count(self.app),BEFORE)
        self.assertEqual(self.app.read_trip(self.a,self.trip_id),self.base)

    def test_01_chat_and_preview_zero_business_writes(self):
        result=asyncio.run(self.app.turn(self.a,MESSAGE,trip_id=self.trip_id,
                         conversation_factory=model_factory(self.app.clock),trace=TRACE))
        validate_reply(result,fixture_mode=True)
        p=result["result"]
        self.assertEqual((p["kind"],p["scope"]["dates"],p["base_revision"]),("patch_preview",[DAY2],7))
        self.assertIn("观山湖",p["changes"][0]["before"])
        self.assertIn("室内展馆",p["changes"][0]["after"])
        self.assertEqual(p["cost_impact"]["known_delta_fen"],6000)
        self.assertIsNone(p["cost_impact"]["delta_fen"])
        self.assertIn("d2-ride",p["cost_impact"]["missing_item_ids"])
        self.assertEqual(p["rule_status"],"needs_verification")
        self.assertEqual(p["save_status"],"awaiting_confirmation")
        self.assertEqual(p["next_stop_impact"],[dict(date=DAY2,before="d2-park",after=None)])
        with self.assertRaises(PatchError) as error:
            self.confirm(result,confirmed=False)
        self.assertEqual(error.exception.code,"confirmation_required")
        self.assert_original()
        TRACE.record("m09_preview",result=p,revision_before=7,writes=0)

    def test_02_one_revision_preserves_first_day_and_full_return_lock(self):
        packet=self.preview()
        saved=self.confirm(packet)
        latest=self.app.read_trip(self.a,self.trip_id)
        self.assertEqual((saved["save_status"],saved["revision"]),("saved",8))
        self.assertEqual(count(self.app),AFTER)
        old=self.base.timeline["result"]; new=latest.timeline["result"]
        self.assertEqual(new["days"][0],old["days"][0])
        self.assertNotEqual(new["days"][1],old["days"][1])
        self.assertEqual(old["days"][1]["next_stop_id"],"d2-park")
        self.assertIsNone(new["days"][1]["next_stop_id"])
        self.assertEqual(new["locked_items"],old["locked_items"])
        self.assertEqual(latest.locked_items,self.base.locked_items)
        self.assertEqual(next(x for x in latest.locked_items if x.kind=="rail").fields["train_number"],"D-FIXTURE")
        self.assertEqual(next(x for x in latest.locked_items if x.kind=="hotel").fields["check_out"],DAY2)
        self.assertEqual(fields_digest(latest.locked_items),fields_digest(self.base.locked_items))
        self.assertNotIn("d2-park",new["validation_report"]["time_details"])
        self.assertNotIn("d2-park",str(new["validation_report"]["rule_results"]))
        self.assertEqual([d["date"] for d in new["days"]],[DAY1,DAY2])
        TRACE.record("m09_sqlite_snapshots",revision_before=7,revision_after=8,
                     first_day_equal=new["days"][0]==old["days"][0],
                     return_hash_before=fields_digest(self.base.locked_items),return_hash_after=fields_digest(latest.locked_items),
                     day_two_before=old["days"][1],day_two_after=new["days"][1],counts=count(self.app))

    def test_03_reject_out_of_scope_and_report_budget_or_return_conflict(self):
        for mutation in ("outside","lock","budget","late"):
            async def candidate(*args):
                plan=await planner(*args)
                row=plan["operations"][0]
                if mutation=="outside":
                    row.update(date=DAY1,stop_id="d1-jiaxiu")
                elif mutation=="lock":
                    row["locked_items"]=[{"departure_at":"2026-11-29T19:30:00+08:00"}]
                elif mutation=="budget":
                    row["ticket_fen"]=40000
                else:
                    row.update(arrive="17:50",leave="18:20")
                return plan

            self.service.planner=candidate
            p=self.preview()
            result=p["result"]
            if mutation in {"outside","lock"}:
                self.assertEqual((p["run_status"],result["kind"]),("partial","unavailable"))
            else:
                self.assertEqual(result["rule_status"],"conflict")
                self.assertIn("BUDGET_EXCEEDED" if mutation=="budget" else "LOCKED_RETURN_TIME_CONFLICT",result["rule_codes"])
                with self.assertRaises(PatchError) as error:
                    self.confirm(p)
                self.assertEqual(error.exception.status,409)
            self.assert_original()

    def test_04_discard_timeout_invalid_and_partial(self):
        packet=self.preview()
        r=self.service.discard(self.a,self.trip_id,proposal_id=packet["result"]["proposal_id"],base_revision=7,trace=TRACE)
        self.assertEqual(r["save_status"],"discarded")
        with self.assertRaises(PatchError):
            self.confirm(packet)
        for raw,reason in [(TimeoutError(),"timeout"),({"operations":"invalid"},"invalid_response")]:
            async def candidate(*_):
                if isinstance(raw,Exception):
                    raise raw
                return raw
            self.service.planner=candidate
            p=self.preview()
            self.assertEqual((p["run_status"],p["result"]["reason"]),("partial",reason))
            self.assert_original()
        async def partial(*args):
            plan=await planner(*args)
            plan["routes"].pop()
            return plan
        self.service.planner=partial
        p=self.preview()
        self.assertEqual((p["run_status"],p["result"]["kind"]),("partial","patch_preview"))
        self.assertIn("RETURN_UNVERIFIED",p["result"]["rule_codes"])
        self.assert_original()

    def test_05_concurrent_tabs_one_wins_other_is_409(self):
        packets=[self.preview(),self.preview()]
        barrier=Barrier(2)
        def try_save(index):
            barrier.wait(timeout=6)
            try:
                return self.confirm(packets[index],key=f"tab-{index}")
            except PatchError as err:
                return {"save_status":err.save_status,"http_status":err.status}
        with ThreadPoolExecutor(max_workers=2) as workers:
            statuses=list(workers.map(try_save,range(2)))
        self.assertEqual(sorted(r["save_status"] for r in statuses),["conflict","saved"])
        self.assertEqual(next(r["http_status"] for r in statuses if "http_status" in r),409)
        self.assertEqual(count(self.app),AFTER)
        TRACE.record("m09_concurrent_tabs",responses=statuses,counts=count(self.app))

    def test_06_response_lost_status_lookup_retry_and_key_rebinding(self):
        p=self.preview()
        key="same-recovery-key"
        self.assertEqual(self.service.operation_status(self.a,self.trip_id,key)["save_status"],"unknown_commit")
        def dropped_response():
            self.confirm(p,key)
            raise TimeoutError("response lost after commit")
        with self.assertRaises(TimeoutError):
            dropped_response()
        restarted=PatchService(TravelApplication(self.db,clock=lambda:self.now),planner)
        status=restarted.operation_status(self.a,self.trip_id,key,trace=TRACE)
        self.assertEqual((status["save_status"],status["revision"]),("saved",8))
        self.assertEqual([self.confirm(p,key) for _ in range(3)],[status]*3)
        invalid=deepcopy(p)
        invalid["result"]["proposal_id"]="other-proposal"
        with self.assertRaises((PatchError,NotFound)):
            self.confirm(invalid,key)
        self.assertEqual(count(self.app),AFTER)
        TRACE.record("m09_lost_response_recovery",recovered=status,successful_revision=8,counts=count(self.app))

    def test_default_indoor_location_keeps_unverified_route_and_price_unknown(self):
        source=load_fixture()
        catalog={key:replace(point,source_url=f"https://example.org/test-endpoints/{key}")
                 for key,point in source.catalog.items()}

        async def missing_route(_):
            return None

        workflow=TimelineWorkflow(self.app,catalog,RouteAdapter(missing_route,self.app.clock,fixture=False),source.costs)
        baseline=asyncio.run(workflow.preview(self.b,source.form))
        self.assertEqual(baseline["result"]["data_mode"],"snapshot")
        saved=workflow.save(self.b,baseline["result"]["draft_id"],"indoor-baseline",confirmed=True)
        point=json.loads(MAPPED_MUSEUM.read_text(encoding="utf-8"))
        self.now=datetime.fromisoformat(point["collected_at"])+timedelta(minutes=1)
        self.app.patch_service=None
        service=self.app.get_patch_service()
        before=count(self.app)
        preview=asyncio.run(service.preview(self.b,saved.trip_id,scope_dates=[DAY2],
                                           instruction=MESSAGE,base_revision=1,trace=TRACE))
        validate_reply(preview,fixture_mode=False)
        result=preview["result"]
        self.assertEqual((result["kind"],preview["run_status"],result["rule_status"]),
                         ("patch_preview","partial","needs_verification"))
        self.assertIn("贵州省地质博物馆",result["changes"][0]["after"])
        self.assertTrue(all(route["after_min"] is None for route in result["route_changes"]))
        self.assertIsNone(result["cost_impact"]["after_total_fen"])
        self.assertTrue(any(row["collected_at"]==point["collected_at"] and row["source_url"]==point["source_url"]
                            for row in preview["evidence"]))
        self.assertEqual(count(self.app),before)

    def test_write_failure_rolls_back_both_records_and_kill_switch(self):
        packet=self.preview()
        with self.app.repository.transaction() as db:
            db.execute("""""" + """CREATE TRIGGER m09_abort_save BEFORE INSERT ON save_keys
                          BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END""" + """""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.confirm(packet)
        self.assert_original()
        with self.app.repository.transaction() as db:
            db.execute("DROP TRIGGER m09_abort_save")
        with patch.dict("os.environ",{"TRAVEL_M09_COMMIT":"disabled"}):
            with self.assertRaises(PatchError) as paused:
                self.confirm(packet)
            self.assertEqual(paused.exception.code,"commit_disabled")
            self.assert_original()
        self.assertEqual(self.confirm(packet)["revision"],8)
        self.assertEqual(count(self.app),AFTER)
        TRACE.record("m09_atomic_rollback_verified", counts_before=BEFORE, counts_after_retry=AFTER)

    def test_07_cross_owner_and_expiry_are_no_write(self):
        p=self.preview()
        for operation in (
            lambda:self.preview(token=self.b),
            lambda:self.confirm(p,"owned-key",token=self.b),
            lambda:self.service.operation_status(self.b,self.trip_id,"owned-key"),
            lambda:self.service.discard(self.b,self.trip_id,proposal_id=p["result"]["proposal_id"],base_revision=7),
        ):
            with self.assertRaises(NotFound):
                operation()
        self.now+=timedelta(minutes=11)
        with self.assertRaises(PatchError) as bad:
            self.confirm(p,"owned-key")
        self.assertEqual(bad.exception.code,"expired")
        self.assert_original()

    def test_08_browser_fixture_contract_and_reject_expired_quote(self):
        async def with_quote(*args):
            plan=await planner(*args)
            plan["quote_expires_at"]=(NOW+timedelta(minutes=1)).isoformat()
            return plan
        self.service.planner=with_quote
        r=self.preview()
        p=r["result"]
        self.assertEqual((p["needs_user_choice"],p["cost_impact"]["known_delta_fen"],p["cost_impact"]["status"]),
                         (True,6000,"unknown"))
        self.assertTrue(p["next_stop_impact"])
        self.now+=timedelta(minutes=2)
        with self.assertRaises(PatchError) as bad:
            self.confirm(r)
        self.assertEqual(bad.exception.code,"expired")
        self.assert_original()


if __name__=="__main__":
    unittest.main()

