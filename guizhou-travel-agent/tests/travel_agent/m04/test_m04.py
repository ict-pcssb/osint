import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from support.guards import Trace, offline_guard, isolated_database
from travel_contract import ContractError, validate_reply
from travel_state_app import TravelApplication, NotFound
from travel_timeline import TimelineWorkflow, budget_summary, next_stop, load_fixture
from travel_timeline_route import RouteAdapter

NOW = datetime.fromisoformat("2026-11-26T10:00:00+08:00")


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.trace = Trace("m04")
        self.enterContext(offline_guard(self.trace))
        root = Path(self.enterContext(TemporaryDirectory(prefix="travel-m04-")))
        self.path = root / "travel_m04.sqlite3"
        self.enterContext(isolated_database(self.path, root))
        self.app = TravelApplication(self.path, clock=lambda: NOW)
        self.app.create_account("fixture_a", "fixture-account-password")
        self.token = self.app.login("fixture_a", "fixture-account-password")
        self.fixture = load_fixture()
        self.invoke = AsyncMock(side_effect=self.fixture.route)
        self.adapter = RouteAdapter(self.invoke, lambda: NOW, fixture=True)
        self.flow = TimelineWorkflow(self.app, self.fixture.catalog, self.adapter, self.fixture.costs)
        self.form = deepcopy(self.fixture.form)

    def make(self):
        return asyncio.run(self.flow.preview(self.token, self.form, trace=self.trace))

    def saved_counts(self):
        with closing(sqlite3.connect(str(self.path))) as db:
            return [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("trips", "trip_versions", "save_keys")]

    def test_m4_01_end_to_end_reply(self):
        reply = self.make()
        validate_reply(reply, fixture_mode=True)
        result = reply["result"]
        self.assertEqual((result["kind"], result["data_mode"]), ("itinerary","fixture"))
        self.assertEqual([len(day["stops"]) for day in result["days"]], [2,2])
        self.assertEqual(result["budget"]["cap_fen"], 80000)
        self.assertEqual(result["locked_items"][0]["departure_at"], "2026-11-29T18:30:00+08:00")
        self.assertEqual(result["return_leg"]["to_id"], "guiyang-north-entry")
        self.assertEqual(result["return_leg"]["coordinate_system"], "WGS84")
        self.assertEqual(self.invoke.await_count, 5)
        self.assertEqual(self.saved_counts(), [0,0,0])
        for mutate in (
            lambda r: r["budget"].update(total_fen=80000),
            lambda r: r["days"][0]["stops"][0]["incoming_leg"].update(travel_min=1),
            lambda r: r["days"][0]["stops"][1]["incoming_leg"].update(from_id="museum-entry"),
            lambda r: r["map_points"][0].update(id="unbound-endpoint"),
        ):
            altered = deepcopy(reply)
            mutate(altered["result"])
            with self.assertRaises(ContractError):
                validate_reply(altered, fixture_mode=True)

    def test_m4_02_budget_total(self):
        summary = budget_summary(80000, self.fixture.costs)
        self.assertEqual((summary["total_fen"],summary["cap_fen"],summary["scope"],summary["budget_status"]),
                         (68000,80000,"group","within"))
        self.assertEqual(set(summary["included_categories"]), {"hotel","meal","transport","ticket"})
        self.assertTrue(summary["excluded_locked_return"])

    def test_m4_03_unknown_expenses(self):
        self.flow.costs = [dict(item, unit_fen=None) if item["item_id"] in {"lodging","d2-ride"} else item
                           for item in self.fixture.costs]
        result = self.make()["result"]
        self.assertIsNone(result["budget"]["total_fen"])
        self.assertEqual(result["budget"]["known_lower_fen"],34000)
        self.assertEqual(set(result["budget"]["missing_costs"]), {"lodging","d2-ride"})
        self.assertEqual(result["budget"]["budget_status"],"unknown")
        self.assertEqual(result["feasibility"],"needs_verification")

    def test_m4_04_route_errors_and_missing_entrance(self):
        for raw, status in (({}, "invalid_response"), (None, "empty"),
                            (TimeoutError(), "timeout"),
                            ({"profile":"foot","distance_m":"2km","duration_s":20}, "invalid_response")):
            with self.subTest(status=status):
                self.invoke.side_effect = raw if isinstance(raw, Exception) else None
                self.invoke.return_value = raw
                reply = self.make()
                first = reply["result"]["days"][0]["stops"][0]
                self.assertEqual(first["incoming_leg"]["reason"], status)
                self.assertIsNone(first["arrival_local"])
                self.assertIsNone(first["incoming_leg"]["travel_min"])
        self.invoke.reset_mock()
        self.form["day_starts"][0]["entrance_id"] = "unknown"
        reply = self.make()
        self.assertEqual(reply["result"]["kind"], "clarification")
        self.assertEqual(reply["result"]["field"], "place_id")
        self.invoke.assert_not_called()

    def test_m4_05_next_eligible_stop(self):
        a = dict(stop_id="passed", plan_role="primary", arrival_local="known", opening={"status":"confirmed"},
                 incoming_leg={"status":"estimate"}, completed=True)
        b = dict(a, stop_id="backup", plan_role="alternative", completed=False)
        c = dict(a, stop_id="next", completed=False)
        self.assertEqual(next_stop([a,b,c]), "next")
        self.assertIsNone(next_stop([a,b]))
        self.assertIsNone(next_stop([dict(c, arrival_local=None)]))
        self.assertIsNone(self.make()["result"]["days"][0]["next_stop_id"])

    def test_m4_06_form_and_one_question(self):
        self.form.pop("start_date")
        reply = self.make()
        self.assertEqual(reply["result"]["field"], "start_date")
        self.assertEqual(reply["result"]["question"].count("？"), 1)
        self.invoke.assert_not_called()

    def test_m4_07_edit_cancel_save_boundaries(self):
        initial = self.make()
        source = initial["result"]
        edited = self.flow.edit(self.token, source["draft_id"], "d1-jiaxiu", "10:15", "11:15")
        self.assertNotEqual(edited["result"]["draft_id"], source["draft_id"])
        self.assertIn("10:15",edited["result"]["days"][0]["stops"][0]["arrival_local"])
        self.assertEqual(edited["result"]["locked_items"],source["locked_items"])
        self.assertEqual(self.invoke.await_count, 5)
        self.flow.cancel(self.token,edited["result"]["draft_id"])
        self.assertEqual(self.saved_counts(), [0,0,0])
        with self.assertRaises(NotFound):
            self.flow.save(self.token,edited["result"]["draft_id"],"cancel-key",confirmed=True)
        with self.assertRaises(ValueError):
            self.flow.save(self.token,source["draft_id"],"save-key",confirmed=False)
        saved = self.flow.save(self.token, source["draft_id"], "save-key", confirmed=True)
        saved_again = self.flow.save(self.token, source["draft_id"], "save-key", confirmed=True)
        self.assertEqual(saved.trip_id, saved_again.trip_id)
        self.assertEqual(self.saved_counts(), [1,1,1])
        self.trace.record("timeline_explicit_save", rows=self.saved_counts(), revision=saved.revision, reused=True)
        reread = self.app.read_trip(self.token, saved.trip_id)
        self.assertEqual(reread.timeline["result"]["locked_items"], source["locked_items"])
        with self.assertRaises(ValueError):
            self.flow.edit(self.token,source["draft_id"],"d1-jiaxiu","10:25","11:15")

    def test_m4_09_locked_return_conflict_preserves_other_results(self):
        reply = self.make()
        changed = self.flow.edit(self.token, reply["result"]["draft_id"],"d2-park","13:00","18:25")
        self.assertEqual(changed["result"]["feasibility"],"conflict")
        self.assertEqual(changed["result"]["locked_items"],reply["result"]["locked_items"])
        self.assertEqual(changed["result"]["days"][0],reply["result"]["days"][0])
        self.assertTrue(changed["result"]["pending"])
        with self.assertRaises(ValueError):
            self.flow.save(self.token,changed["result"]["draft_id"],"conflict",confirmed=True)
        self.assertEqual(self.saved_counts(),[0,0,0])

