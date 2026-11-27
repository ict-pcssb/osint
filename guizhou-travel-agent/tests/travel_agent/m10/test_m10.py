"""M10 acceptance with fixed business data and actual isolated SQLite."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from threading import Barrier, Event
from concurrent.futures import ThreadPoolExecutor
import unittest

from support.guards import Trace, isolated_database
from travel_recheck import RecheckError, RecheckService
from travel_state_app import NotFound
from travel_agent import Conversation
from fixture_m10 import DAY, NOW, make_case, observed, previous

TRACE = Trace("m10-fixture")


class Module10(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="travel-m10-")))
        db = self.enterContext(isolated_database(home/"travel_m10.sqlite3", home))
        self.now = NOW
        self.app, self.a, self.b, self.trip = make_case(db, clock=lambda:self.now)
        self.trip_id = self.trip.trip_id
        self.original = self.trip
        self.calls = []

    def service(self, provider):
        async def call(fact, budget):
            budget.take("fixture")
            self.calls.append(fact["fact_id"])
            return await provider(fact)
        service = RecheckService(self.app, {kind:call for kind in ("opening","weather","last_service","quote")},
                                 timeout=.4, item_timeout=.15)
        self.addCleanup(service.close)
        return service

    def run_check(self, service, revision=8):
        run = service.start(self.a,self.trip_id,base_revision=revision,trigger="open",trace=TRACE)
        service.wait(run["run_id"], timeout=3)
        return service.get(self.a,self.trip_id,run["run_id"])

    def unchanged(self):
        self.assertEqual(self.app.read_trip(self.a,self.trip_id),self.original)
        with self.app.repository.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM trips").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM trip_versions").fetchone()[0], 8)
            self.assertEqual(db.execute("SELECT count(*) FROM save_keys").fetchone()[0], 2)

    def test_01_current_opening_no_provider_and_original_time(self):
        async def provider(_): self.fail("fresh fact should remain cached")
        svc = self.service(provider)
        opened = svc.open(self.a,self.trip_id)
        self.assertEqual((opened["revision"], opened["freshness"][0]["data_freshness"]),(8,"current"))
        report = self.run_check(svc)
        self.assertEqual((report["check_state"],report["provider_calls"]),("unchanged",0))
        self.assertEqual(report["station_changes"][0]["previous"]["collected_at"],"2026-11-25T09:00:00+08:00")
        self.unchanged()

    def test_02_old_article_keeps_previous_and_pending(self):
        self.move_clock(days=2)
        async def provider(_): return observed("closed", start="2026-11-26", end="2026-11-26")
        report = self.run_check(self.service(provider))
        row = report["station_changes"][0]
        self.assertEqual((row["status"],row["reason"],row["latest"]),("pending","outside_effective_period",None))
        self.assertEqual(row["previous"]["value"],"open")
        self.assertEqual(row["previous"]["source_url"],previous()["source_url"])
        self.unchanged()

    def test_03_new_official_notice_effective_day_conflicts(self):
        self.move_clock(days=2)
        async def provider(_): return observed("closed")
        report = self.run_check(self.service(provider))
        row = report["station_changes"][0]
        self.assertEqual((report["check_state"],report["feasibility"],report["requires_user_choice"]),("changed","conflict",True))
        self.assertEqual((row["previous"]["value"],row["latest"]["value"],row["effective_date"]),("open","closed",DAY))
        self.assertNotEqual(row["latest"]["published_at"][:10],row["effective_date"])
        self.unchanged()

    def test_04_decline_reopen_and_accept_uses_m9_preview(self):
        self.move_clock(days=2)
        async def provider(_): return observed("open")
        async def closed(_): return observed("closed")
        self.set_facts([dict(previous(),value="closed")])
        svc = self.service(provider)
        report = self.run_check(svc)
        self.assertTrue(report["requires_user_choice"])
        packet = asyncio.run(svc.accept(self.a,self.trip_id,report["run_id"],trace=TRACE))
        self.assertEqual(packet["result"]["kind"],"patch_preview")
        self.unchanged()
        svc.decline(self.a,self.trip_id,report["run_id"],trace=TRACE)
        self.assertFalse(self.run_check(svc)["requires_user_choice"])
        self.now += timedelta(minutes=16)
        renewed = self.run_check(svc)
        self.assertFalse(renewed["requires_user_choice"])
        self.unchanged()

    def test_05_partial_faults_with_budget(self):
        self.move_clock(days=2)
        self.set_facts([previous("weather"), previous("last_service"), previous("quote"), previous()])
        async def provider(fact):
            if fact["kind"]=="weather":
                await asyncio.sleep(2)
            if fact["kind"]=="last_service":
                return {"status":"rate_limited"}
            if fact["kind"]=="quote":
                return {"status":"success","observation":"broken"}
            return observed("closed")
        report = self.run_check(self.service(provider))
        self.assertEqual((report["run_status"],report["check_state"],report["provider_calls"]),("partial","partial",4))
        self.assertEqual(set(row["reason"] for row in report["pending_checks"]),{"timeout","rate_limited","invalid_response"})
        self.assertTrue(any(row["latest"] and row["latest"]["value"]=="closed" for row in report["station_changes"]))
        self.unchanged()

    def test_06_stale_price_is_history_and_time_preserved(self):
        self.move_clock(days=3)
        self.set_facts([previous("quote")])
        async def provider(_): return {"status":"unbound"}
        report = self.run_check(self.service(provider))
        row = report["station_changes"][0]
        self.assertEqual((row["data_freshness"],row["previous"]["collected_at"],row["latest"]),
                         ("stale","2026-11-25T09:00:00+08:00",None))
        self.assertEqual(row["previous"]["valid_until"],"2026-11-25T09:05:00+08:00")
        self.unchanged()

    def test_07_concurrent_dedup_and_account_isolation(self):
        self.move_clock(days=2)
        gate = Event()
        barrier = Barrier(2)
        async def provider(_):
            for _ in range(30):
                if gate.is_set(): break
                await asyncio.sleep(.003)
            return observed("closed")
        svc = self.service(provider)
        def start():
            barrier.wait()
            return svc.start(self.a,self.trip_id,base_revision=8,trigger="open",trace=TRACE)
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = [run["run_id"] for run in pool.map(lambda _:start(),range(2))]
        gate.set()
        self.assertEqual(ids[0],ids[1])
        svc.wait(ids[0], timeout=3)
        self.assertEqual(len(self.calls),1)
        for action in (lambda:svc.open(self.b,self.trip_id), lambda:svc.get(self.b,self.trip_id,ids[0]),
                       lambda:svc.decline(self.b,self.trip_id,ids[0]),
                       lambda:asyncio.run(svc.accept(self.b,self.trip_id,ids[0]))):
            with self.assertRaises(NotFound): action()
        self.unchanged()

    def test_08_old_task_cannot_overwrite_new_revision(self):
        from fixture_m10 import advance
        self.move_clock(days=2)
        self.set_facts([dict(previous(),value="closed")])
        async def provider(_):
            await asyncio.sleep(.045)
            return observed("open")
        svc = self.service(provider)
        active = svc.start(self.a,self.trip_id,base_revision=8,trigger="open",trace=TRACE)
        newest = advance(self.app,self.a,self.original)
        svc.wait(active["run_id"], timeout=3)
        report = svc.get(self.a,self.trip_id,active["run_id"])
        self.assertEqual((report["base_revision"],report["current_revision"]), (8,9))
        with self.assertRaises(RecheckError) as invalid:
            asyncio.run(svc.accept(self.a,self.trip_id,active["run_id"]))
        self.assertEqual(invalid.exception.status,409)
        self.assertEqual(self.app.read_trip(self.a,self.trip_id),newest)

    def move_clock(self, **kwargs):
        self.now += timedelta(**kwargs)
        self.a,self.b=(self.app.login(name,"isolated-m10") for name in ("m10_fixture_a","m10_fixture_b"))

    def test_single_agent_read_only_recheck_and_call_limit(self):
        self.move_clock(days=2)
        self.set_facts([dict(previous("last_service"),scope={"direction":f"方向 {i}"}) for i in range(8)])
        async def provider(_): return observed("16:30")
        svc = self.service(provider)
        self.app.recheck_service = svc
        async def classify(*_):
            return '{"intent":"recheck_report","confidence_band":"high","requested_outputs":["recheck_report"],"parameters":{}}'
        packet = asyncio.run(self.app.turn(self.a,"复查这份行程",trip_id=self.trip_id,trace=TRACE,
                         conversation_factory=lambda:Conversation(typed_output=True,clock=self.app.clock,
                                                                  intent_model=classify,fixture_mode=True)))
        result=packet["result"]
        self.assertEqual((result["kind"],result["base_revision"],packet["persistence"]),("recheck_report",8,"none"))
        svc.wait(result["run_id"],timeout=3)
        done=svc.get(self.a,self.trip_id,result["run_id"])
        self.assertEqual((done["provider_calls"],len(self.calls)),(6,6))
        self.assertEqual([x["reason"] for x in done["pending_checks"]],["budget_exhausted","budget_exhausted"])
        self.unchanged()

    def set_facts(self, facts):
        from fixture_m10 import set_facts
        self.original = set_facts(self.app,self.a,self.trip_id,facts)
